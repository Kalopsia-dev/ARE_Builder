"""AREDev console entry point and lazily loaded application exports."""

import os
from pathlib import Path
import sys
from collections.abc import Sequence

__all__ = [
    "AREDevAutoSuggest",
    "AREDevCompleter",
    "AREDevController",
    "ProcessResult",
    "SCREEN_CLEAR",
    "main",
]


def main(argv: Sequence[str] | None = None) -> int:
    """Supervise script launchers while leaving executable launchers untouched."""
    script_launch = argv is None
    argv = list(sys.argv[1:] if script_launch else argv)
    if (
        script_launch
        and not os.environ.get("AREDEV_NATIVE_UPDATE_REQUEST")
        and os.environ.get("AREDEV_IN_CONTAINER") != "1"
        and Path(sys.argv[0]).suffix.lower() != ".exe"
    ):
        from arebuilder._native_update import main as supervise

        return supervise(["aredev", *argv])
    from arebuilder.app.aredev.controller import main as run

    return run(argv)


def __getattr__(name: str):
    # Preserve the public controller exports without loading their native
    # dependencies in the script-launching supervisor.
    if name in __all__:
        from arebuilder.app.aredev import controller

        return getattr(controller, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
