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
        lambda _nwn_root: {"ttr01.set"},
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
        lambda _nwn_root: {"ttr01.set"},
    )
    hak_dir = tmp_path / "hak"
    hak_dir.mkdir()
    _write_hak(hak_dir / "present.hak", ["custom01.set"])

    availability = discover_available_tilesets(
        ["present", "missing"],
        hak_dir=hak_dir,
        nwn_root=None,
    )

    assert "custom01.set" in availability.resources
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


def _write_area(path: Path, tileset: str) -> None:
    write_gff(
        path,
        gff.Struct(0xFFFFFFFF, Tileset=gff.ResRef(tileset)),
        "ARE ",
    )


def _write_hak(path: Path, filenames: list[str]) -> None:
    write_erf_archive(
        path,
        b"HAK ",
        [(filename, b"placeholder") for filename in filenames],
    )
