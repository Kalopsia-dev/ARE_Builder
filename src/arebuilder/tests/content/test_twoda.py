from pathlib import Path

import pytest

from arebuilder.content.twoda import TwoDAError, load_2da, parse_2da_text, write_2da


@pytest.mark.parametrize(
    ("row", "actual_count"), [("007 ENTRY", 1), ("007", 0), ("007 ENTRY 1 EXTRA", 3)]
)
@pytest.mark.parametrize("validate_index", [True, False])
def test_parse_2da_rejects_wrong_cell_count_before_reindexing(
    row: str, actual_count: int, validate_index: bool
) -> None:
    """Reject incomplete or oversized rows without losing their source location."""

    with pytest.raises(TwoDAError) as exc_info:
        parse_2da_text(
            f"2DA V2.0\n\nDEFAULT: ****\n\nLabel Value\n8 FIRST 1\n\n{row}\n",
            source_path=Path("Custom content/table.2da"),
            validate_index=validate_index,
        )

    message = str(exc_info.value)
    assert "Custom content/table.2da:8:" in message
    assert "Row 007" in message
    assert f"{actual_count} values" in message
    assert "expected 2" in message


def test_load_2da_rejects_missing_cells(tmp_path: Path) -> None:
    """File loading must enforce the same strict shape checks as text parsing."""

    path = tmp_path / "short.2da"
    path.write_text("2DA V2.0\n\nLabel Value\n0 ENTRY\n", encoding="latin-1")

    with pytest.raises(TwoDAError) as exc_info:
        load_2da(path)

    assert f"{path}:4:" in str(exc_info.value)
    assert "Row 0" in str(exc_info.value)


def test_parse_2da_preserves_explicit_cells_with_default_and_blank_lines() -> None:
    """DEFAULT and blank lines must not change cell counts or explicit values."""

    table = parse_2da_text(
        '2DA V2.0\n\nDEFAULT: ****\n\nLabel Value\n'
        '\n0 FRAZ-URB\'LUU ****\n\n1 "Name With Spaces" ""\n'
        '2 ENTRY "escaped \\"quote\\" and \\\\ path"\n'
    )

    assert table.default_value == "****"
    assert [row.values for row in table.rows] == [
        {"Label": "FRAZ-URB'LUU", "Value": "****"},
        {"Label": "Name With Spaces", "Value": ""},
        {"Label": "ENTRY", "Value": 'escaped "quote" and \\ path'},
    ]


def test_parse_and_write_2da_preserves_quoted_values(tmp_path: Path) -> None:
    """Verify 2DA parsing and writing preserve quoted values."""

    source_text = """2DA V2.0

Label Name Description
0 ENTRY "Name With Spaces" "Quoted Description"
"""
    table = parse_2da_text(source_text)

    assert table.columns == ["Label", "Name", "Description"]
    assert table.rows[0].values["Name"] == "Name With Spaces"
    assert table.rows[0].values["Description"] == "Quoted Description"

    output_path = tmp_path / "quoted.2da"
    write_2da(output_path, table)

    written_text = output_path.read_text(encoding="latin-1")
    assert written_text.startswith("2DA V2.0\n\n")
    assert '"Name With Spaces"' in written_text
    assert '"Quoted Description"' in written_text


def test_load_2da_reindexes_non_monotonic_rows(tmp_path: Path, capsys) -> None:
    """Verify 2DA loading reindexes non-monotonic rows with a warning."""

    path = tmp_path / "unordered.2da"
    path.write_text(
        """2DA V2.0

Label Value
1 ONE 1
0 ZERO 0
""",
        encoding="latin-1",
    )

    table = load_2da(path)

    assert [row.index for row in table.rows] == [0, 1]
    captured = capsys.readouterr()
    assert (
        "Row indices stop ascending at row 0 (previous row 1). Reindexing..."
        in captured.out
    )


def test_load_2da_can_disable_row_order_validation(tmp_path: Path, capsys) -> None:
    """Verify 2DA loading can skip row-order validation."""

    path = tmp_path / "unordered.2da"
    path.write_text(
        """2DA V2.0

Label Value
1 ONE 1
0 ZERO 0
""",
        encoding="latin-1",
    )

    table = load_2da(path, validate_index=False)

    assert [row.index for row in table.rows] == [1, 0]
    captured = capsys.readouterr()
    assert captured.out == ""


def test_parse_2da_can_reindex_without_warning(capsys) -> None:
    """Verify 2DA parsing can reindex without emitting a warning."""

    source_text = """2DA V2.0

Label Value
1 ONE 1
0 ZERO 0
"""

    table = parse_2da_text(
        source_text,
        source_path=Path("unordered.2da"),
        warn_on_reindex=False,
    )

    assert [row.index for row in table.rows] == [0, 1]
    captured = capsys.readouterr()
    assert captured.out == ""


def test_parse_2da_treats_apostrophes_as_literal_characters() -> None:
    """Verify 2DA parsing treats apostrophes as literal characters."""

    source_text = """2DA V2.0

Label Name
249 DEITY_FRAZ-URB'LUU ****
"""

    table = parse_2da_text(source_text)

    assert table.rows[0].values["Label"] == "DEITY_FRAZ-URB'LUU"
    assert table.rows[0].values["Name"] == "****"
