from dataclasses import dataclass, field
from pathlib import Path

from nwn import erf, key

from arebuilder.config.module_settings import ModuleSettings
from arebuilder.nwn.compat import read_gff


@dataclass(frozen=True, slots=True)
class HakInspectionIssue:
    """Record an enabled HAK that could not be inspected."""

    hak_name: str
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class AreaTilesetOmission:
    """Record an area omitted because its tileset dependency is unavailable."""

    area_name: str
    area_path: Path
    tileset: str
    reason: str = "unavailable"
    required_tile_id: int | None = None
    available_tile_count: int | None = None
    tileset_provider: str | None = None


@dataclass(frozen=True, slots=True)
class TilesetAvailability:
    """Tileset resources visible through the base game and enabled HAKs."""

    resources: frozenset[str]
    base_tilesets_known: bool = True
    tile_counts: dict[str, int] = field(default_factory=dict)
    tileset_providers: dict[str, str] = field(default_factory=dict)
    missing_haks: tuple[HakInspectionIssue, ...] = ()
    unreadable_haks: tuple[HakInspectionIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class _TilesetResourceIndex:
    """Tileset resource names and inferred tile counts from one source."""

    resources: set[str] = field(default_factory=set)
    tile_counts: dict[str, int] = field(default_factory=dict)


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
        tileset, max_tile_id = area_tileset_usage(path)
        if tileset is None:
            continue
        tileset_resource = _tileset_resource_name(tileset)
        tile_count = availability.tile_counts.get(tileset_resource)
        if tileset_resource in availability.resources and (
            max_tile_id is None or tile_count is None or max_tile_id < tile_count
        ):
            continue

        filtered_files.pop(basename, None)
        reason = (
            "tile_index"
            if tileset_resource in availability.resources
            else "unavailable"
        )
        omitted_areas.append(
            AreaTilesetOmission(
                area_name=Path(basename).stem,
                area_path=path,
                tileset=tileset,
                reason=reason,
                required_tile_id=max_tile_id if reason == "tile_index" else None,
                available_tile_count=tile_count if reason == "tile_index" else None,
                tileset_provider=availability.tileset_providers.get(tileset_resource)
                if reason == "tile_index"
                else None,
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

    base_index = _tileset_resources_from_nwn_install(nwn_root)
    resources = set(base_index.resources if base_index is not None else ())
    tile_counts = dict(base_index.tile_counts if base_index is not None else {})
    tileset_providers: dict[str, str] = {}

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
            hak_index = _tileset_index_from_hak(hak_path)
        except Exception as exc:
            unreadable_haks.append(
                HakInspectionIssue(
                    hak_name=hak_name,
                    path=hak_path,
                    reason=str(exc),
                )
            )
            continue
        resources.update(hak_index.resources)
        _merge_tile_counts(
            tile_counts,
            hak_index.tile_counts,
            tileset_providers,
            hak_path.name,
        )

    return TilesetAvailability(
        resources=frozenset(resources),
        base_tilesets_known=base_index is not None,
        tile_counts=tile_counts,
        tileset_providers=tileset_providers,
        missing_haks=tuple(missing_haks),
        unreadable_haks=tuple(unreadable_haks),
    )


def area_tileset(path: Path) -> str | None:
    """Return an area's ``Tileset`` resref, if present."""

    tileset, _ = area_tileset_usage(path)
    return tileset


def area_tileset_usage(path: Path) -> tuple[str | None, int | None]:
    """Return an area's tileset resref and highest referenced tile id."""

    root, _ = read_gff(path)
    tileset = root.get("Tileset")
    if tileset is None:
        return None, None
    normalized = str(tileset).strip().lower()
    tile_ids = [
        int(tile["Tile_ID"]) for tile in root.get("Tile_List", []) if "Tile_ID" in tile
    ]
    return normalized or None, max(tile_ids, default=None)


def _tileset_resources_from_nwn_install(
    nwn_root: Path | None,
) -> _TilesetResourceIndex | None:
    """Index base-game tilesets from known NWN key-file layouts."""

    if nwn_root is None:
        return None

    key_paths = _candidate_key_files(nwn_root)
    if not key_paths:
        return None
    index = _TilesetResourceIndex()
    read_any = False
    for key_path in key_paths:
        key_index = _read_key_tileset_index(key_path, nwn_root)
        if key_index is None:
            continue
        read_any = True
        index.resources.update(key_index.resources)
        _merge_tile_counts(index.tile_counts, key_index.tile_counts)
    return index if read_any else None


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


def _read_key_tileset_index(
    key_path: Path,
    nwn_root: Path,
) -> _TilesetResourceIndex | None:
    """Read tileset resources from one key file, trying common BIF path bases."""

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
            return _tileset_index_from_reader(reader.filenames, reader.read_file)
        except Exception:
            continue
        finally:
            reader.close()
    return None


def _tileset_index_from_hak(path: Path) -> _TilesetResourceIndex:
    """Read tileset resources from one HAK archive."""

    with path.open("rb") as handle:
        reader = erf.Reader(handle)
        return _tileset_index_from_reader(reader.filenames, reader.read_file)


def _tileset_index_from_reader(filenames, read_file) -> _TilesetResourceIndex:
    """Return normalized tileset names and counts from archive/key filenames."""

    index = _TilesetResourceIndex()
    for filename in sorted(filenames):
        if Path(filename).suffix.lower() != ".set":
            continue
        resource_name = filename.lower()
        index.resources.add(resource_name)
        tile_count = _tileset_tile_count(read_file(filename))
        if tile_count is not None:
            index.tile_counts[resource_name] = max(
                tile_count,
                index.tile_counts.get(resource_name, 0),
            )
    return index


def _tileset_tile_count(data: bytes) -> int | None:
    """Infer the number of tile entries in a ``.set`` resource."""

    tile_ids = []
    for line in data.decode("latin-1", errors="ignore").splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("[tile") and stripped.endswith("]"):
            tile_id = stripped[5:-1]
            if tile_id.isdigit():
                tile_ids.append(int(tile_id))
    return max(tile_ids) + 1 if tile_ids else None


def _merge_tile_counts(
    target: dict[str, int],
    source: dict[str, int],
    providers: dict[str, str] | None = None,
    provider: str | None = None,
) -> None:
    """Merge tileset counts, keeping the largest visible definition."""

    for resource_name, tile_count in source.items():
        current_count = target.get(resource_name)
        if current_count is not None and tile_count <= current_count:
            continue
        target[resource_name] = tile_count
        if providers is not None and provider is not None:
            providers[resource_name] = provider


def _hak_path(hak_dir: Path, hak_name: str) -> Path:
    """Resolve a settings HAK entry to the expected archive path."""

    if hak_name.lower().endswith(".hak"):
        return hak_dir / hak_name
    return hak_dir / f"{hak_name}.hak"


def _tileset_resource_name(tileset: str) -> str:
    """Return the canonical ``.set`` resource name for a tileset resref."""

    return f"{tileset.lower()}.set"
