import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class PlannedSymlink:
    """Describe one symlink destination, target, and overwrite policy."""

    link_path: Path
    target_path: str
    overwrite: bool
    source_path: Path | None = None


def plan_symlinks_for_all(
    *,
    override_dir: Path,
    shared_root: Path,
    compiled_root: Path,
    builder_mount_root: str = "/var/builder",
) -> list[PlannedSymlink]:
    """Plan non-overwriting shared-resource symlinks for the full build command."""

    plans: list[PlannedSymlink] = []
    # Link targets use the builder/container view of the project tree so the
    # same override links work both inside Docker and from the host wrapper.
    plans.extend(
        _plan_recursive_directory(
            link_dir=override_dir,
            source_root=shared_root / "gff",
            mount_root=f"{builder_mount_root}/are-resources/gff",
            overwrite=False,
        )
    )
    plans.extend(
        _plan_recursive_directory(
            link_dir=override_dir,
            source_root=shared_root / "override",
            mount_root=f"{builder_mount_root}/are-resources/override",
            overwrite=False,
        )
    )
    if compiled_root.exists():
        for child in sorted(compiled_root.iterdir()):
            if child.is_file():
                # Shared compiled outputs are global resources; all-mode planning
                # avoids overwriting target-specific links that may already exist.
                plans.append(
                    PlannedSymlink(
                        link_path=override_dir / child.name,
                        target_path=f"{builder_mount_root}/compiled-resources/{child.name}",
                        overwrite=False,
                        source_path=child,
                    )
                )
    return plans


def plan_symlinks_for_target(
    *,
    target_name: str,
    override_dir: Path,
    builder_root: Path,
    compiled_root: Path,
    source_root: Path | None = None,
    builder_mount_root: str = "/var/builder",
) -> list[PlannedSymlink]:
    """Plan target-specific symlinks for the ``link`` command."""

    target_source_root = source_root or _default_target_source_root(
        builder_root, target_name
    )
    # Target mode refreshes source-resource links aggressively, but leaves text
    # metadata and raw NSS source files out of the NWN override directory.
    plans = _plan_recursive_directory(
        link_dir=override_dir,
        source_root=target_source_root,
        mount_root=_mounted_source_root(
            builder_root=builder_root,
            source_root=target_source_root,
            builder_mount_root=builder_mount_root,
            fallback_name=target_name,
        ),
        overwrite=True,
        exclude_suffixes={".txt", ".nss"},
    )
    compiled_target_root = compiled_root / target_name
    if compiled_target_root.exists():
        for child in sorted(compiled_target_root.iterdir()):
            if child.is_file():
                plans.append(
                    PlannedSymlink(
                        link_path=override_dir / child.name,
                        target_path=f"{builder_mount_root}/compiled-resources/{target_name}/{child.name}",
                        overwrite=True,
                        source_path=child,
                    )
                )
    return plans


def prune_stale_symlinks_for_target(
    *,
    target_name: str,
    override_dir: Path,
    builder_root: Path,
    compiled_root: Path,
    source_root: Path | None = None,
    builder_mount_root: str = "/var/builder",
    active_plans: list[PlannedSymlink] | None = None,
) -> int:
    """Remove obsolete override links owned by the target link plan."""

    target_source_root = source_root or _default_target_source_root(
        builder_root, target_name
    )
    source_mount_root = _mounted_source_root(
        builder_root=builder_root,
        source_root=target_source_root,
        builder_mount_root=builder_mount_root,
        fallback_name=target_name,
    )
    target_mount_roots = (
        source_mount_root,
        f"{builder_mount_root}/compiled-resources/{target_name}",
    )
    if active_plans is None:
        active_plans = plan_symlinks_for_target(
            target_name=target_name,
            override_dir=override_dir,
            builder_root=builder_root,
            compiled_root=compiled_root,
            source_root=source_root,
            builder_mount_root=builder_mount_root,
        )

    active_links = {plan.link_path for plan in _deduplicate_symlink_plans(active_plans)}
    removed = 0
    if not override_dir.exists():
        return removed

    for link_path in sorted(override_dir.iterdir()):
        if link_path in active_links or not link_path.is_symlink():
            continue
        target_path = os.readlink(link_path)
        if _target_is_under_mount_roots(target_path, target_mount_roots):
            link_path.unlink()
            removed += 1
    return removed


def _default_target_source_root(builder_root: Path, target_name: str) -> Path:
    """Return the conventional resource root for a target name."""

    if target_name.startswith("are-dev-"):
        return builder_root / f"{target_name.removeprefix('are-dev-')}-resources"
    return builder_root / target_name


