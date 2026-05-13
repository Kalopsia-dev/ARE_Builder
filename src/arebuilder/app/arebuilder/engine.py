import contextlib
import io
import os
import shutil
from pathlib import Path
from typing import Callable, Iterable

from arebuilder.builder.archive import build_archive
from arebuilder.builder.module_file import build_module_ifo
from arebuilder.builder.symlinks import (
    apply_symlink_plan,
    plan_symlinks_for_all,
    plan_symlinks_for_target,
    prune_stale_symlinks_for_target,
)
from arebuilder.config.module_settings import ModuleSettings
from arebuilder.config.runtime import BuildConfig, BuildModule, RuntimePaths
from arebuilder.content.palette import DEFAULT_PALETTES, generate_palette
from arebuilder.content.talktable import build_custom_content
from arebuilder.nwn.compat import TalkTable, load_talk_table, write_gff
from arebuilder.nwn.compile import compile_scripts

ScriptCompilerRunner = Callable[..., object]


class BuildEngine:
    """Execute builder commands for configured module workspaces."""

    def __init__(
        self,
        config: BuildConfig,
        runtime_paths: RuntimePaths | None = None,
        *,
        script_compiler: ScriptCompilerRunner | None = None,
    ):
        """Initialize the instance with its required collaborators and state."""

        self.config = config
        self.runtime_paths = runtime_paths or RuntimePaths()
        self.script_compiler = script_compiler or compile_scripts
        self._talk_table: TalkTable | None = None
        self.default_palettes = list(DEFAULT_PALETTES)
        self._custom_content_cache: set[tuple[str, str, str, str | None, str]] = set()

    def run_all(self, target_name: str) -> int:
        """Run a full build: stage artifacts, generate content, link resources, and pack archives."""

        for module, workspace in self._iter_module_workspaces(target_name):
            # A full build stages scripts and copied binaries before later steps
            # inspect the build directory for palettes, links, and packing.
            self._stage_target_artifacts(module, workspace)
            self._generate_custom_content(workspace)
            self._generate_palettes(workspace, self.default_palettes)
            self._apply_all_symlinks()
            self._write_and_pack(module, workspace)
            print("Full build complete.", flush=True)
        return 0

    def run_link(self, target_name: str) -> int:
        """Run link mode, refreshing override symlinks for the requested target or all targets."""

        for module in self._iter_modules(target_name):
            self._apply_target_symlinks(module)
        return 0

    def run_compile(self, selector: str | None = None) -> int:
        """Run the stateful shared-script compile command with the provided selector."""

        self._compile_shared_scripts(selector)
        return 0

    def run_pack(self, target_name: str) -> int:
        """Regenerate module.ifo files and pack archives without restaging resources."""

        for module, workspace in self._iter_module_workspaces(target_name):
            self._write_and_pack(module, workspace)
        return 0

    def run_palette(self, target_name: str, palette_types: list[str]) -> int:
        """Regenerate the selected custom palette files for each affected module."""

        for _, workspace in self._iter_module_workspaces(target_name):
            self._generate_palettes(workspace, palette_types)
        return 0

    def run_quick(self, target_name: str) -> int:
        """Run the quick build path, reusing existing palettes while refreshing scripts and archives."""

        for module, workspace in self._iter_module_workspaces(target_name):
            self._stage_target_artifacts(module, workspace)
            self._generate_custom_content(workspace)
            self._write_and_pack(module, workspace)
            print("Quick build complete (using existing palettes)", flush=True)
        return 0

    def _iter_modules(self, target_name: str) -> Iterable[BuildModule]:
        """Yield concrete modules selected by a target name, validating unknown targets early."""

        # "all" is the only pseudo-target; every other value must match a
        # resolved module name so a typo fails before any filesystem work starts.
        if target_name == "all":
            return self.config.modules.values()
        if target_name not in self.config.modules:
            raise KeyError(f"Unknown target: {target_name}")
        return [self.config.modules[target_name]]

    @property
    def talk_table(self) -> TalkTable:
        """Load the talk table only for commands that need localized strings."""

        if self._talk_table is None:
            self._talk_table = load_talk_table(self.config.talktable_path)
        return self._talk_table

    def _iter_module_workspaces(
        self, target_name: str
    ) -> Iterable[tuple[BuildModule, "ModuleBuildWorkspace"]]:
        """Resolve each selected module into a workspace immediately before command execution."""

        for module in self._iter_modules(target_name):
            yield module, ModuleBuildWorkspace.from_spec(module)

    def _stage_target_artifacts(
        self,
        module: BuildModule,
        workspace: "ModuleBuildWorkspace",
    ) -> None:
        """Copy precompiled scripts and compile target-local scripts into the build directory."""

        # Precompiled scripts are copied first so a fresh compile can intentionally
        # replace them with target-local output of the same basename.
        self._copy_precompiled_scripts(module, workspace.build_dir)
        self._compile_target_scripts(module, workspace)

    def _generate_palettes(
        self,
        workspace: "ModuleBuildWorkspace",
        palette_types: list[str],
    ) -> None:
        """Generate each requested palette type using the module source precedence order."""

        for palette_name in palette_types:
            print(f"Generating {palette_name} palette...", flush=True)
            generate_palette(
                palette_type=palette_name,
                source_dirs=workspace.source_dirs,
                build_dir=workspace.build_dir,
                talk_table=self.talk_table,
            )

    def _write_and_pack(
        self,
        module: BuildModule,
        workspace: "ModuleBuildWorkspace",
    ) -> None:
        """Write the generated module metadata file and pack the final module archive."""

        workspace.write_module_file()
        print(f"Packing module: {module.name}", flush=True)
        workspace.pack_archive()

    def _apply_all_symlinks(self) -> None:
        """Apply the full override symlink plan for shared and compiled resources."""

        print("Generating symlinks...", flush=True)
        apply_symlink_plan(
            plan_symlinks_for_all(
                override_dir=self.runtime_paths.override_dir,
                shared_root=self.runtime_paths.shared_root,
                compiled_root=self.runtime_paths.compiled_root,
                builder_mount_root=self.runtime_paths.builder_mount_root,
            )
        )

    def _apply_target_symlinks(self, module: BuildModule) -> None:
        """Apply override symlinks for one target module, replacing stale target links."""

        print("Generating symlinks...", flush=True)
        plans = plan_symlinks_for_target(
            target_name=module.name,
            override_dir=self.runtime_paths.override_dir,
            builder_root=self.runtime_paths.builder_root,
            source_root=self.runtime_paths.module_source_root(module),
            compiled_root=self.runtime_paths.compiled_root,
            builder_mount_root=self.runtime_paths.builder_mount_root,
        )
        prune_stale_symlinks_for_target(
            target_name=module.name,
            override_dir=self.runtime_paths.override_dir,
            builder_root=self.runtime_paths.builder_root,
            source_root=self.runtime_paths.module_source_root(module),
            compiled_root=self.runtime_paths.compiled_root,
            builder_mount_root=self.runtime_paths.builder_mount_root,
            active_plans=plans,
        )
        apply_symlink_plan(plans)

    def _compile_shared_scripts(self, selector: str | None) -> None:
        """Compile shared ARE scripts through the stateful direct command."""

        self.script_compiler(
            script_dir=self.runtime_paths.shared_compile_script_dir,
            output_dir=self.runtime_paths.shared_compile_output_dir,
            selector=selector,
            state_path=self.runtime_paths.state_file,
            nwn_root=self.runtime_paths.nwn_root,
            include_dirs=[],
            secondary_output_dir=self.runtime_paths.live_compile_output_dir,
            num_workers=self.runtime_paths.compile_workers,
            use_state=True,
            clear_output_on_all=True,
        )

    def _compile_target_scripts(
        self,
        module: BuildModule,
        workspace: "ModuleBuildWorkspace",
    ) -> None:
        """Compile only target-local scripts into a module build directory."""

        script_dir = self.runtime_paths.target_script_dir(module)
        if not script_dir.is_dir():
            return

        print("Compiling module scripts...", flush=True)
        self._run_quietly_on_success(
            lambda: self.script_compiler(
                script_dir=script_dir,
                output_dir=workspace.build_dir,
                selector="all",
                state_path=None,
                nwn_root=self.runtime_paths.nwn_root,
                include_dirs=self.runtime_paths.target_include_dirs(),
                secondary_output_dir=None,
                num_workers=self.runtime_paths.compile_workers,
                use_state=False,
                clear_output_on_all=False,
            )
        )

    def _copy_precompiled_scripts(self, module: BuildModule, build_dir: Path) -> None:
        """Flatten conventional precompiled files into the build directory."""

        build_dir.mkdir(parents=True, exist_ok=True)
        for compiled_dir in module.precompiled_dirs:
            if not compiled_dir.exists():
                continue
            for current_root, _, filenames in os.walk(compiled_dir):
                for filename in sorted(filenames):
                    source_path = Path(current_root) / filename
                    shutil.copy2(source_path, build_dir / filename)

    def _generate_custom_content(self, workspace: "ModuleBuildWorkspace") -> None:
        """Generate custom TLK and HAK outputs when a module enables them."""

        custom_tlk_name = workspace.settings.custom_tlk
        if not custom_tlk_name:
            return

        # HAK/TLK generation can be shared by multiple module runs in one process.
        # The cache key includes every path that can affect the generated output.
        cache_key = (
            custom_tlk_name,
            str(self.runtime_paths.resolved_custom_content_root),
            str(self.runtime_paths.hak_dir),
            str(self.runtime_paths.tlk_dir),
            str(self.runtime_paths.resolved_custom_content_reference)
            if self.runtime_paths.resolved_custom_content_reference is not None
            else "",
        )
        if cache_key in self._custom_content_cache:
            return

        print("Generating TLK references...", flush=True)
        build_custom_content(
            custom_tlk_name=custom_tlk_name,
            custom_content_root=self.runtime_paths.resolved_custom_content_root,
            hak_dir=self.runtime_paths.hak_dir,
            tlk_dir=self.runtime_paths.tlk_dir,
            reference_path=self.runtime_paths.resolved_custom_content_reference,
        )
        self._custom_content_cache.add(cache_key)

    @staticmethod
    def _run_quietly_on_success(action: Callable[[], object]) -> object:
        """Capture noisy compiler output and replay it only when the wrapped action fails."""

        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                return action()
        except Exception:
            captured_output = output.getvalue()
            if captured_output:
                print(captured_output, end="", flush=True)
            raise


