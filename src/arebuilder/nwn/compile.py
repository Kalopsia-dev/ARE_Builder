import json
import os
import re
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from nwn.key import Reader as KeyReader
from nwn.nwscript.comp import CompilationError
from nwn.nwscript.comp import Compiler as NwScriptCompiler


class CompileError(RuntimeError):
    """Raised when NWScript compilation cannot complete successfully."""


class CompilerBackend(Protocol):
    """Minimal interface required from an NWScript compiler backend."""

    def compile(self, script_name: str):
        """Compile one script and return bytes or the ``nwn`` tuple result."""


CompilerFactory = Callable[[Callable[[str], bytes | None]], CompilerBackend]
MIN_COMPILER_THREAD_STACK_SIZE = 8 * 1024 * 1024
_compiler_thread_stack_lock = threading.Lock()
_compiler_thread_stack_configured = False


@dataclass(slots=True, frozen=True)
class CompileResult:
    """Record the number of scripts successfully compiled by one compiler run."""

    compiled_count: int


class Script:
    """An indexed NWScript source file and its dependency metadata."""

    def __init__(self, name: str, script_index: "ScriptIndex") -> None:
        """Initialize the instance with its required collaborators and state."""

        self.name = ScriptIndex.normalise_script_name(name)
        self.index = script_index
        self.is_include = False

        script_path = script_index.find_script_path(self.name)
        self.primary = script_path is not None and script_index.is_primary_script_path(
            script_path
        )
        if script_path is None:
            # Missing local files are usually base-game includes. We still keep a
            # lightweight Script object so dependency graphs can reference them.
            self.path = None
            self.hash = _hash_text(self.name)
            self.include_names: set[str] = set()
            self.includes: set[Script] = set()
            self.contents = None
            self.declares_main = False
            self.is_entrypoint_alias = False
            self.has_main = False
            script_index.add(self)
            return

        self.path = script_path
        self.contents = _read_script_bytes(script_path)
        self.hash = hash_script_contents(self.contents)
        script_index.add(self)

        text = re.sub(
            script_index.regex_comments,
            "",
            self.contents.decode(encoding="ISO-8859-1"),
        )
        # Entry-point detection is performed after comment removal because many
        # shared include files mention main() in documentation or dead snippets.
        self.declares_main = bool(re.search(script_index.regex_main_fns, text))
        self.include_names = {
            ScriptIndex.normalise_script_name(include_name)
            for include_name in re.findall(script_index.regex_includes, text)
        }
        self.is_entrypoint_alias = (
            not self.declares_main
            and bool(self.include_names)
            and not re.sub(script_index.regex_includes, "", text).strip()
        )
        self.has_main = self.declares_main
        self.includes: set[Script] = set()

    def __hash__(self) -> int:
        """Return the stable hash used when storing this object in sets or dicts."""

        return self.hash

    def __eq__(self, other: object) -> bool:
        """Return whether another object represents the same logical value."""

        if not isinstance(other, Script):
            return False
        return self.hash == other.hash and self.name == other.name

    def __repr__(self) -> str:
        """Return a compact debug representation for log and assertion output."""

        def format_name(
            script_type: str,
            dependencies: int | None = None,
            derived: int | None = None,
        ) -> str:
            """Format one script summary line for debug representations."""

            result = f"{self.name.ljust(16)} {script_type.ljust(9)}"
            if dependencies is not None:
                result += f" {dependencies:3d} dependencies"
            if derived is not None:
                result += f", {derived:4d} derived"
            return result

        if not self.contents:
            return format_name("[Base Script]")
        dependencies = len(self.includes)
        if self.is_include or not self.declares_main:
            derived = len(self.index.includes.get(self, set()))
            return format_name("[Include]", dependencies, derived)
        return format_name("[Script]", dependencies)


