"""Regressions for native updates outside the worker's imported environment."""

import importlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest


def supervisor(monkeypatch):
    """Isolate distribution metadata and inherited private handoff flags."""
    assert importlib.util.find_spec("arebuilder._native_update") is not None
    module = importlib.import_module("arebuilder._native_update")
    monkeypatch.setattr(
        module.importlib.metadata,
        "requires",
        lambda _name: ["nwn==0.0.22", "tqdm==4.67.3"],
    )
    monkeypatch.delenv(module.REQUEST_ENV, raising=False)
    monkeypatch.delenv(module.COMPLETE_ENV, raising=False)
    return module


@pytest.mark.parametrize("one_shot", [False, True])
def test_update_waits_for_worker_and_always_starts_fresh_code(
    tmp_path, monkeypatch, one_shot
):
    native = supervisor(monkeypatch)
    project = tmp_path / "Project With Spaces"
    project.mkdir()
    argv = ["aredev", "--root", str(project), *(["update"] if one_shot else [])]
    stages = []
    children = []
    alive = False

    def run(args, *, env, cwd=None):
        assert not alive
        assert args[:3] == [sys.executable, "-m", "pip"]
        assert native.REQUEST_ENV not in env and native.COMPLETE_ENV not in env
        assert "--no-deps" not in args
        if "check" in args:
            stages.append("check")
        elif "--force-reinstall" in args:
            stages.append("install")
            assert args[-1] == "git+https://example.invalid/custom.git"
            assert cwd == str(project)
        else:
            stages.append("guard")
            assert args[-2:] == ["nwn==0.0.22", "tqdm==4.67.3"]
            assert "--upgrade" not in args
        return SimpleNamespace(returncode=0)

    def call(args, *, env):
        nonlocal alive
        alive = True
        children.append(dict(env))
        stages.append("worker")
        if len(children) == 1:
            assert args == [sys.executable, "-m", "arebuilder", *argv]
            Path(env[native.REQUEST_ENV]).write_text(
                json.dumps(
                    {
                        "requirement": "git+https://example.invalid/custom.git",
                        "root": str(project),
                    }
                )
            )
            alive = False
            return 75
        assert args[0] == sys.executable
        assert Path(args[1]).is_file()
        assert args[2:] == argv
        assert native.COMPLETE_ENV not in env
        assert not Path(env[native.REQUEST_ENV]).exists()
        alive = False
        return 0

    monkeypatch.setattr(native.subprocess, "run", run)
    monkeypatch.setattr(native.subprocess, "call", call)
    monkeypatch.setattr(
        native.importlib.metadata,
        "version",
        lambda _name: pytest.fail("Restart must not depend on version comparison"),
    )
    assert native.main(argv) == 0
    assert stages == ["guard", "worker", "install", "check", "worker"]


@pytest.mark.parametrize(
    ("failure", "status", "expected"),
    [
        ("guard", 13, ["guard"]),
        ("worker", 17, ["guard", "worker"]),
        ("install", 19, ["guard", "worker", "install"]),
        ("check", 23, ["guard", "worker", "install", "check"]),
        ("continuation", 7, ["guard", "worker", "install", "check", "continuation"]),
    ],
)
def test_failure_reaches_original_caller(
    monkeypatch, tmp_path, failure, status, expected
):
    native = supervisor(monkeypatch)
    stages = []

    def run(args, **kwargs):
        stage = (
            "check"
            if "check" in args
            else "install"
            if "--force-reinstall" in args
            else "guard"
        )
        stages.append(stage)
        return SimpleNamespace(returncode=status if failure == stage else 0)

    def call(args, *, env):
        stage = "worker" if args[1:3] == ["-m", "arebuilder"] else "continuation"
        assert native.COMPLETE_ENV not in env, (
            "Continuation must not rely on installed target flags"
        )
        stages.append(stage)
        if stage == "continuation" or failure == "worker":
            return status
        Path(env[native.REQUEST_ENV]).write_text(
            json.dumps({"requirement": "git+repo", "root": str(tmp_path)})
        )
        return 75

    monkeypatch.setattr(native.subprocess, "run", run)
    monkeypatch.setattr(native.subprocess, "call", call)
    assert native.main(["aredev", "update"]) == status
    assert stages == expected


