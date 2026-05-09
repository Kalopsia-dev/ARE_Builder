from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from arebuilder.config.env import (
    expand_path,
    parse_key_value_text,
    validate_build_target,
)
from arebuilder.config.nwn_paths import resolve_nwn_home_root, resolve_nwn_install_root
from arebuilder.config.runtime import BuilderSettings

BuilderBackend = Literal["native", "docker"]
AREBUILDER_ENV_FILENAME = "arebuilder.env"
NWN_INSTALL_PATH_DESCRIPTION = (
    "NWN_INSTALL_PATH should point to your Neverwinter Nights install folder on "
    "the host machine. Use the folder that contains the game's data/ and bin/ "
    "directories, for example data/nwn_base.key or bin/.../nwmain."
)
NWN_HOME_PATH_DESCRIPTION = (
    "NWN_HOME_PATH should point to your Neverwinter Nights user folder on the "
    "host machine, usually Documents/Neverwinter Nights. This is the folder "
    "that contains or receives client-side hak/ and tlk/ directories."
)


class BuilderConfigError(ValueError):
    """Raised when the AREDev project env file cannot be loaded safely."""


@dataclass(slots=True, frozen=True)
class InferredProjectPaths:
    """Detected host paths written into a generated AREDev env file."""

    nwn_install_path: str = ""
    nwn_home_path: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class BuilderConfig:
    """Project-local settings shared by AREDev wrappers and Python commands."""

    build_target: str = "pgcc"
    builder_backend: BuilderBackend = "native"
    builder_image: str = "kalopsiadev/arebuilder:latest"
    nwserver_image: str = "dmhoodoo/aredevnwnxserver:latest"
    nwn_install_path: str = ""
    nwn_home_path: str = ""

    @property
    def module_name(self) -> str:
        """Return the module name used by build commands."""

        return f"are-dev-{self.build_target}"

    @property
    def nwn_install_root(self) -> Path | None:
        """Return the configured or auto-detected NWN install root."""

        return resolve_nwn_install_root(self.nwn_install_path)

    @property
    def nwn_home_root(self) -> Path | None:
        """Return the configured or auto-detected NWN home root."""

        return resolve_nwn_home_root(self.nwn_home_path)


@dataclass(slots=True, frozen=True)
class ProjectLayout:
    """Expose conventional paths under an initialized AREDev project root."""

    root: Path

    @classmethod
    def from_root(cls, root: Path | str) -> "ProjectLayout":
        """Create a layout from a user-supplied project root."""

        return cls(root=expand_path(root).resolve())

    @property
    def config_dir(self) -> Path:
        """Return the project configuration directory."""

        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        """Return the project data directory."""

        return self.root / "data"

    @property
    def temp_dir(self) -> Path:
        """Return the project temporary working directory."""

        return self.root / "temp"

    @property
    def compiled_resources_dir(self) -> Path:
        """Return the generated compiled-resources directory."""

        return self.root / "compiled-resources"

    @property
    def logs_dir(self) -> Path:
        """Return the server log directory."""

        return self.root / "logs"

    @property
    def server_dir(self) -> Path:
        """Return the local NWN home/server directory."""

        return self.root / "server"

    @property
    def development_dir(self) -> Path:
        """Return the live development resource directory."""

        return self.server_dir / "development"

    @property
    def hak_dir(self) -> Path:
        """Return the project HAK directory."""

        return self.server_dir / "hak"

    @property
    def localvault_dir(self) -> Path:
        """Return the local player vault directory."""

        return self.server_dir / "localvault"

    @property
    def modules_dir(self) -> Path:
        """Return the server modules directory."""

        return self.server_dir / "modules"

    @property
    def override_dir(self) -> Path:
        """Return the server override directory."""

        return self.server_dir / "override"

    @property
    def servervault_dir(self) -> Path:
        """Return the server vault directory."""

        return self.server_dir / "servervault"

    @property
    def tlk_dir(self) -> Path:
        """Return the project talk-table directory."""

        return self.server_dir / "tlk"

    @property
    def arebuilder_env_path(self) -> Path:
        """Return the AREBuilder environment file path."""

        return self.config_dir / AREBUILDER_ENV_FILENAME

    @property
    def nwserver_env_path(self) -> Path:
        """Return the NWServer environment file path."""

        return self.config_dir / "nwserver.env"

    @property
    def compose_file(self) -> Path:
        """Return the Docker Compose file path."""

        return self.root / "docker-compose.yml"

    @property
    def are_resources_dir(self) -> Path:
        """Return the shared ARE resources directory."""

        return self.root / "are-resources"

    def target_resources_dir(self, build_target: str) -> Path:
        """Return the host path for one target's resources checkout."""

        return self.root / f"{build_target}-resources"

    def build_dir(self, module_name: str) -> Path:
        """Return the generated build directory for one module."""

        return self.compiled_resources_dir / module_name

    def module_archive_path(self, module_name: str) -> Path:
        """Return the final module archive path for one module."""

        return self.modules_dir / f"{module_name}.mod"

    def ensure_runtime_dirs(self) -> None:
        """Create runtime directories that are safe to materialize eagerly."""

        for directory in (
            self.config_dir,
            self.data_dir,
            self.temp_dir,
            self.compiled_resources_dir,
            self.logs_dir,
            self.development_dir,
            self.hak_dir,
            self.localvault_dir,
            self.modules_dir,
            self.override_dir,
            self.servervault_dir,
            self.tlk_dir,
        ):
            if directory.is_symlink():
                continue
            directory.mkdir(parents=True, exist_ok=True)


