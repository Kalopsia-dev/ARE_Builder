import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from nwn import gff

from arebuilder.app.aredev import AREDevController, ProcessResult
from arebuilder.app.aredev.project import ProjectLayout, load_arebuilder_env
from arebuilder.app.aredev.scaffold import initialize_aredev_project
from arebuilder.nwn.compat import write_gff

FAKE_HOST_ROOT = "/host/project"
FAKE_HOST_NWN_INSTALL_ROOT = "/host/nwn-install"
FAKE_HOST_NWN_HOME_ROOT = "/host/nwn-home"
FAKE_WINDOWS_HOST_ROOT = r"X:\synthetic-project"
PLAYER_PASSWORD = "dummy-player-password"
DM_PASSWORD = "dummy-dm-password"


class FakeRunner:
    """Subprocess runner test double that records calls for assertions."""

    def __init__(self, *, running: str = ""):
        self.running = running
        self.calls: list[list[str]] = []
        self.cwd: list[Path] = []
        self.envs: list[dict[str, str]] = []
        self.detached: list[bool] = []

    def __call__(self, args, *, cwd, env, capture_output, background, detach=False):
        self.calls.append(list(args))
        self.cwd.append(Path(cwd))
        self.envs.append(dict(env or {}))
        self.detached.append(detach)
        if list(args[:3]) == ["docker", "ps", "--format"]:
            return ProcessResult(0, stdout=self.running)
        if list(args[:4]) == ["docker", "volume", "ls", "--format"]:
            return ProcessResult(0, stdout="aredev_database\naredev_data\n")
        return ProcessResult(0)


class FakeProgress:
    """Toolset progress test double that records updates."""

    def __init__(self, total: int):
        self.total = total
        self.updates: list[int] = []
        self.closed = False

    def update(self, count: int = 1) -> None:
        self.updates.append(count)

    def close(self) -> None:
        self.closed = True


def make_controller(
    tmp_path: Path,
    *,
    runner: FakeRunner | None = None,
    output: Callable[[str], None] | None = None,
    input_reader: Callable[[str], str] | None = None,
    screen_clearer: Callable[[], None] | None = None,
    nwn_home: Path | None = None,
    nwn_install: Path | None = None,
    toolset_progress_factory: Callable[[int], FakeProgress] | None = None,
) -> tuple[
    AREDevController, ProjectLayout, list[tuple[str, list[str], bool]], FakeRunner
]:
    """Create a controller test fixture with fake process and build runners."""

    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    layout = ProjectLayout.from_root(project)
    create_resource_dirs(layout)
    resolved_nwn_home = nwn_home or tmp_path / "nwn-home"
    (resolved_nwn_home / "hak").mkdir(parents=True)
    (resolved_nwn_home / "tlk").mkdir(parents=True)
    extra_env = f"NWN_HOME_PATH={resolved_nwn_home}\n"
    if nwn_install is not None:
        extra_env += f"NWN_INSTALL_PATH={nwn_install}\n"
    layout.arebuilder_env_path.write_text(
        layout.arebuilder_env_path.read_text(encoding="utf-8") + extra_env,
        encoding="utf-8",
    )
    build_calls: list[tuple[str, list[str], bool]] = []
    fake_runner = runner or FakeRunner()

    def build_runner(command: str, args: list[str], live: bool) -> int:
        build_calls.append((command, list(args), live))
        return 0

    controller = AREDevController(
        layout=layout,
        config=load_arebuilder_env(project),
        process_runner=fake_runner,
        build_runner=build_runner,
        output=output or (lambda _message: None),
        input_reader=input_reader,
        screen_clearer=screen_clearer,
        toolset_progress_factory=toolset_progress_factory,
    )
    return controller, layout, build_calls, fake_runner


def create_resource_dirs(layout: ProjectLayout) -> None:
    """Create the minimal resource directories required by controller tests."""

    (layout.are_resources_dir / "gff").mkdir(parents=True)
    layout.target_resources_dir("pgcc").mkdir(parents=True)