def test_requestless_restart_signal_is_forwarded(monkeypatch):
    native = supervisor(monkeypatch)
    commands = []
    monkeypatch.setattr(
        native.subprocess,
        "run",
        lambda args, **kw: commands.append(args) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(native.subprocess, "call", lambda *args, **kw: 75)
    assert native.main(["aredev"]) == 75
    assert len(commands) == 1  # Startup repair only; no self-install.


def test_repeated_updates_do_not_reuse_request_or_repair_again(monkeypatch, tmp_path):
    native = supervisor(monkeypatch)
    stages = []
    count = 0

    def call(args, *, env):
        nonlocal count
        count += 1
        assert not Path(env[native.REQUEST_ENV]).exists()
        if count == 3:
            return 0
        Path(env[native.REQUEST_ENV]).write_text(
            json.dumps({"requirement": "git+repo", "root": str(tmp_path)})
        )
        return 75

    def run(args, **kw):
        stages.append(
            "check"
            if "check" in args
            else "install"
            if "--force-reinstall" in args
            else "guard"
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(native.subprocess, "call", call)
    monkeypatch.setattr(native.subprocess, "run", run)
    assert native.main(["aredev"]) == 0
    assert count == 3 and stages == ["guard", "install", "check", "install", "check"]


def test_bad_request_reports_failure_without_installing(monkeypatch, capsys):
    native = supervisor(monkeypatch)
    commands = []
    monkeypatch.setattr(
        native.subprocess,
        "run",
        lambda args, **kw: commands.append(args) or SimpleNamespace(returncode=0),
    )

    def call(args, *, env):
        Path(env[native.REQUEST_ENV]).write_text("not JSON")
        return 75

    monkeypatch.setattr(native.subprocess, "call", call)
    assert native.main(["aredev"]) == 1
    assert len(commands) == 1
    assert "Native update failed" in capsys.readouterr().err


def test_interrupted_repair_returns_interrupt_status(monkeypatch):
    native = supervisor(monkeypatch)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(native.subprocess, "run", interrupted)
    assert native.main(["aredev"]) == 130


def test_inherited_continuation_is_not_replayed(monkeypatch):
    native = supervisor(monkeypatch)
    monkeypatch.setenv(native.COMPLETE_ENV, "1")

    def call(args, *, env):
        assert native.COMPLETE_ENV not in env
        return 0

    monkeypatch.setattr(
        native.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(native.subprocess, "call", call)
    assert native.main(["aredev"]) == 0


def test_supervisor_import_does_not_load_application_dependencies():
    source = str(Path(__file__).resolve().parents[3])
    code = f"import sys; sys.path.insert(0, {source!r}); import arebuilder._native_update; assert not any(n.split('.')[0] in {{'pydantic', 'pydantic_core', 'nwn', 'prompt_toolkit', 'wcwidth'}} for n in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("args", "worker", "container", "expected"),
    [
        (["aredev"], False, False, 6),
        (["aredev", "update"], True, False, 4),
        (["aredev"], False, True, 4),
        (["--help"], False, False, 4),
    ],
)
def test_module_routes_only_host_aredev_through_supervision(
    monkeypatch, args, worker, container, expected
):
    from arebuilder.app import cli

    monkeypatch.setattr(cli, "main", lambda: 4)
    monkeypatch.setitem(
        sys.modules, "arebuilder._native_update", SimpleNamespace(main=lambda argv: 6)
    )
    monkeypatch.setattr(sys, "argv", ["arebuilder", *args])
    monkeypatch.delenv("AREDEV_NATIVE_UPDATE_REQUEST", raising=False)
    monkeypatch.delenv("AREDEV_IN_CONTAINER", raising=False)
    if worker:
        monkeypatch.setenv("AREDEV_NATIVE_UPDATE_REQUEST", "request.json")
    if container:
        monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    with pytest.raises(SystemExit) as exit:
        runpy.run_module("arebuilder", run_name="__main__")
    assert exit.value.code == expected


@pytest.mark.parametrize(
    ("launcher", "worker", "container", "expected"),
    [
        ("/env/bin/aredev", False, False, 6),
        ("/env/Scripts/aredev.exe", False, False, 4),
        ("/env/bin/aredev", True, False, 4),
        ("/env/bin/aredev", False, True, 4),
    ],
)
def test_existing_aredev_script_routes_through_supervision(
    monkeypatch, launcher, worker, container, expected
):
    from arebuilder.app import aredev
    from arebuilder.app.aredev import controller

    native = supervisor(monkeypatch)
    monkeypatch.setattr(sys, "argv", [launcher, "--root", "Project With Spaces"])
    monkeypatch.setattr(
        native,
        "main",
        lambda argv: 6 if argv == ["aredev", "--root", "Project With Spaces"] else 99,
    )
    monkeypatch.setattr(controller, "main", lambda argv: 4)
    monkeypatch.delenv("AREDEV_IN_CONTAINER", raising=False)
    if worker:
        monkeypatch.setenv(native.REQUEST_ENV, "request.json")
    if container:
        monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    assert aredev.main() == expected


def test_aredev_package_import_remains_free_of_native_dependencies():
    source = str(Path(__file__).resolve().parents[3])
    code = f"import sys; sys.path.insert(0, {source!r}); import arebuilder.app.aredev; assert not any(n.split('.')[0] in {{'pydantic', 'pydantic_core', 'nwn', 'prompt_toolkit', 'wcwidth'}} for n in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("one_shot", "container_status", "expected", "events", "repeat"),
    [
        (True, 0, 0, ["pull"], False),
        (False, 0, 29, ["pull", "prompt"], False),
        (False, 7, 7, ["pull"], False),
        (False, 0, 75, ["pull", "prompt"], True),
    ],
)
def test_saved_continuation_runs_target_without_new_protocol(
    tmp_path, one_shot, container_status, expected, events, repeat
):
    """The installed target deliberately has no supervisor or completion flag support."""
    from arebuilder import _native_update

    worker = tmp_path / "Saved Worker With Spaces.py"
    worker.write_bytes(Path(_native_update.__file__).read_bytes())
    target = tmp_path / "legacy-target"
    package = target / "arebuilder/app/aredev"
    package.mkdir(parents=True)
    for directory in [target / "arebuilder", target / "arebuilder/app", package]:
        (directory / "__init__.py").write_text("")
    project = tmp_path / "Project With Spaces"
    project.mkdir()
    (package / "controller.py").write_text(f"""import argparse
from pathlib import Path
from types import SimpleNamespace
DEFAULT_AREBUILDER_REPO = "https://example.invalid/default.git"
class BuilderConfigError(ValueError):
    pass
class AREDevController:
    @classmethod
    def from_root(cls, root):
        instance = cls()
        instance.root = Path(root)
        instance.layout = SimpleNamespace(root=Path(root))
        instance.config = SimpleNamespace(arebuilder_repo="https://example.invalid/legacy.git")
        return instance
    def record(self, name):
        with (self.root / "events").open("a") as file:
            file.write(name + "\\n")
    def _update_containers(self):
        self.record("pull")
        return {container_status}
    def output(self, message):
        pass
    def run_interactive(self):
        self.record("prompt")
        if {repeat}:
            return self._update_native_environment()
        return 29
    def _update_native_environment(self):
        raise AssertionError("Legacy in-process installation must not run")
    def update(self):
        raise AssertionError("Must not re-enter the legacy installer")
def _parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root")
    parser.add_argument("command", nargs="?")
    return parser.parse_args(argv)
""")
    command = [
        sys.executable,
        str(worker),
        "aredev",
        "--root",
        str(project),
        *(["update"] if one_shot else []),
    ]
    result = subprocess.run(
        command,
        env={
            **os.environ,
            "PYTHONPATH": str(target),
            "AREDEV_NATIVE_UPDATE_REQUEST": str(tmp_path / "request.json"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stderr
    assert (project / "events").read_text().splitlines() == events
    if repeat:
        assert json.loads((tmp_path / "request.json").read_text()) == {
            "requirement": "git+https://example.invalid/legacy.git",
            "root": str(project),
        }
