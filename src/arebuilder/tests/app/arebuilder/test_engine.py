from pathlib import Path

from arebuilder.app.arebuilder.engine import scan_included_files


def test_scan_included_files_uses_last_source_wins_by_basename(tmp_path: Path) -> None:
    """Verify included-resource scanning uses last-source-wins basename precedence."""

    first = tmp_path / "first"
    second = tmp_path / "second"
    build = tmp_path / "build"
    for directory in (first, second, build):
        directory.mkdir()

    (first / "shared.are").write_text("first", encoding="utf-8")
    (second / "shared.are").write_text("second", encoding="utf-8")
    (second / "settings.txt").write_text("ignored", encoding="utf-8")
    (build / "creaturepalcus.itp").write_text("palette", encoding="utf-8")

    included = scan_included_files([first, second], build)
    assert included["shared.are"] == second / "shared.are"
    assert "settings.txt" not in included
    assert included["creaturepalcus.itp"] == build / "creaturepalcus.itp"
