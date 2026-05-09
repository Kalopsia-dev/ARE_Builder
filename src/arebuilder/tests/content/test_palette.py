from pathlib import Path

from arebuilder.content.palette import _parse_palette_file, generate_palette
from arebuilder.nwn.compat import load_talk_table, read_gff
from arebuilder.tests.fixtures import create_synthetic_fixture


def test_creature_palette_uses_tlk_name_cr_and_faction(tmp_path: Path) -> None:
    """Verify that creature palette uses tlk name cr and faction."""

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    output_path = generate_palette(
        "creature",
        [fixture.are_resources_dir / "gff", fixture.module_resources_dir],
        fixture.build_dir,
        load_talk_table(fixture.talktable_path),
    )

    assert output_path is not None and output_path.exists()
    root, _ = read_gff(output_path)
    object_entries = list(_flatten_palette_objects(root["MAIN"]))
    beast = next(entry for entry in object_entries if str(entry["RESREF"]) == "beast")
    assert int(beast["STRREF"]) == 10
    assert float(beast["CR"]) == 5.0
    assert str(beast["FACTION"]) == "Hostile"


def test_palette_identifier_parsing_matches_supported_format(tmp_path: Path) -> None:
    """Verify that palette identifier parsing matches supported format."""

    palette_path = tmp_path / "palette.txt"
    palette_path.write_text(
        ". Root\n.51 Root/WithId\n6783.24 Root/Normal\n42 Root/Plain\n",
        encoding="utf-8",
    )

    categories = _parse_palette_file(palette_path)

    assert [
        (category.strref, category.category_id, category.path)
        for category in categories
    ] == [
        (0, None, "Root"),
        (0, 51, "Root/WithId"),
        (6783, 24, "Root/Normal"),
        (42, None, "Root/Plain"),
    ]


def test_palette_generation_ignores_dotfiles(tmp_path: Path) -> None:
    """Verify that palette generation ignores dotfiles."""

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    creature_dir = fixture.module_resources_dir / "creature" / "Creatures" / "Beasts"
    (creature_dir / ".DS_Store").write_text("macOS metadata", encoding="utf-8")

    output_path = generate_palette(
        "creature",
        [fixture.are_resources_dir / "gff", fixture.module_resources_dir],
        fixture.build_dir,
        load_talk_table(fixture.talktable_path),
    )

    assert output_path is not None and output_path.exists()
    root, _ = read_gff(output_path)
    object_entries = list(_flatten_palette_objects(root["MAIN"]))
    assert [str(entry["RESREF"]) for entry in object_entries] == ["beast"]


def _flatten_palette_objects(entries):
    """Yield palette object structs from a nested MAIN list."""

    for entry in entries:
        if "RESREF" in entry:
            yield entry
        elif "LIST" in entry:
            yield from _flatten_palette_objects(entry["LIST"])
