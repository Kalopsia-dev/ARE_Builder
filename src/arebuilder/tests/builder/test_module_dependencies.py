from pathlib import Path

from nwn import gff

import arebuilder.builder.module_dependencies as module_dependencies
from arebuilder.builder.module_dependencies import (
    discover_available_tilesets,
    filter_area_tileset_dependencies,
)
from arebuilder.config.module_settings import parse_settings_text
from arebuilder.nwn.compat import write_erf_archive, write_gff


def test_filter_area_tileset_dependencies_omits_missing_tileset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify areas using unavailable custom tilesets are removed from module.ifo input."""

    monkeypatch.setattr(
        module_dependencies,
        "_tileset_resources_from_nwn_install",
        lambda _nwn_root: _tileset_index({"ttr01.set"}),
    )
    hak_dir = tmp_path / "hak"
    area_dir = tmp_path / "areas"
    hak_dir.mkdir()
    area_dir.mkdir()

    _write_hak(hak_dir / "custom_tiles.hak", ["custom01.set"])
    _write_area(area_dir / "base_area.are", "ttr01")
    _write_area(area_dir / "custom_area.are", "custom01")
    _write_area(area_dir / "missing_area.are", "missing01")
    settings = _settings("custom_tiles")

    report = filter_area_tileset_dependencies(
        settings,
        {
            "base_area.are": area_dir / "base_area.are",
            "custom_area.are": area_dir / "custom_area.are",
            "missing_area.are": area_dir / "missing_area.are",
            "script.nss": area_dir / "script.nss",
        },
        hak_dir=hak_dir,
        nwn_root=tmp_path / "missing-install",
    )

    assert sorted(report.included_files) == [
        "base_area.are",
        "custom_area.are",
        "script.nss",
    ]
    assert [omission.area_name for omission in report.omitted_areas] == ["missing_area"]
    assert report.omitted_areas[0].tileset == "missing01"


def test_filter_area_tileset_dependencies_omits_unavailable_tile_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify areas are removed when their tileset exists but a tile id does not."""

    monkeypatch.setattr(
        module_dependencies,
        "_tileset_resources_from_nwn_install",
        lambda _nwn_root: _tileset_index({"ttr01.set"}, {"ttr01.set": 2}),
    )
    hak_dir = tmp_path / "hak"
    area_dir = tmp_path / "areas"
    hak_dir.mkdir()
    area_dir.mkdir()
    _write_area(area_dir / "valid_area.are", "ttr01", tile_ids=[0, 1])
    _write_area(area_dir / "bad_area.are", "ttr01", tile_ids=[2])

    report = filter_area_tileset_dependencies(
        _settings(""),
        {
            "valid_area.are": area_dir / "valid_area.are",
            "bad_area.are": area_dir / "bad_area.are",
        },
        hak_dir=hak_dir,
        nwn_root=tmp_path / "nwn",
    )

    assert sorted(report.included_files) == ["valid_area.are"]
    assert [omission.area_name for omission in report.omitted_areas] == ["bad_area"]
    assert report.omitted_areas[0].reason == "tile_index"
    assert report.omitted_areas[0].required_tile_id == 2
    assert report.omitted_areas[0].available_tile_count == 2


def test_filter_area_tileset_dependencies_records_tile_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify tile-id omissions name the enabled HAK providing the tileset."""

    monkeypatch.setattr(
        module_dependencies,
        "_tileset_resources_from_nwn_install",
        lambda _nwn_root: _tileset_index({"ttr01.set"}),
    )
    hak_dir = tmp_path / "hak"
    area_dir = tmp_path / "areas"
    hak_dir.mkdir()
    area_dir.mkdir()
    _write_hak(
        hak_dir / "custom_tiles.hak",
        ["custom01.set"],
        tile_counts={"custom01.set": 2},
    )
    _write_area(area_dir / "bad_area.are", "custom01", tile_ids=[2])

    report = filter_area_tileset_dependencies(
        _settings("custom_tiles"),
        {"bad_area.are": area_dir / "bad_area.are"},
        hak_dir=hak_dir,
        nwn_root=tmp_path / "nwn",
    )

    assert [omission.area_name for omission in report.omitted_areas] == ["bad_area"]
    assert report.omitted_areas[0].tileset_provider == "custom_tiles.hak"


def test_filter_area_tileset_dependencies_keeps_provider_with_selected_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify duplicate tilesets do not name a lower-count HAK as provider."""

    monkeypatch.setattr(
        module_dependencies,
        "_tileset_resources_from_nwn_install",
        lambda _nwn_root: _tileset_index({"ttr01.set"}),
    )
    hak_dir = tmp_path / "hak"
    area_dir = tmp_path / "areas"
    hak_dir.mkdir()
    area_dir.mkdir()
    _write_hak(
        hak_dir / "larger_tiles.hak",
        ["custom01.set"],
        tile_counts={"custom01.set": 5},
    )
    _write_hak(
        hak_dir / "smaller_tiles.hak",
        ["custom01.set"],
        tile_counts={"custom01.set": 2},
    )
    _write_area(area_dir / "bad_area.are", "custom01", tile_ids=[5])

    report = filter_area_tileset_dependencies(
        _settings("larger_tiles smaller_tiles"),
        {"bad_area.are": area_dir / "bad_area.are"},
        hak_dir=hak_dir,
        nwn_root=tmp_path / "nwn",
    )

    omission = report.omitted_areas[0]
    assert omission.available_tile_count == 5
    assert omission.tileset_provider == "larger_tiles.hak"


