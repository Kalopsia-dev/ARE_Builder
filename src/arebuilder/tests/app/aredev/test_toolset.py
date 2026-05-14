import shutil
from pathlib import Path

import pytest

import arebuilder.builder.module_dependencies as module_dependencies
from arebuilder.tests.app.aredev.helpers import (
    FAKE_WINDOWS_HOST_ROOT,
    assert_toolset_resource,
    make_controller,
    stage_toolset_sources,
    write_area,
)


def test_toolset_requires_built_module(tmp_path: Path) -> None:
    """Verify toolset bundling fails clearly before resources are linked."""

    output: list[str] = []
    controller, _, build_calls, runner = make_controller(
        tmp_path,
        output=output.append,
    )

    assert controller.run("toolset", []) == 1

    assert output == ["You must build the module first."]
    assert build_calls == []
    assert runner.calls == []


def test_toolset_creates_bundle_in_nwn_home_modules(tmp_path: Path) -> None:
    """Verify toolset links resources by basename and copies the built module."""

    nwn_home = tmp_path / "nwn-home"
    output: list[str] = []
    controller, layout, build_calls, runner = make_controller(
        tmp_path,
        output=output.append,
        nwn_home=nwn_home,
    )
    sources = stage_toolset_sources(layout)

    assert controller.run("toolset", []) == 0

    module_dir = nwn_home / "modules" / "are-dev-pgcc"
    assert_toolset_resource(module_dir / "shared_only.are", sources["shared_only"])
    assert_toolset_resource(module_dir / "shared.are", sources["target_shared"])
    assert_toolset_resource(module_dir / "include.nss", sources["script"])
    assert_toolset_resource(module_dir / "target.utc", sources["target"])
    assert_toolset_resource(module_dir / "module.ifo", sources["compiled"])
    assert (nwn_home / "modules" / "are-dev-pgcc.mod").read_bytes() == b"module"
    assert output == [
        "Planning symlinks...",
        "Toolset bundle ready.",
    ]
    assert build_calls == []
    assert runner.calls == []


def test_toolset_prunes_obsolete_resource_links(tmp_path: Path) -> None:
    """Verify Toolset symlink bundles prune resource links absent from the new plan."""

    nwn_home = tmp_path / "nwn-home"
    controller, layout, _, _ = make_controller(tmp_path, nwn_home=nwn_home)
    stage_toolset_sources(layout)
    module_dir = nwn_home / "modules" / "are-dev-pgcc"
    module_dir.mkdir(parents=True)
    stale_link = module_dir / "removed.utc"
    stale_link.symlink_to(
        (layout.target_resources_dir("pgcc") / "removed.utc").resolve()
    )
    moved_link = module_dir / "shared_only.are"
    moved_link.symlink_to(
        (layout.are_resources_dir / "gff" / "old" / "shared_only.are").resolve()
    )
    script_duplicate = layout.are_resources_dir / "scripts" / "duplicate.are"
    script_duplicate.write_text("script duplicate", encoding="utf-8")
    gff_duplicate = layout.are_resources_dir / "gff" / "duplicate.are"
    write_area(gff_duplicate)
    duplicate_link = module_dir / "duplicate.are"
    duplicate_link.symlink_to(script_duplicate.resolve())
    unrelated_link = module_dir / "manual.utc"
    unrelated_link.symlink_to((tmp_path / "external" / "manual.utc").resolve())

    assert controller.run("toolset", []) == 0

    assert not stale_link.is_symlink()
    assert_toolset_resource(
        moved_link,
        layout.are_resources_dir / "gff" / "shared_only.are",
    )
    assert_toolset_resource(duplicate_link, script_duplicate)
    assert unrelated_link.is_symlink()


def test_toolset_prunes_obsolete_links_when_source_root_was_removed(
    tmp_path: Path,
) -> None:
    """Verify stale Toolset links are pruned even when their source root is gone."""

    nwn_home = tmp_path / "nwn-home"
    controller, layout, _, _ = make_controller(tmp_path, nwn_home=nwn_home)
    stage_toolset_sources(layout)
    module_dir = nwn_home / "modules" / "are-dev-pgcc"
    module_dir.mkdir(parents=True)
    target_root = layout.target_resources_dir("pgcc")
    stale_link = module_dir / "removed.utc"
    stale_link.symlink_to((target_root / "removed.utc").resolve())
    shutil.rmtree(target_root)

    assert controller.run("toolset", []) == 0

    assert not stale_link.is_symlink()


