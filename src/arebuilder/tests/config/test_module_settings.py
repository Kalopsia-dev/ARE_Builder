from pathlib import Path

from arebuilder.config.module_settings import ModuleSettings, parse_settings_text


def test_parse_settings_preserves_defaults_and_multivalue_fields() -> None:
    """Verify that parse settings preserves defaults and multivalue fields."""

    settings = parse_settings_text(
        """
        name Test Module
        description A description with spaces
        entry_area start
        entry_x 1.0
        entry_y 2.0
        entry_z 3.0
        entry_facing 180
        event OnHeartbeat hb_script
        string TEST_STRING hello world
        int TEST_INT -5
        """,
        Path("settings.txt"),
    )

    assert settings.name == "Test Module"
    assert settings.description == "A description with spaces"
    assert settings.tag == "MODULE"
    assert settings.events["OnHeartbeat"] == "hb_script"
    assert settings.strings["TEST_STRING"] == "hello world"
    assert settings.ints["TEST_INT"] == -5
    assert settings.dawn_hour == 6
