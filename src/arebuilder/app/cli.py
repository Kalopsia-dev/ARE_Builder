import argparse
import sys
from typing import Sequence

from pydantic import ValidationError

from arebuilder.app.arebuilder.build_command import (
    VALID_COMMANDS,
    execute_build_command,
)
from arebuilder.config import BuilderSettings


class CliArgumentError(ValueError):
    """Raised for CLI argument errors that should return a process code."""


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports errors through ``main`` return codes."""

    def error(self, message: str) -> None:
        """Convert argparse parser failures into the project-specific exception path."""

        raise CliArgumentError(message)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the top-level builder CLI, including AREDev subcommands and direct build commands."""

    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    if argv and argv[0] == "init":
        from arebuilder.app.aredev.scaffold_cli import main as scaffold_main

        return scaffold_main(argv[1:])

    if argv and argv[0] == "aredev":
        from arebuilder.app.aredev import main as aredev_main

        return aredev_main(argv[1:])

    parser = _build_parser()
    try:
        args = _parse_args(argv, parser)
    except CliArgumentError as exc:
        print(f"Invalid arguments: {exc}", flush=True)
        return 1

    command = args.command
    if command is None:
        parser.print_help()
        return 0

    if command not in VALID_COMMANDS:
        print(f"Invalid command: {command}", flush=True)
        return 1

    palette_types = (
        list(args.palette_types)
        if command == "palette" and args.palette_types
        else None
    )

    try:
        settings = BuilderSettings(**_cli_overrides(args))
        if command == "compile" and args.palette_types:
            raise CliArgumentError("compile accepts at most one selector argument.")
        return execute_build_command(
            command=command,
            target_name=args.target_name,
            palette_types=palette_types,
            settings=settings,
        )
    except CliArgumentError as exc:
        print(f"Invalid arguments: {exc}", flush=True)
        return 1
    except ValidationError as exc:
        print(str(exc), flush=True)
        return 1
    except KeyError as exc:
        print(str(exc), flush=True)
        return 1
    except Exception as exc:
        print(str(exc), flush=True)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI argument parser."""

    parser = _ArgumentParser(
        prog="arebuilder",
        description="Build Neverwinter Nights module artifacts.",
    )
    parser.add_argument("--root", help="AREDev project root. Defaults to cwd.")
    parser.add_argument(
        "--builder-mount-root",
        help="Container-visible root written into generated symlink targets.",
    )
    parser.add_argument(
        "--server-root",
        help="NWN home/server root used for modules, override, and runtime paths.",
    )
    parser.add_argument(
        "--hak-dir",
        help="Directory where generated HAK files are written.",
    )
    parser.add_argument(
        "--tlk-dir",
        help="Directory where generated TLK files are written.",
    )
    parser.add_argument(
        "--state-file",
        help="State file used for stateful shared-script compilation.",
    )
    parser.add_argument(
        "--script-dir",
        help="Shared compile input directory override for the compile command.",
    )
    parser.add_argument(
        "--output-dir",
        help="Shared compile output directory override for the compile command.",
    )
    parser.add_argument(
        "--nwn-root",
        help="Neverwinter Nights install root used to resolve base scripts.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="Worker count for NWScript compilation. Defaults to CPU count.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=None,
        help="Also write direct compile output to the server development folder.",
    )
    parser.add_argument(
        "--custom-content-reference",
        help="Custom content TLK seed path override.",
    )
    parser.add_argument("command", nargs="?")
    parser.add_argument("target_name", nargs="?")
    parser.add_argument("palette_types", nargs="*")
    return parser


def _parse_args(
    argv: Sequence[str], parser: argparse.ArgumentParser
) -> argparse.Namespace:
    """Parse command-line arguments without executing the command."""

    return parser.parse_args(list(argv))


def _cli_overrides(args: argparse.Namespace) -> dict[str, object]:
    """Translate parsed CLI options into BuilderSettings keyword overrides."""

    overrides: dict[str, object] = {}

    mapping = {
        "root": "project_root",
        "builder_mount_root": "builder_mount_root",
        "server_root": "server_root",
        "state_file": "state_file",
        "script_dir": "script_dir",
        "output_dir": "output_dir",
        "nwn_root": "nwn_root",
        "workers": "compile_workers",
        "live": "compile_live",
        "custom_content_reference": "custom_content_reference",
        "hak_dir": "hak_dir",
        "tlk_dir": "tlk_dir",
    }
    for arg_name, field_name in mapping.items():
        value = getattr(args, arg_name)
        if value is not None:
            overrides[field_name] = value
    return overrides