def stage_toolset_sources(layout: ProjectLayout) -> dict[str, Path]:
    """Create source files used by toolset bundle tests."""

    module_path = layout.module_archive_path("are-dev-pgcc")
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_bytes(b"module")

    shared_only = layout.are_resources_dir / "gff" / "shared_only.are"
    write_area(shared_only)
    shared_duplicate = layout.are_resources_dir / "gff" / "shared.are"
    write_area(shared_duplicate)

    script_dir = layout.are_resources_dir / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "include.nss"
    script.write_text("void helper() {}\n", encoding="latin-1")

    target_dir = layout.target_resources_dir("pgcc") / "nested"
    target_dir.mkdir(parents=True)
    target = target_dir / "target.utc"
    target.write_text("target", encoding="utf-8")
    target_shared = target_dir / "shared.are"
    write_area(target_shared)
    write_settings(layout.are_resources_dir / "gff")

    compiled_dir = layout.build_dir("are-dev-pgcc")
    compiled_dir.mkdir(parents=True)
    compiled = compiled_dir / "module.ifo"
    compiled.write_bytes(b"compiled module ifo")
    (compiled_dir / "compiled_script.ncs").write_bytes(b"compiled script")

    return {
        "shared_only": shared_only,
        "shared_duplicate": shared_duplicate,
        "script": script,
        "target": target,
        "target_shared": target_shared,
        "compiled": compiled,
    }


def write_area(
    path: Path,
    tileset: str | None = None,
    tile_ids: list[int] | None = None,
) -> None:
    """Write a minimal ARE resource for Toolset tests."""

    fields = {"Tileset": gff.ResRef(tileset)} if tileset is not None else {}
    if tile_ids is not None:
        fields["Tile_List"] = gff.List(
            [gff.Struct(0, Tile_ID=gff.Int(tile_id)) for tile_id in tile_ids]
        )
    write_gff(path, gff.Struct(0xFFFFFFFF, **fields), "ARE ")


def write_settings(path: Path, *, entry_area: str = "shared_only") -> None:
    """Write minimal module settings for Toolset dependency filtering."""

    path.mkdir(parents=True, exist_ok=True)
    (path / "settings.txt").write_text(
        f"""
        name Test Module
        tag TEST_MODULE
        entry_area {entry_area}
        entry_x 0.0
        entry_y 0.0
        entry_z 0.0
        entry_facing 0.0
        """,
        encoding="utf-8",
    )


def assert_toolset_resource(
    path: Path,
    source_path: Path,
    *,
    symlink_target: str | None = None,
) -> None:
    """Assert a toolset resource points at or contains the expected source."""

    if path.is_symlink():
        assert os.readlink(path) == (symlink_target or str(source_path.resolve()))
        return
    assert path.exists()
    assert path.read_bytes() == source_path.read_bytes()


def write_host_response(
    layout: ProjectLayout,
    request_id: str,
    *,
    status: str,
    return_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    message: str = "",
) -> None:
    """Write a host-launcher response file for bridge protocol tests."""

    command_dir = layout.temp_dir / "host-commands"
    command_dir.mkdir(parents=True, exist_ok=True)
    resolved_return_code = return_code if return_code is not None else 0
    if return_code is None and status != "ok":
        resolved_return_code = 1
    (command_dir / f"host-{request_id}.response").write_text(
        f"REQUEST_ID\t{request_id}\n"
        f"STATUS\t{status}\n"
        f"RETURN_CODE\t{resolved_return_code}\n"
        f"STDOUT\t{stdout}\n"
        f"STDERR\t{stderr}\n"
        f"MESSAGE\t{message}\n",
        encoding="utf-8",
    )


def read_host_request(layout: ProjectLayout, request_id: str) -> dict[str, str]:
    """Read and parse the host-launcher request emitted by the controller."""

    request_path = layout.temp_dir / "host-commands" / f"host-{request_id}.request"
    values: dict[str, str] = {}
    for line in request_path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("\t")
        values[key] = value
    return values


def patch_request_ids(
    monkeypatch: pytest.MonkeyPatch,
    request_ids: list[str],
) -> None:
    """Patch UUID generation so host-request tests can use deterministic filenames."""

    request_iter = iter(request_ids)
    monkeypatch.setattr(
        "arebuilder.app.aredev.host_bridge.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(request_iter)),
    )


def write_nwn_passwords(layout: ProjectLayout) -> None:
    """Write deterministic dummy passwords for launch-request tests."""

    layout.nwserver_env_path.write_text(
        "NWN_PORT=5121\n"
        f"NWN_PLAYERPASSWORD={PLAYER_PASSWORD}\n"
        f"NWN_DMPASSWORD={DM_PASSWORD}\n",
        encoding="utf-8",
    )
