import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from arebuilder.app.aredev.scaffold import (
    ScaffoldConflictError,
    initialize_aredev_project,
)
from arebuilder.app.cli import main


class _ExitPromptSession:
    """Prompt-session test double that immediately asks the menu to exit."""

    def prompt(self, _prompt: str) -> str:
        """Simulate one interactive prompt read for controller tests."""

        return "exit"


def _pretend_host_platform(monkeypatch, platform: str) -> None:
    """Run scaffold init as though it were executing on one host platform."""

    monkeypatch.setattr("arebuilder.app.aredev.scaffold.sys.platform", platform)


def _assert_uses_crlf_line_endings(data: bytes) -> None:
    """Assert that Windows command scripts use CRLF without stray LF separators."""

    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")


def _env_values(text: str) -> dict[str, str]:
    """Parse simple KEY=VALUE env-file lines into a dictionary."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        values[key] = value
    return values


def _bind_blocks_for_target(compose_text: str, target: str) -> list[str]:
    """Extract bind-mount blocks for one service target from Compose YAML text."""

    blocks: list[list[str]] = []
    current_block: list[str] = []

    for line in compose_text.splitlines():
        if line == "      - type: bind":
            if current_block:
                blocks.append(current_block)
            current_block = [line]
            continue
        if current_block and line.startswith("        "):
            current_block.append(line)
            continue
        if current_block:
            blocks.append(current_block)
            current_block = []

    if current_block:
        blocks.append(current_block)

    return [
        "\n".join(block)
        for block in blocks
        if any(line.strip() == f"target: {target}" for line in block)
    ]


def _assert_compose_uses_minimal_builder_mounts(compose_text: str) -> None:
    """Assert generated Compose text exposes the intended builder and NWN mounts."""

    targets = [
        line.strip().removeprefix("target: ")
        for line in compose_text.splitlines()
        if line.strip().startswith("target: ")
    ]

    builder_root_blocks = _bind_blocks_for_target(compose_text, "/var/builder")
    assert len(builder_root_blocks) == 2
    assert all(
        "source: ${AREDEV_HOST_ROOT:-.}" in block for block in builder_root_blocks
    )
    assert any("read_only: true" in block for block in builder_root_blocks)
    assert any("read_only: true" not in block for block in builder_root_blocks)

    assert "/nwn/install" in targets
    assert "/nwn/home/hak" in targets
    assert "/nwn/home/tlk" in targets
    assert "source: ${AREDEV_NWN_INSTALL_ROOT:-${NWN_INSTALL_PATH}}" in compose_text
    assert (
        "AREDEV_NWN_INSTALL_ROOT: ${AREDEV_NWN_INSTALL_ROOT:-${NWN_INSTALL_PATH}}"
        in compose_text
    )
    assert (
        "AREDEV_NWN_HOME_ROOT: ${AREDEV_NWN_HOME_ROOT:-${NWN_HOME_PATH:-./server}}"
        in compose_text
    )
    assert (
        "source: ${AREDEV_NWN_HOME_ROOT:-${NWN_HOME_PATH:-./server}}/hak"
        in compose_text
    )
    assert "NWN_ROOT: /nwn/install" in compose_text
    assert "HAK_DIR: /nwn/home/hak" in compose_text
    assert "TLK_DIR: /nwn/home/tlk" in compose_text


def test_init_creates_expected_scaffold_without_resource_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify that init creates expected scaffold without resource artifacts."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"

    result = initialize_aredev_project(project)

    assert result.root == project.resolve()
    assert project / "AREDev.sh" in result.created_files
    assert project / "AREDev.bat" not in result.created_files
    for relative_path in [
        "AREDev.sh",
        "docker-compose.yml",
        "config/arebuilder.env",
        "config/db.env",
        "config/nwserver.env",
        "data/bin/aredev-host-launcher.sh",
        "data/help.txt",
        "data/logo.txt",
        "data/timeinit.sql",
    ]:
        assert (project / relative_path).is_file()
    assert not (project / "AREDev.bat").exists()
    assert not (project / "data" / "bin" / "aredev-host-launcher.ps1").exists()

    for relative_path in [
        "temp",
        "compiled-resources",
        "logs",
        "server/development",
        "server/hak",
        "server/localvault",
        "server/modules",
        "server/override",
        "server/servervault",
        "server/tlk",
    ]:
        assert (project / relative_path).is_dir()

    arebuilder_env = _env_values(
        (project / "config" / "arebuilder.env").read_text(encoding="utf-8")
    )
    compose_text = (project / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile_text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.14-alpine" in dockerfile_text
    assert "libnwn-musl-compat.so" in dockerfile_text
    assert "LD_PRELOAD=/usr/local/lib/libnwn-musl-compat.so" in dockerfile_text
    assert arebuilder_env["BUILD_TARGET"] == "pgcc"
    assert arebuilder_env["BUILDER_BACKEND"] == "native"
    assert arebuilder_env["NWSERVER_IMAGE"] == "dmhoodoo/aredevnwnxserver:latest"
    env_template = resources.files("arebuilder").joinpath(
        "templates", "aredev", "config", "arebuilder.env"
    )
    assert env_template.is_file()
    assert "working_dir: /var/builder" in compose_text
    assert "AREDEV_IN_CONTAINER" in compose_text
    assert "AREDEV_HOST_ROOT" in compose_text
    assert "AREDEV_CONFIG_ROOT" in compose_text
    assert "AREDEV_NWN_INSTALL_ROOT" in compose_text
    assert "AREDEV_HOST_LAUNCHER" in compose_text
    _assert_compose_uses_minimal_builder_mounts(compose_text)

    assert not (project / "are-resources").exists()
    assert not (project / "pgcc-resources").exists()
    assert list((project / "server" / "hak").iterdir()) == []
    assert list((project / "server" / "tlk").iterdir()) == []
    shell_wrapper = (project / "AREDev.sh").read_text(encoding="utf-8")
    assert "scaffold_root=" in shell_wrapper
    assert "aredev-host-launcher.sh" in shell_wrapper
    assert 'exec "$launcher" run "$scaffold_root"' in shell_wrapper
    assert "AREDEV_ROOT=${AREDEV_ROOT:-$scaffold_root}" in shell_wrapper
    assert 'exec aredev --root "$AREDEV_ROOT"' in shell_wrapper
    shell_launcher = (project / "data" / "bin" / "aredev-host-launcher.sh").read_text(
        encoding="utf-8"
    )
    for command in [
        "container_status",
        "compose",
        "down_quiet",
        "pull_ignore_failures",
        "volume_drop",
        "update_restart",
    ]:
        assert command in shell_launcher
    assert "run_docker_session" in shell_launcher
    assert "prepare_docker_session" in shell_launcher
    assert "resolve_docker_runtime_paths" in shell_launcher
    assert "run_bridge_loop" in shell_launcher
    assert "run_update_session" in shell_launcher
    assert "aredev-host-launcher.sh prepare <project-root>" in shell_launcher
    assert 'prepare) prepare_docker_session "$@"' in shell_launcher
    assert 'update) run_update_session "$@"' in shell_launcher
    assert '"$launcher" bridge "$AREDEV_ROOT" &' in shell_launcher
    assert "--ignore-pull-failures" in shell_launcher
    assert "docker compose --progress quiet" in shell_launcher
    assert 'client_dir=$(dirname "$client")' in shell_launcher
    assert 'cd "$client_dir"' in shell_launcher
    assert os.access(project / "AREDev.sh", os.X_OK)
    assert os.access(
        project / "data" / "bin" / "aredev-host-launcher.sh",
        os.X_OK,
    )


def test_init_generates_windows_batch_wrapper_without_shell_wrapper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify that Windows init generates the batch wrapper instead of the shell wrapper."""

    _pretend_host_platform(monkeypatch, "win32")
    project = tmp_path / "AREDev"

    result = initialize_aredev_project(project)

    assert project / "AREDev.bat" in result.created_files
    assert project / "data" / "bin" / "aredev-host-launcher.ps1" in result.created_files
    assert project / "AREDev.sh" not in result.created_files
    assert (project / "AREDev.bat").is_file()
    assert (project / "data" / "bin" / "aredev-host-launcher.ps1").is_file()
    assert not (project / "AREDev.sh").exists()
    assert not (project / "data" / "bin" / "aredev-host-launcher.sh").exists()

    _assert_uses_crlf_line_endings((project / "AREDev.bat").read_bytes())
    batch_wrapper = (project / "AREDev.bat").read_text(encoding="utf-8")
    powershell_launcher = (
        project / "data" / "bin" / "aredev-host-launcher.ps1"
    ).read_text(encoding="utf-8")
    assert "aredev-host-launcher.ps1" in batch_wrapper
    assert r"%AREDEV_ROOT%\data\bin\aredev-host-launcher.ps1" in batch_wrapper
    assert '-Mode prepare -ProjectRoot "%AREDEV_ROOT%"' in batch_wrapper
    assert '-Mode bridge -ProjectRoot "%AREDEV_ROOT%"' in batch_wrapper
    assert '-Mode update -ProjectRoot "%AREDEV_ROOT%"' in batch_wrapper
    assert "docker compose --progress quiet" in batch_wrapper
    assert "run --rm" in batch_wrapper
    assert "WHERE arebuilder" in batch_wrapper
    assert 'arebuilder aredev --root "%AREDEV_ROOT%"' in batch_wrapper
    assert "py -m arebuilder aredev" in batch_wrapper
    assert "powershell.exe" in batch_wrapper
    assert "%%~B" in batch_wrapper
    assert "PAUSE" in batch_wrapper
    assert "GOTO EXIT_AREDEV" in batch_wrapper
    for command in [
        "container_status",
        "compose",
        "down_quiet",
        "pull_ignore_failures",
        "volume_drop",
        "update_restart",
    ]:
        assert command in powershell_launcher
    assert "Invoke-Prepare" in powershell_launcher
    assert "Invoke-BridgeLoop" in powershell_launcher
    assert "Invoke-Update" in powershell_launcher
    assert 'ValidateSet("prepare", "bridge", "update")' in powershell_launcher
    assert "--ignore-pull-failures" in powershell_launcher
    assert "docker compose --progress quiet" in powershell_launcher
    assert "-WorkingDirectory (Split-Path -Parent $client)" in powershell_launcher