class ScriptIndex:
    """Index custom scripts and resolve their include relationships."""

    regex_comments = re.compile(r"//[^\r\n]*|/\*[\s\S]*?(?:\*/|\Z)")
    regex_includes = re.compile(r'^\s*#include\s+"([^"]+)"(?![^"\n]*//)', re.MULTILINE)
    regex_main_fns = re.compile(
        r"^\s*(?:void\s+main|int\s+StartingConditional)\s*\(\s*\)", re.MULTILINE
    )

    def __init__(
        self,
        script_dir: Path,
        *,
        include_dirs: Iterable[Path] = (),
    ) -> None:
        """Initialize the instance with its required collaborators and state."""

        if not script_dir.is_dir():
            raise FileNotFoundError(f"Script directory '{script_dir}' does not exist.")

        print("Indexing scripts...")
        self.directory = script_dir
        self.include_dirs = [path for path in include_dirs if path.is_dir()]
        self.script_paths, primary_script_paths = self._index_script_paths()
        self.scripts: dict[str, Script] = {}

        for script_path in primary_script_paths:
            self.get_or_create(script_path.name)

        self._discover_referenced_scripts()
        self._resolve_include_graph()
        self.includes = self._group_by_includes()

    def __iter__(self):
        """Iterate over indexed Script objects in insertion order."""

        return iter(self.scripts.values())

    def __contains__(self, script: str | Script) -> bool:
        """Return whether the collection contains the requested item."""

        if isinstance(script, Script):
            return script in self.scripts.values()
        return self.normalise_script_name(script) in self.scripts

    def add(self, script: Script) -> Script:
        """Register a script in the index and return it for fluent construction."""

        self.scripts[script.name] = script
        return script

    def get(self, script_name: str) -> Script | None:
        """Return a matching script payload or ``None`` when it is unavailable."""

        return self.scripts.get(self.normalise_script_name(script_name))

    def get_or_create(self, script_name: str) -> Script:
        """Return an existing Script entry or create and index one on demand."""

        return self.get(script_name) or Script(script_name, script_index=self)

    def find_script_path(self, script_name: str) -> Path | None:
        """Find a script file by normalized name across primary and include directories."""

        return self.script_paths.get(self.normalise_script_name(script_name))

    def is_primary_script_path(self, script_path: Path) -> bool:
        """Return whether a path belongs directly to the primary script directory."""

        return script_path.parent == self.directory

    def primary_scripts(self) -> set[Script]:
        """Return indexed scripts that originated in the primary script directory."""

        return {script for script in self if script.primary and script.contents}

    def compilable_primary_scripts(self) -> set[Script]:
        """Return primary scripts that can produce executable NWScript output."""

        return {
            script for script in self.primary_scripts() if self.is_compilable(script)
        }

    def get_modified(self, state_path: Path) -> set[Script]:
        """Return primary scripts whose content hash differs from the saved state."""

        hash_index = read_hash_index(state_path)
        return {
            script
            for script in self.primary_scripts()
            if script.name not in hash_index or hash_index[script.name] != script.hash
        }

    def get_related(self, scripts: set[Script]) -> set[Script]:
        """Return compilable scripts directly selected or dependent on selected includes."""

        related = set(scripts)
        for script in scripts:
            if script.is_include:
                related.update(self.includes.get(script, set()))
        return {
            script
            for script in related
            if script.primary and script.contents and self.is_compilable(script)
        }

    def is_compilable(self, script: Script) -> bool:
        """Return whether a primary script should produce a compiled NCS."""

        return script.declares_main or (script.is_entrypoint_alias and script.has_main)

    def _index_script_paths(self) -> tuple[dict[str, Path], list[Path]]:
        """Return normalized script paths using primary and include directory precedence."""

        script_paths: dict[str, Path] = {}
        primary_script_paths = sorted(self.directory.glob("*.nss"))
        for script_path in primary_script_paths:
            script_paths.setdefault(
                self.normalise_script_name(script_path.name),
                script_path,
            )
        for directory in [self.directory, *self.include_dirs]:
            if directory == self.directory:
                continue
            for script_path in sorted(directory.glob("*.nss")):
                script_paths.setdefault(
                    self.normalise_script_name(script_path.name),
                    script_path,
                )
        return script_paths, primary_script_paths

    def _discover_referenced_scripts(self) -> None:
        """Index every custom script reachable from primary script include directives."""

        pending = deque(self.scripts.values())
        visited: set[Script] = set()
        while pending:
            script = pending.popleft()
            if script in visited:
                continue
            visited.add(script)
            for include_name in sorted(script.include_names):
                include = self.get_or_create(include_name)
                if include not in visited:
                    pending.append(include)

    def _resolve_include_graph(self) -> None:
        """Expand direct include edges into transitive include dependencies."""

        direct_includes = {
            script: {
                self.get_or_create(include_name)
                for include_name in script.include_names
            }
            for script in self
        }

        try:
            ordered_scripts = tuple(TopologicalSorter(direct_includes).static_order())
        except CycleError as exc:
            cycle_path = " -> ".join(
                f"{script.name}.nss" for script in reversed(exc.args[1])
            )
            raise CompileError(f"Error: Include cycle detected: {cycle_path}") from exc

        for script in ordered_scripts:
            includes = set(direct_includes[script])
            for include in direct_includes[script]:
                includes.update(include.includes)
            script.includes = includes

    def _group_by_includes(self) -> dict[Script, set[Script]]:
        """Build a reverse include map from include scripts to dependent scripts."""

        includes: dict[Script, set[Script]] = {}
        for script in self:
            for include in script.includes:
                dependents = includes.get(include)
                if dependents is None:
                    # The reverse map drives modified-include recompiles; marking
                    # includes here keeps the forward Script model compact
                    # during indexing.
                    include.is_include = True
                    dependents = set()
                    includes[include] = dependents
                dependents.add(script)
                # If an include itself has an entry point, dependents are treated
                # as executable even when the direct script lacks main().
                if include.has_main:
                    script.has_main = True
        return includes

    @staticmethod
    @lru_cache(maxsize=None)
    def normalise_script_name(script_name: str) -> str:
        """Normalize a script filename or resref to the lowercase NWScript stem."""

        return Path(script_name).stem.lower()


