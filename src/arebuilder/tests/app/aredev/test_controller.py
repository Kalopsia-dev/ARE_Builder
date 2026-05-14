import sys
from pathlib import Path

import pytest

from arebuilder.app.aredev import AREDevController
from arebuilder.app.aredev.project import ProjectLayout, load_arebuilder_env
from arebuilder.app.aredev.scaffold import initialize_aredev_project

from arebuilder.tests.app.aredev.helpers import (
    FAKE_HOST_NWN_HOME_ROOT,
    FAKE_HOST_ROOT,
    FakeRunner,
    create_resource_dirs,
    make_controller,
    write_nwn_passwords,
)


def test_build_runs_native_builder(tmp_path: Path) -> None:
    """Verify the build command invokes the native builder in native mode."""

    controller, _, build_calls, runner = make_controller(tmp_path)

    assert controller.run("build", []) == 0

    assert build_calls == [("all", ["are-dev-pgcc"], False)]
    assert runner.calls == [["docker", "ps", "--format", "{{.Names}}"]]


def test_build_refuses_while_server_is_running(tmp_path: Path) -> None:
    """Verify the build command does not run while NWServer is active."""

    output: list[str] = []
    runner = FakeRunner(running="aredevnwserver\n")
    controller, layout, build_calls, runner = make_controller(
        tmp_path,
        runner=runner,
        output=output.append,
        nwn_home=tmp_path / "nwn-home",
    )

    assert controller.run("build", []) == 1

    assert output == ["Server is running."]
    assert build_calls == []
    assert runner.calls == [["docker", "ps", "--format", "{{.Names}}"]]
    assert not layout.hak_dir.is_symlink()
    assert not layout.tlk_dir.is_symlink()


@pytest.mark.parametrize(
    ("command", "args", "running", "expected_build_calls", "expected_runner"),
    [
        (
            "compile",
            ["nw_s0_sleep"],
            "aredevnwserver\n",
            [("compile", ["nw_s0_sleep"], True)],
            None,
        ),
        (
            "database",
            ["drop"],
            "",
            [],
            ["docker", "volume", "rm", "aredev_data", "aredev_database"],
        ),
    ],
    ids=["compile", "database-drop"],
)
def test_aredev_workflow_commands_dispatch_to_expected_backend(
    tmp_path: Path,
    command: str,
    args: list[str],
    running: str,
    expected_build_calls: list[tuple[str, list[str], bool]],
    expected_runner: list[str] | None,
) -> None:
    """Verify representative AREDev workflow commands dispatch correctly."""

    runner = FakeRunner(running=running)
    controller, _, build_calls, runner = make_controller(tmp_path, runner=runner)

    assert controller.run(command, args) == 0
    assert build_calls == expected_build_calls
    if expected_runner is not None:
        assert expected_runner in runner.calls


