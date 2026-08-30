import stat
import sys
from pathlib import Path

import pytest

from arebuilder.app.aredev import AREDevController, ProcessResult
from arebuilder.app.aredev.project import ProjectLayout, load_arebuilder_env
from arebuilder.app.aredev.scaffold import initialize_aredev_project
from arebuilder.tests.app.aredev.helpers import (
    DM_PASSWORD,
    FAKE_HOST_NWN_HOME_ROOT,
    FAKE_HOST_ROOT,
    PLAYER_PASSWORD,
    FakeRunner,
    create_resource_dirs,
    make_controller,
    write_nwn_passwords,
)


def _create_executable(path: Path) -> Path:
    """Create a fake executable file for Docker startup tests."""

    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _append_arebuilder_env(layout: ProjectLayout, *lines: str) -> None:
    """Append config lines to one generated AREDev env file."""

    layout.arebuilder_env_path.write_text(
        layout.arebuilder_env_path.read_text(encoding="utf-8")
        + "".join(f"{line}\n" for line in lines),
        encoding="utf-8",
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


@pytest.mark.parametrize(
    ("command", "running", "expected_output"),
    [
        ("start", "", ["Server started."]),
        (
            "stop",
            "aredevnwserver\n",
            ["Stopping server...", "Cleaning development folder..."],
        ),
    ],
)
def test_start_and_stop_hide_successful_compose_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    running: str,
    expected_output: list[str],
) -> None:
    """Keep successful Compose chatter out of interactive start/stop output."""

    output: list[str] = []
    fake_runner = FakeRunner(running=running)
    controller, _, _, _ = make_controller(tmp_path, output=output.append)

    def noisy_compose_runner(
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        capture_output: bool,
        background: bool,
        detach: bool = False,
    ) -> ProcessResult:
        result = fake_runner(
            args,
            cwd=cwd,
            env=env,
            capture_output=capture_output,
            background=background,
            detach=detach,
        )
        if "compose" not in args:
            return result
        if not capture_output:
            print("successful Compose output")
        return ProcessResult(
            0,
            stdout="successful Compose output\n",
            stderr="successful Compose warning\n",
        )

    controller.process_runner = noisy_compose_runner

    assert controller.run(command, []) == 0

    assert output == expected_output
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("command", "running", "expected_output"),
    [
        ("start", "", ["Compose failed."]),
        ("stop", "aredevnwserver\n", ["Stopping server...", "Compose failed."]),
    ],
)
def test_start_and_stop_report_captured_compose_failures(
    tmp_path: Path,
    command: str,
    running: str,
    expected_output: list[str],
) -> None:
    """Keep genuine Compose failures visible while successful output stays quiet."""

    output: list[str] = []
    fake_runner = FakeRunner(running=running)
    controller, _, _, _ = make_controller(tmp_path, output=output.append)

    def failing_compose_runner(
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None,
        capture_output: bool,
        background: bool,
        detach: bool = False,
    ) -> ProcessResult:
        result = fake_runner(
            args,
            cwd=cwd,
            env=env,
            capture_output=capture_output,
            background=background,
            detach=detach,
        )
        if "compose" in args:
            return ProcessResult(125, stderr="Compose failed.\n")
        return result

    controller.process_runner = failing_compose_runner

    assert controller.run(command, []) == 125

    assert output == expected_output


def test_start_makes_only_database_init_files_container_readable(
    tmp_path: Path,
) -> None:
    """Verify restrictive host umasks do not block MariaDB initialization."""

    controller, layout, _, _ = make_controller(tmp_path)
    schema = layout.are_resources_dir / "db_schema" / "schema_mysql.sql"
    schema.parent.mkdir(parents=True)
    schema.write_text("CREATE TABLE example (id INT);\n", encoding="utf-8")
    timeinit = layout.data_dir / "timeinit.sql"
    unrelated = layout.data_dir / "private.sql"
    unrelated.write_text("private\n", encoding="utf-8")
    for path in (schema, timeinit, unrelated):
        path.chmod(0o600)

    assert controller.run("start", []) == 0

    assert stat.S_IMODE(schema.stat().st_mode) == 0o644
    assert stat.S_IMODE(timeinit.stat().st_mode) == 0o644
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o600


