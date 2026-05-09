from dataclasses import dataclass
from pathlib import Path

from nwn import gff
from nwn.types import Language

from arebuilder.nwn.compat import (
    INVALID_STRREF,
    TalkTable,
    normalize_locstring_entries,
    read_gff,
    write_gff,
)

DEFAULT_PALETTES = (
    "creature",
    "door",
    "encounter",
    "item",
    "placeable",
    "sound",
    "store",
    "trigger",
    "waypoint",
)

NAME_FIELD_BY_TYPE = {
    "creature": "FirstName",
    "door": "LocName",
    "placeable": "LocName",
    "sound": "LocName",
    "store": "LocName",
    "encounter": "LocalizedName",
    "item": "LocalizedName",
    "trigger": "LocalizedName",
    "waypoint": "LocalizedName",
}


@dataclass(slots=True, frozen=True)
class PaletteCategory:
    """Represent one palette.txt category row with its TLK strref, optional ID, and path."""

    strref: int
    category_id: int | None
    path: str


@dataclass(slots=True)
class PaletteObject:
    """A resolved palette entry produced from a GFF resource file."""

    sort_name: str
    resource_name: str
    display_name: str | None
    strref: int | None
    challenge_rating: float | None = None
    faction_name: str | None = None


def generate_palette(
    palette_type: str,
    source_dirs: list[Path],
    build_dir: Path,
    talk_table: TalkTable,
) -> Path | None:
    """Generate a single palette file into the build directory."""

    if palette_type not in DEFAULT_PALETTES:
        raise ValueError(
            f"Invalid palette type: {palette_type}, expecting one of {', '.join(DEFAULT_PALETTES)}"
        )

    palette_file = _find_palette_file(source_dirs, palette_type)
    if palette_file is None:
        return None

    categories = _parse_palette_file(palette_file)
    factions_root = None

    def get_factions_root():
        """Load and cache the faction table only if creature palette entries need it."""

        nonlocal factions_root
        if factions_root is None:
            # Only creature palettes need repute.fac. Delaying the load lets all
            # other palette types run without a faction file.
            factions_root = _load_factions(source_dirs)
        return factions_root

    root = gff.Struct(
        0xFFFFFFFF,
        MAIN=_build_palette_list(
            palette_type=palette_type,
            source_dirs=source_dirs,
            talk_table=talk_table,
            categories=categories,
            get_factions_root=get_factions_root,
            current_path="",
            depth=0,
        ),
    )
    output_path = build_dir / f"{palette_type}palcus.itp"
    write_gff(output_path, root, "ITP ")
    return output_path


def _find_palette_file(source_dirs: list[Path], palette_type: str) -> Path | None:
    """Return the last matching ``palette.txt`` file across the source directories."""

    return next(
        (
            candidate
            for source_dir in reversed(source_dirs)
            if (candidate := source_dir / palette_type / "palette.txt").exists()
        ),
        None,
    )


def _parse_palette_file(path: Path) -> list[PaletteCategory]:
    """Parse palette.txt category definitions into ordered PaletteCategory entries."""

    categories: list[PaletteCategory] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.rstrip()
        if not stripped:
            continue
        identifier_text, category_path = stripped.split(" ", 1)
        strref_text, category_id = _parse_palette_identifier(identifier_text)
        categories.append(
            PaletteCategory(
                strref=int(strref_text or "0"),
                category_id=category_id,
                path=category_path,
            )
        )
    return categories


def _parse_palette_identifier(identifier_text: str) -> tuple[str, int | None]:
    """Parse a palette category identifier using split semantics."""

    if identifier_text == ".":
        return "0", None
    if identifier_text.startswith("."):
        return "0", int(identifier_text[1:] or "0")
    if "." in identifier_text:
        strref_text, category_id_text = identifier_text.split(".", 1)
        return strref_text, int(category_id_text or "0")
    return identifier_text, None


def _build_palette_list(
    *,
    palette_type: str,
    source_dirs: list[Path],
    talk_table: TalkTable,
    categories: list[PaletteCategory],
    get_factions_root,
    current_path: str,
    depth: int,
):
    """Recursively build the GFF list for nested categories and resource entries."""

    entries = []

    for category in categories:
        if category.path.count("/") != depth or not category.path.startswith(
            current_path
        ):
            continue

        fields = {}
        if category.category_id is not None:
            fields["ID"] = gff.Byte(category.category_id)
        if _category_has_entries(source_dirs, palette_type, category.path):
            # Palette categories are represented as nested LIST fields; the depth
            # check above prevents a child path from appearing at multiple levels.
            fields["LIST"] = _build_palette_list(
                palette_type=palette_type,
                source_dirs=source_dirs,
                talk_table=talk_table,
                categories=categories,
                get_factions_root=get_factions_root,
                current_path=f"{category.path}/",
                depth=depth + 1,
            )
        fields["STRREF"] = gff.Dword(category.strref)
        entries.append(gff.Struct(0, **fields))

    objects: dict[str, PaletteObject] = {}
    for source_dir in source_dirs:
        resource_dir = source_dir / palette_type / current_path
        if not resource_dir.exists():
            continue
        for child in _iter_visible_palette_children(resource_dir):
            if child.is_dir():
                continue
            # Later source directories override earlier ones by resource name,
            # matching the module builder's last-source-wins resource precedence.
            palette_object = _read_palette_object(
                palette_type=palette_type,
                resource_path=child,
                talk_table=talk_table,
                factions_root=get_factions_root()
                if palette_type == "creature"
                else None,
            )
            objects[palette_object.resource_name] = palette_object

    for palette_object in sorted(objects.values(), key=lambda item: item.sort_name):
        fields = {"RESREF": gff.ResRef(palette_object.resource_name)}
        if palette_type == "creature":
            fields["CR"] = gff.Float(palette_object.challenge_rating or 0.0)
            fields["FACTION"] = gff.CExoString(palette_object.faction_name or "")
        if palette_object.strref is not None:
            fields["STRREF"] = gff.Dword(palette_object.strref)
        else:
            fields["NAME"] = gff.CExoString(palette_object.display_name or "")
        entries.append(gff.Struct(0, **fields))

    return gff.List(entries)


