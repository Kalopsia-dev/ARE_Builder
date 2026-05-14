import argparse
import importlib.metadata
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Protocol

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings import completion as completion_bindings
from prompt_toolkit.shortcuts.prompt import CompleteStyle

from arebuilder.app.arebuilder.build_command import execute_build_command
from arebuilder.app.arebuilder.engine import (
    ModuleBuildWorkspace,
    _print_area_dependency_warnings,
)
from arebuilder.app.aredev.host_bridge import (
    HOST_COMMAND_DIR,
    HOST_DOCKER_TIMEOUT_SECONDS,
    HOST_LAUNCH_TIMEOUT_SECONDS,
    HostBridge,
    host_bridge_error,
    host_path_looks_windows,
    join_host_path,
)
from arebuilder.app.aredev.process import ProcessResult, default_process_runner
from arebuilder.app.aredev.project import (
    DEFAULT_AREBUILDER_REPO,
    NWN_HOME_PATH_DESCRIPTION,
    NWN_INSTALL_PATH_DESCRIPTION,
    BuilderConfig,
    BuilderConfigError,
    ProjectLayout,
    build_project_builder_settings,
    load_arebuilder_env,
    load_env_file,
)
from arebuilder.builder.symlinks import (
    PlannedSymlink,
    apply_symlink_plan,
    count_symlink_plan_steps,
)
from arebuilder.config.nwn_paths import find_nwn_client_executable
from arebuilder.config.runtime import BuildModule

COMPOSE_ARGS = ["docker", "compose", "--progress", "quiet"]
AREDEV_PROJECT = "aredev"
SCREEN_CLEAR = "\033[H\033[2J\033[3J"
HOST_MODULES_ENV = "AREDEV_NWN_HOME_MODULES_ROOT"
AREDEV_RESTART_EXIT_CODE = 75
UPDATE_LOCK_WAIT_SECONDS_ENV = "AREDEV_UPDATE_LOCK_WAIT_SECONDS"
UPDATE_LOCK_DEFAULT_WAIT_SECONDS = 1800.0
UPDATE_LOCK_POLL_SECONDS = 2.0
AREDEV_COMMANDS = (
    "update",
    "compile",
    "build",
    "start",
    "stop",
    "database",
    "nwn",
    "toolset",
    "help",
    "clear",
    "exit",
    "quit",
)
AREDEV_COMMAND_HANDLERS = {
    "help": ("show_help", False),
    "clear": ("clear", False),
    "update": ("update", False),
    "compile": ("compile", True),
    "build": ("build", False),
    "start": ("start", False),
    "stop": ("stop", False),
    "database": ("database", True),
    "nwn": ("nwn", True),
    "toolset": ("toolset", True),
}
AREDEV_SUBCOMMANDS = {
    "database": ("drop",),
    "nwn": ("dm",),
    "toolset": ("run",),
}


ProcessRunner = Callable[..., ProcessResult]
BuildRunner = Callable[[str, list[str], bool], int]
Output = Callable[[str], None]
Input = Callable[[str], str]
ScreenClearer = Callable[[], None]
ServerState = Callable[[], bool | None]


class ToolsetProgress(Protocol):
    """Small progress protocol used by Toolset bundle preparation."""

    def update(self, count: int = 1) -> None:
        """Advance the progress display."""

    def close(self) -> None:
        """Close the progress display."""


ToolsetProgressFactory = Callable[[int], ToolsetProgress]


@dataclass(frozen=True, slots=True)
class _AREDevCompletionCandidate:
    """A prompt candidate and the length of text it would replace before the cursor."""

    text: str
    replacement_length: int


class _ServerStateCache:
    """Cache the running-server probe without blocking prompt completions."""

    def __init__(self, probe: Callable[[], bool], ttl_seconds: float = 2.0):
        """Initialize the cache with a state probe and short time-to-live."""

        self.probe = probe
        self.ttl_seconds = ttl_seconds
        self.value: bool | None = None
        self.expires_at = 0.0
        self._lock = threading.Lock()
        self._refreshing = False
        self._refresh_thread: threading.Thread | None = None
        self._generation = 0

    def __call__(self) -> bool | None:
        """Return the cached state and refresh stale values in the background."""

        now = time.monotonic()
        with self._lock:
            value = self.value
            if now < self.expires_at:
                return value
            if self._refreshing:
                return value
            self._refreshing = True
            generation = self._generation
            thread = threading.Thread(
                target=self._refresh,
                args=(generation,),
                daemon=True,
            )
            self._refresh_thread = thread
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                self._refreshing = False
            return value
        return value

    def set(self, value: bool | None) -> None:
        """Set the current state after an authoritative command result."""

        with self._lock:
            self._generation += 1
            self.value = value
            self.expires_at = time.monotonic() + self.ttl_seconds

    def invalidate(self) -> None:
        """Mark the current value stale while preserving it as a fallback."""

        with self._lock:
            self.expires_at = 0.0

    def wait_for_refresh(self, timeout_seconds: float) -> bool:
        """Wait for an active refresh thread, returning whether it completed."""

        with self._lock:
            thread = self._refresh_thread
        if thread is None:
            return True
        thread.join(timeout_seconds)
        return not thread.is_alive()

    def _refresh(self, generation: int) -> None:
        """Refresh the cached value in a background thread."""

        try:
            value = self.probe()
        except Exception:
            value = None
        with self._lock:
            if generation == self._generation:
                self.value = value
                self.expires_at = time.monotonic() + self.ttl_seconds
            self._refreshing = False


class _NullToolsetProgress:
    """No-op Toolset progress handle for non-interactive output."""

    def update(self, count: int = 1) -> None:
        """Ignore progress updates."""

    def close(self) -> None:
        """Ignore progress close."""


class _TqdmToolsetProgress:
    """Wrap tqdm for Toolset bundle progress."""

    def __init__(self, total: int):
        """Initialize the progress bar."""

        from tqdm import tqdm

        self.progress = tqdm(
            total=total,
            desc="Linking resources",
            unit="file",
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
            leave=True,
        )

    def update(self, count: int = 1) -> None:
        """Advance the progress bar."""

        self.progress.update(count)

    def close(self) -> None:
        """Close the progress bar."""

        self.progress.close()


class AREDevCompleter(Completer):
    """Offer interactive completions for AREDev commands, subcommands, and script selectors."""

    def __init__(
        self,
        layout: ProjectLayout,
        server_state: ServerState | None = None,
    ):
        """Initialize the instance with its required collaborators and state."""

        self.layout = layout
        self.server_state = server_state

    def get_completions(self, document, complete_event):
        """Yield command or argument completions for the current prompt text."""

        for candidate in _interactive_completion_candidates(
            self.layout,
            document.text_before_cursor,
            server_state=self.server_state,
        ):
            yield Completion(
                candidate.text,
                start_position=-candidate.replacement_length,
            )


