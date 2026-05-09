import stat
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from arebuilder.app.aredev.project import (
    BuilderBackend,
    BuilderConfigError,
    ProjectLayout,
    infer_project_paths,
    render_arebuilder_env,
)


class ScaffoldConflictError(FileExistsError):
    """Raised when init would overwrite a user-edited scaffold file."""


@dataclass(slots=True)
class ScaffoldResult:
    """Report which scaffold paths were created, preserved, or skipped during init."""

    root: Path
    created_dirs: list[Path] = field(default_factory=list)
    created_files: list[Path] = field(default_factory=list)
    preserved_files: list[Path] = field(default_factory=list)
    overwritten_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class _TemplateFile:
    """Description of one scaffold template and its generated destination."""

    relative_path: str
    executable: bool = False
    newline: str = "\n"


_COMMON_TEMPLATE_FILES = (
    _TemplateFile("docker-compose.yml"),
    _TemplateFile("config/db.env"),
    _TemplateFile("config/nwserver.env"),
    _TemplateFile("data/help.txt"),
    _TemplateFile("data/logo.txt"),
    _TemplateFile("data/timeinit.sql"),
)


def initialize_aredev_project(
    target_dir: Path | str,
    *,
    build_target: str = "pgcc",
    backend: BuilderBackend = "native",
    force: bool = False,
) -> ScaffoldResult:
    """Create the folder and file scaffold expected by AREDev workflows."""

    try:
        inferred_paths = infer_project_paths()
        builder_env = render_arebuilder_env(
            build_target=build_target,
            builder_backend=backend,
            nwn_install_path=inferred_paths.nwn_install_path,
            nwn_home_path=inferred_paths.nwn_home_path,
        )
    except BuilderConfigError:
        raise

    layout = ProjectLayout.from_root(target_dir)
    result = ScaffoldResult(root=layout.root)
    result.warnings.extend(inferred_paths.warnings)

    for directory in _scaffold_directories(layout):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            result.created_dirs.append(directory)
        else:
            directory.mkdir(parents=True, exist_ok=True)

    for template in _template_files_for_host():
        text = _read_template(template.relative_path)
        _write_scaffold_file(
            layout.root / template.relative_path,
            text,
            force=force,
            executable=template.executable,
            newline=template.newline,
            result=result,
        )

    _write_scaffold_file(
        layout.arebuilder_env_path,
        builder_env,
        force=force,
        executable=False,
        newline="\n",
        result=result,
    )
    return result


def _template_files_for_host() -> tuple[_TemplateFile, ...]:
    """Return scaffold templates generated for the current host platform."""

    if sys.platform == "win32":
        return (
            _TemplateFile("AREDev.bat", newline="\r\n"),
            _TemplateFile("data/bin/aredev-host-launcher.ps1"),
            *_COMMON_TEMPLATE_FILES,
        )
    return (
        _TemplateFile("AREDev.sh", executable=True),
        _TemplateFile("data/bin/aredev-host-launcher.sh", executable=True),
        *_COMMON_TEMPLATE_FILES,
    )


def _scaffold_directories(layout: ProjectLayout) -> tuple[Path, ...]:
    """Return all directories created by project scaffolding."""

    return (
        layout.root,
        layout.config_dir,
        layout.data_dir,
        layout.temp_dir,
        layout.compiled_resources_dir,
        layout.logs_dir,
        layout.development_dir,
        layout.hak_dir,
        layout.localvault_dir,
        layout.modules_dir,
        layout.override_dir,
        layout.servervault_dir,
        layout.tlk_dir,
    )


def _read_template(relative_path: str) -> str:
    """Read a packaged scaffold template using importlib resources."""

    template_root = resources.files("arebuilder").joinpath("templates", "aredev")
    return template_root.joinpath(relative_path).read_text(encoding="utf-8")


def _write_scaffold_file(
    path: Path,
    text: str,
    *,
    force: bool,
    executable: bool,
    newline: str,
    result: ScaffoldResult,
) -> None:
    """Write or preserve one scaffold file while detecting conflicting content."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == text:
            result.preserved_files.append(path)
            _ensure_executable(path, executable)
            return
        if not force:
            raise ScaffoldConflictError(
                f"Refusing to overwrite existing scaffold file: {path}"
            )
        path.write_text(text, encoding="utf-8", newline=newline)
        result.overwritten_files.append(path)
        _ensure_executable(path, executable)
        return

    path.write_text(text, encoding="utf-8", newline=newline)
    result.created_files.append(path)
    _ensure_executable(path, executable)


def _ensure_executable(path: Path, executable: bool) -> None:
    """Apply executable mode bits to shell scripts on platforms that support chmod."""

    if not executable:
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
