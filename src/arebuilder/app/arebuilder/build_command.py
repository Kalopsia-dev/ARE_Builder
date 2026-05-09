from arebuilder.app.arebuilder.engine import BuildEngine
from arebuilder.config import (
    BuilderSettings,
    RuntimeResolver,
)
from arebuilder.content import DEFAULT_PALETTES

VALID_COMMANDS = {"all", "link", "compile", "pack", "palette", "quick"}
__all__ = [
    "VALID_COMMANDS",
    "execute_build_command",
]


def execute_build_command(
    *,
    command: str,
    target_name: str | None = None,
    palette_types: list[str] | None = None,
    settings: BuilderSettings | None = None,
) -> int:
    """Validate command defaults and route one request to the build engine."""

    if command not in VALID_COMMANDS:
        raise ValueError(f"Invalid command: {command}")

    runtime = RuntimeResolver(settings or BuilderSettings()).resolve()
    engine = BuildEngine(
        runtime.config,
        runtime.paths,
    )
    resolved_target = target_name or "all"
    resolved_palettes = (
        list(palette_types)
        if command == "palette" and palette_types
        else list(DEFAULT_PALETTES)
    )
    return {
        "all": lambda: engine.run_all(resolved_target),
        "link": lambda: engine.run_link(resolved_target),
        "compile": lambda: engine.run_compile(target_name),
        "pack": lambda: engine.run_pack(resolved_target),
        "palette": lambda: engine.run_palette(resolved_target, resolved_palettes),
        "quick": lambda: engine.run_quick(resolved_target),
    }[command]()
