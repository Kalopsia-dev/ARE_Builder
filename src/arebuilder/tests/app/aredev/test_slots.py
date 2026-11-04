from pathlib import Path

import pytest

from arebuilder.app.aredev import ProcessResult, slots
from arebuilder.tests.app.aredev.helpers import FakeRunner, make_controller


class SlotRunner(FakeRunner):
    """Fake runner that also answers the log and state probes of slot servers."""

    def __init__(self, server_log: str = "", server_running: bool = True):
        super().__init__()
        self.server_log = server_log
        self.server_running = server_running
        self.states = ""

    def __call__(self, args, **kwargs):
        result = super().__call__(args, **kwargs)
        if list(args[:2]) == ["docker", "logs"]:
            return ProcessResult(0, stdout=self.server_log)
        if list(args[:3]) == ["docker", "ps", "-a"]:
            return ProcessResult(0, stdout=self.states)
        if list(args[:2]) == ["docker", "inspect"]:
            return ProcessResult(0, stdout=f"{str(self.server_running).lower()}\n")
        return result


def _make(tmp_path: Path, runner: SlotRunner | None = None):
    output: list[str] = []
    controller, layout, _, runner = make_controller(
        tmp_path, runner=runner or SlotRunner(), output=output.append
    )
    layout.ensure_runtime_dirs()
    layout.module_archive_path(controller.config.module_name).write_bytes(b"module")
    return controller, layout, runner, output


def _compose_calls(runner: FakeRunner, project: str) -> list[list[str]]:
    """Return the Compose arguments that follow the project and file options."""

    calls = []
    for call in runner.calls:
        if call[:2] == ["docker", "compose"] and project in call:
            args = call[call.index(project) + 1 :]
            while args[:1] == ["-f"]:
                args = args[2:]
            calls.append(args)
    return calls


def test_claim_copies_home_without_shared_content(tmp_path: Path) -> None:
    """A claim copies the NWN home but mounts built and live content from the project."""

    controller, layout, _, output = _make(tmp_path)
    (layout.development_dir / "settings.2da").write_text("2DA V2.0\n", encoding="utf-8")
    (layout.development_dir / "live.ncs").write_bytes(b"ncs")
    (layout.override_dir / "shared.nss").write_text("shared", encoding="utf-8")
    (layout.servervault_dir / "player").mkdir()
    (layout.servervault_dir / "player" / "char.bic").write_bytes(b"bic")

    assert controller.run("slot", ["claim", "first"]) == 0
    assert controller.run("slot", ["claim"]) == 0

    assert output == ["1", "2"]
    home = slots.Slots(controller).home_dir(1)
    assert (home / "development" / "settings.2da").is_file()
    assert not (home / "development" / "live.ncs").exists()
    assert (home / "servervault" / "player" / "char.bic").read_bytes() == b"bic"
    assert not any((home / "override").iterdir())
    assert not any((home / "modules").iterdir())
    override = slots.Slots(controller).override_file(1).read_text(encoding="utf-8")
    assert 'container_name: "aredev-slot1-nwserver"' in override
    assert 'ports: !override ["5122:5122/udp"]' in override
    assert 'depends_on: !override ["db"]' in override


def test_claim_fails_when_every_slot_is_taken(tmp_path: Path) -> None:
    controller, _, _, output = _make(tmp_path)
    for _ in range(slots.SLOT_COUNT):
        assert controller.run("slot", ["claim"]) == 0

    assert controller.run("slot", ["claim"]) == 1
    assert output[-1] == f"No free slot: all {slots.SLOT_COUNT} slots are claimed."