def test_start_does_not_launch_configured_docker_executable_when_ready(
    tmp_path: Path,
) -> None:
    """Verify Docker startup is skipped when the Docker engine is already ready."""

    runner = FakeRunner(docker_info=[0, 0])
    controller, layout, build_calls, runner = make_controller(tmp_path, runner=runner)
    starter = _create_executable(tmp_path / "start-docker")
    _append_arebuilder_env(layout, f"DOCKER_EXECUTABLE={starter}")
    controller.config = load_arebuilder_env(layout.root)

    assert controller.run("start", []) == 0

    assert build_calls == [("link", ["are-dev-pgcc"], False)]
    assert [str(starter)] not in runner.calls
    assert runner.calls[0] == ["docker", "info"]


def test_start_launches_configured_docker_executable_until_ready(
    tmp_path: Path,
) -> None:
    """Verify start launches the configured Docker starter and waits for readiness."""

    output: list[str] = []
    runner = FakeRunner(docker_info=[1, 0, 0])
    controller, layout, build_calls, runner = make_controller(
        tmp_path,
        runner=runner,
        output=output.append,
    )
    starter = _create_executable(tmp_path / "start-docker")
    _append_arebuilder_env(layout, f"DOCKER_EXECUTABLE={starter}")
    controller.config = load_arebuilder_env(layout.root)

    assert controller.run("start", []) == 0

    assert [str(starter)] in runner.calls
    assert runner.detached[runner.calls.index([str(starter)])] is True
    assert "Starting Docker..." in output
    assert build_calls == [("link", ["are-dev-pgcc"], False)]