def test_start_links_then_starts_server_with_module_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify start links resources before launching the configured module."""

    runner = FakeRunner()
    nwn_home = tmp_path / "nwn-home"
    controller, layout, build_calls, _ = make_controller(
        tmp_path,
        runner=runner,
        nwn_home=nwn_home,
    )
    nwnplayer = layout.are_resources_dir / "config" / "nwnplayer.ini"
    nwnplayer.parent.mkdir(parents=True)
    nwnplayer.write_text("[Profile]\n", encoding="utf-8")

    def reject_copystat(*_args, **_kwargs) -> None:
        raise PermissionError("metadata denied")

    monkeypatch.setattr(
        "arebuilder.app.aredev.controller.shutil.copystat",
        reject_copystat,
    )

    assert controller.run("start", []) == 0

    assert build_calls == [("link", ["are-dev-pgcc"], False)]
    assert (layout.server_dir / "nwnplayer.ini").read_text(encoding="utf-8") == (
        "[Profile]\n"
    )
    assert [
        "docker",
        "compose",
        "--progress",
        "quiet",
        "-p",
        "aredev",
        "up",
        "-d",
        "nwserver",
    ] in runner.calls
    assert runner.envs[-1]["NWN_MODULE"] == "are-dev-pgcc"
    assert runner.envs[-1]["NWN_HOME_PATH"] == str(nwn_home)


def test_docker_backend_runs_builder_through_compose(tmp_path: Path) -> None:
    """Verify Docker backend dispatches builder commands through Compose."""

    project = tmp_path / "AREDev"
    initialize_aredev_project(project, backend="docker")
    layout = ProjectLayout.from_root(project)
    nwn_home = tmp_path / "nwn-home"
    (nwn_home / "hak").mkdir(parents=True)
    (nwn_home / "tlk").mkdir(parents=True)
    layout.arebuilder_env_path.write_text(
        layout.arebuilder_env_path.read_text(encoding="utf-8")
        + f"NWN_HOME_PATH={nwn_home}\n",
        encoding="utf-8",
    )
    create_resource_dirs(layout)
    runner = FakeRunner()
    controller = AREDevController(
        layout=layout,
        config=load_arebuilder_env(project),
        process_runner=runner,
        output=lambda _message: None,
    )

    assert controller.run("build", []) == 0

    assert [
        "docker",
        "compose",
        "--progress",
        "quiet",
        "-p",
        "aredev",
        "run",
        "--rm",
        "builder",
        "all",
        "are-dev-pgcc",
    ] in runner.calls


def test_docker_backend_inside_builder_container_reuses_current_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Docker backend avoids recursive Compose calls from inside the builder."""

    project = tmp_path / "AREDev"
    initialize_aredev_project(project, backend="docker")
    layout = ProjectLayout.from_root(project)
    create_resource_dirs(layout)
    runner = FakeRunner()
    captured: dict[str, object] = {}

    def execute_build_command(**kwargs) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    monkeypatch.setenv("AREDEV_HOST_ROOT", FAKE_HOST_ROOT)
    monkeypatch.setenv("AREDEV_NWN_HOME_ROOT", FAKE_HOST_NWN_HOME_ROOT)
    monkeypatch.setattr(
        "arebuilder.app.aredev.controller.execute_build_command",
        execute_build_command,
    )
    controller = AREDevController(
        layout=layout,
        config=load_arebuilder_env(project),
        process_runner=runner,
        output=lambda _message: None,
    )

    assert controller.run("build", []) == 0

    assert runner.calls == []
    assert captured["command"] == "all"
    assert captured["target_name"] == "are-dev-pgcc"
    settings = captured["settings"]
    assert settings.project_root == layout.root
    assert settings.server_root == layout.server_dir


def test_native_update_installs_repo_then_pulls_compose_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify native update refreshes arebuilder before pulling Compose images."""

    custom_repo = "https://example.invalid/ARE_Builder.git"
    runner = FakeRunner()
    controller, layout, build_calls, runner = make_controller(tmp_path, runner=runner)
    layout.arebuilder_env_path.write_text(
        layout.arebuilder_env_path.read_text(encoding="utf-8")
        + f"AREBUILDER_REPO={custom_repo}\n",
        encoding="utf-8",
    )
    controller.config = load_arebuilder_env(layout.root)
    fingerprints = iter(["same", "same"])
    monkeypatch.setattr(
        "arebuilder.app.aredev.controller._installed_arebuilder_version",
        lambda: next(fingerprints),
    )

    assert controller.run("update", []) == 0

    assert runner.calls[0][-1] == f"git+{custom_repo}"
    assert any(call[-2:] == ["pull", "--ignore-pull-failures"] for call in runner.calls)
    assert build_calls == []


def test_native_update_restart_depends_on_package_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify native update restart is gated by the package fingerprint."""

    class Restarted(Exception):
        pass

    exec_calls: list[tuple[str, list[str]]] = []
    controller, _, _, _ = make_controller(tmp_path)
    controller._running_interactive = True

    def execv(executable: str, args: list[str]) -> None:
        exec_calls.append((executable, args))
        raise Restarted

    monkeypatch.setattr("arebuilder.app.aredev.controller.os.execv", execv)

    assert controller._handle_native_update_restart(False, 0) == 0
    assert exec_calls == []
    assert controller._handle_native_update_restart(True, 7) == 7
    assert exec_calls == []
    with pytest.raises(Restarted):
        controller._handle_native_update_restart(True, 0)
    assert exec_calls[0][0] == sys.executable
    assert exec_calls[0][1][:4] == [sys.executable, "-m", "arebuilder", "aredev"]


