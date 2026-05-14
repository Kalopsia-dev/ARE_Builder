from pathlib import Path

from arebuilder.app.arebuilder.engine import (
    _print_area_dependency_warnings,
    scan_included_files,
)
from arebuilder.builder.module_dependencies import (
    AreaDependencyReport,
    AreaTilesetOmission,
    TilesetAvailability,
)
from arebuilder.config.module_settings import parse_settings_text


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


def test_area_dependency_warning_prints_one_line_per_omitted_area(
    tmp_path: Path,
    capsys,
) -> None:
    """Verify missing tileset warnings stay concise and specific."""

    settings = parse_settings_text(
        """
        name Test Module
        tag TEST_MODULE
        entry_area start
        entry_x 0.0
        entry_y 0.0
        entry_z 0.0
        entry_facing 0.0
        """,
        Path("settings.txt"),
    )
    report = AreaDependencyReport(
        included_files={},
        omitted_areas=(
            AreaTilesetOmission(
                area_name="test_area",
                area_path=tmp_path / "test_area.are",
                tileset="aef19",
            ),
        ),
        availability=TilesetAvailability(resources=frozenset()),
    )

    _print_area_dependency_warnings(settings, report)

    assert capsys.readouterr().out == (
        "W: test_area: Tileset aef19.set is unavailable; omitting area.\n"
    )


def test_area_dependency_warning_prints_unavailable_tile_id(
    tmp_path: Path,
    capsys,
) -> None:
    """Verify outdated tileset warnings identify the unavailable tile id."""

    settings = parse_settings_text(
        """
        name Test Module
        tag TEST_MODULE
        entry_area start
        entry_x 0.0
        entry_y 0.0
        entry_z 0.0
        entry_facing 0.0
        """,
        Path("settings.txt"),
    )
    report = AreaDependencyReport(
        included_files={},
        omitted_areas=(
            AreaTilesetOmission(
                area_name="test_area",
                area_path=tmp_path / "test_area.are",
                tileset="tcn01",
                reason="tile_index",
                required_tile_id=408,
                available_tile_count=408,
            ),
        ),
        availability=TilesetAvailability(resources=frozenset()),
    )

    _print_area_dependency_warnings(settings, report)

    assert capsys.readouterr().out == (
        "W: test_area: Tile 408 is unavailable in tcn01.set; omitting area.\n"
    )


def test_area_dependency_warning_prints_tile_provider(
    tmp_path: Path,
    capsys,
) -> None:
    """Verify outdated tileset warnings name the enabled provider when known."""

    settings = parse_settings_text(
        """
        name Test Module
        tag TEST_MODULE
        entry_area start
        entry_x 0.0
        entry_y 0.0
        entry_z 0.0
        entry_facing 0.0
        """,
        Path("settings.txt"),
    )
    report = AreaDependencyReport(
        included_files={},
        omitted_areas=(
            AreaTilesetOmission(
                area_name="test_area",
                area_path=tmp_path / "test_area.are",
                tileset="custom01",
                reason="tile_index",
                required_tile_id=2,
                available_tile_count=2,
                tileset_provider="custom_tiles.hak",
            ),
        ),
        availability=TilesetAvailability(resources=frozenset()),
    )

    _print_area_dependency_warnings(settings, report)

    assert capsys.readouterr().out == (
        "W: test_area: Tile 2 is unavailable in custom01.set; omitting area. "
        "Please update custom_tiles.hak.\n"
    )