class GameScriptSource:
    """Read base game scripts from an NWN key file or script directory."""

    def __init__(self, path: Path) -> None:
        """Initialize the instance with its required collaborators and state."""

        self.lock = threading.Lock()
        self.path = path
        self.reader: KeyReader | None = None
        self.scripts: set[str] = set()

        if path.is_dir():
            # Tests and loose installs can provide base scripts as plain files.
            self.scripts = {script_path.name for script_path in path.glob("*.nss")}
        elif path.is_file() and path.suffix == ".key":
            # Production NWN installs expose most base scripts through key files.
            self.reader = KeyReader(str(path))
            self.scripts = {
                filename
                for filename in _key_reader_filenames(self.reader)
                if filename.endswith(".nss")
            }

    def __contains__(self, script_name: str) -> bool:
        """Return whether the collection contains the requested item."""

        return script_name in self.scripts

    @lru_cache(maxsize=None)
    def get(self, script_name: str) -> bytes | None:
        """Return a matching script payload or ``None`` when it is unavailable."""

        if script_name not in self:
            return None
        if self.reader is not None:
            with self.lock:
                return self.reader.read_file(script_name)
        with self.lock:
            return (self.path / script_name).read_bytes()


class GameScriptSources:
    """Search base-game script sources in the order expected by the NWN compiler."""

    def __init__(self, nwn_root: Path | None) -> None:
        """Initialize the instance with its required collaborators and state."""

        self.sources: list[GameScriptSource] = []
        if nwn_root is None:
            return

        self.sources.extend(
            [
                GameScriptSource(nwn_root / "data" / "nwn_retail.key"),
                GameScriptSource(nwn_root / "ovr"),
                GameScriptSource(nwn_root / "data" / "nwn_base.key"),
            ]
        )

    def get(self, script_name: str) -> bytes | None:
        """Return a matching script payload or ``None`` when it is unavailable."""

        for source in self.sources:
            if script := source.get(script_name):
                return script
        return None


def _key_reader_filenames(reader: KeyReader) -> list[str]:
    """Return key-reader filenames across nwn callable/property API shapes."""

    filenames = reader.filenames
    if callable(filenames):
        return filenames()
    return filenames


