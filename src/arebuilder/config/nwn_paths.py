import os
import platform
from collections.abc import Mapping
from pathlib import Path

from arebuilder.config.env import expand_path


def resolve_nwn_install_root(
    explicit_path: str = "",
    *,
    system: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve an NWN install root from explicit config, env, or known locations."""

    explicit = _expand_optional_path(explicit_path)
    if explicit is not None:
        return explicit

    # Explicit environment overrides beat heuristics so custom installs and CI
    # jobs can provide exact roots without mimicking a known store layout.
    env_path = _first_env_path(
        environ or os.environ,
        ("NWN_INSTALL_PATH", "NWN_ROOT", "NWN_INSTALL", "NWN_DIR"),
    )
    if env_path is not None:
        return env_path

    for candidate in _install_candidates(
        system=system or platform.system(),
        home=home or Path.home(),
        environ=environ or os.environ,
    ):
        if _looks_like_nwn_install(candidate):
            return candidate
    return None


def resolve_nwn_home_root(
    explicit_path: str = "",
    *,
    system: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve an existing NWN user-home root from config, env, or known paths."""

    explicit = _expand_optional_path(explicit_path)
    if explicit is not None:
        return explicit

    env_path = _first_env_path(environ or os.environ, ("NWN_HOME_PATH", "NWN_HOME"))
    if env_path is not None:
        return env_path

    for candidate in _home_candidates(
        system=system or platform.system(),
        home=home or Path.home(),
        environ=environ or os.environ,
    ):
        if candidate.exists():
            return candidate
    return None


def find_nwn_client_executable(
    install_root: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
) -> Path | None:
    """Return the platform-specific NWN client executable under an install root."""

    bin_dir = (
        install_root
        / "bin"
        / _client_os_variant(
            system=system or platform.system(),
            machine=machine or platform.machine(),
        )
    )
    if not bin_dir.exists():
        return None

    if bin_dir.name == "macos":
        app_binary = bin_dir / "nwmain.app" / "Contents" / "MacOS" / "nwmain"
        if app_binary.exists():
            return app_binary

    # Beamdog packages have used both nwmain and nwmain.exe; the glob fallback
    # covers suffixed binaries while still preferring the conventional names.
    preferred_names = ("nwmain.exe", "nwmain")
    for name in preferred_names:
        candidate = bin_dir / name
        if candidate.exists():
            return candidate

    matches = sorted(path for path in bin_dir.glob("nwmain*") if path.is_file())
    return matches[0] if matches else None


def _client_os_variant(*, system: str, machine: str) -> str:
    """Return the expected NWN client binary variant for the host OS."""

    if system == "Windows":
        return "win32"
    if system == "Darwin":
        return "macos"
    normalized_machine = machine.lower()
    if normalized_machine in {"aarch64", "arm64"}:
        return "linux-arm64"
    return "linux-x86"


def _install_candidates(
    *,
    system: str,
    home: Path,
    environ: Mapping[str, str],
) -> list[Path]:
    """Yield likely NWN installation roots in preference order."""

    steam_roots = _steam_roots(system=system, home=home, environ=environ)
    candidates = [
        root / "steamapps" / "common" / "Neverwinter Nights" for root in steam_roots
    ]

    if system == "Darwin":
        # macOS installs may come from Steam or app bundles under /Applications.
        candidates.extend(
            [
                home
                / "Library"
                / "Application Support"
                / "Steam"
                / "steamapps"
                / "common"
                / "Neverwinter Nights",
                Path("/Applications/Neverwinter Nights.app/Contents/Resources"),
                Path(
                    "/Applications/Neverwinter Nights Enhanced Edition.app"
                    "/Contents/Resources"
                ),
            ]
        )
    elif system == "Windows":
        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            program_files = environ.get(env_name)
            if program_files:
                # Windows users commonly install through Steam, GOG Galaxy, or
                # the standalone enhanced-edition installer.
                base = Path(program_files)
                candidates.extend(
                    [
                        base / "Steam" / "steamapps" / "common" / "Neverwinter Nights",
                        base
                        / "GOG Galaxy"
                        / "Games"
                        / "Neverwinter Nights Enhanced Edition",
                        base / "Neverwinter Nights Enhanced Edition",
                    ]
                )
    else:
        candidates.extend(
            [
                home
                / ".local"
                / "share"
                / "Steam"
                / "steamapps"
                / "common"
                / "Neverwinter Nights",
                home
                / ".steam"
                / "steam"
                / "steamapps"
                / "common"
                / "Neverwinter Nights",
                home
                / ".var"
                / "app"
                / "com.valvesoftware.Steam"
                / ".local"
                / "share"
                / "Steam"
                / "steamapps"
                / "common"
                / "Neverwinter Nights",
            ]
        )

    return _deduplicate_paths(candidates)


def _home_candidates(
    *,
    system: str,
    home: Path,
    environ: Mapping[str, str],
) -> list[Path]:
    """Yield likely NWN home directories without creating them."""

    candidates = [home / "Documents" / "Neverwinter Nights"]
    if system != "Windows":
        xdg_data_home = environ.get("XDG_DATA_HOME")
        if xdg_data_home:
            candidates.append(expand_path(xdg_data_home) / "Neverwinter Nights")
        candidates.append(home / ".local" / "share" / "Neverwinter Nights")
    return _deduplicate_paths(candidates)


def _steam_roots(
    *,
    system: str,
    home: Path,
    environ: Mapping[str, str],
) -> list[Path]:
    """Yield Steam library roots that may contain NWN installations."""

    roots = [
        _expand_optional_path(environ.get(name, ""))
        for name in ("STEAM_LIBRARY", "STEAM_DIR", "STEAM_PATH")
    ]
    if system == "Darwin":
        roots.append(home / "Library" / "Application Support" / "Steam")
    elif system == "Windows":
        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            program_files = environ.get(env_name)
            if program_files:
                roots.append(Path(program_files) / "Steam")
    else:
        roots.extend(
            [
                home / ".local" / "share" / "Steam",
                home / ".steam" / "steam",
                home
                / ".var"
                / "app"
                / "com.valvesoftware.Steam"
                / ".local"
                / "share"
                / "Steam",
            ]
        )
    return _deduplicate_paths([root for root in roots if root is not None])


def _first_env_path(
    environ: Mapping[str, str],
    names: tuple[str, ...],
) -> Path | None:
    """Return the first existing path named by the given environment variables."""

    for name in names:
        path = _expand_optional_path(environ.get(name, ""))
        if path is not None:
            return path
    return None


def _expand_optional_path(value: str) -> Path | None:
    """Expand an optional path string, returning ``None`` for blanks."""

    value = value.strip()
    if not value:
        return None
    return expand_path(value.replace("!HOMEPATH!", str(Path.home())))


def _looks_like_nwn_install(path: Path) -> bool:
    """Return whether a directory contains recognizable NWN install files."""

    markers = [
        path / "lang" / "en" / "data" / "dialog.tlk",
        path / "data" / "nwn_base.key",
        path / "data" / "nwn_retail.key",
        path / "bin" / "win32" / "nwmain.exe",
        path / "bin" / "linux-x86" / "nwmain",
        path / "bin" / "linux-arm64" / "nwmain",
        path / "bin" / "macos" / "nwmain.app",
    ]
    return any(marker.exists() for marker in markers)


def _deduplicate_paths(paths: list[Path]) -> list[Path]:
    """Yield paths once while preserving candidate order."""

    deduplicated: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = path.expanduser().as_posix()
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(path.expanduser())
    return deduplicated