class ModuleBuildWorkspace:
    """Collect the source directories, settings, build directory, and archive target for one module."""

    def __init__(
        self,
        *,
        module_name: str,
        source_dirs: list[Path],
        build_dir: Path,
        target_path: Path,
        settings: ModuleSettings,
        included_files: dict[str, Path],
    ):
        """Initialize the instance with its required collaborators and state."""

        self.module_name = module_name
        self.source_dirs = source_dirs
        self.build_dir = build_dir
        self.target_path = target_path
        self.settings = settings
        self.included_files = included_files

    @classmethod
    def from_spec(cls, module: BuildModule) -> "ModuleBuildWorkspace":
        """Resolve settings and included resource files for a parsed module specification."""

        module.build_dir.mkdir(parents=True, exist_ok=True)
        settings_path = _find_settings_file(module.source_dirs)
        if settings_path is None:
            raise FileNotFoundError(
                "No settings.txt file found within source directories."
            )
        settings = ModuleSettings.load(settings_path)
        included_files = scan_included_files(module.source_dirs, module.build_dir)
        return cls(
            module_name=module.name,
            source_dirs=list(module.source_dirs),
            build_dir=module.build_dir,
            target_path=module.target_path,
            settings=settings,
            included_files=included_files,
        )

    def write_module_file(self) -> None:
        """Generate module.ifo from parsed settings and write it as a GFF resource."""

        module_ifo = build_module_ifo(self.settings, self.included_files)
        write_gff(self.build_dir / "module.ifo", module_ifo, "IFO ")

    def pack_archive(self) -> None:
        """Pack the module build directory into the configured final archive path."""

        build_archive(self.target_path, self.build_dir)


def scan_included_files(source_dirs: list[Path], build_dir: Path) -> dict[str, Path]:
    """Build a basename inclusion map with last-source-wins precedence."""

    names: dict[str, Path] = {}
    for source_dir in [*source_dirs, build_dir]:
        if not source_dir.exists():
            continue
        for current_root, dirnames, filenames in os.walk(source_dir, topdown=True):
            # Editing dirnames in place prevents os.walk from descending into
            # hidden directories while preserving deterministic traversal order.
            dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
            for filename in sorted(filenames):
                # These files configure the build itself and should not become
                # resources packed into module archives.
                if any(
                    part.startswith(".") for part in Path(filename).parts
                ) or filename in {
                    "palette.txt",
                    "settings.txt",
                }:
                    continue
                names[filename] = Path(current_root) / filename
    return names


def _find_settings_file(source_dirs: list[Path]) -> Path | None:
    """Return the last ``settings.txt`` found across the source directories."""

    return next(
        (
            candidate
            for source_dir in reversed(source_dirs)
            if (candidate := source_dir / "settings.txt").exists()
        ),
        None,
    )