class AREDevAutoSuggest(AutoSuggest):
    """Offer fish-style inline suggestions for AREDev commands and arguments."""

    def __init__(
        self,
        layout: ProjectLayout,
        server_state: ServerState | None = None,
    ):
        """Initialize the instance with its required collaborators and state."""

        self.layout = layout
        self.server_state = server_state

    def get_suggestion(self, _buffer, document):
        """Return the inline suffix for the first matching AREDev completion."""

        if document.cursor_position != len(document.text):
            return None
        if not document.text_before_cursor.strip():
            return None

        for candidate in _interactive_completion_candidates(
            self.layout,
            document.text_before_cursor,
            server_state=self.server_state,
        ):
            suffix = candidate.text[candidate.replacement_length :]
            if suffix:
                return Suggestion(suffix)
        return None


class AREDevController:
    """Coordinate AREDev build, server, database, Docker, and host-launcher workflows."""

    def __init__(
        self,
        *,
        layout: ProjectLayout,
        config: BuilderConfig,
        process_runner: ProcessRunner | None = None,
        build_runner: BuildRunner | None = None,
        output: Output | None = None,
        input_reader: Input | None = None,
        screen_clearer: ScreenClearer | None = None,
        toolset_progress_factory: ToolsetProgressFactory | None = None,
    ):
        """Initialize the instance with its required collaborators and state."""

        self.layout = layout
        self.config = config
        self.host_bridge = HostBridge(layout.temp_dir)
        self.process_runner = process_runner or default_process_runner
        self.build_runner = build_runner or self._run_configured_builder
        self.output = output or _print_line
        self.input_reader = input_reader
        self.screen_clearer = screen_clearer or _clear_terminal
        self.toolset_progress_factory = (
            toolset_progress_factory or _create_toolset_progress
        )
        self._prompt_session: PromptSession | None = None
        self._server_state_cache: _ServerStateCache | None = None
        self._path_warnings_shown = False
        self._running_interactive = False

    @classmethod
    def from_root(
        cls,
        root: Path | str,
        *,
        process_runner: ProcessRunner | None = None,
        build_runner: BuildRunner | None = None,
        output: Output | None = None,
        input_reader: Input | None = None,
        screen_clearer: ScreenClearer | None = None,
        toolset_progress_factory: ToolsetProgressFactory | None = None,
    ) -> "AREDevController":
        """Load project layout and config, then create an AREDev controller with optional test hooks."""

        layout = ProjectLayout.from_root(root)
        return cls(
            layout=layout,
            config=load_arebuilder_env(layout.root),
            process_runner=process_runner,
            build_runner=build_runner,
            output=output,
            input_reader=input_reader,
            screen_clearer=screen_clearer,
            toolset_progress_factory=toolset_progress_factory,
        )

    def run(self, command: str | None, args: list[str]) -> int:
        """Dispatch one non-interactive AREDev command and return its process-style status code."""

        lock_status = self._handle_update_lock(command)
        if lock_status is not None:
            return lock_status

        if command in (None, ""):
            self.show_banner()
            return 0
        command = command.lower()
        if command in {"exit", "quit"}:
            return 0
        handler = AREDEV_COMMAND_HANDLERS.get(command)
        if handler is not None:
            method_name, accepts_args = handler
            # Resolve the method at dispatch time so tests and callers can swap
            # controller behavior after construction.
            method = getattr(self, method_name)
            result = method(args) if accepts_args else method()
            return 0 if result is None else result

        self.output("Invalid command. Type 'help' for usage instructions.")
        return 1

    def run_interactive(self) -> int:
        """Run a blocking AREDev command prompt until the user exits."""

        lock_status = self._handle_update_lock(None)
        if lock_status is not None:
            return lock_status

        self._running_interactive = True
        self.clear()
        while True:
            try:
                line = self._read_interactive_line(f"{self.config.build_target}> ")
            except EOFError:
                return 0
            except KeyboardInterrupt:
                self.output("")
                return 0

            line = line.strip()
            if not line:
                continue
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                # Bad quoting should not terminate an interactive session; report
                # it and let the user enter the command again.
                self.output(f"Invalid input: {exc}")
                continue

            command, *args = parts
            if command.lower() in {"exit", "quit"}:
                return 0
            result = self.run(command, args)
            if result == AREDEV_RESTART_EXIT_CODE:
                return result

    def show_banner(self) -> None:
        """Print the generated project logo followed by the command help text."""

        self._print_file(self.layout.data_dir / "logo.txt")
        self.show_help()
        self._warn_missing_paths_once()

    def show_help(self) -> None:
        """Print the packaged AREDev command help text through the output channel."""

        self._print_file(self.layout.data_dir / "help.txt")

    def clear(self) -> None:
        """Clear the interactive terminal and redraw the banner so the prompt starts cleanly."""

        self.screen_clearer()
        self.show_banner()

    def update(self) -> int:
        """Update the configured AREDev builder runtime."""

        if _in_builder_container():
            if not self._ensure_host_bridge():
                return 1
            result = self.host_bridge.request(
                {"COMMAND": "update_restart"},
                timeout_seconds=HOST_LAUNCH_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                return self._report_process_failure(result)
            self.output("Updating containers. AREDev will restart after the update.")
            return AREDEV_RESTART_EXIT_CODE

        if self.config.builder_backend == "native":
            return self._update_native_environment()

        return self._update_containers()

    def _update_native_environment(self) -> int:
        """Install the configured GitHub builder package and refresh Docker images."""

        package_before = _installed_arebuilder_version()
        repo = (self.config.arebuilder_repo or DEFAULT_AREBUILDER_REPO).strip()
        if not repo:
            self.output("AREBUILDER_REPO is empty in config/arebuilder.env.")
            return 1

        requirement = repo if repo.startswith("git+") else f"git+{repo}"
        self.output("Updating arebuilder...")
        result = self.process_runner(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--upgrade",
                "--force-reinstall",
                "--no-deps",
                requirement,
            ],
            cwd=self.layout.root,
            env=os.environ.copy(),
            capture_output=False,
            background=False,
        )
        if result.returncode != 0:
            return self._report_process_failure(result)

        package_after = _installed_arebuilder_version()
        package_changed = package_before != package_after
        if package_changed and package_after is not None:
            self.output(f"Updated arebuilder to version {package_after}.")
        container_status = self._update_containers()
        return self._handle_native_update_restart(package_changed, container_status)

    def _update_containers(self) -> int:
        """Pull Docker images for the AREDev server stack."""

        if self._is_server_running():
            result = self._run_compose(["down"])
            if result.returncode != 0:
                return self._report_process_failure(result)
        self.output("Updating containers...")
        result = self._run_compose(["pull", "--ignore-pull-failures"])
        return self._report_process_failure(result)

    def _handle_native_update_restart(
        self,
        package_changed: bool,
        update_status: int,
    ) -> int:
        """Restart the interactive native prompt when pip installed new code."""

        if update_status != 0:
            return update_status

        if not package_changed:
            self.output("Update complete.")
            return update_status

        if not self._running_interactive:
            self.output("Update complete. Please restart AREDev.")
            return update_status

        self.output("Update complete. Restarting...")
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-m",
                "arebuilder",
                "aredev",
                "--root",
                str(self.layout.root),
            ],
        )
        return update_status

    def compile(self, args: list[str]) -> int:
        """Compile shared scripts, mirroring live output when the server runs."""

        if len(args) > 1:
            self.output("compile accepts at most one selector argument.")
            return 1
        if not self.layout.are_resources_dir.exists():
            self.output(
                "are-resources directory not found. Clone the resources repository first."
            )
            return 1

        live = self._is_server_running()
        if live:
            self.output("Server is running, live compiling.")
        return self.build_runner("compile", args, live)

    def build(self) -> int:
        """Validate runtime paths, run the configured builder, and surface build failures."""

        if self._is_server_running():
            self.output("Server is running.")
            return 1
        if not self._validate_target_resources():
            return 1
        if not self._prepare_runtime_environment():
            return 1
        return self.build_runner("all", [self.config.module_name], False)

    def start(self) -> int:
        """Build/link resources, prepare runtime files, and start the Dockerized NWN server."""

        if not self._ensure_host_bridge():
            return 1
        if not self._validate_target_resources():
            return 1
        if not self._prepare_runtime_environment():
            return 1
        self._copy_nwnplayer_ini()

        if self._is_server_running():
            self.output("Server is already running.")
            self._set_cached_server_state(True)
            return 0

        link_status = self.build_runner("link", [self.config.module_name], False)
        if link_status != 0:
            return link_status

        result = self._run_compose(
            ["up", "-d", "nwserver"],
            extra_env={"NWN_MODULE": self.config.module_name},
        )
        if result.returncode == 0:
            self.output("Server started.")
            self._set_cached_server_state(True)
        return self._report_process_failure(result)

    def stop(self) -> int:
        """Stop the Dockerized NWN server and clear live compile output."""

        if not self._ensure_host_bridge():
            return 1
        if not self._is_server_running():
            self.output("Server is not running.")
            self._set_cached_server_state(False)
            return 0

        self.output("Stopping server...")
        result = self._run_compose(["down"])
        if result.returncode != 0:
            return self._report_process_failure(result)
        self.output("Cleaning development folder...")
        self._clear_development_scripts()
        self._set_cached_server_state(False)
        return 0

    def database(self, args: list[str]) -> int:
        """Show database connection guidance or drop AREDev database volumes on request."""

        if not self._ensure_host_bridge():
            return 1
        if len(args) > 1:
            self.output("database accepts at most one argument.")
            return 1
        if args and args[0] == "drop":
            return self._drop_database()
        if args:
            self.output("Invalid database command. Use 'database' or 'database drop'.")
            return 1
        if not self._is_container_running("aredevdbserver"):
            self.output("Database is not running.")
            return 0

        env_values = load_env_file(self.layout.nwserver_env_path)
        username = env_values.get("NWNX_SQL_USERNAME", "aredev")
        password = env_values.get("NWNX_SQL_PASSWORD", "aredevdbpass")
        database = env_values.get("NWNX_SQL_DATABASE", "nwn")
        if _in_builder_container():
            self._print_database_connection_info(
                username=username,
                password=password,
                database=database,
            )
            return 0

        result = self._run_compose(
            ["exec", "db", "mysql", f"-u{username}", f"-p{password}", database]
        )
        return self._report_process_failure(result)

    def nwn(self, args: list[str]) -> int:
        """Launch the NWN client and connect to the running test server."""

        if not self._ensure_host_bridge():
            return 1
        if len(args) > 1 or (args and args[0] != "dm"):
            self.output("Usage: nwn [dm]")
            return 1
        if not self._is_server_running():
            self.output("Server is not running.")
            return 0

        env_values = load_env_file(self.layout.nwserver_env_path)
        port = env_values.get("NWN_PORT", "5121")
        password_key = "NWN_DMPASSWORD" if args == ["dm"] else "NWN_PLAYERPASSWORD"
        password = env_values.get(password_key, "aredev")
        if _in_builder_container():
            result = self.host_bridge.request(
                {
                    "COMMAND": "nwn",
                    "MODE": "dm" if args == ["dm"] else "player",
                    "PORT": port,
                    "PASSWORD": password,
                },
                timeout_seconds=HOST_LAUNCH_TIMEOUT_SECONDS,
            )
            return self._report_process_failure(result)

        client_args = self._client_command()
        if not client_args:
            self.output(
                "Unable to find the NWN client executable. Set NWN_INSTALL_PATH "
                "in config/arebuilder.env if autodetection missed your install."
            )
            return 1

        connection_args = ["+connect", f"127.0.0.1:{port}", "+password", password]
        if args == ["dm"]:
            connection_args.insert(0, "-dmc")
        result = self.process_runner(
            [*client_args, *connection_args],
            cwd=Path(client_args[0]).parent,
            env=os.environ.copy(),
            capture_output=False,
            background=True,
            detach=True,
        )
        return self._report_process_failure(result)

    def toolset(self, args: list[str]) -> int:
        """Prepare a Toolset-ready module bundle and optionally launch the Toolset."""

        if len(args) > 1 or (args and args[0] != "run"):
            self.output("Usage: toolset [run]")
            return 1

        should_launch = args == ["run"]
        launch_command: list[str] = []
        launch_cwd: Path | None = None
        if should_launch:
            if _in_builder_container():
                if not self._ensure_host_bridge():
                    return 1
                check_result = self.host_bridge.request(
                    {"COMMAND": "toolset_check"},
                    timeout_seconds=HOST_LAUNCH_TIMEOUT_SECONDS,
                )
                if check_result.returncode != 0:
                    return self._report_process_failure(check_result)
            else:
                launch_command = self._toolset_launch_command()
                if not launch_command:
                    return 1
                toolset = self._toolset_executable()
                launch_cwd = toolset.parent if toolset is not None else None

        bundle_status = self._prepare_toolset_bundle()
        if bundle_status != 0:
            return bundle_status

        if not should_launch:
            return 0

        if _in_builder_container():
            result = self.host_bridge.request(
                {"COMMAND": "toolset"},
                timeout_seconds=HOST_LAUNCH_TIMEOUT_SECONDS,
            )
        else:
            result = self.process_runner(
                launch_command,
                cwd=launch_cwd or self.layout.root,
                env=os.environ.copy(),
                capture_output=False,
                background=True,
                detach=True,
            )
        status = self._report_process_failure(result)
        if status == 0:
            self.output("Launching toolset...")
        return status

    def _run_configured_builder(
        self,
        command: str,
        args: list[str],
        live: bool,
    ) -> int:
        """Run the selected native or Docker builder backend."""

        # Docker mode starts a builder container from the host, but once inside
        # that container commands execute natively to avoid recursive Compose runs.
        if self.config.builder_backend == "docker" and not _in_builder_container():
            return self._run_docker_builder(command, args, live)
        return self._run_native_builder(command, args, live)

    def _run_native_builder(
        self,
        command: str,
        args: list[str],
        live: bool,
    ) -> int:
        """Run the builder directly in the current Python process."""

        settings = build_project_builder_settings(
            layout=self.layout,
            config=self.config,
            live=live,
            containerized=_in_builder_container(),
        )
        target_name = args[0] if args else None
        try:
            return execute_build_command(
                command=command,
                target_name=target_name,
                settings=settings,
            )
        except Exception as exc:
            self.output(str(exc))
            return 1

    def _run_docker_builder(
        self,
        command: str,
        args: list[str],
        live: bool,
    ) -> int:
        """Run the builder service through Docker Compose."""

        builder_args: list[str] = []
        if live:
            builder_args.append("--live")
        builder_args.extend([command, *args])
        result = self._run_compose(["run", "--rm", "builder", *builder_args])
        return self._report_process_failure(result)

    def _prepare_runtime_environment(self) -> bool:
        """Create runtime directories and validate host content links before work starts."""

        self.layout.ensure_runtime_dirs()
        if _in_builder_container():
            return True
        try:
            self._link_nwn_home_content_dirs()
        except (BuilderConfigError, OSError) as exc:
            self.output(str(exc))
            return False
        return True

    def _link_nwn_home_content_dirs(self) -> None:
        """Link project HAK and TLK directories into an external NWN home when configured."""

        nwn_home_root = self.config.nwn_home_root
        if nwn_home_root is None:
            return
        nwn_home_root = nwn_home_root.resolve()

        for name, link_path in (
            ("hak", self.layout.hak_dir),
            ("tlk", self.layout.tlk_dir),
        ):
            target_path = nwn_home_root / name
            target_path.mkdir(parents=True, exist_ok=True)
            # HAK/TLK folders may be large and shared with the user's NWN client,
            # so the project points at them instead of copying their contents.
            _ensure_directory_symlink(link_path=link_path, target_path=target_path)

    def _validate_target_resources(self) -> bool:
        """Validate that required shared and target resource directories exist."""

        if not self.layout.are_resources_dir.exists():
            self.output(
                "are-resources directory not found. Clone the resources repository first."
            )
            return False
        target_dir = self.layout.target_resources_dir(self.config.build_target)
        if not target_dir.exists():
            self.output(f"Invalid build target: {target_dir.name} does not exist.")
            return False
        return True

    def _copy_nwnplayer_ini(self) -> None:
        """Copy project NWN player settings into the runtime server directory."""

        source = self.layout.are_resources_dir / "config" / "nwnplayer.ini"
        if source.exists():
            shutil.copyfile(source, self.layout.server_dir / "nwnplayer.ini")

    def _clear_development_scripts(self) -> None:
        """Remove live development compiled scripts before starting a fresh server session."""

        self.layout.development_dir.mkdir(parents=True, exist_ok=True)
        for path in self.layout.development_dir.glob("*.ncs"):
            if path.is_file() or path.is_symlink():
                path.unlink()

    def _drop_database(self) -> int:
        """Remove AREDev database volumes when the server is stopped."""

        if self._is_container_running("aredevdbserver"):
            self.output("Database is running. Stop the server to perform this action.")
            return 1

        if _in_builder_container():
            result = self.host_bridge.request(
                {"COMMAND": "volume_drop"},
                timeout_seconds=HOST_DOCKER_TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and result.stdout:
                self.output(result.stdout.rstrip())
            return self._report_process_failure(result)

        result = self.process_runner(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            cwd=self.layout.root,
            env=self._compose_env(),
            capture_output=True,
            background=False,
        )
        if result.returncode != 0:
            return self._report_process_failure(result)

        expected = {
            f"{AREDEV_PROJECT}_database",
            f"{AREDEV_PROJECT}_data",
        }
        existing = sorted(expected.intersection(result.stdout.splitlines()))
        if not existing:
            self.output("Database volume not found.")
            return 0

        remove_result = self.process_runner(
            ["docker", "volume", "rm", *existing],
            cwd=self.layout.root,
            env=self._compose_env(),
            capture_output=False,
            background=False,
        )
        if remove_result.returncode == 0:
            self.output("Database dropped.")
        return self._report_process_failure(remove_result)

    def _is_server_running(self) -> bool:
        """Return whether the AREDev NWServer container is currently running."""

        return self._is_container_running("aredevnwserver")

    def _set_cached_server_state(self, running: bool) -> None:
        """Update prompt completion state after a successful server command."""

        if self._server_state_cache is not None:
            self._server_state_cache.set(running)

    def _is_container_running(self, container_name: str) -> bool:
        """Return whether a named Docker container is running, delegating to the host when needed."""

        if _in_builder_container():
            result = self.host_bridge.request(
                {
                    "COMMAND": "container_status",
                    "CONTAINER": container_name,
                },
                timeout_seconds=HOST_DOCKER_TIMEOUT_SECONDS,
            )
            return result.returncode == 0 and result.stdout.strip() == "true"

        result = self.process_runner(
            ["docker", "ps", "--format", "{{.Names}}"],
            cwd=self.layout.root,
            env=self._compose_env(),
            capture_output=True,
            background=False,
        )
        if result.returncode != 0:
            return False
        return container_name in set(result.stdout.splitlines())

    def _run_compose(
        self,
        args: list[str],
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        """Run Docker Compose locally or delegate it to the host launcher bridge."""

        self._ensure_compose_support_files()
        if _in_builder_container():
            return self.host_bridge.compose(args, extra_env=extra_env)

        return self.process_runner(
            [
                *COMPOSE_ARGS,
                "-p",
                AREDEV_PROJECT,
                *args,
            ],
            cwd=self.layout.root,
            env=self._compose_env(extra_env),
            capture_output=False,
            background=False,
        )

    def _compose_env(
        self,
        extra_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the environment passed to Docker Compose commands."""

        env = os.environ.copy()
        host_root = os.environ.get("AREDEV_HOST_ROOT") or str(self.layout.root)
        config_root = os.environ.get("AREDEV_CONFIG_ROOT") or str(self.layout.root)
        nwn_install_path = os.environ.get("AREDEV_NWN_INSTALL_ROOT") or str(
            self.config.nwn_install_root or ""
        )
        nwn_home_path = os.environ.get("AREDEV_NWN_HOME_ROOT") or str(
            self.config.nwn_home_root or self.layout.server_dir
        )
        # Host paths stay in the environment even for containerized Python; the
        # wrapper/host launcher use them to run Docker and NWN on the host side.
        env.update(
            {
                "PWD": host_root,
                "AREDEV_HOST_ROOT": host_root,
                "AREDEV_CONFIG_ROOT": config_root,
                "AREDEV_NWN_HOME_ROOT": nwn_home_path,
                "BUILD_TARGET": self.config.build_target,
                "BUILDER_IMAGE": self.config.builder_image,
                "NWSERVER_IMAGE": self.config.nwserver_image,
                "AREDEV_NWN_INSTALL_ROOT": nwn_install_path,
                "NWN_INSTALL_PATH": nwn_install_path,
                "NWN_HOME_PATH": nwn_home_path,
            }
        )
        if extra_env:
            env.update(extra_env)
        if os.environ.get("DOCKER_HOST"):
            env.setdefault("COMPOSE_CONVERT_WINDOWS_PATHS", "1")
        return env

    def _ensure_compose_support_files(self) -> None:
        """Restore generated Compose support files that may be missing from an older scaffold."""

        for relative_path in (
            "config/db.env",
            "config/nwserver.env",
            "data/timeinit.sql",
        ):
            path = self.layout.root / relative_path
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_read_aredev_template(relative_path), encoding="utf-8")

    def _client_command(self) -> list[str]:
        """Return the host NWN client executable command, if it can be found."""

        nwn_root = self.config.nwn_install_root
        if nwn_root is None:
            return []
        client = find_nwn_client_executable(nwn_root)
        return [str(client)] if client is not None else []

    def _prepare_toolset_bundle(self) -> int:
        """Link resources and copy the built module into the NWN Toolset module tree."""

        module_path = self.layout.module_archive_path(self.config.module_name)
        if not module_path.exists():
            self.output("You must build the module first.")
            return 1

        modules_root = self._toolset_modules_root()
        if modules_root is None:
            return 1
        toolset_module_dir = modules_root / self.config.module_name
        toolset_module_dir.mkdir(parents=True, exist_ok=True)

        self.output("Planning symlinks...")
        resource_plans = self._toolset_symlink_plans(toolset_module_dir)
        resource_plans = self._filter_toolset_area_dependency_plans(resource_plans)
        copy_mode = _in_builder_container() and host_path_looks_windows(
            os.environ.get("AREDEV_HOST_ROOT", "")
        )
        manifest_entries = self._load_toolset_manifest_entries() if copy_mode else {}
        next_manifest_entries: dict[str, dict[str, object]] = {}
        resource_keys = {f"resource:{plan.link_path.name}" for plan in resource_plans}
        resource_work = (
            len(resource_plans)
            if copy_mode
            else count_symlink_plan_steps(resource_plans)
        )
        progress = self.toolset_progress_factory(resource_work + 1)
        try:
            if copy_mode:
                self._prune_toolset_manifest_entries(
                    toolset_module_dir=toolset_module_dir,
                    manifest_entries=manifest_entries,
                    active_resource_keys=resource_keys,
                )
                self._copy_toolset_resources(
                    resource_plans,
                    manifest_entries=manifest_entries,
                    next_manifest_entries=next_manifest_entries,
                    progress=progress,
                )
            else:
                self._prune_stale_toolset_symlinks(
                    toolset_module_dir=toolset_module_dir,
                    resource_plans=resource_plans,
                )
                apply_symlink_plan(resource_plans, progress=progress.update)

            module_destination = modules_root / module_path.name
            if copy_mode:
                self._copy_toolset_file_with_manifest(
                    source_path=module_path,
                    destination_path=module_destination,
                    kind="module",
                    key=f"module:{module_path.name}",
                    overwrite=True,
                    manifest_entries=manifest_entries,
                    next_manifest_entries=next_manifest_entries,
                    progress=progress,
                )
                self._save_toolset_manifest_entries(next_manifest_entries)
            else:
                if not _same_existing_file(module_path, module_destination):
                    shutil.copyfile(module_path, module_destination)
                progress.update()
        finally:
            progress.close()

        self.output("Toolset bundle ready.")
        return 0

    def _toolset_modules_root(self) -> Path | None:
        """Return the modules directory the Toolset should read from."""

        if _in_builder_container():
            modules_root = os.environ.get(HOST_MODULES_ENV, "")
            if not modules_root:
                self.output(
                    "Dockerized toolset bundling requires a refreshed AREDev "
                    "scaffold with the NWN home modules mount."
                )
                return None
            return Path(modules_root)

        nwn_home_root = self.config.nwn_home_root
        if nwn_home_root is None:
            self.output(
                "Unable to find NWN_HOME_PATH. Set NWN_HOME_PATH in "
                "config/arebuilder.env before preparing a Toolset bundle."
            )
            return None
        return nwn_home_root / "modules"

    def _toolset_symlink_plans(self, toolset_module_dir: Path) -> list[PlannedSymlink]:
        """Return the resource links that make a module folder Toolset-ready."""

        plans: list[PlannedSymlink] = []
        plans.extend(
            self._toolset_directory_plans(
                self.layout.are_resources_dir / "gff",
                toolset_module_dir,
                overwrite=False,
            )
        )
        plans.extend(
            self._toolset_directory_plans(
                self.layout.are_resources_dir / "scripts",
                toolset_module_dir,
                overwrite=False,
            )
        )
        plans.extend(
            self._toolset_directory_plans(
                self.layout.target_resources_dir(self.config.build_target),
                toolset_module_dir,
                overwrite=True,
            )
        )
        plans.extend(
            self._toolset_directory_plans(
                self.layout.build_dir(self.config.module_name),
                toolset_module_dir,
                overwrite=False,
                exclude_suffixes={".ncs"},
            )
        )
        return plans

    def _filter_toolset_area_dependency_plans(
        self,
        resource_plans: list[PlannedSymlink],
    ) -> list[PlannedSymlink]:
        """Remove Toolset area resources that cannot load with enabled HAKs."""

        module = BuildModule(
            name=self.config.module_name,
            source_dirs=[
                self.layout.are_resources_dir / "gff",
                self.layout.target_resources_dir(self.config.build_target),
            ],
            build_dir=self.layout.build_dir(self.config.module_name),
            target_path=self.layout.module_archive_path(self.config.module_name),
        )
        workspace = ModuleBuildWorkspace.from_spec(module)
        report = workspace.filter_area_dependencies(
            hak_dir=self.layout.hak_dir,
            nwn_root=self.config.nwn_install_root,
        )
        _print_area_dependency_warnings(workspace.settings, report)
        omitted_areas = {
            omission.area_name.lower() for omission in report.omitted_areas
        }
        if not omitted_areas:
            return resource_plans
        area_suffixes = {".are", ".git", ".gic"}
        return [
            plan
            for plan in resource_plans
            if not (
                plan.link_path.suffix.lower() in area_suffixes
                and plan.link_path.stem.lower() in omitted_areas
            )
        ]

    def _prune_stale_toolset_symlinks(
        self,
        *,
        toolset_module_dir: Path,
        resource_plans: Sequence[PlannedSymlink],
    ) -> None:
        """Remove obsolete symlinks owned by the Toolset resource bundle."""

        if not toolset_module_dir.exists():
            return
        active_targets = {(plan.link_path, plan.target_path) for plan in resource_plans}
        managed_roots = self._toolset_managed_target_roots()
        for link_path in sorted(toolset_module_dir.iterdir()):
            if not link_path.is_symlink():
                continue
            target_path = os.readlink(link_path)
            if (link_path, target_path) in active_targets:
                continue
            if _path_string_is_under_roots(target_path, managed_roots):
                link_path.unlink()

    def _toolset_managed_target_roots(self) -> tuple[str, ...]:
        """Return target strings for source roots managed by the Toolset bundle."""

        roots = (
            self.layout.are_resources_dir / "gff",
            self.layout.are_resources_dir / "scripts",
            self.layout.target_resources_dir(self.config.build_target),
            self.layout.build_dir(self.config.module_name),
        )
        return tuple(self._toolset_source_target(root) for root in roots)

    def _toolset_directory_plans(
        self,
        source_root: Path,
        toolset_module_dir: Path,
        *,
        overwrite: bool,
        exclude_suffixes: set[str] | None = None,
    ) -> list[PlannedSymlink]:
        """Plan flat-by-basename resource links for one Toolset source tree."""

        if not source_root.exists():
            return []
        excluded = exclude_suffixes or set()
        plans: list[PlannedSymlink] = []
        for source_path in sorted(source_root.rglob("*")):
            if not source_path.is_file():
                continue
            if source_path.suffix.lower() in excluded:
                continue
            plans.append(
                PlannedSymlink(
                    link_path=toolset_module_dir / source_path.name,
                    target_path=self._toolset_source_target(source_path),
                    overwrite=overwrite,
                    source_path=source_path,
                )
            )
        return plans

    def _copy_toolset_resources(
        self,
        plans: Sequence[PlannedSymlink],
        *,
        manifest_entries: Mapping[str, Mapping[str, object]],
        next_manifest_entries: dict[str, dict[str, object]],
        progress: ToolsetProgress,
    ) -> None:
        """Apply Toolset resource plans as plain file copies."""

        for plan in plans:
            if plan.source_path is None:
                msg = f"Toolset copy plan for {plan.link_path} has no source path."
                raise ValueError(msg)
            self._copy_toolset_file_with_manifest(
                source_path=plan.source_path,
                destination_path=plan.link_path,
                kind="resource",
                key=f"resource:{plan.link_path.name}",
                overwrite=plan.overwrite,
                manifest_entries=manifest_entries,
                next_manifest_entries=next_manifest_entries,
                progress=progress,
            )

    def _copy_toolset_file_with_manifest(
        self,
        *,
        source_path: Path,
        destination_path: Path,
        kind: str,
        key: str,
        overwrite: bool,
        manifest_entries: Mapping[str, Mapping[str, object]],
        next_manifest_entries: dict[str, dict[str, object]],
        progress: ToolsetProgress,
    ) -> None:
        """Copy one Toolset file, skipping unchanged managed destinations."""

        try:
            entry = manifest_entries.get(key)
            if entry is not None and self._toolset_manifest_entry_is_current(
                entry,
                kind=kind,
                source_path=source_path,
                destination_path=destination_path,
            ):
                next_manifest_entries[key] = dict(entry)
                return

            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if destination_path.is_symlink():
                destination_path.unlink()
            elif os.path.lexists(destination_path):
                managed = entry is not None and self._toolset_manifest_entry_owns(
                    entry,
                    kind=kind,
                    source_path=source_path,
                    destination_path=destination_path,
                )
                if overwrite or managed:
                    destination_path.unlink()
                else:
                    return

            shutil.copyfile(source_path, destination_path)
            next_manifest_entries[key] = self._toolset_manifest_entry(
                kind=kind,
                source_path=source_path,
                destination_path=destination_path,
            )
        finally:
            progress.update()

    def _prune_toolset_manifest_entries(
        self,
        *,
        toolset_module_dir: Path,
        manifest_entries: Mapping[str, Mapping[str, object]],
        active_resource_keys: set[str],
    ) -> None:
        """Remove obsolete Toolset copies tracked by the previous manifest."""

        for key, entry in manifest_entries.items():
            if key in active_resource_keys or entry.get("kind") != "resource":
                continue
            destination_name = entry.get("destination")
            if not isinstance(destination_name, str):
                continue
            if not destination_name or Path(destination_name).name != destination_name:
                continue
            destination_path = toolset_module_dir / destination_name
            if self._toolset_manifest_destination_matches(entry, destination_path):
                destination_path.unlink()

    def _load_toolset_manifest_entries(self) -> dict[str, Mapping[str, object]]:
        """Load the Windows Docker Toolset copy manifest entries."""

        path = self._toolset_manifest_path()
        if not path.exists():
            return {}
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(manifest, dict):
            return {}
        if manifest.get("schema_version") != 1:
            return {}
        if manifest.get("module_name") != self.config.module_name:
            return {}
        if manifest.get("copy_mode") != "docker-windows":
            return {}
        entries = manifest.get("entries")
        if not isinstance(entries, dict):
            return {}
        return {
            str(key): value for key, value in entries.items() if isinstance(value, dict)
        }

    def _save_toolset_manifest_entries(
        self,
        entries: Mapping[str, Mapping[str, object]],
    ) -> None:
        """Save the Windows Docker Toolset copy manifest under project temp."""

        path = self._toolset_manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": 1,
            "module_name": self.config.module_name,
            "copy_mode": "docker-windows",
            "host_root": os.environ.get("AREDEV_HOST_ROOT", ""),
            "entries": entries,
        }
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _toolset_manifest_path(self) -> Path:
        """Return the Toolset copy manifest path in the project temp directory."""

        return self.layout.temp_dir / f"toolset-manifest-{self.config.module_name}.json"

    def _toolset_manifest_entry_is_current(
        self,
        entry: Mapping[str, object],
        *,
        kind: str,
        source_path: Path,
        destination_path: Path,
    ) -> bool:
        """Return whether a manifest entry still matches source and destination."""

        if destination_path.is_symlink() or not destination_path.is_file():
            return False
        if not self._toolset_manifest_entry_owns(
            entry,
            kind=kind,
            source_path=source_path,
            destination_path=destination_path,
        ):
            return False
        try:
            source_stat = source_path.stat()
        except OSError:
            return False
        if not self._toolset_manifest_destination_matches(entry, destination_path):
            return False
        return (
            entry.get("source_size") == source_stat.st_size
            and entry.get("source_mtime_ns") == source_stat.st_mtime_ns
        )

    def _toolset_manifest_destination_matches(
        self,
        entry: Mapping[str, object],
        destination_path: Path,
    ) -> bool:
        """Return whether a Toolset manifest entry still owns the destination."""

        if destination_path.is_symlink() or not destination_path.is_file():
            return False
        try:
            destination_stat = destination_path.stat()
        except OSError:
            return False
        return (
            entry.get("destination") == destination_path.name
            and entry.get("destination_size") == destination_stat.st_size
            and entry.get("destination_mtime_ns") == destination_stat.st_mtime_ns
        )

    def _toolset_manifest_entry_owns(
        self,
        entry: Mapping[str, object],
        *,
        kind: str,
        source_path: Path,
        destination_path: Path,
    ) -> bool:
        """Return whether a manifest entry describes this managed copy."""

        return (
            entry.get("kind") == kind
            and entry.get("destination") == destination_path.name
            and entry.get("source") == self._toolset_manifest_source(source_path)
        )

    def _toolset_manifest_entry(
        self,
        *,
        kind: str,
        source_path: Path,
        destination_path: Path,
    ) -> dict[str, object]:
        """Build one manifest entry after copying a Toolset file."""

        source_stat = source_path.stat()
        destination_stat = destination_path.stat()
        return {
            "kind": kind,
            "destination": destination_path.name,
            "source": self._toolset_manifest_source(source_path),
            "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "destination_size": destination_stat.st_size,
            "destination_mtime_ns": destination_stat.st_mtime_ns,
        }

    def _toolset_manifest_source(self, source_path: Path) -> str:
        """Return a stable source path string for the Toolset copy manifest."""

        try:
            return (
                source_path.resolve().relative_to(self.layout.root.resolve()).as_posix()
            )
        except ValueError:
            return str(source_path.resolve())

    def _toolset_source_target(self, source_path: Path) -> str:
        """Return the host-visible target string for a Toolset resource link."""

        if _in_builder_container():
            host_root = os.environ.get("AREDEV_HOST_ROOT")
            if host_root:
                try:
                    relative_path = source_path.resolve().relative_to(
                        self.layout.root.resolve()
                    )
                except ValueError:
                    return str(source_path)
                return join_host_path(host_root, relative_path)
        return str(source_path.resolve())

    def _toolset_executable(self) -> Path | None:
        """Return the configured Windows Toolset executable, if it exists."""

        nwn_root = self.config.nwn_install_root
        if nwn_root is None:
            return None
        toolset = nwn_root / "bin" / "win32" / "nwtoolset.exe"
        return toolset if toolset.exists() else None

    def _toolset_launch_command(self) -> list[str]:
        """Return the native host launch command for the NWN Toolset."""

        toolset = self._toolset_executable()
        if toolset is None:
            self.output(
                "Unable to find the NWN Toolset executable. Set NWN_INSTALL_PATH "
                "in config/arebuilder.env if autodetection missed your install."
            )
            return []
        if sys.platform == "win32":
            return [str(toolset)]

        wine = shutil.which("wine64") or shutil.which("wine")
        if wine is None:
            self.output(
                "The NWN Toolset is unavailable on this platform and wine was "
                "not found on PATH."
            )
            return []
        return [wine, "start", "/unix", str(toolset)]

    def _print_database_connection_info(
        self,
        *,
        username: str,
        password: str,
        database: str,
    ) -> None:
        """Print host-side database connection guidance for users."""

        self.output("Run this from the AREDev project root on the host:")
        self.output(
            "docker compose --env-file config/arebuilder.env "
            f"-p aredev exec db mysql -u{username} -p{password} {database}"
        )
        self.output(
            f"Compose network URL: mysql://{username}:{password}@db:3306/{database}"
        )

    def _ensure_host_bridge(self) -> bool:
        """Return whether containerized workflows can reach the host-launcher bridge."""

        if not _in_builder_container() or self.host_bridge.available():
            return True
        self.output(host_bridge_error())
        return False

    def _handle_update_lock(self, command: str | None) -> int | None:
        """Wait for an in-progress host update before dispatching container commands."""

        if not _in_builder_container():
            return None

        waited = self._wait_for_update_lock()
        if waited is None:
            return 1
        if waited and command is not None and command.lower() == "update":
            self.output("Update already completed by another AREDev process.")
            return 0
        return None

    def _wait_for_update_lock(self) -> bool | None:
        """Wait for the host-side update lock to disappear, returning whether we waited."""

        lock_dir = self.layout.temp_dir / HOST_COMMAND_DIR / "update.lock"
        if not lock_dir.is_dir():
            return False

        self.output("AREDev update is already running; waiting for it to finish...")
        deadline = time.monotonic() + _update_lock_wait_seconds()
        while lock_dir.is_dir():
            now = time.monotonic()
            if now >= deadline:
                self.output(f"Timed out waiting for AREDev update lock: {lock_dir}")
                self.output(
                    "If no update is running, inspect and remove that directory."
                )
                return None
            time.sleep(min(UPDATE_LOCK_POLL_SECONDS, max(0.0, deadline - now)))
        return True

    def _print_file(self, path: Path) -> None:
        """Print a text file through the controller output channel if it exists."""

        if path.exists():
            self.output(path.read_text(encoding="utf-8").rstrip())

    def _report_process_failure(self, result: ProcessResult) -> int:
        """Print captured stderr for failed subprocesses and return their exit status."""

        if result.returncode == 0:
            return 0
        if result.stderr:
            self.output(result.stderr.rstrip())
        return result.returncode

    def _warn_missing_paths_once(self) -> None:
        """Emit missing NWN path warnings at most once per controller instance."""

        if self._path_warnings_shown:
            return
        self._path_warnings_shown = True
        if self.config.nwn_install_root is None:
            self.output(
                "Warning: Unable to infer NWN_INSTALL_PATH. "
                f"{NWN_INSTALL_PATH_DESCRIPTION} Set it in "
                "config/arebuilder.env before compiling scripts or using "
                "Dockerized AREDev."
            )
        if self.config.nwn_home_root is None:
            self.output(
                "Warning: Unable to infer NWN_HOME_PATH. "
                f"{NWN_HOME_PATH_DESCRIPTION} Set it in "
                "config/arebuilder.env if you want generated HAK and TLK "
                "content linked into the game client."
            )

    def _read_interactive_line(self, prompt: str) -> str:
        """Read one interactive command from the configured input source."""

        if self.input_reader is not None:
            return self.input_reader(prompt)
        if self._prompt_session is None:
            self._server_state_cache = _ServerStateCache(self._is_server_running)
            self._prompt_session = _create_prompt_session(
                self.layout,
                server_state=self._server_state_cache,
            )
        elif self._server_state_cache is not None:
            self._server_state_cache.invalidate()
        return self._prompt_session.prompt(prompt)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the AREDev controller entry point and translate exceptions into exit codes."""

    try:
        args = _parse_args(argv or [])
        controller = AREDevController.from_root(args.root)
        if args.command is None:
            return controller.run_interactive()
        return controller.run(args.command, list(args.args))
    except (BuilderConfigError, ValueError) as exc:
        print(str(exc), flush=True)
        return 1


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments without executing the command."""

    parser = argparse.ArgumentParser(
        prog="arebuilder aredev",
        description="Run AREDev project workflow commands.",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("AREDEV_ROOT", "."),
        help="AREDev project root. Defaults to AREDEV_ROOT or cwd.",
    )
    parser.add_argument("command", nargs="?")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser.parse_args(list(argv))


def _print_line(text: str) -> None:
    """Print one flushed line of user-visible output."""

    print(text, flush=True)


def _installed_arebuilder_version() -> str | None:
    """Return the installed arebuilder package version."""

    try:
        return importlib.metadata.version("arebuilder")
    except importlib.metadata.PackageNotFoundError:
        return None


def _clear_terminal() -> None:
    """Emit the ANSI sequence used to clear the interactive screen."""

    print(SCREEN_CLEAR, end="", flush=True)


def _create_toolset_progress(total: int) -> ToolsetProgress:
    """Create an interactive Toolset progress bar when stderr is a terminal."""

    if total < 2 or not sys.stderr.isatty():
        return _NullToolsetProgress()
    try:
        return _TqdmToolsetProgress(total)
    except ImportError:
        return _NullToolsetProgress()


def _in_builder_container() -> bool:
    """Return whether the current process is running inside the builder container."""

    return os.environ.get("AREDEV_IN_CONTAINER") == "1"


def _update_lock_wait_seconds() -> float:
    """Return the configured maximum wait for a host-side AREDev update."""

    raw_value = os.environ.get(UPDATE_LOCK_WAIT_SECONDS_ENV)
    if raw_value is None:
        return UPDATE_LOCK_DEFAULT_WAIT_SECONDS
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return UPDATE_LOCK_DEFAULT_WAIT_SECONDS


def _read_aredev_template(relative_path: str) -> str:
    """Read one packaged AREDev scaffold template by relative path."""

    template_root = resources.files("arebuilder").joinpath("templates", "aredev")
    return template_root.joinpath(relative_path).read_text(encoding="utf-8")


def _same_existing_file(left: Path, right: Path) -> bool:
    """Return whether two existing paths name the same filesystem object."""

    if not right.exists():
        return False
    try:
        return left.samefile(right)
    except OSError:
        return False


def _create_prompt_session(
    layout: ProjectLayout,
    server_state: ServerState | None = None,
) -> PromptSession:
    """Create the prompt-toolkit session with session-local history."""

    layout.temp_dir.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=InMemoryHistory(),
        completer=AREDevCompleter(layout, server_state=server_state),
        auto_suggest=AREDevAutoSuggest(layout, server_state=server_state),
        complete_while_typing=False,
        complete_style=CompleteStyle.READLINE_LIKE,
        key_bindings=_create_prompt_key_bindings(),
    )


def _create_prompt_key_bindings() -> KeyBindings:
    """Create AREDev prompt key bindings."""

    bindings = KeyBindings()

    @Condition
    def suggestion_available() -> bool:
        buffer = get_app().current_buffer
        return (
            buffer.suggestion is not None
            and bool(buffer.suggestion.text)
            and buffer.document.is_cursor_at_the_end
        )

    @bindings.add("tab", filter=suggestion_available, eager=True)
    def _accept_suggestion(event) -> None:
        suggestion = event.current_buffer.suggestion
        if suggestion is not None:
            event.current_buffer.insert_text(suggestion.text)

    @bindings.add("s-tab", eager=True)
    def _display_all_completions(event) -> None:
        _display_all_completions_like_readline(event)

    return bindings


def _display_all_completions_like_readline(event) -> None:
    """Display every matching completion in prompt-toolkit's readline-style list."""

    buffer = event.current_buffer
    if buffer.completer is None:
        return

    completions = list(
        buffer.completer.get_completions(
            buffer.document,
            CompleteEvent(completion_requested=True),
        )
    )
    if completions:
        # The public readline handler can insert a common suffix instead of
        # listing candidates; Shift+Tab is intentionally a pure listing command.
        completion_bindings._display_completions_like_readline(event.app, completions)


def _interactive_completion_candidates(
    layout: ProjectLayout,
    text_before_cursor: str,
    *,
    server_state: ServerState | None = None,
) -> Sequence[_AREDevCompletionCandidate]:
    """Return completion candidates for the AREDev prompt text before the cursor."""

    stripped = text_before_cursor.lstrip()
    if not stripped or (" " not in stripped and "\t" not in stripped):
        prefix = stripped.lower()
        return tuple(
            _AREDevCompletionCandidate(f"{command} ", len(stripped))
            for command in _ordered_aredev_commands(server_state)
            if command.startswith(prefix)
        )

    command = stripped.split(maxsplit=1)[0].lower()
    prefix = "" if stripped.endswith((" ", "\t")) else stripped.rsplit(None, 1)[-1]
    replacement_length = len(prefix)
    if command == "compile":
        return tuple(
            _AREDevCompletionCandidate(script_name, replacement_length)
            for script_name in _compile_candidates(layout)
            if script_name.startswith(prefix.lower())
        )

    subcommand_candidates = _subcommand_candidates(command)
    if subcommand_candidates and _subcommand_argument_is_complete(stripped):
        return ()

    return tuple(
        _AREDevCompletionCandidate(f"{candidate} ", replacement_length)
        for candidate in subcommand_candidates
        if candidate.startswith(prefix.lower())
    )


def _compile_candidates(layout: ProjectLayout) -> list[str]:
    """Return script names that can be offered after the compile command."""

    script_dir = layout.are_resources_dir / "scripts"
    candidates = {"all"}
    if script_dir.is_dir():
        candidates.update(path.stem.lower() for path in script_dir.glob("*.nss"))
    return sorted(candidates)


def _ordered_aredev_commands(server_state: ServerState | None) -> tuple[str, ...]:
    """Return top-level commands with start/stop ordered by current server state."""

    commands = list(AREDEV_COMMANDS)
    if server_state is None:
        return tuple(commands)
    try:
        server_running = server_state()
    except Exception:
        return tuple(commands)
    if server_running is None:
        return tuple(commands)

    preferred = "stop" if server_running else "start"
    secondary = "start" if server_running else "stop"
    preferred_index = commands.index(preferred)
    secondary_index = commands.index(secondary)
    if preferred_index > secondary_index:
        commands[preferred_index], commands[secondary_index] = (
            commands[secondary_index],
            commands[preferred_index],
        )
    return tuple(commands)


def _subcommand_candidates(command: str) -> tuple[str, ...]:
    """Return valid subcommands for a top-level AREDev command."""

    return AREDEV_SUBCOMMANDS.get(command, ())


def _subcommand_argument_is_complete(
    text: str,
) -> bool:
    """Return whether the cursor is past a single subcommand argument."""

    parts = text.split()
    if len(parts) > 2:
        return True
    return len(parts) == 2 and text.endswith((" ", "\t"))


def _ensure_directory_symlink(*, link_path: Path, target_path: Path) -> None:
    """Ensure a directory path is a symlink to the requested target."""

    link_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_target = target_path.resolve()
    if _path_is_junction(link_path):
        if link_path.resolve() == resolved_target:
            return
        raise BuilderConfigError(
            f"Refusing to replace existing directory junction: {link_path} -> "
            f"{link_path.resolve()}"
        )
    if link_path.is_symlink():
        if link_path.resolve() == resolved_target:
            return
        raise BuilderConfigError(
            f"Refusing to replace existing symlink: {link_path} -> "
            f"{link_path.resolve()}"
        )

    if link_path.exists():
        if not link_path.is_dir():
            raise BuilderConfigError(
                f"Refusing to replace existing non-directory path: {link_path}"
            )
        if any(link_path.iterdir()):
            raise BuilderConfigError(
                f"Refusing to replace non-empty directory with NWN home symlink: "
                f"{link_path}"
            )
        # Empty scaffold-created directories are safe to replace with symlinks,
        # while populated directories are protected above.
        link_path.rmdir()

    try:
        link_path.symlink_to(target_path, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            try:
                _create_directory_junction(link_path=link_path, target_path=target_path)
                return
            except OSError:
                pass
        link_path.mkdir(parents=True, exist_ok=True)
        raise


def _path_string_is_under_roots(path: str, roots: Sequence[str]) -> bool:
    """Return whether a host-visible path string belongs to any managed root."""

    normalized_path = path.rstrip("/\\")
    for root in roots:
        normalized_root = root.rstrip("/\\")
        if normalized_path == normalized_root:
            return True
        if normalized_path.startswith(f"{normalized_root}/"):
            return True
        if normalized_path.startswith(f"{normalized_root}\\"):
            return True
    return False


def _path_is_junction(path: Path) -> bool:
    """Return whether a path is a Windows directory junction."""

    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _create_directory_junction(*, link_path: Path, target_path: Path) -> None:
    """Create a Windows directory junction without requiring symlink privileges."""

    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise OSError(message or f"Unable to create directory junction: {link_path}")