def load_arebuilder_env(root: Path | str) -> BuilderConfig:
    """Load project-local AREDev settings from an initialized project root."""

    layout = ProjectLayout.from_root(root)
    values = _default_config_values()
    config_path = layout.arebuilder_env_path
    if not config_path.exists():
        raise BuilderConfigError(f"Missing builder configuration: {config_path}")

    values.update(parse_key_value_text(config_path.read_text(encoding="utf-8")))
    return _build_config(values)


def infer_project_paths(system: str | None = None) -> InferredProjectPaths:
    """Return inferred NWN paths and warnings for missing required paths."""

    warnings: list[str] = []
    nwn_install_root = resolve_nwn_install_root(system=system)
    nwn_home_root = resolve_nwn_home_root(system=system)

    if nwn_install_root is None:
        # Scaffolding should still complete when detection fails; the generated
        # env file keeps an empty field that the user can fill in later.
        warnings.append(
            "Warning: Unable to infer NWN_INSTALL_PATH. "
            f"{NWN_INSTALL_PATH_DESCRIPTION} Set it in "
            f"config/{AREBUILDER_ENV_FILENAME} before compiling scripts or "
            "using Dockerized AREDev."
        )
    if nwn_home_root is None:
        # NWN_HOME_PATH is optional for pure server builds, so this remains a
        # warning rather than a scaffold-blocking error.
        warnings.append(
            "Warning: Unable to infer NWN_HOME_PATH. "
            f"{NWN_HOME_PATH_DESCRIPTION} Set it in "
            f"config/{AREBUILDER_ENV_FILENAME} if you want generated HAK and "
            "TLK content linked into the game client."
        )

    return InferredProjectPaths(
        nwn_install_path=nwn_install_root.as_posix()
        if nwn_install_root is not None
        else "",
        nwn_home_path=nwn_home_root.as_posix() if nwn_home_root is not None else "",
        warnings=tuple(warnings),
    )