def _mounted_source_root(
    *,
    builder_root: Path,
    source_root: Path,
    builder_mount_root: str,
    fallback_name: str,
) -> str:
    """Translate a local source root into the path visible inside the builder mount."""

    try:
        relative_source_root = source_root.resolve().relative_to(builder_root.resolve())
    except ValueError:
        # External roots cannot be represented under the mounted builder root, so
        # the fallback keeps the target path predictable for generated projects.
        return f"{builder_mount_root}/{fallback_name}"
    return f"{builder_mount_root}/{relative_source_root.as_posix()}"


def _target_is_under_mount_roots(
    target_path: str,
    mount_roots: tuple[str, ...],
) -> bool:
    """Return whether a symlink target belongs to one of the mounted roots."""

    normalized_target = target_path.rstrip("/")
    for mount_root in mount_roots:
        normalized_root = mount_root.rstrip("/")
        if normalized_target == normalized_root or normalized_target.startswith(
            f"{normalized_root}/"
        ):
            return True
    return False


def count_symlink_plan_steps(plans: list[PlannedSymlink]) -> int:
    """Return the number of deduplicated symlink work items."""

    return len(_deduplicate_symlink_plans(plans))


def apply_symlink_plan(
    plans: list[PlannedSymlink],
    *,
    progress: Callable[[int], None] | None = None,
) -> None:
    """Apply a deduplicated symlink plan while preserving existing protected links."""

    for plan in _deduplicate_symlink_plans(plans):
        try:
            plan.link_path.parent.mkdir(parents=True, exist_ok=True)

            if _symlink_already_matches(plan):
                continue

            # We intentionally treat broken symlinks as existing because the project
            # stores in-container absolute targets that are often broken on the host.
            if not plan.overwrite and os.path.lexists(plan.link_path):
                continue

            if plan.overwrite and os.path.lexists(plan.link_path):
                plan.link_path.unlink()

            try:
                _link_resource(plan)
            except FileExistsError:
                if not plan.overwrite or _symlink_already_matches(plan):
                    continue
                plan.link_path.unlink()
                _link_resource(plan)
        finally:
            if progress is not None:
                progress(1)


def _plan_recursive_directory(
    *,
    link_dir: Path,
    source_root: Path,
    mount_root: str,
    overwrite: bool,
    exclude_suffixes: set[str] | None = None,
) -> list[PlannedSymlink]:
    """Plan symlinks for a recursively walked source directory."""

    exclude_suffixes = exclude_suffixes or set()
    plans: list[PlannedSymlink] = []
    if not source_root.exists():
        return plans

    for source_path in sorted(source_root.rglob("*")):
        if not source_path.is_file():
            continue
        relative_path = source_path.relative_to(source_root)
        # Dotfiles from editors and operating systems are never meaningful NWN
        # resources, and linking them would pollute the server override folder.
        if any(part.startswith(".") for part in Path(relative_path).parts):
            continue
        if source_path.suffix.lower() in exclude_suffixes:
            continue
        plans.append(
            PlannedSymlink(
                link_path=link_dir / source_path.name,
                target_path=f"{mount_root}/{relative_path.as_posix()}",
                overwrite=overwrite,
                source_path=source_path,
            )
        )
    return plans


def _deduplicate_symlink_plans(plans: list[PlannedSymlink]) -> list[PlannedSymlink]:
    """Collapse duplicate destinations using command-specific overwrite rules.

    ``all`` mode keeps the first destination encountered because it only creates
    symlinks that do not already exist. ``link`` mode keeps the last destination
    encountered because it refreshes target-specific links.
    """

    deduplicated: list[PlannedSymlink] = []
    index_by_link_path: dict[Path, int] = {}

    for plan in plans:
        existing_index = index_by_link_path.get(plan.link_path)
        if existing_index is None:
            index_by_link_path[plan.link_path] = len(deduplicated)
            deduplicated.append(plan)
            continue
        if plan.overwrite:
            deduplicated[existing_index] = plan

    return deduplicated


def _symlink_already_matches(plan: PlannedSymlink) -> bool:
    """Return whether the destination already points at the requested target."""

    if not plan.link_path.is_symlink():
        return False
    return os.readlink(plan.link_path) == plan.target_path


def _link_resource(plan: PlannedSymlink) -> None:
    """Create the best available link for one override resource."""

    try:
        os.symlink(plan.target_path, plan.link_path)
    except OSError:
        if plan.source_path is None:
            raise
        _link_or_copy_file(plan.source_path, plan.link_path)


def _link_or_copy_file(source_path: Path, link_path: Path) -> None:
    """Hard-link a file, falling back to a copy when the filesystem cannot link it."""

    try:
        os.link(source_path, link_path)
    except OSError:
        shutil.copy2(source_path, link_path)