class ProgressBar:
    """Wrap tqdm so tiny compile runs can skip progress UI without branching elsewhere."""

    def __init__(self, total: int) -> None:
        """Initialize the instance with its required collaborators and state."""

        if total < 2:
            self.progress = None
            return
        try:
            from tqdm import tqdm

            self.progress = tqdm(
                total=total,
                desc="Compiling",
                unit="script",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
                leave=True,
            )
        except ImportError:
            print("Compiling...")
            self.progress = None

    def update(self, count: int = 1) -> None:
        """Advance the progress display when one is active."""

        if self.progress:
            self.progress.update(count)

    def close(self, leave: bool = True) -> None:
        """Close the progress display and optionally leave it visible."""

        if not self.progress:
            return
        self.progress.leave = leave
        self.progress.close()
        self.progress = None


class StatefulScriptCompiler:
    """Compile NWScript files using dependency-aware state tracking."""

    def __init__(
        self,
        *,
        script_dir: Path,
        output_dir: Path,
        selector: str | None,
        state_path: Path | None,
        nwn_root: Path | None,
        include_dirs: Iterable[Path] = (),
        secondary_output_dir: Path | None = None,
        num_workers: int = -1,
        use_state: bool = True,
        clear_output_on_all: bool = True,
        compiler_factory: CompilerFactory | None = None,
    ) -> None:
        """Initialize the instance with its required collaborators and state."""

        self.script_dir = script_dir
        self.output_dir = output_dir
        self.selector = selector
        self.state_path = state_path
        self.include_dirs = list(include_dirs)
        self.output_dirs = [output_dir]
        if secondary_output_dir is not None:
            self.output_dirs.append(secondary_output_dir)
        self.num_workers = (
            num_workers if num_workers > 0 else max(os.cpu_count() or 1, 1)
        )
        self.use_state = use_state
        self.clear_output_on_all = clear_output_on_all
        self.uses_default_compiler = compiler_factory is None
        self.compiler_factory = compiler_factory or _create_nwn_compiler
        self.game_scripts = GameScriptSources(nwn_root)
        self.start_time = time.time()
        self.script_index: ScriptIndex | None = None

    def run(self) -> CompileResult:
        """Run the configured compiler mode, creating state first when smart compile needs it."""

        self._validate_game_script_sources()
        selector = self.selector
        if self.use_state and selector not in {"all", "*"} and not self._has_state():
            print("All scripts will be compiled to initialise the index.", end="\n\n")
            selector = "all"

        self.script_index = ScriptIndex(self.script_dir, include_dirs=self.include_dirs)

        if selector is None:
            return self.compile_modified()

        param = ScriptIndex.normalise_script_name(selector)
        if param == "all":
            return self.compile_all()
        if param.endswith("*"):
            return self.compile_wildcard(param)
        return self.compile_script(param)

    def compile_script(self, script_name: str) -> CompileResult:
        """Compile one named primary script and refresh state hashes."""

        script_index = self._require_index()
        script = script_index.get(script_name)
        if script is None or not script.primary:
            raise CompileError(f"Error: Unable to find {script_name}.nss")
        if not script.contents:
            raise CompileError(f"Error: {script_name}.nss is a base game script.")

        if script.is_include or not script_index.is_compilable(script):
            print("Include file detected. Checking dependencies...", end="\n\n")
            to_compile = script_index.get_related({script})
            if not to_compile:
                print(f"No scripts include {script.name}.")
                return CompileResult(compiled_count=0)
            print(f"{len(to_compile)} derived script(s) will be compiled.", end="\n\n")
            return self.compile(
                scripts=to_compile,
                new_hash_index=self._apply_hashes({script}),
            )

        print(f"\nCompiling: {script.name}")
        return self.compile(
            scripts={script},
            new_hash_index=self._apply_hashes({script}),
        )

    def compile_all(self) -> CompileResult:
        """Compile every primary script that declares an entry point."""

        script_index = self._require_index()
        scripts = script_index.compilable_primary_scripts()
        if self.clear_output_on_all:
            self.clear_output_folders()
        print(f"\nAll {len(scripts)} script(s) will be compiled.", end="\n\n")
        return self.compile(
            scripts=scripts,
            new_hash_index=self._hash_all() if self.use_state else None,
        )

    def compile_modified(self) -> CompileResult:
        """Compile scripts changed since the saved hash state."""

        script_index = self._require_index()
        if not self.use_state:
            return self.compile_all()
        if self.state_path is None:
            raise CompileError("A state path is required for modified compilation.")

        modified = script_index.get_modified(state_path=self.state_path)
        modified.update(self._missing_output_scripts())
        to_compile = script_index.get_related(modified)
        if not modified or not to_compile:
            print("All scripts are up to date.")
            return CompileResult(compiled_count=0)

        changed_lines = "\n- ".join(sorted(str(script) for script in modified))
        print(
            f"\n{len(modified)} change(s) found:",
            f"\n- {changed_lines}\n",
            f"\n{len(to_compile)} related script(s) will be compiled.",
            end="\n\n",
        )
        return self.compile(
            scripts=to_compile,
            new_hash_index=self._apply_hashes(modified),
        )

    def compile_wildcard(self, script_name: str) -> CompileResult:
        """Compile all scripts or every script matching a glob-style selector."""

        if script_name == "*":
            return self.compile_all()

        script_index = self._require_index()
        wildcard = re.compile(script_name.replace("*", ".*"), re.IGNORECASE)
        matches = {
            script
            for script in script_index.primary_scripts()
            if wildcard.match(script.name)
        }
        to_compile = script_index.get_related(matches)
        if not matches or not to_compile:
            print("No matches found.")
            return CompileResult(compiled_count=0)

        match_lines = "\n- ".join(sorted(str(script) for script in matches))
        print(
            f"\n{len(matches)} match(es) found:",
            f"\n- {match_lines}\n",
            f"\n{len(to_compile)} related script(s) will be compiled.",
            end="\n\n",
        )
        return self.compile(
            scripts=to_compile,
            new_hash_index=self._apply_hashes(matches),
        )

    def compile(
        self,
        scripts: Iterable[Script],
        *,
        new_hash_index: dict[str, int] | None = None,
    ) -> CompileResult:
        """Compile a concrete script set in parallel, write outputs, and persist state hashes."""

        script_set = set(scripts)
        if not script_set:
            print("No scripts to compile.\n\nOperation aborted.", end="\n\n")
            return CompileResult(compiled_count=0)

        _ensure_compiler_thread_stack_size()

        init_lock = threading.Lock()
        thread_local = threading.local()
        progress = ProgressBar(total=len(script_set))

        def init_compiler() -> None:
            """Create the thread-local compiler instance for worker threads."""

            # The third-party compiler is not shared across threads; one backend
            # instance is bound to each worker through thread-local storage.
            with init_lock:
                thread_local.compiler = self.compiler_factory(self.load_script)

        def compile_in_thread(script: Script) -> tuple[str, bytes]:
            """Compile one script inside the worker pool and normalize backend return shapes."""

            try:
                compiled = thread_local.compiler.compile(f"{script.name}.nss")
                ncs_bytes = compiled[0] if isinstance(compiled, tuple) else compiled
                progress.update()
                return f"{script.name}.ncs", ncs_bytes
            except CompilationError as error:
                progress.close()
                print(f"\n{error.message.splitlines()[0].split(' [')[0]}")
                raise

        try:
            with ThreadPoolExecutor(
                initializer=init_compiler,
                max_workers=min(self.num_workers, len(script_set)),
            ) as executor:
                # Sorting by script name keeps parallel compiles reproducible for
                # tests and for users reading compiler output.
                compiled = {
                    ncs_name: ncs_bytes
                    for ncs_name, ncs_bytes in executor.map(
                        compile_in_thread,
                        sorted(script_set, key=lambda script: script.name),
                    )
                    if ncs_bytes is not None
                }
            progress.close()
        except CompilationError as exc:
            raise CompileError(
                "\nStopping processing on first error.\n\n1 error; see above for context."
            ) from exc
        except KeyboardInterrupt as exc:
            progress.close(leave=False)
            raise CompileError("\nStopping processing on user request.") from exc
        except Exception as exc:
            progress.close()
            print("\nAn unexpected error occurred during compilation:", end="\n\n")
            traceback.print_exc()
            raise CompileError("\nStopping processing.") from exc

        print("\nWriting script(s) to output folder...")
        for output_dir in self.output_dirs:
            output_dir.mkdir(parents=True, exist_ok=True)
            for ncs_name, ncs_bytes in compiled.items():
                # State-enabled shared compiles may write to both the project
                # compiled directory and the live development output directory.
                (output_dir / ncs_name).write_bytes(ncs_bytes)

        if new_hash_index is not None and self.use_state:
            if self.state_path is None:
                raise CompileError("A state path is required to write script hashes.")
            write_hash_index(self.state_path, new_hash_index)

        compile_time = time.time() - self.start_time
        print(f"Success!\n\nTotal Execution time = {compile_time:.4f} seconds\n")
        return CompileResult(compiled_count=len(compiled))

    def load_script(self, script_name: str) -> bytes | None:
        """Load a custom script first, then fall back to base-game sources."""

        normalised = ScriptIndex.normalise_script_name(script_name)
        script_index = self._require_index()
        if (script := script_index.get(normalised)) and script.contents:
            return script.contents
        return self.game_scripts.get(f"{normalised}.nss")

    def clear_output_folders(self) -> None:
        """Remove compiled ``.ncs`` files and reset state before a full rebuild."""

        if self.use_state and self.state_path is not None:
            delete_hash_index(self.state_path)
        for directory in self.output_dirs:
            for file_path in directory.glob("*.ncs"):
                file_path.unlink()

    def _missing_output_scripts(self) -> set[Script]:
        """Return current scripts missing from at least one configured output directory."""

        script_index = self._require_index()
        return {
            script
            for script in script_index.compilable_primary_scripts()
            if any(
                not (output_dir / f"{script.name}.ncs").is_file()
                for output_dir in self.output_dirs
            )
        }

    def _has_state(self) -> bool:
        """Return whether compiled output and hash state already exist."""

        if self.state_path is None:
            return False
        return bool(list(self.output_dir.glob("*.ncs"))) and self.state_path.exists()

    def _apply_hashes(self, modified: set[Script]) -> dict[str, int] | None:
        """Merge modified-script hashes into the persisted hash index."""

        if not self.use_state:
            return None
        if self.state_path is None:
            raise CompileError("A state path is required to update script hashes.")
        hashes = read_hash_index(self.state_path)
        hashes.update(
            {
                script.name: script.hash
                for script in modified
                if script.primary and script.contents
            }
        )
        return hashes

    def _hash_all(self) -> dict[str, int] | None:
        """Build a complete hash index for every primary script currently indexed."""

        if not self.use_state:
            return None
        return {
            script.name: script.hash
            for script in self._require_index().primary_scripts()
        }

    def _require_index(self) -> ScriptIndex:
        """Return the initialized script index or fail with a programming error."""

        if self.script_index is None:
            raise RuntimeError("Script index has not been initialised.")
        return self.script_index

    def _validate_game_script_sources(self) -> None:
        """Ensure the default compiler can resolve nwscript.nss before compilation starts."""

        if not self.uses_default_compiler:
            return
        if self.game_scripts.get("nwscript.nss") is not None:
            return
        raise CompileError(
            "Error: Unable to find nwscript.nss in the NWN install data. "
            "Set NWN_INSTALL_PATH to the host Neverwinter Nights install root "
            "and make sure it is mounted at /nwn/install in Dockerized AREDev."
        )


