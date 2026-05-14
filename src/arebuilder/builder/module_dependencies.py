from dataclasses import dataclass, field
from pathlib import Path

from nwn import key

from arebuilder.config.module_settings import ModuleSettings
from arebuilder.nwn.compat import list_erf_filenames, read_gff


@dataclass(frozen=True, slots=True)
class HakInspectionIssue:
    """Record an enabled HAK that could not be inspected."""

    hak_name: str
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class AreaTilesetOmission:
    """Record an area omitted because its tileset is unavailable."""

    area_name: str
    area_path: Path
    tileset: str


@dataclass(frozen=True, slots=True)
class TilesetAvailability:
    """Tileset resources visible through the base game and enabled HAKs."""

    resources: frozenset[str]
    base_tilesets_known: bool = True
    missing_haks: tuple[HakInspectionIssue, ...] = ()
    unreadable_haks: tuple[HakInspectionIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class AreaDependencyReport:
    """Filtered resource map and warnings from area dependency validation."""

    included_files: dict[str, Path]
    omitted_areas: tuple[AreaTilesetOmission, ...] = ()
    availability: TilesetAvailability = field(
        default_factory=lambda: TilesetAvailability(frozenset())
    )


def filter_area_tileset_dependencies(
    settings: ModuleSettings,
    included_files: dict[str, Path],
    *,
    hak_dir: Path,
    nwn_root: Path | None,
) -> AreaDependencyReport:
    """Omit areas whose ``Tileset`` resource is unavailable to the module."""

    availability = discover_available_tilesets(
        settings.haks,
        hak_dir=hak_dir,
        nwn_root=nwn_root,
    )
    filtered_files = dict(included_files)
    omitted_areas: list[AreaTilesetOmission] = []
    if not availability.base_tilesets_known:
        return AreaDependencyReport(
            included_files=filtered_files,
            availability=availability,
        )

    for basename, path in sorted(included_files.items()):
        if Path(basename).suffix.lower() != ".are":
            continue
        tileset = area_tileset(path)
        if tileset is None:
            continue
        if _tileset_resource_name(tileset) in availability.resources:
            continue

        filtered_files.pop(basename, None)
        omitted_areas.append(
            AreaTilesetOmission(
                area_name=Path(basename).stem,
                area_path=path,
                tileset=tileset,
            )
        )

    return AreaDependencyReport(
        included_files=filtered_files,
        omitted_areas=tuple(omitted_areas),
        availability=availability,
    )


def discover_available_tilesets(
    haks: list[str],
    *,
    hak_dir: Path,
    nwn_root: Path | None,
) -> TilesetAvailability:
    """Return lower-case ``.set`` resources available to a module."""

    base_resources = _tileset_resources_from_nwn_install(nwn_root)
    resources = set(base_resources or ())

    missing_haks: list[HakInspectionIssue] = []
    unreadable_haks: list[HakInspectionIssue] = []
    for hak_name in haks:
        hak_path = _hak_path(hak_dir, hak_name)
        if not hak_path.exists():
            missing_haks.append(
                HakInspectionIssue(
                    hak_name=hak_name,
                    path=hak_path,
                    reason="file does not exist",
                )
            )
            continue
        try:
            filenames = list_erf_filenames(hak_path)
        except Exception as exc:
            unreadable_haks.append(
                HakInspectionIssue(
                    hak_name=hak_name,
                    path=hak_path,
                    reason=str(exc),
                )
            )
            continue
        resources.update(_tileset_resources_from_filenames(filenames))

    return TilesetAvailability(
        resources=frozenset(resources),
        base_tilesets_known=base_resources is not None,
        missing_haks=tuple(missing_haks),
        unreadable_haks=tuple(unreadable_haks),
    )


def area_tileset(path: Path) -> str | None:
    """Return an area's ``Tileset`` resref, if present."""

    root, _ = read_gff(path)
    tileset = root.get("Tileset")
    if tileset is None:
        return None
    normalized = str(tileset).strip().lower()
    return normalized or None


def _tileset_resources_from_nwn_install(nwn_root: Path | None) -> set[str] | None:
    """Index base-game tilesets from known NWN key-file layouts."""

    if nwn_root is None:
        return None

    key_paths = _candidate_key_files(nwn_root)
    if not key_paths:
        return None
    resources: set[str] = set()
    read_any = False
    for key_path in key_paths:
        filenames = _read_key_filenames(key_path, nwn_root)
        if filenames is None:
            continue
        read_any = True
        resources.update(_tileset_resources_from_filenames(filenames))
    return resources if read_any else None


def _candidate_key_files(nwn_root: Path) -> list[Path]:
    """Return known NWN key files without assuming classic or EE layout."""

    candidates = [nwn_root / "chitin.key", nwn_root / "data" / "nwn_base.key"]
    for directory in (nwn_root, nwn_root / "data"):
        if directory.is_dir():
            candidates.extend(sorted(directory.glob("*.key")))

    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        unique_candidates.append(candidate)
        seen.add(candidate)
    return unique_candidates


def _read_key_filenames(key_path: Path, nwn_root: Path) -> list[str] | None:
    """Read one key file, trying common BIF path bases."""

    bif_roots = [nwn_root, key_path.parent, nwn_root / "data"]
    seen: set[Path] = set()
    for bif_root in bif_roots:
        if bif_root in seen:
            continue
        seen.add(bif_root)
        try:
            reader = key.Reader(key_path, bif_directory=bif_root)
        except Exception:
            continue
        try:
            return sorted(reader.filenames)
        finally:
            reader.close()
    return None


def _tileset_resources_from_filenames(filenames: list[str]) -> set[str]:
    """Return normalized tileset resource names from archive/key filenames."""

    return {
        filename.lower()
        for filename in filenames
        if Path(filename).suffix.lower() == ".set"
    }


def _hak_path(hak_dir: Path, hak_name: str) -> Path:
    """Resolve a settings HAK entry to the expected archive path."""

    if hak_name.lower().endswith(".hak"):
        return hak_dir / hak_name
    return hak_dir / f"{hak_name}.hak"


def _tileset_resource_name(tileset: str) -> str:
    """Return the canonical ``.set`` resource name for a tileset resref."""

    return f"{tileset.lower()}.set"
