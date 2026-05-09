from pathlib import Path

from arebuilder.content.talktable import (
    CUSTOM_TLK_OFFSET,
    CustomContentError,
    CustomTalkTable,
    build_custom_content,
    load_label_rows,
)
from arebuilder.content.twoda import parse_2da_text
from arebuilder.nwn.compat import load_talk_table, read_erf_members
from arebuilder.tests.fixtures import create_synthetic_fixture


def test_load_label_rows_accepts_utf8_sig_and_keeps_last_duplicate(
    tmp_path: Path, capsys
) -> None:
    """Verify that load label rows accepts utf8 sig and keeps last duplicate."""

    json_path = tmp_path / "labels.json"
    json_path.write_text(
        """[
  {"id": 0, "Name": "First"},
  {"id": 0, "Name": "Second"}
]
""",
        encoding="utf-8-sig",
    )

    rows = load_label_rows(json_path)

    assert rows == {0: {"Name": "Second"}}
    captured = capsys.readouterr()
    assert "Duplicate entries for 2DA row(s): [0]" in captured.out


def test_load_label_rows_requires_id_column(tmp_path: Path) -> None:
    """Verify label JSON rows must include the ID column used as the 2DA row key."""

    json_path = tmp_path / "invalid.json"
    json_path.write_text('[{"Name": "Missing"}]\n', encoding="utf-8")

    try:
        load_label_rows(json_path)
    except ValueError as exc:
        assert "missing ID column" in str(exc)
    else:
        raise AssertionError("Expected missing id column to raise a ValueError.")


def test_custom_talk_table_reuses_blank_ids_from_json_seed(tmp_path: Path) -> None:
    """Verify that custom talk table reuses blank ids from json seed."""

    seed_path = tmp_path / "seed.json"
    seed_path.write_text(
        """{
  "language": 0,
  "entries": [
    {"id": 0, "text": "Zero"},
    {"id": 2, "text": "Two"}
  ]
}
""",
        encoding="utf-8",
    )

    table = CustomTalkTable.from_reference(seed_path)

    assert table.add("One") == CUSTOM_TLK_OFFSET + 1
    assert table.add("One") == CUSTOM_TLK_OFFSET + 1
    assert table.to_entries() == ["Zero", "One", "Two"]


def test_build_custom_content_warns_and_falls_back_for_missing_same_name_json(
    tmp_path: Path, capsys
) -> None:
    """
    Verify that build custom content warns and falls back for missing same name json.
    """

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    (fixture.custom_content_root / "Input json" / "classes.json").unlink()

    result = build_custom_content(
        custom_tlk_name="fixture",
        custom_content_root=fixture.custom_content_root,
        hak_dir=fixture.hak_dir,
        tlk_dir=fixture.tlk_dir,
    )

    assert result is not None
    captured = capsys.readouterr()
    assert "W: classes.2da: Missing Input json/classes.json." in captured.out

    members = read_erf_members(result.hak_path)
    classes_table = parse_2da_text(
        members["classes.2da"].decode("latin-1"),
        source_path=Path("classes.2da"),
        validate_index=False,
        warn_on_reindex=False,
    )
    class_row = classes_table.rows[0]
    assert class_row.values["Name"] == "****"
    assert class_row.values["Plural"] == "****"
    assert class_row.values["Lower"] == "****"


def test_build_custom_content_rejects_duplicate_2da_basename(tmp_path: Path) -> None:
    """Verify custom-content builds reject duplicate 2DA basenames."""

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    _write_test_2da(
        fixture.custom_content_root / "arelith_2da" / "classes.2da",
        columns="Label Value",
        rows=["0 STATIC 1"],
    )

    try:
        build_custom_content(
            custom_tlk_name="fixture",
            custom_content_root=fixture.custom_content_root,
            hak_dir=fixture.hak_dir,
            tlk_dir=fixture.tlk_dir,
        )
    except CustomContentError as exc:
        message = str(exc)
        assert message == "W: classes.2da: Appears in both Input 2das and arelith_2da."
    else:
        raise AssertionError("Expected duplicate 2DA basename to raise an error.")


def test_build_custom_content_warns_before_duplicate_2da_error(
    tmp_path: Path, capsys
) -> None:
    """Verify duplicate 2DA warnings are emitted before the fatal error."""

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    (fixture.custom_content_root / "Input json" / "classes.json").unlink()
    _write_test_2da(
        fixture.custom_content_root / "arelith_2da" / "classes.2da",
        columns="Label Value",
        rows=["0 STATIC 1"],
    )

    try:
        build_custom_content(
            custom_tlk_name="fixture",
            custom_content_root=fixture.custom_content_root,
            hak_dir=fixture.hak_dir,
            tlk_dir=fixture.tlk_dir,
        )
    except CustomContentError as exc:
        assert str(exc) == "W: classes.2da: Appears in both Input 2das and arelith_2da."
        captured = capsys.readouterr()
        assert "W: classes.2da: Missing Input json/classes.json." in captured.out
    else:
        raise AssertionError("Expected duplicate 2DA basename to raise an error.")


def test_build_custom_content_accepts_empty_same_name_json(tmp_path: Path) -> None:
    """Verify that build custom content accepts empty same name json."""

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    _write_test_2da(
        fixture.custom_content_root / "Input 2das" / "empty_labels.2da",
        columns="Label Value",
        rows=["0 ENTRY 1"],
    )
    (fixture.custom_content_root / "Input json" / "empty_labels.json").write_text(
        "[]\n",
        encoding="utf-8",
    )

    result = build_custom_content(
        custom_tlk_name="fixture",
        custom_content_root=fixture.custom_content_root,
        hak_dir=fixture.hak_dir,
        tlk_dir=fixture.tlk_dir,
    )

    assert result is not None
    members = read_erf_members(result.hak_path)
    assert "empty_labels.2da" in members


