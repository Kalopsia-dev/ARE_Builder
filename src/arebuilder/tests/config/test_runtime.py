from pathlib import Path

from arebuilder.config import (
    BuilderSettings,
    RuntimePaths,
    RuntimeResolver,
)
from arebuilder.tests.fixtures import create_synthetic_fixture

ENV_NAMES = [
    "BUILD_TARGET",
    "BUILDER_MOUNT_ROOT",
    "COMPILE_WORKERS",
    "CUSTOM_CONTENT_REFERENCE",
    "HAK_DIR",
    "NWN_ROOT",
    "OUTPUT_DIR",
    "AREDEV_ROOT",
    "SCRIPT_DIR",
    "SERVER_ROOT",
    "STATE_FILE",
    "TLK_DIR",
]


def _clear_settings_env(monkeypatch) -> None:
    """Remove builder-related environment variables before settings tests run."""

    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_builder_settings_loads_project_env(monkeypatch, tmp_path: Path) -> None:
    """Verify BuilderSettings combines CLI defaults with project env-file values."""

    _clear_settings_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AREDEV_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv("BUILD_TARGET", "demo")

    settings = BuilderSettings()

    assert settings.project_root == tmp_path / "project"
    assert settings.build_target == "demo"


def test_builder_settings_defaults_to_current_project(monkeypatch) -> None:
    """Verify that builder settings defaults to current project."""

    _clear_settings_env(monkeypatch)

    settings = BuilderSettings()

    assert settings.project_root == Path(".")


def test_builder_settings_loads_dotenv_from_cwd(monkeypatch, tmp_path: Path) -> None:
    """Verify that builder settings loads dotenv from cwd."""

    _clear_settings_env(monkeypatch)
    project_root = tmp_path / "dotenv-project"
    (tmp_path / ".env").write_text(
        f"AREDEV_ROOT={project_root}\nCOMPILE_WORKERS=5\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = BuilderSettings()

    assert settings.project_root == project_root
    assert settings.compile_workers == 5


def test_builder_settings_cli_values_override_environment(
    monkeypatch, tmp_path: Path
) -> None:
    """Verify explicit BuilderSettings values take precedence over environment variables."""

    _clear_settings_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AREDEV_ROOT", str(tmp_path / "env-root"))

    settings = BuilderSettings(project_root=tmp_path / "cli-root")

    assert settings.project_root == tmp_path / "cli-root"


def test_builder_settings_coerces_int(monkeypatch, tmp_path: Path) -> None:
    """Verify numeric settings from environment values are coerced to integers."""

    _clear_settings_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPILE_WORKERS", "3")

    settings = BuilderSettings()

    assert settings.compile_workers == 3


def test_builder_settings_supports_nwn_root_env(monkeypatch, tmp_path: Path) -> None:
    """Verify legacy NWN root environment variables populate builder settings."""

    _clear_settings_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    nwn_root = tmp_path / "nwn-install"
    monkeypatch.setenv("NWN_ROOT", str(nwn_root))

    settings = BuilderSettings()

    assert settings.nwn_root == nwn_root


def test_builder_settings_supports_state_file_env(monkeypatch, tmp_path: Path) -> None:
    """Verify the state file environment setting populates builder settings."""

    _clear_settings_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    state_file = tmp_path / "state" / "custom-index.json"
    monkeypatch.setenv("STATE_FILE", str(state_file))

    settings = BuilderSettings()

    assert settings.state_file == state_file


def test_builder_runtime_resolves_project_layout(tmp_path: Path, monkeypatch) -> None:
    """Verify that builder runtime resolves project layout."""

    _clear_settings_env(monkeypatch)
    monkeypatch.setattr(
        "arebuilder.config.runtime.resolve_nwn_install_root", lambda *_args: None
    )
    fixture = create_synthetic_fixture(tmp_path / "fixture")
    settings = BuilderSettings(
        project_root=fixture.root,
        builder_mount_root="/var/builder",
    )

    runtime = RuntimeResolver(settings).resolve()

    assert runtime.config.talktable_path == fixture.talktable_path
    assert list(runtime.config.modules) == [fixture.module_name]
    module = runtime.config.modules[fixture.module_name]
    assert module.source_dirs == [
        fixture.are_resources_dir / "gff",
        fixture.module_resources_dir,
    ]
    assert module.build_dir == fixture.build_dir
    assert module.target_path == fixture.module_archive_path
    assert module.precompiled_dirs == [fixture.precompiled_dir]
    assert runtime.paths.builder_root == fixture.root
    assert runtime.paths.builder_mount_root == "/var/builder"
    assert runtime.paths.shared_root == fixture.root / "are-resources"
    assert runtime.paths.compiled_root == fixture.root / "compiled-resources"
    assert runtime.paths.override_dir == fixture.override_dir
    assert runtime.paths.server_root == fixture.nwn_home_dir
    assert runtime.paths.hak_dir == fixture.hak_dir
    assert runtime.paths.tlk_dir == fixture.tlk_dir
    assert runtime.paths.state_file == fixture.root / "temp" / "script_index.json"
    assert runtime.paths.nwn_root == RuntimePaths.default_nwn_root


def test_builder_runtime_uses_explicit_hak_and_tlk_dirs(
    tmp_path: Path, monkeypatch
) -> None:
    """Verify explicit custom-content output dirs are independent of server root."""

    _clear_settings_env(monkeypatch)
    fixture = create_synthetic_fixture(tmp_path / "fixture")
    hak_dir = tmp_path / "nwn-home" / "hak"
    tlk_dir = tmp_path / "nwn-home" / "tlk"

    runtime = RuntimeResolver(
        BuilderSettings(
            project_root=fixture.root,
            hak_dir=hak_dir,
            tlk_dir=tlk_dir,
        )
    ).resolve()

    assert runtime.paths.server_root == fixture.nwn_home_dir
    assert runtime.paths.hak_dir == hak_dir
    assert runtime.paths.tlk_dir == tlk_dir


def test_builder_runtime_uses_nwn_install_talktable_when_project_copy_is_absent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """
    Verify that builder runtime uses NWN install talktable when project copy is absent.
    """

    _clear_settings_env(monkeypatch)
    fixture = create_synthetic_fixture(tmp_path / "fixture")
    fixture.talktable_path.unlink()
    nwn_root = tmp_path / "nwn-install"
    install_talktable = nwn_root / "lang" / "en" / "data" / "dialog.tlk"
    install_talktable.parent.mkdir(parents=True)
    install_talktable.write_bytes(b"tlk")

    runtime = RuntimeResolver(
        BuilderSettings(project_root=fixture.root, nwn_root=nwn_root)
    ).resolve()

    assert runtime.config.talktable_path == install_talktable


def test_runtime_resolver_keeps_standalone_compile_overrides(
    tmp_path: Path,
) -> None:
    """Verify that build runtime paths keeps standalone compile overrides."""

    fixture = create_synthetic_fixture(tmp_path / "fixture")
    settings = BuilderSettings(
        project_root=fixture.root,
        script_dir=tmp_path / "scripts",
        output_dir=tmp_path / "compiled",
        state_file=tmp_path / "custom-index.json",
    )

    runtime_paths = RuntimeResolver(settings).resolve().paths

    assert runtime_paths.shared_compile_script_dir == tmp_path / "scripts"
    assert runtime_paths.shared_compile_output_dir == tmp_path / "compiled"
    assert runtime_paths.state_file == tmp_path / "custom-index.json"
