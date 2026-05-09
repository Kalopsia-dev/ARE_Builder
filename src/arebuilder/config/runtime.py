from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from arebuilder.config.env import (
    expand_path,
    parse_key_value_text,
    validate_build_target,
)
from arebuilder.config.nwn_paths import resolve_nwn_install_root

AREBUILDER_ENV_FILENAME = "arebuilder.env"


@dataclass(slots=True)
class BuildModule:
    """A buildable module resolved from the standard AREDev project layout."""

    name: str
    source_dirs: list[Path]
    build_dir: Path
    target_path: Path
    precompiled_dirs: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class BuildConfig:
    """Builder input derived from ``arebuilder.env`` and project conventions."""

    talktable_path: Path
    modules: dict[str, BuildModule]


@dataclass(slots=True)
class RuntimePaths:
    """Group all filesystem roots and compile options needed by one builder invocation."""

    default_nwn_root: ClassVar[Path] = Path("/nwn/install")
    default_state_file: ClassVar[Path] = Path("temp/script_index.json")

    builder_root: Path = Path(".")
    builder_mount_root: str = "."
    shared_root: Path = Path("are-resources")
    compiled_root: Path = Path("compiled-resources")
    server_root: Path = Path("server")
    hak_dir: Path = Path("server/hak")
    tlk_dir: Path = Path("server/tlk")
    state_file: Path = default_state_file
    nwn_root: Path | None = None
    compile_script_dir: Path | None = None
    compile_output_dir: Path | None = None
    compile_workers: int = -1
    compile_live: bool = False
    custom_content_reference: Path | None = None
    target_source_roots: dict[str, Path] = field(default_factory=dict)

    @property
    def override_dir(self) -> Path:
        """Return the server override directory."""

        return self.server_root / "override"

    @property
    def modules_dir(self) -> Path:
        """Return the server modules directory."""

        return self.server_root / "modules"

    @property
    def shared_compile_script_dir(self) -> Path:
        """Return the shared script source directory."""

        return self.compile_script_dir or self.shared_root / "scripts"

    @property
    def shared_compile_output_dir(self) -> Path:
        """Return the shared compiled-script output directory."""

        return self.compile_output_dir or self.compiled_root

    @property
    def live_compile_output_dir(self) -> Path | None:
        """Return the live development output directory when live compile is enabled."""

        if not self.compile_live:
            return None
        return self.server_root / "development"

    @property
    def resolved_custom_content_root(self) -> Path:
        """Return the root that contains custom-content source data."""

        return self.shared_root / "Custom content"

    @property
    def resolved_custom_content_reference(self) -> Path | None:
        """Return the resolved custom-content reference path, if configured."""

        if self.custom_content_reference is None:
            return None
        if self.custom_content_reference.is_absolute():
            return self.custom_content_reference
        return self.resolved_custom_content_root / self.custom_content_reference

    def module_source_root(self, module: BuildModule) -> Path:
        """Return the target-specific source root for a build module."""

        return self.target_source_roots.get(module.name) or module.source_dirs[-1]

    def target_script_dir(self, module: BuildModule) -> Path:
        """Return the script directory for a target module."""

        return self.module_source_root(module) / "scripts"

    def target_include_dirs(self) -> list[Path]:
        """Return shared include directories used by target script compilation."""

        return [self.shared_root / "scripts"]


@dataclass(slots=True, frozen=True)
class BuilderRuntime:
    """Bundle original settings with resolved build config and runtime filesystem paths."""

    settings: "BuilderSettings"
    config: BuildConfig
    paths: RuntimePaths


@dataclass(slots=True)
class RuntimeResolver:
    """Resolve builder settings into project modules and runtime paths."""

    settings: "BuilderSettings"

    def resolve(self) -> BuilderRuntime:
        """Resolve settings into runtime paths and module build configuration."""

        project_root = self.settings.project_root.resolve()
        env_values = load_project_env_values(project_root)
        build_target = self.build_target(env_values)
        # AREDev derives module archive names from the active target. Keeping that
        # convention here prevents CLI, scaffold, and runtime paths from drifting.
        module_name = f"are-dev-{build_target}"
        paths = RuntimePathResolver(
            settings=self.settings,
            project_root=project_root,
            env_values=env_values,
            build_target=build_target,
            module_name=module_name,
        ).resolve()
        return BuilderRuntime(
            settings=self.settings,
            config=self.build_config(
                build_target=build_target,
                module_name=module_name,
                paths=paths,
            ),
            paths=paths,
        )

    def build_target(self, env_values: dict[str, str]) -> str:
        """Resolve and validate the active build target from CLI, env, or default config."""

        target = self.settings.build_target or env_values.get("BUILD_TARGET") or "pgcc"
        validate_build_target(target)
        return target

    def build_config(
        self,
        *,
        build_target: str,
        module_name: str,
        paths: RuntimePaths,
    ) -> BuildConfig:
        """Construct the single-target BuildConfig from resolved runtime paths."""

        target_source_root = paths.builder_root / f"{build_target}-resources"
        module = BuildModule(
            name=module_name,
            source_dirs=[paths.shared_root / "gff", target_source_root],
            build_dir=paths.compiled_root / module_name,
            target_path=paths.modules_dir / f"{module_name}.mod",
            precompiled_dirs=[paths.builder_root / "precompiled"],
        )
        return BuildConfig(
            talktable_path=_talktable_path(paths),
            modules={module_name: module},
        )


