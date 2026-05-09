from pathlib import Path

import pytest

from arebuilder.app.aredev.controller import AREDEV_RESTART_EXIT_CODE

from arebuilder.tests.app.aredev.helpers import (
    FAKE_HOST_NWN_HOME_ROOT,
    FAKE_HOST_NWN_INSTALL_ROOT,
    FAKE_HOST_ROOT,
    PLAYER_PASSWORD,
    FakeRunner,
    make_controller,
    patch_request_ids,
    read_host_request,
    write_host_response,
    write_nwn_passwords,
)


def test_containerized_start_requests_compose_on_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify containerized start requests Compose through the host bridge."""

    runner = FakeRunner()
    controller, layout, build_calls, runner = make_controller(tmp_path, runner=runner)
    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    monkeypatch.setenv("AREDEV_HOST_ROOT", FAKE_HOST_ROOT)
    monkeypatch.setenv("AREDEV_CONFIG_ROOT", "/var/builder")
    monkeypatch.setenv("AREDEV_NWN_INSTALL_ROOT", FAKE_HOST_NWN_INSTALL_ROOT)
    monkeypatch.setenv("AREDEV_NWN_HOME_ROOT", FAKE_HOST_NWN_HOME_ROOT)
    monkeypatch.setenv("AREDEV_HOST_LAUNCHER", "1")
    patch_request_ids(monkeypatch, ["status", "compose"])
    write_host_response(layout, "status", status="ok", stdout="false")
    write_host_response(layout, "compose", status="ok")

    assert controller.run("start", []) == 0

    assert build_calls == [("link", ["are-dev-pgcc"], False)]
    assert runner.calls == []
    compose_request = read_host_request(layout, "compose")
    assert compose_request["COMMAND"] == "compose"
    assert compose_request["ACTION"] == "up_nwserver"
    assert compose_request["NWN_MODULE"] == "are-dev-pgcc"


def test_containerized_stop_uses_host_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify containerized stop requests Compose shutdown through the host bridge."""

    output: list[str] = []
    controller, layout, build_calls, runner = make_controller(
        tmp_path,
        output=output.append,
    )
    (layout.development_dir / "live.ncs").write_bytes(b"compiled")
    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    monkeypatch.setenv("AREDEV_HOST_LAUNCHER", "1")
    patch_request_ids(monkeypatch, ["status", "compose"])
    write_host_response(layout, "status", status="ok", stdout="true")
    write_host_response(layout, "compose", status="ok")

    assert controller.run("stop", []) == 0

    compose_request = read_host_request(layout, "compose")
    assert compose_request["COMMAND"] == "compose"
    assert compose_request["ACTION"] == "down"
    assert not (layout.development_dir / "live.ncs").exists()
    assert output == ["Stopping server...", "Cleaning development folder..."]
    assert build_calls == []
    assert runner.calls == []


def test_containerized_nwn_requests_host_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify containerized NWN launch writes a host-launch request."""

    output: list[str] = []
    runner = FakeRunner(running="aredevnwserver\n")
    controller, layout, _, runner = make_controller(
        tmp_path,
        runner=runner,
        output=output.append,
    )
    write_nwn_passwords(layout)
    request_id = "request123"
    patch_request_ids(monkeypatch, ["status", request_id])
    write_host_response(layout, "status", status="ok", stdout="true")
    write_host_response(layout, request_id, status="ok")
    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    monkeypatch.setenv("AREDEV_HOST_LAUNCHER", "1")

    assert controller.run("nwn", []) == 0

    request_values = read_host_request(layout, request_id)
    assert request_values["COMMAND"] == "nwn"
    assert request_values["MODE"] == "player"
    assert request_values["PORT"] == "5121"
    assert request_values["PASSWORD"] == PLAYER_PASSWORD
    assert output == []
    assert runner.calls == []


def test_containerized_nwn_requires_host_launcher_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify containerized NWN launch reports a clear error without the host bridge."""

    output: list[str] = []
    runner = FakeRunner(running="aredevnwserver\n")
    controller, _, build_calls, runner = make_controller(
        tmp_path,
        runner=runner,
        output=output.append,
    )
    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    monkeypatch.delenv("AREDEV_HOST_LAUNCHER", raising=False)

    assert controller.run("nwn", []) == 1

    assert build_calls == []
    assert runner.calls == []
    assert any("host launcher bridge" in message for message in output)


def test_containerized_update_requests_host_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify containerized update requests a host restart."""

    output: list[str] = []
    controller, layout, build_calls, runner = make_controller(
        tmp_path,
        output=output.append,
    )
    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    monkeypatch.setenv("AREDEV_HOST_LAUNCHER", "1")
    patch_request_ids(monkeypatch, ["update"])
    write_host_response(layout, "update", status="ok")

    assert controller.run("update", []) == AREDEV_RESTART_EXIT_CODE

    request_values = read_host_request(layout, "update")
    assert request_values["COMMAND"] == "update_restart"
    assert build_calls == []
    assert runner.calls == []
    assert output == ["Updating containers. AREDev will restart after the update."]


def test_containerized_database_drop_uses_host_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify containerized database drop is delegated through the host bridge."""

    output: list[str] = []
    controller, layout, build_calls, runner = make_controller(
        tmp_path,
        output=output.append,
    )
    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    monkeypatch.setenv("AREDEV_HOST_LAUNCHER", "1")
    patch_request_ids(monkeypatch, ["status", "drop"])
    write_host_response(layout, "status", status="ok", stdout="false")
    write_host_response(layout, "drop", status="ok", stdout="Database dropped.")

    assert controller.run("database", ["drop"]) == 0

    request_values = read_host_request(layout, "drop")
    assert request_values["COMMAND"] == "volume_drop"
    assert output == ["Database dropped."]
    assert build_calls == []
    assert runner.calls == []
