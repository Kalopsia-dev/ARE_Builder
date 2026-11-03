"""Supervise native AREDev updates without importing application dependencies."""

import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence

REQUEST_ENV = "AREDEV_NATIVE_UPDATE_REQUEST"
COMPLETE_ENV = "AREDEV_NATIVE_UPDATE_COMPLETE"
RESTART_EXIT_CODE = 75


def request_update(root: Path, repo: str, output: Callable[[str], None]) -> int:
    """Send a native update request from current or legacy controller code."""
    request_path = os.environ.get(REQUEST_ENV)
    if not request_path:
        output("Native update requires launching with python -m arebuilder aredev.")
        return 1
    repo = repo.strip()
    if not repo:
        output("AREBUILDER_REPO is empty in config/arebuilder.env.")
        return 1
    requirement = repo if repo.startswith("git+") else f"git+{repo}"
    try:
        Path(request_path).write_text(
            json.dumps({"requirement": requirement, "root": str(root)}),
            encoding="utf-8",
        )
    except OSError as error:
        output(f"Unable to request native update: {error}")
        return 1
    output("Updating arebuilder after AREDev exits...")
    return RESTART_EXIT_CODE


def main(argv: Sequence[str]) -> int:
    """Repair dependencies, then install updates only after the AREDev worker exits."""
    env = os.environ.copy()
    env.pop(REQUEST_ENV, None)
    env.pop(COMPLETE_ENV, None)
    try:
        # The previous updater used --no-deps, so repair before importing the
        # newly installed application. Satisfied requirements are left in place.
        requirements = importlib.metadata.requires("arebuilder") or []
        if requirements:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--disable-pip-version-check",
                    *requirements,
                ],
                env=env,
            )
            if result.returncode:
                return result.returncode

        with tempfile.TemporaryDirectory(prefix="arebuilder-update-") as directory:
            request_path = Path(directory) / "request.json"
            # Installation may replace this module with an older release. Keep
            # our worker available without requiring a protocol in that target.
            continuation = Path(directory) / "continue_update.py"
            continuation.write_bytes(Path(__file__).read_bytes())
            worker_env = {**env, REQUEST_ENV: str(request_path)}
            worker_command = [sys.executable, "-m", "arebuilder", *argv]
            while True:
                status = subprocess.call(
                    worker_command,
                    env=worker_env,
                )
                if status != RESTART_EXIT_CODE or not request_path.exists():
                    return status

                request = json.loads(request_path.read_text(encoding="utf-8"))
                request_path.unlink()
                requirement, root = request["requirement"], request["root"]
                if not isinstance(requirement, str) or not isinstance(root, str):
                    raise ValueError("Invalid native update request")
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--quiet",
                        "--upgrade",
                        "--force-reinstall",
                        requirement,
                    ],
                    cwd=root,
                    env=env,
                )
                if result.returncode:
                    return result.returncode
                result = subprocess.run([sys.executable, "-m", "pip", "check"], env=env)
                if result.returncode:
                    return result.returncode
                worker_command = [sys.executable, str(continuation), *argv]
    except KeyboardInterrupt:
        return 130
    except (
        OSError,
        ValueError,
        KeyError,
        importlib.metadata.PackageNotFoundError,
    ) as error:
        print(f"Native update failed: {error}", file=sys.stderr)
        return 1


def _continue_worker(argv: Sequence[str]) -> int:
    """Refresh containers in the installed target without re-entering its updater."""
    from arebuilder.app.aredev.controller import (
        AREDevController,
        BuilderConfigError,
        DEFAULT_AREBUILDER_REPO,
        _parse_args,
    )

    try:
        args = _parse_args(list(argv[1:]))
        controller = AREDevController.from_root(args.root)
        status = controller._update_containers()
        if status != 0:
            return status
        controller.output("Update complete.")
        if args.command is not None:
            return 0

        def update_native() -> int:
            return request_update(
                controller.layout.root,
                controller.config.arebuilder_repo or DEFAULT_AREBUILDER_REPO,
                controller.output,
            )

        # A legacy controller must hand subsequent updates back to this parent,
        # rather than invoking its own in-process pip installer.
        controller._update_native_environment = update_native
        return controller.run_interactive()
    except (BuilderConfigError, ValueError) as error:
        print(str(error), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(_continue_worker(sys.argv[1:]))