def test_build_custom_content_generates_expected_tlk_and_hak(tmp_path: Path) -> None:
    """Verify that build custom content generates expected tlk and hak."""

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    hak_dir = tmp_path / "explicit-output" / "hak"
    tlk_dir = tmp_path / "explicit-output" / "tlk"

    result = build_custom_content(
        custom_tlk_name="fixture",
        custom_content_root=fixture.custom_content_root,
        hak_dir=hak_dir,
        tlk_dir=tlk_dir,
    )

    assert result is not None
    assert result.tlk_path == tlk_dir / "fixture.tlk"
    assert result.hak_path == hak_dir / "fixture_hak.hak"
    assert not (fixture.tlk_dir / "fixture.tlk").exists()
    assert not (fixture.hak_dir / "fixture_hak.hak").exists()
    talk_table = load_talk_table(result.tlk_path)
    assert talk_table.entries[5000] == "Custom Spell"
    assert talk_table.entries[5001] == "Spell Description Zero"
    assert talk_table.entries[5003] == "Spell Description One"

    members = read_erf_members(result.hak_path)
    assert sorted(members) == [
        "classes.2da",
        "feat.2da",
        "iprp_feats.2da",
        "iprp_spells.2da",
        "racialtypes.2da",
        "spells.2da",
        "static_example.2da",
    ]

    classes_table = parse_2da_text(members["classes.2da"].decode("latin-1"))
    class_row = classes_table.rows[0]
    assert (
        talk_table.entries[int(class_row.values["Name"]) - CUSTOM_TLK_OFFSET]
        == "Wizard"
    )
    assert (
        talk_table.entries[int(class_row.values["Plural"]) - CUSTOM_TLK_OFFSET]
        == "Wizards"
    )
    assert (
        talk_table.entries[int(class_row.values["Lower"]) - CUSTOM_TLK_OFFSET]
        == "wizard"
    )

    racialtypes_table = parse_2da_text(members["racialtypes.2da"].decode("latin-1"))
    race_row = racialtypes_table.rows[0]
    assert talk_table.entries[int(race_row.values["Name"]) - CUSTOM_TLK_OFFSET] == "Elf"
    assert (
        talk_table.entries[int(race_row.values["ConverName"]) - CUSTOM_TLK_OFFSET]
        == "Elven"
    )
    assert (
        talk_table.entries[int(race_row.values["ConverNameLower"]) - CUSTOM_TLK_OFFSET]
        == "elven"
    )
    assert (
        talk_table.entries[int(race_row.values["NamePlural"]) - CUSTOM_TLK_OFFSET]
        == "Elves"
    )

    iprp_feats_table = parse_2da_text(members["iprp_feats.2da"].decode("latin-1"))
    feat_row = iprp_feats_table.rows[0]
    assert (
        talk_table.entries[int(feat_row.values["Name"]) - CUSTOM_TLK_OFFSET]
        == "Power Attack Display"
    )

    iprp_spells_table = parse_2da_text(members["iprp_spells.2da"].decode("latin-1"))
    iprp_spell_row = iprp_spells_table.rows[0]
    assert (
        talk_table.entries[int(iprp_spell_row.values["Name"]) - CUSTOM_TLK_OFFSET]
        == "Custom Spell (3)"
    )

    spells_table = parse_2da_text(members["spells.2da"].decode("latin-1"))
    spell_rows = {row.index: row for row in spells_table.rows}
    assert spell_rows[0].values["Name"] == str(CUSTOM_TLK_OFFSET + 5000)
    assert spell_rows[0].values["SpellDesc"] == str(CUSTOM_TLK_OFFSET + 5001)
    assert spell_rows[1].values["SpellDesc"] == str(CUSTOM_TLK_OFFSET + 5003)


def test_build_custom_content_limits_row_order_warning_scope(
    tmp_path: Path, capsys
) -> None:
    """Verify that build custom content limits row order warning scope."""

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    _write_test_2da(
        fixture.custom_content_root / "Input 2das" / "warning_scope.2da",
        columns="Label Value",
        rows=["1 FIRST one", "0 SECOND two"],
    )
    (fixture.custom_content_root / "Input json" / "warning_scope.json").write_text(
        "[]\n",
        encoding="utf-8",
    )
    _write_test_2da(
        fixture.custom_content_root / "arelith_2da" / "staticdesc.2da",
        columns="Label Value",
        rows=["1 STATIC one", "0 OLDER zero"],
    )

    result = build_custom_content(
        custom_tlk_name="fixture",
        custom_content_root=fixture.custom_content_root,
        hak_dir=fixture.hak_dir,
        tlk_dir=fixture.tlk_dir,
    )

    assert result is not None
    captured = capsys.readouterr()
    assert (
        "W: warning_scope.2da: Row indices stop ascending at row 0 "
        "(previous row 1). Reindexing..." in captured.out
    )
    assert "staticdesc.2da" not in captured.out

    members = read_erf_members(result.hak_path)
    warning_scope_table = parse_2da_text(members["warning_scope.2da"].decode("latin-1"))
    assert [row.index for row in warning_scope_table.rows] == [0, 1]

    static_descending_table = parse_2da_text(
        members["staticdesc.2da"].decode("latin-1"),
        source_path=Path("staticdesc.2da"),
        validate_index=False,
        warn_on_reindex=False,
    )
    assert [row.index for row in static_descending_table.rows] == [1, 0]


def _write_test_2da(path: Path, *, columns: str, rows: list[str]) -> None:
    """Write a compact test 2DA with caller-provided columns and row text."""

    path.write_text(
        "\n".join(["2DA V2.0", "", columns, *rows]) + "\n",
        encoding="latin-1",
    )