def compile_scripts(
    *,
    script_dir: Path,
    output_dir: Path,
    selector: str | None = None,
    state_path: Path | None = None,
    nwn_root: Path | None = None,
    include_dirs: Iterable[Path] = (),
    secondary_output_dir: Path | None = None,
    num_workers: int = -1,
    use_state: bool = True,
    clear_output_on_all: bool = True,
    compiler_factory: CompilerFactory | None = None,
) -> CompileResult:
    """Compile NWScript sources using the stateful compiler facade and return a result summary."""

    compiler = StatefulScriptCompiler(
        script_dir=script_dir,
        output_dir=output_dir,
        selector=selector,
        state_path=state_path,
        nwn_root=nwn_root,
        include_dirs=include_dirs,
        secondary_output_dir=secondary_output_dir,
        num_workers=num_workers,
        use_state=use_state,
        clear_output_on_all=clear_output_on_all,
        compiler_factory=compiler_factory,
    )
    return compiler.run()


def hash_script_contents(script_contents: bytes) -> int:
    """Return a stable integer hash for script contents."""

    return int(sha256(script_contents).hexdigest(), 16)


def _read_script_bytes(script_path: Path) -> bytes:
    """Read a script, remapping Docker-visible host-root symlink targets when needed."""

    try:
        return script_path.read_bytes()
    except FileNotFoundError:
        remapped_path = _remap_containerized_host_symlink(script_path)
        if remapped_path is None:
            raise
        try:
            return remapped_path.read_bytes()
        except FileNotFoundError as remapped_exc:
            raise FileNotFoundError(
                f"Unable to read symlinked script '{script_path}'. "
                f"The symlink target '{script_path.readlink()}' is not visible "
                "inside this environment."
            ) from remapped_exc