def _category_has_entries(
    source_dirs: list[Path], palette_type: str, category_path: str
) -> bool:
    """Return whether the category contains any filesystem entries in any source directory."""

    for source_dir in source_dirs:
        candidate = source_dir / palette_type / category_path
        if candidate.exists() and any(_iter_visible_palette_children(candidate)):
            return True
    return False


def _iter_visible_palette_children(path: Path):
    """Yield palette directory entries while ignoring macOS and other dotfiles."""

    for child in path.iterdir():
        if child.name.startswith(".") or child.name == "palette.txt":
            continue
        yield child


def _read_palette_object(
    *,
    palette_type: str,
    resource_path: Path,
    talk_table: TalkTable,
    factions_root,
) -> PaletteObject:
    """Read a resource GFF and extract the fields needed for its palette entry."""

    root, _ = read_gff(resource_path)
    name_field = NAME_FIELD_BY_TYPE[palette_type]

    challenge_rating = None
    faction_name = None
    if palette_type == "creature":
        challenge_rating = float(root["ChallengeRating"])
        faction_index = int(root["FactionID"])
        faction_name = str(factions_root["FactionList"][faction_index]["FactionName"])

    name_value = root[name_field]
    display_name, string_ref = _resolve_name(name_value, talk_table)
    resource_name = resource_path.name.rsplit(".", 1)[0].lower()
    sort_name = (
        display_name if display_name is not None else talk_table.text(string_ref or 0)
    )

    return PaletteObject(
        sort_name=sort_name,
        resource_name=resource_name,
        display_name=display_name,
        strref=string_ref,
        challenge_rating=challenge_rating,
        faction_name=faction_name,
    )


def _resolve_name(name_value, talk_table: TalkTable) -> tuple[str | None, int | None]:
    """Resolve a localized name field into either inline text or a TLK strref."""

    if isinstance(name_value, gff.CExoLocString):
        entries = normalize_locstring_entries(name_value.entries)
        if int(Language.ENGLISH) in entries:
            return entries[int(Language.ENGLISH)], None
        string_ref = int(name_value.strref)
        if string_ref != int(INVALID_STRREF):
            try:
                # A valid TLK reference is preferred over inline fallbacks because
                # the palette can store compact STRREF fields for those entries.
                talk_table.text(string_ref)
                return None, string_ref
            except ValueError:
                pass

        inline_text = _fallback_inline_locstring_text(entries, talk_table)
        if inline_text is not None:
            return inline_text, None

        talk_table.text(string_ref)
        return None, string_ref
    if isinstance(name_value, str):
        return name_value, None
    raise TypeError(f"Unsupported localized name field type: {type(name_value)!r}")


def _fallback_inline_locstring_text(
    entries: dict[int, str], talk_table: TalkTable
) -> str | None:
    """Pick a stable inline localized string when TLK lookup is unavailable.

    This keeps entry ``0`` and valid TLK-backed locstrings stable, but avoids
    aborting palette generation for resources that only provide inline
    localized text under non-zero entry IDs.
    """

    if not entries:
        return None

    talktable_language = int(talk_table.language)
    preferred_entry_ids = [
        talktable_language,
        *sorted(
            entry_id for entry_id in entries if (entry_id & 0xFF) == talktable_language
        ),
        *sorted(entries),
    ]

    seen_entry_ids: set[int] = set()
    for entry_id in preferred_entry_ids:
        # Some resources include language variants with duplicated low-byte
        # language IDs; the seen set keeps fallback selection deterministic.
        if entry_id in seen_entry_ids:
            continue
        seen_entry_ids.add(entry_id)
        text = entries.get(entry_id)
        if text:
            return text
    return None


def _load_factions(source_dirs: list[Path]):
    """Load the last ``repute.fac`` file across the source directories."""

    faction_path: Path | None = None
    for source_dir in source_dirs:
        candidate = source_dir / "repute.fac"
        if candidate.exists():
            faction_path = candidate
    if faction_path is None:
        raise FileNotFoundError("Unable to find faction reputation file (repute.fac).")
    root, _ = read_gff(faction_path)
    return root
