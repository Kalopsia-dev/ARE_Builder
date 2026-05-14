from pathlib import Path

import pytest

import arebuilder.builder.symlinks as symlink_module
from arebuilder.builder.symlinks import (
    PlannedSymlink,
    apply_symlink_plan,
    count_symlink_plan_steps,
    plan_symlinks_for_all,
    plan_symlinks_for_target,
    prune_stale_symlinks_for_all,
    prune_stale_symlinks_for_target,
)


def _assert_applied_resource_link(
    path: Path,
    *,
    target_path: str | None = None,
) -> None:
    """Assert one planned resource link was materialized for the current platform."""

    if path.is_symlink():
        if target_path is None:
            return
        assert path.readlink().as_posix() == target_path
        return
    assert path.exists()
    assert path.is_file()


def _planned_target_path(plans: list[PlannedSymlink], name: str) -> str:
    """Return the planned target path for one destination basename."""

    return next(item for item in plans if item.link_path.name == name).target_path


def _last_planned_target_path(plans: list[PlannedSymlink], name: str) -> str:
    """Return the last planned target path for one destination basename."""

    return [item for item in plans if item.link_path.name == name][-1].target_path


def test_symlink_plans_match_command_modes(tmp_path: Path) -> None:
    """Verify all/link symlink plans use the correct overwrite and source-root policies."""

    shared_root = tmp_path / "are-resources"
    compiled_root = tmp_path / "compiled-resources"
    builder_root = tmp_path / "builder-root"
    override_dir = tmp_path / "override"
    for directory in (
        shared_root / "gff" / "areas",
        shared_root / "override",
        compiled_root / "are-dev-test",
        builder_root / "test-resources" / "scripts",
        builder_root / "test-resources" / "creature",
        override_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    (shared_root / "gff" / "areas" / "shared.are").write_text(
        "shared", encoding="utf-8"
    )
    (shared_root / "override" / "shared_override.2da").write_text(
        "override", encoding="utf-8"
    )
    (compiled_root / "global_script.ncs").write_text("compiled", encoding="utf-8")
    (compiled_root / "are-dev-test" / "module.ifo").write_text(
        "ifo",
        encoding="utf-8",
    )
    (builder_root / "test-resources" / "notes.txt").write_text(
        "ignore", encoding="utf-8"
    )
    (builder_root / "test-resources" / "scripts" / "ignored.nss").write_text(
        "ignore", encoding="utf-8"
    )
    (builder_root / "test-resources" / "creature" / "beast.utc").write_text(
        "creature", encoding="utf-8"
    )

    all_plan = plan_symlinks_for_all(
        override_dir=override_dir,
        shared_root=shared_root,
        compiled_root=compiled_root,
    )
    apply_symlink_plan(all_plan)
    _assert_applied_resource_link(override_dir / "shared.are")
    _assert_applied_resource_link(override_dir / "shared_override.2da")
    _assert_applied_resource_link(override_dir / "global_script.ncs")

    link_plan = plan_symlinks_for_target(
        target_name="are-dev-test",
        override_dir=override_dir,
        builder_root=builder_root,
        compiled_root=compiled_root,
    )
    apply_symlink_plan(link_plan)
    _assert_applied_resource_link(override_dir / "beast.utc")
    _assert_applied_resource_link(override_dir / "module.ifo")
    assert not (override_dir / "notes.txt").exists()
    assert not (override_dir / "ignored.nss").exists()


def test_link_application_falls_back_to_hardlinks_without_symlink_privilege(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify link application can avoid admin-only symlink creation."""

    source = tmp_path / "resources" / "beast.utc"
    override_dir = tmp_path / "override"
    source.parent.mkdir(parents=True)
    source.write_text("creature", encoding="utf-8")

    def reject_symlink(*_args):
        raise OSError("privilege not held")

    monkeypatch.setattr(symlink_module.os, "symlink", reject_symlink)

    apply_symlink_plan(
        [
            PlannedSymlink(
                link_path=override_dir / source.name,
                target_path="/var/builder/test-resources/creature/beast.utc",
                overwrite=True,
                source_path=source,
            )
        ]
    )

    linked = override_dir / source.name
    assert linked.is_file()
    assert not linked.is_symlink()
    assert linked.samefile(source)


def test_apply_symlink_plan_reports_progress_per_deduplicated_item(
    tmp_path: Path,
) -> None:
    """Verify symlink application can report incremental progress."""

    source = tmp_path / "resources" / "beast.utc"
    override_dir = tmp_path / "override"
    source.parent.mkdir(parents=True)
    source.write_text("creature", encoding="utf-8")
    duplicate_source = tmp_path / "resources" / "replacement.utc"
    duplicate_source.write_text("replacement", encoding="utf-8")
    plan = [
        PlannedSymlink(
            link_path=override_dir / source.name,
            target_path="/var/builder/test-resources/creature/beast.utc",
            overwrite=False,
            source_path=source,
        ),
        PlannedSymlink(
            link_path=override_dir / source.name,
            target_path="/var/builder/test-resources/creature/replacement.utc",
            overwrite=False,
            source_path=duplicate_source,
        ),
    ]
    progress: list[int] = []

    assert count_symlink_plan_steps(plan) == 1

    apply_symlink_plan(plan, progress=progress.append)

    assert progress == [1]


def test_link_application_copies_when_hardlink_fallback_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify link application still succeeds when hard links are unavailable."""

    source = tmp_path / "resources" / "beast.utc"
    override_dir = tmp_path / "override"
    source.parent.mkdir(parents=True)
    source.write_text("creature", encoding="utf-8")

    def reject_symlink(*_args):
        raise OSError("privilege not held")

    def reject_hardlink(*_args):
        raise OSError("cross-device link")

    monkeypatch.setattr(symlink_module.os, "symlink", reject_symlink)
    monkeypatch.setattr(symlink_module.os, "link", reject_hardlink)

    apply_symlink_plan(
        [
            PlannedSymlink(
                link_path=override_dir / source.name,
                target_path="/var/builder/test-resources/creature/beast.utc",
                overwrite=True,
                source_path=source,
            )
        ]
    )

    linked = override_dir / source.name
    assert linked.is_file()
    assert not linked.is_symlink()
    assert linked.read_text(encoding="utf-8") == "creature"
    assert not linked.samefile(source)


def test_all_mode_duplicate_basenames_keep_first_link(tmp_path: Path) -> None:
    """Verify that all mode duplicate basenames keep first link."""

    override_dir = tmp_path / "override"
    shared_root = tmp_path / "are-resources"
    compiled_root = tmp_path / "compiled-resources"
    for directory in (
        override_dir,
        shared_root / "gff" / "creature" / "NPCs" / "Elves",
        shared_root / "gff" / "creature" / "Wizards",
        shared_root / "override",
        compiled_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    first_resource = (
        shared_root / "gff" / "creature" / "NPCs" / "Elves" / "duplicate.utc"
    )
    second_resource = shared_root / "gff" / "creature" / "Wizards" / "duplicate.utc"
    first_resource.write_text("first", encoding="utf-8")
    second_resource.write_text("second", encoding="utf-8")

    plan = plan_symlinks_for_all(
        override_dir=override_dir,
        shared_root=shared_root,
        compiled_root=compiled_root,
    )
    expected_target = "/var/builder/are-resources/gff/creature/NPCs/Elves/duplicate.utc"
    assert _planned_target_path(plan, "duplicate.utc") == expected_target

    apply_symlink_plan(plan)
    _assert_applied_resource_link(
        override_dir / "duplicate.utc",
        target_path=expected_target,
    )


def test_link_mode_duplicate_basenames_keep_last_link(tmp_path: Path) -> None:
    """Verify that link mode duplicate basenames keep last link."""

    override_dir = tmp_path / "override"
    builder_root = tmp_path / "builder-root"
    compiled_root = tmp_path / "compiled-resources"
    for directory in (
        override_dir,
        builder_root / "test-resources" / "creature" / "Elves",
        builder_root / "test-resources" / "creature" / "Wizards",
        compiled_root / "are-dev-test",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    first_resource = (
        builder_root / "test-resources" / "creature" / "Elves" / "duplicate.utc"
    )
    second_resource = (
        builder_root / "test-resources" / "creature" / "Wizards" / "duplicate.utc"
    )
    first_resource.write_text("first", encoding="utf-8")
    second_resource.write_text("second", encoding="utf-8")

    plan = plan_symlinks_for_target(
        target_name="are-dev-test",
        override_dir=override_dir,
        builder_root=builder_root,
        compiled_root=compiled_root,
    )
    expected_target = "/var/builder/test-resources/creature/Wizards/duplicate.utc"
    assert _last_planned_target_path(plan, "duplicate.utc") == expected_target

    apply_symlink_plan(plan)
    _assert_applied_resource_link(
        override_dir / "duplicate.utc",
        target_path=expected_target,
    )


def test_target_prune_removes_only_obsolete_owned_links(tmp_path: Path) -> None:
    """Verify target pruning removes stale links without touching unrelated resources."""

    override_dir = tmp_path / "override"
    builder_root = tmp_path / "builder-root"
    compiled_root = tmp_path / "compiled-resources"
    current_source = builder_root / "test-resources" / "creature" / "current.utc"
    current_compiled = compiled_root / "are-dev-test" / "current.ncs"
    for directory in (
        override_dir,
        current_source.parent,
        current_compiled.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    current_source.write_text("current", encoding="utf-8")
    current_compiled.write_text("compiled", encoding="utf-8")

    stale_source_link = override_dir / "stale.utc"
    stale_source_link.symlink_to("/var/builder/test-resources/creature/stale.utc")
    stale_compiled_link = override_dir / "stale.ncs"
    stale_compiled_link.symlink_to(
        "/var/builder/compiled-resources/are-dev-test/stale.ncs"
    )
    shared_link = override_dir / "shared.utc"
    shared_link.symlink_to("/var/builder/are-resources/gff/creature/shared.utc")
    other_target_link = override_dir / "other.utc"
    other_target_link.symlink_to("/var/builder/other-resources/creature/other.utc")
    manual_file = override_dir / "manual.utc"
    manual_file.write_text("manual", encoding="utf-8")

    plans = plan_symlinks_for_target(
        target_name="are-dev-test",
        override_dir=override_dir,
        builder_root=builder_root,
        compiled_root=compiled_root,
    )

    removed = prune_stale_symlinks_for_target(
        target_name="are-dev-test",
        override_dir=override_dir,
        builder_root=builder_root,
        compiled_root=compiled_root,
        active_plans=plans,
    )

    assert removed == 2
    assert not stale_source_link.is_symlink()
    assert not stale_compiled_link.is_symlink()
    assert shared_link.is_symlink()
    assert other_target_link.is_symlink()
    assert manual_file.exists()


def test_all_prune_removes_only_obsolete_shared_links(tmp_path: Path) -> None:
    """Verify all-mode pruning removes stale shared links but preserves target links."""

    override_dir = tmp_path / "override"
    shared_root = tmp_path / "are-resources"
    compiled_root = tmp_path / "compiled-resources"
    current_gff = shared_root / "gff" / "area" / "current.are"
    moved_gff = shared_root / "gff" / "new" / "moved.are"
    duplicate_gff = shared_root / "gff" / "area" / "duplicate.are"
    duplicate_override = shared_root / "override" / "duplicate.are"
    current_compiled = compiled_root / "current.ncs"
    target_compiled = compiled_root / "are-dev-test" / "target.ncs"
    for directory in (
        override_dir,
        current_gff.parent,
        moved_gff.parent,
        duplicate_override.parent,
        current_compiled.parent,
        target_compiled.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    current_gff.write_text("area", encoding="utf-8")
    moved_gff.write_text("moved", encoding="utf-8")
    duplicate_gff.write_text("gff duplicate", encoding="utf-8")
    duplicate_override.write_text("override duplicate", encoding="utf-8")
    current_compiled.write_text("compiled", encoding="utf-8")
    target_compiled.write_text("target", encoding="utf-8")

    stale_gff_link = override_dir / "stale.are"
    stale_gff_link.symlink_to("/var/builder/are-resources/gff/area/stale.are")
    moved_gff_link = override_dir / "moved.are"
    moved_gff_link.symlink_to("/var/builder/are-resources/gff/old/moved.are")
    duplicate_link = override_dir / "duplicate.are"
    duplicate_link.symlink_to("/var/builder/are-resources/override/duplicate.are")
    stale_compiled_link = override_dir / "stale.ncs"
    stale_compiled_link.symlink_to("/var/builder/compiled-resources/stale.ncs")
    target_link = override_dir / "target.ncs"
    target_link.symlink_to("/var/builder/compiled-resources/are-dev-test/target.ncs")
    manual_file = override_dir / "manual.utc"
    manual_file.write_text("manual", encoding="utf-8")

    plans = plan_symlinks_for_all(
        override_dir=override_dir,
        shared_root=shared_root,
        compiled_root=compiled_root,
    )

    removed = prune_stale_symlinks_for_all(
        override_dir=override_dir,
        shared_root=shared_root,
        compiled_root=compiled_root,
        active_plans=plans,
    )

    assert removed == 3
    assert not stale_gff_link.is_symlink()
    assert not moved_gff_link.is_symlink()
    assert duplicate_link.readlink().as_posix() == (
        "/var/builder/are-resources/override/duplicate.are"
    )
    assert not stale_compiled_link.is_symlink()
    assert target_link.is_symlink()
    assert manual_file.exists()


@pytest.mark.skipif(
    symlink_module.os.name == "nt",
    reason="Windows resource links use hard links/copies instead of symlinks.",
)
def test_apply_symlink_plan_is_idempotent_for_duplicate_existing_links(
    tmp_path: Path,
) -> None:
    """Verify that apply symlink plan is idempotent for duplicate existing links."""

    override_dir = tmp_path / "override"
    override_dir.mkdir(parents=True, exist_ok=True)
    existing_link = override_dir / "duplicate.utc"
    existing_link.symlink_to("/var/builder/are-resources/gff/creature/duplicate.utc")

    apply_symlink_plan(
        [
            PlannedSymlink(
                link_path=existing_link,
                target_path="/var/builder/are-resources/gff/creature/duplicate.utc",
                overwrite=False,
            ),
            PlannedSymlink(
                link_path=existing_link,
                target_path="/var/builder/are-resources/gff/creature/duplicate.utc",
                overwrite=False,
            ),
        ]
    )

    assert (
        existing_link.readlink().as_posix()
        == "/var/builder/are-resources/gff/creature/duplicate.utc"
    )


def test_symlink_planning_ignores_dotfiles_and_dotdirs(tmp_path: Path) -> None:
    """Verify that symlink planning ignores dotfiles and dotdirs."""

    override_dir = tmp_path / "override"
    shared_root = tmp_path / "are-resources"
    builder_root = tmp_path / "builder-root"
    compiled_root = tmp_path / "compiled-resources"
    for directory in (
        override_dir,
        shared_root / "gff" / "creature",
        shared_root / "gff" / ".AppleDouble",
        shared_root / "override",
        builder_root / "test-resources" / "creature",
        builder_root / "test-resources" / ".git",
        compiled_root / "are-dev-test",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    (shared_root / "gff" / "creature" / ".DS_Store").write_text(
        "metadata", encoding="utf-8"
    )
    (shared_root / "gff" / "creature" / "beast.utc").write_text(
        "creature", encoding="utf-8"
    )
    (shared_root / "gff" / ".AppleDouble" / "hidden.utc").write_text(
        "hidden", encoding="utf-8"
    )
    (builder_root / "test-resources" / "creature" / ".DS_Store").write_text(
        "metadata", encoding="utf-8"
    )
    (builder_root / "test-resources" / "creature" / "bat.utc").write_text(
        "bat", encoding="utf-8"
    )
    (builder_root / "test-resources" / ".git" / "config.utc").write_text(
        "hidden", encoding="utf-8"
    )

    all_plan = plan_symlinks_for_all(
        override_dir=override_dir,
        shared_root=shared_root,
        compiled_root=compiled_root,
    )
    assert {plan.link_path.name for plan in all_plan} == {"beast.utc"}

    link_plan = plan_symlinks_for_target(
        target_name="are-dev-test",
        override_dir=override_dir,
        builder_root=builder_root,
        compiled_root=compiled_root,
    )
    assert {plan.link_path.name for plan in link_plan} == {"bat.utc"}
