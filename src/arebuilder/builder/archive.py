from pathlib import Path

from nwn.res import extension_to_restype

from arebuilder.nwn.compat import read_erf_members, write_erf_archive

ARCHIVE_TYPES = {
    ".mod": b"MOD ",
    ".erf": b"ERF ",
    ".hak": b"HAK ",
}


def build_archive(
    target_path: Path,
    build_dir: Path,
) -> None:
    """Write the archive file using the generated build artifacts."""

    file_type = ARCHIVE_TYPES.get(target_path.suffix.lower())
    if file_type is None:
        raise ValueError(
            "Make sure the Target for this build is a .mod, .erf or .hak file."
        )

    members = [
        (resource_path.name, resource_path.read_bytes())
        for resource_path in iter_archive_members(build_dir)
    ]
    write_erf_archive(target_path, file_type, members)


def iter_archive_members(build_dir: Path) -> list[Path]:
    """Return archive members in deterministic order, excluding hidden and unsupported files."""

    members = []
    for current_path in sorted(build_dir.rglob("*")):
        # Archive resources are selected from a recursive build directory, but the
        # ERF member names themselves remain flat and basename-based.
        if (
            current_path.is_file()
            and current_path.suffix.lower() in {".ifo", ".itp"}
            and _is_packable_resource(current_path)
        ):
            members.append(current_path)
    return members


def _is_packable_resource(path: Path) -> bool:
    """Return whether a file can be written as an ERF resource member."""

    # Validate names before handing them to nwn's extension mapper so hidden files
    # and overlong legacy ResRefs are ignored consistently.
    if path.name.startswith(".") or any(
        part.startswith(".") for part in Path(path).parts
    ):
        return False
    if len(path.stem) > 16 or not path.suffix:
        return False
    try:
        extension_to_restype(path.suffix[1:].lower())
    except ValueError:
        return False
    return True


__all__ = ["ARCHIVE_TYPES", "build_archive", "iter_archive_members", "read_erf_members"]