def test_interactive_prompt_runs_commands_until_exit(tmp_path: Path) -> None:
    """Verify the interactive prompt dispatches commands until the user exits."""

    commands = iter(["build", "help", "quit"])
    output: list[str] = []
    controller, _, build_calls, _ = make_controller(
        tmp_path,
        output=output.append,
        input_reader=lambda _prompt: next(commands),
    )

    assert controller.run_interactive() == 0

    assert build_calls == [("all", ["are-dev-pgcc"], False)]
    assert any("Usage:" in message for message in output)


def test_build_links_empty_hak_and_tlk_dirs_to_nwn_home(tmp_path: Path) -> None:
    """Verify build links project HAK and TLK dirs to NWN home."""

    controller, layout, build_calls, _ = make_controller(
        tmp_path,
        nwn_home=tmp_path / "nwn-home",
    )

    assert controller.run("build", []) == 0

    assert build_calls == [("all", ["are-dev-pgcc"], False)]
    assert layout.hak_dir.resolve() == tmp_path / "nwn-home" / "hak"
    assert layout.tlk_dir.resolve() == tmp_path / "nwn-home" / "tlk"


def test_build_refuses_to_replace_non_empty_hak_dir(tmp_path: Path) -> None:
    """Verify build protects a non-empty project HAK directory."""

    output: list[str] = []
    controller, layout, build_calls, _ = make_controller(
        tmp_path,
        output=output.append,
        nwn_home=tmp_path / "nwn-home",
    )
    (layout.hak_dir / "existing.hak").write_bytes(b"existing")

    assert controller.run("build", []) == 1

    assert build_calls == []
    assert any("non-empty directory" in message for message in output)


def test_nwn_launches_client_from_detected_install_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify NWN launch uses the detected platform install variant."""

    monkeypatch.setattr("arebuilder.config.nwn_paths.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "arebuilder.config.nwn_paths.platform.machine", lambda: "x86_64"
    )
    runner = FakeRunner(running="aredevnwserver\n")
    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    layout = ProjectLayout.from_root(project)
    write_nwn_passwords(layout)
    client = tmp_path / "nwn-install" / "bin" / "linux-x86" / "nwmain"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"")
    layout.arebuilder_env_path.write_text(
        layout.arebuilder_env_path.read_text(encoding="utf-8")
        + f"NWN_INSTALL_PATH={tmp_path / 'nwn-install'}\n",
        encoding="utf-8",
    )
    controller = AREDevController(
        layout=layout,
        config=load_arebuilder_env(project),
        process_runner=runner,
        output=lambda _message: None,
    )

    assert controller.run("nwn", []) == 0

    assert str(client) in runner.calls[-1]
    assert "+connect" in runner.calls[-1]
    assert "127.0.0.1:5121" in runner.calls[-1]
    assert runner.cwd[-1] == client.parent
    assert runner.detached[-1] is True


@pytest.mark.parametrize(
    ("command", "args", "expected_message"),
    [
        ("compile", ["one", "two"], "compile accepts at most one selector argument."),
        ("database", ["reset"], "Invalid database command."),
        ("nwn", ["player"], "Usage: nwn [dm]"),
    ],
    ids=["compile", "database", "nwn"],
)
def test_aredev_bad_argument_grid(
    tmp_path: Path,
    command: str,
    args: list[str],
    expected_message: str,
) -> None:
    """Verify invalid AREDev argument combinations report expected errors."""

    output: list[str] = []
    controller, _, build_calls, runner = make_controller(tmp_path, output=output.append)

    assert controller.run(command, args) == 1
    assert build_calls == []
    assert runner.calls == []
    assert any(expected_message in message for message in output)