def test_run_starts_database_then_server_and_waits_for_log(tmp_path: Path) -> None:
    """Run writes per-run settings, starts db then server and waits for the log line."""

    runner = SlotRunner(server_log="\x1b[32mServer: Module loaded\x1b[0m\n")
    controller, layout, runner, output = _make(tmp_path, runner)
    mount = tmp_path / "extra"
    mount.mkdir()
    controller.run("slot", ["claim"])

    status = controller.run(
        "slot",
        ["run", "1", "-e", "NWN_PLAYERPASSWORD=a$b", "-v", f"{mount}:/nwn/extra:ro"],
    )

    assert status == 0
    assert _compose_calls(runner, "aredev-slot1") == [
        ["up", "-d", "db"],
        ["exec", "-T", "db", "mysqladmin", "ping", "-h127.0.0.1", "--silent"],
        ["up", "-d", "--no-deps", "--force-recreate", "nwserver"],
    ]
    server_start = next(i for i, call in enumerate(runner.calls) if "nwserver" in call)
    assert runner.envs[server_start]["NWN_MODULE"] == controller.config.module_name
    override = slots.Slots(controller).override_file(1).read_text(encoding="utf-8")
    assert '"NWN_PLAYERPASSWORD=a$$b"' in override
    assert (
        f'"source": "{mount.resolve().as_posix()}", "target": "/nwn/extra"' in override
    )
    assert output[-1] == "aredev-slot1-nwserver: matched /Server: Module loaded/."


def test_run_reports_server_exit_with_log_tail(tmp_path: Path) -> None:
    runner = SlotRunner(server_log="startup failed\n", server_running=False)
    controller, _, _, output = _make(tmp_path, runner)
    controller.run("slot", ["claim"])

    assert controller.run("slot", ["run", "1"]) == 1
    assert output[-2:] == ["startup failed", "aredev-slot1-nwserver exited."]


def test_release_removes_containers_volume_and_files(tmp_path: Path) -> None:
    controller, _, runner, output = _make(tmp_path)
    controller.run("slot", ["claim"])
    slot_dir = slots.Slots(controller).slot_dir(1)
    read_only = slot_dir / "server" / "development" / "read_only.txt"
    read_only.write_text("x", encoding="utf-8")
    read_only.chmod(0o444)

    assert controller.run("slot", ["release", "1"]) == 0

    assert _compose_calls(runner, "aredev-slot1") == [["down", "--remove-orphans"]]
    assert ["docker", "volume", "rm", "-f", "aredev-slot1_database"] in runner.calls
    assert not slot_dir.exists()
    assert output[-1] == "Slot 1 released."


def test_logs_sql_and_install_target_the_slot(tmp_path: Path) -> None:
    controller, layout, runner, _ = _make(tmp_path)
    controller.run("slot", ["claim"])
    script = tmp_path / "test_script.ncs"
    script.write_bytes(b"ncs")

    assert controller.run("slot", ["logs", "1", "--tail", "5"]) == 0
    assert controller.run("slot", ["sql", "1", "SELECT", "1"]) == 0
    assert controller.run("slot", ["install", "1", str(script)]) == 0

    assert ["docker", "logs", "--tail", "5", "aredev-slot1-nwserver"] in runner.calls
    sql_call = _compose_calls(runner, "aredev-slot1")[-1]
    assert sql_call[:3] == ["exec", "-T", "db"]
    assert sql_call[-1] == "SELECT 1"
    home = slots.Slots(controller).home_dir(1)
    assert (home / "development" / "test_script.ncs").read_bytes() == b"ncs"
    assert not (layout.development_dir / "test_script.ncs").exists()


def test_status_lists_claims_and_server_state(tmp_path: Path) -> None:
    runner = SlotRunner()
    runner.states = "aredev-slot1-nwserver\trunning\n"
    controller, _, _, output = _make(tmp_path, runner)
    controller.run("slot", ["claim", "owner"])
    output.clear()

    assert controller.run("slot", ["status"]) == 0
    assert output[1].split()[:4] == ["1", "5122", "running", "owner"]
    assert output[2].split() == ["2", "5123", "-", "free"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["run", "1"], "Slot 1 is not claimed."),
        (["run", "9"], "Slot must be between 1 and 4."),
        (["run", "1", "-e", "NOVALUE"], "-e needs KEY=VALUE"),
        (["run", "1", "-v", "no-target"], "-v needs HOST:/container/path"),
        (["bogus"], "slot: argument command: invalid choice"),
    ],
)
def test_invalid_slot_commands_report_one_error(
    tmp_path: Path, args: list[str], message: str
) -> None:
    controller, _, _, output = _make(tmp_path)

    assert controller.run("slot", args) == 1
    assert message in output[-1]


def test_slot_commands_are_refused_inside_builder_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, runner, output = _make(tmp_path)
    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")

    assert controller.run("slot", ["status"]) == 1
    assert "on the host" in output[-1]
    assert runner.calls == []