def test_init_generates_posix_shell_launcher_on_macos(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify non-Windows hosts generate only the POSIX launcher helpers."""

    _pretend_host_platform(monkeypatch, "darwin")
    project = tmp_path / "AREDev"

    result = initialize_aredev_project(project)

    assert project / "AREDev.sh" in result.created_files
    assert project / "data" / "bin" / "aredev-host-launcher.sh" in result.created_files
    assert (project / "AREDev.sh").is_file()
    assert (project / "data" / "bin" / "aredev-host-launcher.sh").is_file()
    assert not (project / "AREDev.bat").exists()
    assert not (project / "data" / "bin" / "aredev-host-launcher.ps1").exists()
    assert os.access(project / "AREDev.sh", os.X_OK)
    assert os.access(project / "data" / "bin" / "aredev-host-launcher.sh", os.X_OK)


def test_init_is_idempotent_and_preserves_identical_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify scaffold init can be rerun without rewriting identical existing files."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"
    initialize_aredev_project(project)

    result = initialize_aredev_project(project)

    assert project / "AREDev.sh" in result.preserved_files
    assert project / "config" / "arebuilder.env" in result.preserved_files
    assert result.overwritten_files == []


def test_init_refuses_conflicting_files_unless_forced(tmp_path: Path) -> None:
    """Verify scaffold init protects conflicting files unless force mode is enabled."""

    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    arebuilder_env = project / "config" / "arebuilder.env"
    arebuilder_env.write_text("changed=true\n", encoding="utf-8")

    try:
        initialize_aredev_project(project)
    except ScaffoldConflictError as exc:
        assert "arebuilder.env" in str(exc)
    else:
        raise AssertionError("Expected init to reject a conflicting scaffold file.")

    result = initialize_aredev_project(project, force=True)

    assert arebuilder_env in result.overwritten_files
    assert "BUILD_TARGET=pgcc\n" in arebuilder_env.read_text(encoding="utf-8")


def test_init_cli_and_generated_templates_smoke(tmp_path: Path, monkeypatch) -> None:
    """Verify the init CLI writes templates that can be loaded by project config code."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"

    assert main(["init", str(project), "--backend", "docker", "--target", "demo"]) == 0
    arebuilder_env = _env_values(
        (project / "config" / "arebuilder.env").read_text(encoding="utf-8")
    )
    assert arebuilder_env["BUILDER_BACKEND"] == "docker"
    assert arebuilder_env["BUILD_TARGET"] == "demo"
    compose_text = (project / "docker-compose.yml").read_text(encoding="utf-8")
    shell_wrapper = (project / "AREDev.sh").read_text(encoding="utf-8")
    shell_launcher = (project / "data" / "bin" / "aredev-host-launcher.sh").read_text(
        encoding="utf-8"
    )
    _assert_compose_uses_minimal_builder_mounts(compose_text)
    assert "scaffold_root=" in shell_wrapper
    assert 'exec "$launcher" run "$scaffold_root"' in shell_wrapper
    assert "AREDEV_ROOT=${AREDEV_ROOT:-$scaffold_root}" in shell_wrapper
    assert 'exec aredev --root "$AREDEV_ROOT"' in shell_wrapper
    assert "AREDEV_CONFIG_ROOT=/var/builder" in shell_launcher
    assert "AREDEV_HOST_LAUNCHER=1" in shell_launcher
    assert '--project-directory "$AREDEV_ROOT"' in shell_launcher
    assert "prepare_docker_session" in shell_launcher
    assert "run_host_update" in shell_launcher
    assert "run_update_session" in shell_launcher
    assert "update failed; resuming prompt" in shell_launcher

    shell = shutil.which("sh")
    if shell:
        completed = subprocess.run(
            [shell, "-n", str(project / "AREDev.sh")],
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_init_help_distinguishes_project_dir_from_build_target(capsys) -> None:
    """Verify init help uses unambiguous names for the positional and build target."""

    with pytest.raises(SystemExit) as exc_info:
        main(["init", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "usage: arebuilder init" in output
    assert "TARGET_DIR" in output
    assert "--target BUILD_TARGET" in output
    assert "AREDev project directory" in output


def test_aredev_cli_without_command_opens_menu_until_exit(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    """Verify that aredev CLI without command opens menu until exit."""

    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    monkeypatch.setattr(
        "arebuilder.app.aredev.controller._create_prompt_session",
        lambda _layout, **_kwargs: _ExitPromptSession(),
    )

    assert main(["aredev", "--root", str(project)]) == 0

    captured = capsys.readouterr()
    assert "AREDev" in captured.out
    assert "Usage:" in captured.out


def test_aredev_cli_defaults_root_from_project_root_env(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    """Verify direct aredev CLI launch honors AREDEV_ROOT when --root is omitted."""

    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    monkeypatch.setenv("AREDEV_ROOT", str(project))
    monkeypatch.setattr(
        "arebuilder.app.aredev.controller._create_prompt_session",
        lambda _layout, **_kwargs: _ExitPromptSession(),
    )

    assert main(["aredev"]) == 0

    captured = capsys.readouterr()
    assert "AREDev" in captured.out
    assert "Usage:" in captured.out