def test_start_reports_timeout_when_docker_never_becomes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Docker startup timeout stops before build/link work begins."""

    monkeypatch.setattr(
        "arebuilder.app.aredev.controller.DOCKER_START_TIMEOUT",
        0.0,
    )
    output: list[str] = []
    runner = FakeRunner(docker_info=[1])
    controller, layout, build_calls, runner = make_controller(
        tmp_path,
        runner=runner,
        output=output.append,
    )
    starter = _create_executable(tmp_path / "start-docker")
    _append_arebuilder_env(layout, f"DOCKER_EXECUTABLE={starter}")
    controller.config = load_arebuilder_env(layout.root)

    assert controller.run("start", []) == 1

    assert [str(starter)] in runner.calls
    assert build_calls == []
    assert "Unable to start Docker. Please do so manually." in output


def test_start_requires_configured_docker_executable_when_not_ready(
    tmp_path: Path,
) -> None:
    """Verify Docker startup reports a clear message when no starter is configured."""

    output: list[str] = []
    runner = FakeRunner(docker_info=[1])
    controller, _, build_calls, runner = make_controller(
        tmp_path,
        runner=runner,
        output=output.append,
    )
    _append_arebuilder_env(controller.layout, "DOCKER_EXECUTABLE=")
    controller.config = load_arebuilder_env(controller.layout.root)

    assert controller.run("start", []) == 1

    assert build_calls == []
    assert all(call[:2] != ["docker", "compose"] for call in runner.calls)
    assert output == ["Docker is not ready."]


def test_start_reports_when_no_container_cli_is_installed(tmp_path: Path) -> None:
    """Verify runtime commands distinguish missing CLIs from a stopped engine."""

    output: list[str] = []
    controller, _, build_calls, _ = make_controller(tmp_path, output=output.append)

    def missing_container_cli(
        args,
        *,
        cwd,
        env,
        capture_output,
        background,
        detach=False,
    ):
        raise FileNotFoundError(args[0])

    controller.process_runner = missing_container_cli

    assert controller.run("start", []) == 1

    assert build_calls == []
    assert output == ["Neither docker nor podman was found on PATH."]


def test_start_rejects_invalid_docker_executable(tmp_path: Path) -> None:
    """Verify configured Docker starter paths must exist before launch."""

    output: list[str] = []
    runner = FakeRunner(docker_info=[1])
    controller, layout, build_calls, runner = make_controller(
        tmp_path,
        runner=runner,
        output=output.append,
    )
    missing = tmp_path / "missing-start-docker"
    _append_arebuilder_env(layout, f"DOCKER_EXECUTABLE={missing}")
    controller.config = load_arebuilder_env(layout.root)

    assert controller.run("start", []) == 1

    assert [str(missing)] not in runner.calls
    assert build_calls == []
    assert any("does not exist" in line for line in output)


def test_passive_container_status_probe_does_not_start_docker(tmp_path: Path) -> None:
    """Verify prompt-style Docker probes do not launch the configured starter."""

    runner = FakeRunner(docker_info=[1])
    controller, layout, _, runner = make_controller(tmp_path, runner=runner)
    starter = _create_executable(tmp_path / "start-docker")
    _append_arebuilder_env(layout, f"DOCKER_EXECUTABLE={starter}")
    controller.config = load_arebuilder_env(layout.root)

    assert controller._is_server_running() is False

    assert [str(starter)] not in runner.calls
    assert ["docker", "info"] not in runner.calls


def test_native_compile_runs_without_a_container_cli(tmp_path: Path) -> None:
    """Verify native compilation does not require Docker or Podman on PATH."""

    controller, _, build_calls, _ = make_controller(tmp_path)
    calls: list[list[str]] = []

    def missing_container_cli(
        args,
        *,
        cwd,
        env,
        capture_output,
        background,
        detach=False,
    ):
        calls.append(list(args))
        raise FileNotFoundError(args[0])

    controller.process_runner = missing_container_cli

    assert controller.run("compile", []) == 0

    assert build_calls == [("compile", [], False)]
    assert calls == [
        ["docker", "ps", "--format", "{{.Names}}"],
        ["podman", "ps", "--format", "{{.Names}}"],
    ]


def test_native_compile_falls_back_to_podman_when_docker_is_unhealthy(
    tmp_path: Path,
) -> None:
    """Verify an unusable Docker CLI does not hide a working Podman runtime."""

    controller, _, build_calls, _ = make_controller(tmp_path)
    calls: list[list[str]] = []

    def podman_only_runner(
        args,
        *,
        cwd,
        env,
        capture_output,
        background,
        detach=False,
    ):
        calls.append(list(args))
        if args[0] == "docker":
            return ProcessResult(1, stderr="Docker daemon unavailable")
        return ProcessResult(0, stdout="aredevnwserver\n")

    controller.process_runner = podman_only_runner

    assert controller.run("compile", []) == 0

    assert build_calls == [("compile", [], True)]
    assert calls == [
        ["docker", "ps", "--format", "{{.Names}}"],
        ["podman", "ps", "--format", "{{.Names}}"],
    ]


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


@pytest.mark.parametrize("mode", [[], ["dm"]], ids=["player", "dm"])
def test_nwn_launcher_override_preserves_connection_arguments(
    tmp_path: Path,
    mode: list[str],
) -> None:
    """Use the configured launcher without requiring a discoverable NWN binary."""

    controller, layout, _, runner = make_controller(
        tmp_path,
        runner=FakeRunner(running="aredevnwserver\n"),
        nwn_install=tmp_path / "missing-install",
    )
    write_nwn_passwords(layout)
    launcher = _create_executable(layout.root / "custom launcher")
    _append_arebuilder_env(layout, f'NWN_LAUNCHER_PATH="{launcher.name}"')
    controller.config = load_arebuilder_env(layout.root)

    assert controller.run("nwn", mode) == 0

    launch_args = runner.calls[-1]
    assert Path(launch_args[0]).resolve() == launcher
    assert launch_args[1:] == [
        *(["-dmc"] if mode else []),
        "+connect",
        "127.0.0.1:5121",
        "+password",
        DM_PASSWORD if mode else PLAYER_PASSWORD,
    ]
    assert runner.cwd[-1].resolve() == launcher.parent
    assert runner.detached[-1] is True


@pytest.mark.parametrize("invalid_kind", ["missing", "non-executable"])
def test_nwn_rejects_invalid_launcher_without_falling_back(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    """Report a bad override even when the install has a usable default client."""

    if invalid_kind == "non-executable" and sys.platform == "win32":
        pytest.skip("POSIX executable permissions")
    output: list[str] = []
    install = tmp_path / "install"
    (install / "bin" / "linux-x86").mkdir(parents=True)
    _create_executable(install / "bin" / "linux-x86" / "nwmain")
    controller, layout, _, runner = make_controller(
        tmp_path,
        runner=FakeRunner(running="aredevnwserver\n"),
        nwn_install=install,
        output=output.append,
    )
    launcher = layout.root / "bad-launcher"
    if invalid_kind == "non-executable":
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o644)
    _append_arebuilder_env(layout, f'NWN_LAUNCHER_PATH="{launcher}"')
    controller.config = load_arebuilder_env(layout.root)

    assert controller.run("nwn", []) == 1

    assert all(call[0] == "docker" for call in runner.calls)
    assert any("NWN_LAUNCHER_PATH" in message for message in output)
    assert any(str(launcher) in message for message in output)


def test_nwn_launches_client_from_detected_install_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify NWN launch uses the detected platform install variant."""

    monkeypatch.setattr("arebuilder.config.paths.platform.system", lambda: "Linux")
    monkeypatch.setattr("arebuilder.config.paths.platform.machine", lambda: "x86_64")
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
