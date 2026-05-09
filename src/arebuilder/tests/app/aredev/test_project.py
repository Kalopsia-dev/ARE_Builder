from pathlib import Path

from arebuilder.app.aredev.project import (
    BuilderConfig,
    BuilderConfigError,
    DEFAULT_AREBUILDER_REPO,
    ProjectLayout,
    build_project_builder_settings,
    infer_project_paths,
    load_arebuilder_env,
    render_arebuilder_env,
)


def test_load_arebuilder_env_parses_defaults_and_overrides(tmp_path: Path) -> None:
    """Verify project config loading merges defaults with explicit env-file overrides."""

    project = tmp_path / "AREDev"
    install_root = tmp_path / "nwn-install"
    config_dir = project / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "arebuilder.env").write_text(
        f"""
        # comments are ignored
        BUILD_TARGET=demo
        BUILDER_BACKEND=docker
        AREBUILDER_REPO=https://example.invalid/ARE_Builder.git
        NWSERVER_IMAGE=custom/nwserver:dev
        NWN_INSTALL_PATH="{install_root}"
        """,
        encoding="utf-8",
    )

    config = load_arebuilder_env(project)

    assert config.build_target == "demo"
    assert config.module_name == "are-dev-demo"
    assert config.builder_backend == "docker"
    assert config.arebuilder_repo == "https://example.invalid/ARE_Builder.git"
    assert config.nwserver_image == "custom/nwserver:dev"
    assert config.nwn_install_root == install_root


def test_render_arebuilder_env_populates_inferred_nwn_install_path(monkeypatch) -> None:
    """Verify generated env files include inferred NWN install paths when available."""

    monkeypatch.setattr(
        "arebuilder.app.aredev.project.resolve_nwn_install_root",
        lambda **_kwargs: Path("/synthetic/nwn-install"),
    )
    monkeypatch.setattr(
        "arebuilder.app.aredev.project.resolve_nwn_home_root",
        lambda **_kwargs: Path("/synthetic/nwn-home"),
    )

    arebuilder_env = render_arebuilder_env()

    assert f"AREBUILDER_REPO={DEFAULT_AREBUILDER_REPO}\n" in arebuilder_env
    assert "NWN_INSTALL_PATH=/synthetic/nwn-install\n" in arebuilder_env
    assert "NWN_HOME_PATH=/synthetic/nwn-home\n" in arebuilder_env


def test_infer_project_paths_warns_when_nwn_paths_are_unknown(monkeypatch) -> None:
    """Verify that infer project paths warns when NWN paths are unknown."""

    monkeypatch.setattr(
        "arebuilder.app.aredev.project.resolve_nwn_install_root",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "arebuilder.app.aredev.project.resolve_nwn_home_root",
        lambda **_kwargs: None,
    )

    inferred = infer_project_paths()

    assert inferred.nwn_install_path == ""
    assert inferred.nwn_home_path == ""
    assert any("NWN_INSTALL_PATH" in warning for warning in inferred.warnings)
    assert any("NWN_HOME_PATH" in warning for warning in inferred.warnings)
    assert any(
        "data/" in warning and "bin/" in warning for warning in inferred.warnings
    )
    assert any(
        "Documents/Neverwinter Nights" in warning
        and "hak/" in warning
        and "tlk/" in warning
        for warning in inferred.warnings
    )


def test_load_arebuilder_env_rejects_invalid_backend(tmp_path: Path) -> None:
    """Verify project config loading rejects unsupported builder backends."""

    project = tmp_path / "AREDev"
    config_dir = project / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "arebuilder.env").write_text(
        "BUILDER_BACKEND=other\n", encoding="utf-8"
    )

    try:
        load_arebuilder_env(project)
    except BuilderConfigError as exc:
        assert "BUILDER_BACKEND" in str(exc)
    else:
        raise AssertionError("Expected invalid backend to fail.")


def test_project_builder_settings_use_project_layout_paths(tmp_path: Path) -> None:
    """Verify AREDev builder settings are derived from the resolved project layout."""

    layout = ProjectLayout.from_root(tmp_path / "AREDev")
    nwn_install = tmp_path / "NWN"
    nwn_home = tmp_path / "Home"

    settings = build_project_builder_settings(
        layout=layout,
        config=BuilderConfig(
            build_target="demo",
            nwn_install_path=str(nwn_install),
            nwn_home_path=str(nwn_home),
        ),
        live=True,
    )

    assert settings.project_root == layout.root
    assert settings.build_target == "demo"
    assert settings.builder_mount_root == "/var/builder"
    assert settings.server_root == layout.server_dir
    assert settings.hak_dir == layout.hak_dir
    assert settings.tlk_dir == layout.tlk_dir
    assert settings.nwn_root == nwn_install
    assert settings.compile_live is True
