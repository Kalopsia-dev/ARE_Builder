from pathlib import Path

from arebuilder.config.nwn_paths import (
    find_nwn_client_executable,
    resolve_nwn_home_root,
    resolve_nwn_install_root,
)


def test_resolve_nwn_install_root_prefers_explicit_path(tmp_path: Path) -> None:
    """Verify that resolve NWN install root prefers explicit path."""

    install = tmp_path / "custom-install"

    assert resolve_nwn_install_root(str(install), environ={}) == install


def test_resolve_nwn_install_root_uses_environment_override(tmp_path: Path) -> None:
    """Verify NWN install discovery honors supported environment override variables."""

    install = tmp_path / "env-install"

    assert resolve_nwn_install_root(environ={"NWN_ROOT": str(install)}) == install


def test_resolve_nwn_install_root_detects_steam_layout(tmp_path: Path) -> None:
    """Verify that resolve NWN install root detects steam layout."""

    install = (
        tmp_path / ".steam" / "steam" / "steamapps" / "common" / "Neverwinter Nights"
    )
    (install / "lang" / "en" / "data").mkdir(parents=True)
    (install / "lang" / "en" / "data" / "dialog.tlk").write_bytes(b"tlk")

    assert (
        resolve_nwn_install_root(system="Linux", home=tmp_path, environ={}) == install
    )


def test_resolve_nwn_home_root_detects_existing_documents_home(
    tmp_path: Path,
) -> None:
    """Verify that resolve NWN home root detects existing documents home."""

    nwn_home = tmp_path / "Documents" / "Neverwinter Nights"
    nwn_home.mkdir(parents=True)

    assert resolve_nwn_home_root(system="Darwin", home=tmp_path, environ={}) == nwn_home


def test_resolve_nwn_home_root_does_not_create_missing_home(tmp_path: Path) -> None:
    """Verify that resolve NWN home root does not create missing home."""

    assert resolve_nwn_home_root(system="Linux", home=tmp_path, environ={}) is None


def test_find_nwn_client_executable_supports_os_variants(tmp_path: Path) -> None:
    """Verify NWN client discovery supports known OS variants."""

    install = tmp_path / "Neverwinter Nights"
    linux_arm = install / "bin" / "linux-arm64" / "nwmain"
    linux_arm.parent.mkdir(parents=True)
    linux_arm.write_bytes(b"")

    linux_x86 = install / "bin" / "linux-x86" / "nwmain"
    linux_x86.parent.mkdir(parents=True)
    linux_x86.write_bytes(b"")

    macos = install / "bin" / "macos" / "nwmain.app" / "Contents" / "MacOS" / "nwmain"
    macos.parent.mkdir(parents=True)
    macos.write_bytes(b"")

    win32 = install / "bin" / "win32" / "nwmain.exe"
    win32.parent.mkdir(parents=True)
    win32.write_bytes(b"")

    assert (
        find_nwn_client_executable(install, system="Linux", machine="aarch64")
        == linux_arm
    )
    assert (
        find_nwn_client_executable(install, system="Linux", machine="x86_64")
        == linux_x86
    )
    assert find_nwn_client_executable(install, system="Darwin") == macos
    assert find_nwn_client_executable(install, system="Windows") == win32
