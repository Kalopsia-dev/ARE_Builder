import subprocess
import sys
from pathlib import Path

from nwn import gff
from nwn.types import Gender, GenderedLanguage, Language

from arebuilder.content.palette import _resolve_name
from arebuilder.nwn.compat import (
    INVALID_STRREF,
    load_talk_table,
    normalize_locstring_entries,
    read_gff,
    write_gff,
    write_tlk,
)
from arebuilder.tests.fixtures import create_synthetic_fixture


def test_compat_imports_without_removed_nwn_shared_module() -> None:
    """Verify compat no longer depends on the removed nwn._shared module."""

    script = (
        f"import sys; sys.path[:] = {sys.path!r}; "
        "sys.modules['nwn._shared'] = None; "
        "import arebuilder.nwn.compat"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_write_tlk_accepts_plain_strings(tmp_path: Path) -> None:
    """Verify TLK wrapper keeps accepting plain string entries."""

    tlk_path = tmp_path / "plain-string.tlk"

    write_tlk(tlk_path, ["", "Plain text"], Language.ENGLISH)
    talk_table = load_talk_table(tlk_path)

    assert talk_table.language == Language.ENGLISH
    assert talk_table.entries == ["", "Plain text"]


def test_normalize_locstring_entries_accepts_nwn_key_variants() -> None:
    """Verify locstring helpers preserve raw IDs across nwn key shapes."""

    assert normalize_locstring_entries(
        {
            Language.ENGLISH: "English",
            GenderedLanguage(Language.GERMAN, Gender.FEMALE): "German female",
            260: "Raw locale",
        }
    ) == {
        0: "English",
        5: "German female",
        260: "Raw locale",
    }


def test_read_gff_preserves_unknown_locstring_entry_ids(tmp_path: Path) -> None:
    """Verify that read gff preserves unknown locstring entry ids."""

    gff_path = tmp_path / "unknown-language.utc"
    write_gff(
        gff_path,
        gff.Struct(
            0xFFFFFFFF,
            LocalizedName=gff.CExoLocString(gff.Dword(10), {260: "Custom locale text"}),
        ),
        "UTC ",
    )

    root, _ = read_gff(gff_path)

    assert int(root["LocalizedName"].strref) == 10
    assert root["LocalizedName"].entries == {260: "Custom locale text"}


def test_resolve_name_keeps_tlk_fallback_for_nonzero_inline_entry(
    tmp_path: Path,
) -> None:
    """Verify that resolve name keeps tlk fallback for nonzero inline entry."""

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    localized_name = gff.CExoLocString(gff.Dword(10), {260: "Custom locale text"})

    display_name, string_ref = _resolve_name(
        localized_name,
        fixture_talk_table(fixture.talktable_path),
    )

    assert display_name is None
    assert string_ref == 10


def test_resolve_name_uses_inline_text_when_invalid_strref_has_nonzero_entry(
    tmp_path: Path,
) -> None:
    """
    Verify that resolve name uses inline text when invalid strref has nonzero entry.
    """

    fixture = create_synthetic_fixture(tmp_path / "fixture", layout="host")
    localized_name = gff.CExoLocString(
        INVALID_STRREF,
        {
            260: "Inline fallback",
        },
    )

    display_name, string_ref = _resolve_name(
        localized_name,
        fixture_talk_table(fixture.talktable_path),
    )

    assert display_name == "Inline fallback"
    assert string_ref is None


def fixture_talk_table(talktable_path: Path):
    """Load the synthetic fixture TLK lazily for a focused unit test."""

    from arebuilder.nwn.compat import load_talk_table

    return load_talk_table(talktable_path)