def _remap_containerized_host_symlink(script_path: Path) -> Path | None:
    """Return a container path for a host-absolute symlink target, if applicable."""

    if not script_path.is_symlink():
        return None

    host_root = os.environ.get("AREDEV_HOST_ROOT", "")
    container_root = os.environ.get("AREDEV_CONFIG_ROOT", "")
    if not host_root or not container_root:
        return None

    link_target = script_path.readlink()
    target_text = str(link_target)
    relative_target = _relative_to_host_root(target_text, host_root)
    if relative_target is None:
        return None

    return Path(container_root).joinpath(*relative_target.split("/"))


def _relative_to_host_root(target_path: str, host_root: str) -> str | None:
    """Return a slash-normalized target path relative to the configured host root."""

    normalized_target = _normalize_host_path_text(target_path)
    normalized_root = _normalize_host_path_text(host_root)
    comparable_target = normalized_target
    comparable_root = normalized_root
    if _path_text_looks_windows(target_path) or _path_text_looks_windows(host_root):
        comparable_target = comparable_target.lower()
        comparable_root = comparable_root.lower()

    if comparable_target == comparable_root:
        return ""
    prefix = f"{comparable_root}/"
    if not comparable_target.startswith(prefix):
        return None
    return normalized_target[len(normalized_root) + 1 :]


