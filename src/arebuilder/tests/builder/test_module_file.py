from pathlib import Path

from arebuilder.builder.module_file import build_module_ifo
from arebuilder.config.module_settings import parse_settings_text


def test_build_module_ifo_populates_core_fields_and_variables() -> None:
    """Verify that build module ifo populates core fields and variables."""

    settings = parse_settings_text(
        """
        name Test Module
        tag TEST_MODULE
        description Test Description
        custom_tlk fixture
        haks first_hak second_hak
        dawn_hour 9
        dusk_hour 19
        min_per_hour 6
        start_day 1
        start_hour 12
        start_month 2
        start_year 100
        xp_scale 0
        movie intro_movie
        entry_area start_area
        entry_x 1.5
        entry_y 2.5
        entry_z 3.5
        entry_facing 90
        event OnHeartbeat hb_script
        int TEST_INT 5
        float TEST_FLOAT 1.25
        string TEST_STRING hello
        """,
        Path("settings.txt"),
    )

    module_ifo = build_module_ifo(
        settings,
        {
            "start_area.are": Path("/tmp/start_area.are"),
            "ignore.nss": Path("/tmp/ignore.nss"),
        },
    )

    assert str(module_ifo["Mod_Tag"]) == "TEST_MODULE"
    assert str(module_ifo["Mod_CustomTlk"]) == "fixture"
    assert str(module_ifo["Mod_Entry_Area"]) == "start_area"
    assert float(module_ifo["Mod_Entry_X"]) == 1.5
    assert float(module_ifo["Mod_Entry_Y"]) == 2.5
    assert float(module_ifo["Mod_Entry_Z"]) == 3.5
    assert int(module_ifo["Mod_StartYear"]) == 100
    assert str(module_ifo["Mod_StartMovie"]) == "intro_movie"
    assert str(module_ifo["Mod_OnHeartbeat"]) == "hb_script"
    assert [str(entry["Mod_Hak"]) for entry in module_ifo["Mod_HakList"]] == [
        "first_hak",
        "second_hak",
    ]
    assert [str(entry["Area_Name"]) for entry in module_ifo["Mod_Area_list"]] == [
        "start_area"
    ]
    variable_names = [str(entry["Name"]) for entry in module_ifo["VarTable"]]
    assert variable_names == ["TEST_INT", "TEST_FLOAT", "TEST_STRING"]
