from dataclasses import dataclass
from pathlib import Path

DEFAULT_SCALARS: dict[str, str] = {
    "name": "Module",
    "tag": "MODULE",
    "description": "",
    "custom_tlk": "",
    "haks": "",
    "dawn_hour": "6",
    "dusk_hour": "20",
    "min_per_hour": "2",
    "start_day": "1",
    "start_hour": "12",
    "start_month": "1",
    "start_year": "1",
    "xp_scale": "20",
    "movie": "",
}

REQUIRED_ENTRY_FIELDS = ("entry_area", "entry_x", "entry_y", "entry_z", "entry_facing")


class SettingsError(ValueError):
    """Raised when settings.txt is missing required fields or has invalid values."""


@dataclass(slots=True)
class ModuleSettings:
    """Parsed module settings with typed scalar, event, and variable fields."""

    source_path: Path
    name: str
    tag: str
    description: str
    custom_tlk: str
    haks: list[str]
    dawn_hour: int
    dusk_hour: int
    min_per_hour: int
    start_day: int
    start_hour: int
    start_month: int
    start_year: int
    xp_scale: int
    movie: str
    entry_area: str
    entry_x: float
    entry_y: float
    entry_z: float
    entry_facing: float
    events: dict[str, str]
    floats: dict[str, float]
    ints: dict[str, int]
    strings: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "ModuleSettings":
        """Read settings.txt from disk and parse it into a typed ModuleSettings object."""

        return parse_settings_text(path.read_text(encoding="utf-8"), path)


def parse_settings_text(text: str, source_path: Path) -> ModuleSettings:
    """Parse module settings text into a structured dataclass."""

    values = dict(DEFAULT_SCALARS)
    events: dict[str, str] = {}
    floats: dict[str, float] = {}
    ints: dict[str, int] = {}
    strings: dict[str, str] = {}

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        tokens = stripped.split(maxsplit=2)
        keyword = tokens[0]

        if keyword in {"event", "float", "int", "string"}:
            # Typed directives use directive/name/value columns. Empty values are
            # meaningful and become type-appropriate defaults rather than errors.
            if len(tokens) < 2:
                raise SettingsError(
                    f"{keyword!r} directive requires a name on line {line_number}."
                )
            name = tokens[1]
            value_text = tokens[2] if len(tokens) > 2 else ""
            if keyword == "event":
                events[name] = value_text
            elif keyword == "float":
                floats[name] = float(value_text) if value_text else 0.0
            elif keyword == "int":
                ints[name] = int(value_text) if value_text else 0
            else:
                strings[name] = value_text
            continue

        # Scalar values preserve the rest of the line, allowing descriptions and
        # other free text to contain spaces without a separate quoting language.
        values[keyword] = stripped.split(maxsplit=1)[1] if " " in stripped else ""

    missing = [
        field_name for field_name in REQUIRED_ENTRY_FIELDS if field_name not in values
    ]
    if missing:
        raise SettingsError(
            "Starting area information missing: " + ", ".join(sorted(missing))
        )

    return ModuleSettings(
        source_path=source_path,
        name=values["name"],
        tag=values["tag"],
        description=values["description"],
        custom_tlk=values["custom_tlk"],
        haks=values["haks"].split(),
        dawn_hour=int(values["dawn_hour"]),
        dusk_hour=int(values["dusk_hour"]),
        min_per_hour=int(values["min_per_hour"]),
        start_day=int(values["start_day"]),
        start_hour=int(values["start_hour"]),
        start_month=int(values["start_month"]),
        start_year=int(values["start_year"]),
        xp_scale=int(values["xp_scale"]),
        movie=values["movie"],
        entry_area=values["entry_area"],
        entry_x=float(values["entry_x"]),
        entry_y=float(values["entry_y"]),
        entry_z=float(values["entry_z"]),
        entry_facing=float(values["entry_facing"]),
        events=events,
        floats=floats,
        ints=ints,
        strings=strings,
    )