def _normalize_host_path_text(path: str) -> str:
    """Normalize host path separators for textual prefix comparisons."""

    return path.replace("\\", "/").rstrip("/")


def _path_text_looks_windows(path: str) -> bool:
    """Return whether a path string uses Windows-style syntax."""

    return "\\" in path or (len(path) >= 2 and path[1] == ":")


def read_hash_index(path: Path) -> dict[str, int]:
    """Load script hashes from a state file."""

    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def write_hash_index(path: Path, hash_index: dict[str, int]) -> None:
    """Write script hashes to a state file."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hash_index), encoding="utf-8")
    except OSError as exc:
        raise CompileError(f"Error: Unable to store script hashes ({exc})") from exc


def delete_hash_index(path: Path) -> None:
    """Delete the state file if present."""

    if path.exists():
        path.unlink()


def _create_nwn_compiler(resolver: Callable[[str], bytes | None]) -> CompilerBackend:
    """Create the pinned third-party NWScript compiler backend with project resolver hooks."""

    return NwScriptCompiler(
        resolver=resolver,
        debug_info=False,
        max_include_depth=64,
    )


def _ensure_compiler_thread_stack_size() -> None:
    """Give native compiler worker threads enough stack for deep include trees."""

    global _compiler_thread_stack_configured
    with _compiler_thread_stack_lock:
        if _compiler_thread_stack_configured:
            return

        try:
            current_size = threading.stack_size()
            if current_size == 0 or current_size < MIN_COMPILER_THREAD_STACK_SIZE:
                threading.stack_size(MIN_COMPILER_THREAD_STACK_SIZE)
        except (RuntimeError, ValueError):
            # Some platforms reject explicit stack sizes. Keep the platform default.
            pass
        _compiler_thread_stack_configured = True


def _hash_text(text: str) -> int:
    """Hash synthetic text into the same integer space as script contents."""

    return int(sha256(text.encode("utf-8")).hexdigest(), 16)