def test_filter_area_tileset_dependencies_keeps_areas_when_base_install_unknown(
    tmp_path: Path,
) -> None:
    """Verify areas are preserved when base tilesets cannot be inspected."""

    hak_dir = tmp_path / "hak"
    area_dir = tmp_path / "areas"
    hak_dir.mkdir()
    area_dir.mkdir()
    _write_area(area_dir / "base_area.are", "ttr01")

    report = filter_area_tileset_dependencies(
        _settings(""),
        {"base_area.are": area_dir / "base_area.are"},
        hak_dir=hak_dir,
        nwn_root=tmp_path / "missing-install",
    )

    assert sorted(report.included_files) == ["base_area.are"]
    assert report.omitted_areas == ()
    assert not report.availability.base_tilesets_known


def test_discover_available_tilesets_reports_missing_haks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify missing enabled HAKs are reported while readable HAKs are indexed."""

    monkeypatch.setattr(
        module_dependencies,
        "_tileset_resources_from_nwn_install",
        lambda _nwn_root: _tileset_index({"ttr01.set"}),
    )
    hak_dir = tmp_path / "hak"
    hak_dir.mkdir()
    _write_hak(
        hak_dir / "present.hak",
        ["custom01.set"],
        tile_counts={"custom01.set": 1},
    )

    availability = discover_available_tilesets(
        ["present", "missing"],
        hak_dir=hak_dir,
        nwn_root=None,
    )

    assert "custom01.set" in availability.resources
    assert availability.tileset_providers["custom01.set"] == "present.hak"
    assert "ttr01.set" in availability.resources
    assert [issue.hak_name for issue in availability.missing_haks] == ["missing"]


def _settings(haks: str):
    return parse_settings_text(
        f"""
        name Test Module
        tag TEST_MODULE
        haks {haks}
        entry_area base_area
        entry_x 0.0
        entry_y 0.0
        entry_z 0.0
        entry_facing 0.0
        """,
        Path("settings.txt"),
    )


def _write_area(path: Path, tileset: str, tile_ids: list[int] | None = None) -> None:
    fields = {"Tileset": gff.ResRef(tileset)}
    if tile_ids is not None:
        fields["Tile_List"] = gff.List(
            [gff.Struct(0, Tile_ID=gff.Int(tile_id)) for tile_id in tile_ids]
        )
    write_gff(
        path,
        gff.Struct(0xFFFFFFFF, **fields),
        "ARE ",
    )


def _write_hak(
    path: Path,
    filenames: list[str],
    tile_counts: dict[str, int] | None = None,
) -> None:
    write_erf_archive(
        path,
        b"HAK ",
        [
            (
                filename,
                _tileset_set(tile_counts[filename])
                if tile_counts and filename in tile_counts
                else b"placeholder",
            )
            for filename in filenames
        ],
    )


def _tileset_set(tile_count: int) -> bytes:
    return "\n".join(f"[TILE{tile_id}]" for tile_id in range(tile_count)).encode(
        "latin-1"
    )


def _tileset_index(
    resources: set[str],
    tile_counts: dict[str, int] | None = None,
):
    return module_dependencies._TilesetResourceIndex(
        resources=set(resources),
        tile_counts=dict(tile_counts or {}),
    )