def render_arebuilder_env(
    *,
    build_target: str = "pgcc",
    builder_backend: BuilderBackend = "native",
    nwn_install_path: str | None = None,
    nwn_home_path: str | None = None,
    system: str | None = None,
) -> str:
    """Render a project-local AREDev env file with concrete values."""

    validate_build_target(build_target, error_type=BuilderConfigError)
    _validate_backend(builder_backend)
    inferred_paths = (
        infer_project_paths(system=system)
        if nwn_install_path is None or nwn_home_path is None
        else InferredProjectPaths()
    )
    values = _default_config_values()
    values.update(
        {
            "BUILD_TARGET": build_target,
            "BUILDER_BACKEND": builder_backend,
            "NWN_INSTALL_PATH": nwn_install_path
            if nwn_install_path is not None
            else inferred_paths.nwn_install_path,
            "NWN_HOME_PATH": nwn_home_path
            if nwn_home_path is not None
            else inferred_paths.nwn_home_path,
        }
    )
    # Stable env ordering makes regenerated scaffolds easy to review and keeps
    # tests focused on intentional value changes.
    return _format_env_values(values)


def build_project_builder_settings(
    *,
    layout: ProjectLayout,
    config: BuilderConfig,
    live: bool,
    containerized: bool = False,
) -> BuilderSettings:
    """Return native builder settings for one AREDev project invocation."""

    return BuilderSettings(
        project_root=layout.root,
        build_target=config.build_target,
        builder_mount_root="/var/builder",
        server_root=layout.server_dir,
        hak_dir=Path("/nwn/home/hak") if containerized else layout.hak_dir,
        tlk_dir=Path("/nwn/home/tlk") if containerized else layout.tlk_dir,
        nwn_root=Path("/nwn/install") if containerized else config.nwn_install_root,
        compile_live=live,
    )


def load_env_file(path: Path) -> dict[str, str]:
    """Load a simple env file when present, returning an empty mapping otherwise."""

    if not path.exists():
        return {}
    return parse_key_value_text(path.read_text(encoding="utf-8"))


def _build_config(values: dict[str, str]) -> BuilderConfig:
    """Construct the single-target BuildConfig from resolved runtime paths."""

    backend = values.get("BUILDER_BACKEND", "native").strip().lower()
    _validate_backend(backend)
    build_target = values.get("BUILD_TARGET", "pgcc").strip()
    validate_build_target(build_target, error_type=BuilderConfigError)
    return BuilderConfig(
        build_target=build_target,
        builder_backend=backend,  # type: ignore[arg-type]
        builder_image=values.get("BUILDER_IMAGE", "kalopsiadev/arebuilder:latest"),
        nwserver_image=values.get("NWSERVER_IMAGE", "dmhoodoo/aredevnwnxserver:latest"),
        nwn_install_path=values.get("NWN_INSTALL_PATH", ""),
        nwn_home_path=values.get("NWN_HOME_PATH", ""),
    )


def _default_config_values() -> dict[str, str]:
    """Return scaffold defaults for an AREDev project config file."""

    return {
        "BUILD_TARGET": "pgcc",
        "BUILDER_BACKEND": "native",
        "BUILDER_IMAGE": "kalopsiadev/arebuilder:latest",
        "NWSERVER_IMAGE": "dmhoodoo/aredevnwnxserver:latest",
        "NWN_INSTALL_PATH": "",
        "NWN_HOME_PATH": "",
    }


def _format_env_values(values: dict[str, str]) -> str:
    """Serialize environment values in stable key order for generated config files."""

    lines = ["# AREDev project settings"]
    for key, value in values.items():
        lines.append(f"{key}={_quote_config_value(value)}")
    return "\n".join(lines) + "\n"


def _quote_config_value(value: str | Path) -> str:
    """Quote a config value only when shell-style parsing would otherwise change it."""

    text = value.as_posix() if isinstance(value, Path) else str(value)
    if not text:
        return ""
    if not any(character.isspace() for character in text):
        return text
    escaped = text.replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def _validate_backend(value: str) -> None:
    """Validate that the configured builder backend is one of the supported modes."""

    if value not in {"native", "docker"}:
        raise BuilderConfigError(
            f"Invalid BUILDER_BACKEND {value!r}; expected native or docker."
        )
