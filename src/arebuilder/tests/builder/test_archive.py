from pathlib import Path

from arebuilder.builder.archive import (
    build_archive,
    iter_archive_members,
    read_erf_members,
)


def test_build_archive_replaces_existing_target_file(tmp_path: Path) -> None:
    """Verify that build archive replaces existing target file."""

    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "module.ifo").write_bytes(b"fresh-module-data")

    target_path = tmp_path / "modules" / "existing.mod"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(b"stale-archive-data")

    build_archive(target_path, build_dir)

    members = read_erf_members(target_path)
    assert sorted(members) == ["module.ifo"]
    assert members["module.ifo"] == b"fresh-module-data"


def test_standard_archive_members_use_exact_packable_suffixes(tmp_path: Path) -> None:
    """Verify that standard archive members use exact packable suffixes."""

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "module.ifo").write_bytes(b"module")
    (build_dir / "module.ifo.bak").write_bytes(b"backup")
    (build_dir / ".hidden.itp").write_bytes(b"hidden")
    (build_dir / "creaturepalcus.itp").write_bytes(b"palette")

    members = iter_archive_members(build_dir)

    assert [member.name for member in members] == [
        "creaturepalcus.itp",
        "module.ifo",
    ]
