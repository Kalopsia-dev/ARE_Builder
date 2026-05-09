import argparse
from typing import Sequence

from arebuilder.app.aredev.project import BuilderConfigError
from arebuilder.app.aredev.scaffold import (
    ScaffoldConflictError,
    initialize_aredev_project,
)


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports errors through project exceptions."""

    def error(self, message: str) -> None:
        """Convert argparse parser failures into the project-specific exception path."""

        raise ValueError(message)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the project scaffold CLI and return a status code instead of raising argparse exits."""

    try:
        args = _parse_args(argv or [])
        result = initialize_aredev_project(
            args.target_dir,
            build_target=args.target,
            backend=args.backend,
            force=args.force,
        )
    except (BuilderConfigError, ScaffoldConflictError, ValueError) as exc:
        print(str(exc), flush=True)
        return 1

    print(f"Initialized AREDev project at {result.root}", flush=True)
    for warning in result.warnings:
        print(warning, flush=True)
    return 0


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments without executing the command."""

    parser = _ArgumentParser(
        prog="arebuilder init",
        description="Create an AREDev project scaffold.",
    )
    parser.add_argument(
        "target_dir",
        metavar="TARGET_DIR",
        help="AREDev project directory to create or update.",
    )
    parser.add_argument(
        "--target",
        metavar="BUILD_TARGET",
        default="pgcc",
        help="Default build target.",
    )
    parser.add_argument(
        "--backend",
        choices=["native", "docker"],
        default="native",
        help="Default builder backend written to config/arebuilder.env.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite scaffold files that already exist.",
    )
    return parser.parse_args(list(argv))