def test_toolset_omits_areas_with_unavailable_tilesets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify Toolset links skip areas whose tilesets are not available."""

    monkeypatch.setattr(
        module_dependencies,
        "_tileset_resources_from_nwn_install",
        lambda _nwn_root: _tileset_index({"ttr01.set"}, {"ttr01.set": 2}),
    )
    nwn_home = tmp_path / "nwn-home"
    controller, layout, _, _ = make_controller(tmp_path, nwn_home=nwn_home)
    stage_toolset_sources(layout)
    target_dir = layout.target_resources_dir("pgcc") / "nested"
    available_area = target_dir / "available_area.are"
    missing_area = target_dir / "missing_area.are"
    bad_tile_area = target_dir / "bad_tile_area.are"
    write_area(available_area, "ttr01")
    write_area(missing_area, "missing01")
    write_area(bad_tile_area, "ttr01", tile_ids=[2])
    (target_dir / "missing_area.git").write_bytes(b"git")
    (target_dir / "missing_area.gic").write_bytes(b"gic")
    (target_dir / "bad_tile_area.git").write_bytes(b"git")
    (target_dir / "bad_tile_area.gic").write_bytes(b"gic")

    assert controller.run("toolset", []) == 0

    module_dir = nwn_home / "modules" / "are-dev-pgcc"
    assert_toolset_resource(module_dir / "available_area.are", available_area)
    assert not (module_dir / "missing_area.are").exists()
    assert not (module_dir / "missing_area.git").exists()
    assert not (module_dir / "missing_area.gic").exists()
    assert not (module_dir / "bad_tile_area.are").exists()
    assert not (module_dir / "bad_tile_area.git").exists()
    assert not (module_dir / "bad_tile_area.gic").exists()
    output = capsys.readouterr().out
    assert (
        "W: missing_area: Tileset missing01.set is unavailable; omitting area."
        in output
    )
    assert (
        "W: bad_tile_area: Tile with ID 2 is unavailable; omitting area. "
        "Please update the HAK that provides ttr01.set."
        in output
    )


def test_toolset_copy_mode_prunes_obsolete_manifest_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Windows Docker Toolset copies prune stale managed files."""

    host_modules = tmp_path / "host-modules"
    controller, layout, _, _ = make_controller(tmp_path)
    stage_toolset_sources(layout)
    old_resource = layout.target_resources_dir("pgcc") / "old.utc"
    old_resource.write_text("old", encoding="utf-8")
    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    monkeypatch.setenv("AREDEV_HOST_ROOT", FAKE_WINDOWS_HOST_ROOT)
    monkeypatch.setenv("AREDEV_NWN_HOME_MODULES_ROOT", str(host_modules))

    assert controller.run("toolset", []) == 0
    copied_old_resource = host_modules / "are-dev-pgcc" / "old.utc"
    assert copied_old_resource.exists()

    old_resource.unlink()

    assert controller.run("toolset", []) == 0
    assert not copied_old_resource.exists()


def test_toolset_copy_mode_prunes_omitted_area_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify copy-mode Toolset bundles remove newly omitted area resources."""

    available_tilesets = {"missing01.set"}
    monkeypatch.setattr(
        module_dependencies,
        "_tileset_resources_from_nwn_install",
        lambda _nwn_root: _tileset_index(set(available_tilesets)),
    )
    host_modules = tmp_path / "host-modules"
    controller, layout, _, _ = make_controller(tmp_path)
    stage_toolset_sources(layout)
    target_dir = layout.target_resources_dir("pgcc") / "nested"
    missing_area = target_dir / "missing_area.are"
    write_area(missing_area, "missing01")
    (target_dir / "missing_area.git").write_bytes(b"git")
    (target_dir / "missing_area.gic").write_bytes(b"gic")
    monkeypatch.setenv("AREDEV_IN_CONTAINER", "1")
    monkeypatch.setenv("AREDEV_HOST_ROOT", FAKE_WINDOWS_HOST_ROOT)
    monkeypatch.setenv("AREDEV_NWN_HOME_MODULES_ROOT", str(host_modules))

    assert controller.run("toolset", []) == 0
    copied_area = host_modules / "are-dev-pgcc" / "missing_area.are"
    copied_git = host_modules / "are-dev-pgcc" / "missing_area.git"
    copied_gic = host_modules / "are-dev-pgcc" / "missing_area.gic"
    assert copied_area.exists()
    assert copied_git.exists()
    assert copied_gic.exists()

    available_tilesets.clear()

    assert controller.run("toolset", []) == 0
    assert not copied_area.exists()
    assert not copied_git.exists()
    assert not copied_gic.exists()


def test_toolset_bad_argument_reports_usage(tmp_path: Path) -> None:
    """Verify invalid toolset arguments report usage without running workflows."""

    output: list[str] = []
    controller, _, build_calls, runner = make_controller(tmp_path, output=output.append)

    assert controller.run("toolset", ["open"]) == 1

    assert build_calls == []
    assert runner.calls == []
    assert any("Usage: toolset [run]" in message for message in output)


def _tileset_index(
    resources: set[str],
    tile_counts: dict[str, int] | None = None,
):
    return module_dependencies._TilesetResourceIndex(
        resources=set(resources),
        tile_counts=dict(tile_counts or {}),
    )