@dataclass(slots=True)
class RuntimePathResolver:
    """Build the filesystem layout used by one project invocation."""

    settings: "BuilderSettings"
    project_root: Path
    env_values: dict[str, str]
    build_target: str
    module_name: str

    def resolve(self) -> RuntimePaths:
        """Resolve settings into runtime paths and module build configuration."""

        builder_root = self.project_root
        server_root = self.settings.server_root or builder_root / "server"
        hak_dir = self.settings.hak_dir or server_root / "hak"
        tlk_dir = self.settings.tlk_dir or server_root / "tlk"
        target_source_root = builder_root / f"{self.build_target}-resources"
        return RuntimePaths(
            builder_root=builder_root,
            builder_mount_root=self.settings.builder_mount_root
            or builder_root.as_posix(),
            shared_root=builder_root / "are-resources",
            compiled_root=builder_root / "compiled-resources",
            server_root=server_root,
            hak_dir=hak_dir,
            tlk_dir=tlk_dir,
            state_file=(
                self.settings.state_file
                or builder_root / RuntimePaths.default_state_file
            ),
            nwn_root=self.nwn_root(),
            compile_script_dir=self.settings.script_dir,
            compile_output_dir=self.settings.output_dir,
            compile_workers=(
                self.settings.compile_workers
                if self.settings.compile_workers is not None
                else -1
            ),
            compile_live=self.settings.compile_live,
            custom_content_reference=self.settings.custom_content_reference,
            target_source_roots={self.module_name: target_source_root},
        )

    def nwn_root(self) -> Path | None:
        """Resolve the NWN install root from CLI, project config, or defaults."""

        if self.settings.nwn_root is not None:
            return self.settings.nwn_root
        if nwn_root := resolve_nwn_install_root(
            self.env_values.get("NWN_INSTALL_PATH", "")
        ):
            return nwn_root
        # In Docker, /nwn/install is the mounted install root. Returning it as the
        # fallback keeps containerized builds usable with minimal configuration.
        return RuntimePaths.default_nwn_root


class BuilderSettings(BaseSettings):
    """Runtime settings loaded from CLI overrides, env vars, and ``.env`` files."""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    project_root: Path = Field(
        default=Path("."),
        validation_alias=AliasChoices("AREDEV_ROOT", "project_root"),
    )
    build_target: str | None = None
    builder_mount_root: str | None = None
    server_root: Path | None = None
    hak_dir: Path | None = None
    tlk_dir: Path | None = None
    state_file: Path | None = None
    script_dir: Path | None = None
    output_dir: Path | None = None
    nwn_root: Path | None = None
    compile_workers: int | None = None
    compile_live: bool = False
    custom_content_reference: Path | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Customize settings sources while keeping live compile CLI-only."""

        def filtered_env_settings():
            """Return environment settings with live-compile removed."""

            values = env_settings()
            # Live compile writes into the running server development directory,
            # so it must come from an explicit CLI flag rather than environment.
            values.pop("compile_live", None)
            return values

        def filtered_dotenv_settings():
            """Return dotenv settings with live-compile removed."""

            values = dotenv_settings()
            # Match direct environment handling: .env can configure paths but not
            # enable live writes as a hidden side effect.
            values.pop("compile_live", None)
            return values

        return (
            init_settings,
            filtered_env_settings,
            filtered_dotenv_settings,
            file_secret_settings,
        )

    @field_validator(
        "custom_content_reference",
        "hak_dir",
        "nwn_root",
        "output_dir",
        "script_dir",
        "server_root",
        "state_file",
        "tlk_dir",
        mode="before",
    )
    @classmethod
    def _expand_optional_path(cls, value: object) -> object:
        """Expand an optional path string, returning ``None`` for blanks."""

        if value in (None, ""):
            return None
        return expand_path(str(value))

    @field_validator("project_root", mode="before")
    @classmethod
    def _expand_required_path(cls, value: object) -> object:
        """Expand a required path value, defaulting blanks to the current directory."""

        if value in (None, ""):
            return Path(".")
        return expand_path(str(value))


def load_project_env_values(project_root: Path) -> dict[str, str]:
    """Load ``config/arebuilder.env`` values if an AREDev project has one."""

    env_path = project_root / "config" / AREBUILDER_ENV_FILENAME
    if not env_path.exists():
        return {}
    return parse_key_value_text(env_path.read_text(encoding="utf-8"))


def _talktable_path(paths: RuntimePaths) -> Path:
    """Resolve the talk table path from the project copy or NWN install."""

    project_talktable = paths.builder_root / "data" / "dialog.tlk"
    if project_talktable.exists():
        return project_talktable
    if paths.nwn_root is not None:
        # NWN:EE keeps the base English dialog.tlk under this language/data path
        # in both local installs and the mounted Docker install root.
        return paths.nwn_root / "lang" / "en" / "data" / "dialog.tlk"
    return project_talktable
