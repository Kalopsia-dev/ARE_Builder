import os
from pathlib import Path


def parse_key_value_text(text: str) -> dict[str, str]:
    """Parse simple KEY=value configuration text, ignoring blanks and comments."""

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = strip_matching_quotes(value.strip())
    return values


def strip_matching_quotes(value: str) -> str:
    """Remove one matching pair of surrounding single or double quotes."""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def expand_path(value: str | Path) -> Path:
    """Expand environment variables and user markers in a filesystem path value."""

    return Path(os.path.expandvars(str(value))).expanduser()


def validate_build_target(
    value: str,
    *,
    error_type: type[ValueError] = ValueError,
) -> None:
    """Validate that the build target is non-empty and safe to use in paths."""

    if not value:
        raise error_type("BUILD_TARGET cannot be empty.")
    if value == "are":
        raise error_type('BUILD_TARGET cannot be "are".')
    if "/" in value or "\\" in value or os.sep in value:
        raise error_type("BUILD_TARGET cannot contain path separators.")
