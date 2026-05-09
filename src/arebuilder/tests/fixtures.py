import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from nwn import gff
from nwn.types import Language

from arebuilder.nwn.compat import (
    locstring,
    write_gff,
    write_tlk,
)

SETTINGS_TEXT = """# Synthetic module settings
name Synthetic Module
tag SYNTH_MODULE
description Synthetic test module
entry_area module_entry
entry_x 10.0
entry_y 10.0
entry_z 0.0
entry_facing 90
custom_tlk fixture
haks fixture_hak
dawn_hour 9
dusk_hour 19
min_per_hour 6
start_day 1
start_hour 12
start_month 1
start_year 100
xp_scale 0
movie
event OnClientEnter synthetic_enter
event OnHeartbeat synth_hb
int MI_DEBUG 1
float TEST_FLOAT 1.5
string TEST_STRING hello synthetic world
"""

CREATURE_PALETTE = """1 Creatures
2.0 Creatures/Beasts
"""

GENERIC_PALETTE = """1 Default
"""


@dataclass(slots=True)
class SyntheticFixture:
    """Resolved paths for a generated synthetic fixture tree."""

    root: Path
    build_target: str
    module_name: str
    arebuilder_env_path: Path
    talktable_path: Path
    are_resources_dir: Path
    module_resources_dir: Path
    compiled_resources_dir: Path
    build_dir: Path
    modules_dir: Path
    module_archive_path: Path
    nwn_home_dir: Path
    override_dir: Path
    hak_dir: Path
    tlk_dir: Path
    custom_content_root: Path
    precompiled_dir: Path


def create_synthetic_fixture(
    root: Path,
    *,
    module_name: str = "are-dev-synthetic",
    layout: str = "host",
) -> SyntheticFixture:
    """Create a minimal but realistic fixture tree for builder verification."""

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    are_resources_dir = root / "are-resources"
    build_target = _build_target_from_module_name(module_name)
    module_resources_dir = root / f"{build_target}-resources"
    compiled_resources_dir = root / "compiled-resources"
    build_dir = compiled_resources_dir / module_name
    nwn_home_dir = root / "server"
    modules_dir = nwn_home_dir / "modules"
    override_dir = nwn_home_dir / "override"
    hak_dir = nwn_home_dir / "hak"
    tlk_dir = nwn_home_dir / "tlk"
    data_dir = root / "data"
    config_dir = root / "config"
    precompiled_dir = root / "precompiled"
    custom_content_root = are_resources_dir / "Custom content"

    for directory in (
        are_resources_dir / "gff",
        are_resources_dir / "override",
        custom_content_root,
        module_resources_dir,
        build_dir,
        modules_dir,
        override_dir,
        hak_dir,
        tlk_dir,
        data_dir,
        config_dir,
        precompiled_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    talktable_path = data_dir / "dialog.tlk"
    _write_fixture_tlk(talktable_path)
    _write_shared_resources(are_resources_dir)
    _write_module_resources(module_resources_dir)
    write_synthetic_custom_content(custom_content_root)
    _write_precompiled_scripts(precompiled_dir)
    (compiled_resources_dir / "global_script.ncs").write_bytes(
        b"compiled-global-script"
    )

    arebuilder_env_path = config_dir / "arebuilder.env"
    arebuilder_env_path.write_text(
        f"BUILD_TARGET={build_target}\nNWN_HOME_PATH={nwn_home_dir}\n",
        encoding="utf-8",
    )

    module_archive_path = modules_dir / f"{module_name}.mod"
    return SyntheticFixture(
        root=root,
        build_target=build_target,
        module_name=module_name,
        arebuilder_env_path=arebuilder_env_path,
        talktable_path=talktable_path,
        are_resources_dir=are_resources_dir,
        module_resources_dir=module_resources_dir,
        compiled_resources_dir=compiled_resources_dir,
        build_dir=build_dir,
        modules_dir=modules_dir,
        module_archive_path=module_archive_path,
        nwn_home_dir=nwn_home_dir,
        override_dir=override_dir,
        hak_dir=hak_dir,
        tlk_dir=tlk_dir,
        custom_content_root=custom_content_root,
        precompiled_dir=precompiled_dir,
    )


def _build_target_from_module_name(module_name: str) -> str:
    """Derive the build-target suffix from a conventional are-dev module name."""

    if module_name.startswith("are-dev-"):
        return module_name.removeprefix("are-dev-")
    return module_name


def _write_fixture_tlk(path: Path) -> None:
    """Write a small TLK file with the entries used by the synthetic fixture."""

    entries = [""] * 64
    entries[1] = "Creatures"
    entries[2] = "Beasts"
    entries[10] = "TLK Beast"
    entries[20] = "Default"
    write_tlk(path, entries, Language.ENGLISH)


def write_synthetic_custom_content(custom_content_root: Path) -> None:
    """Create a minimal custom-content corpus for TLK and HAK tests."""

    input_2da_dir = custom_content_root / "Input 2das"
    input_json_dir = custom_content_root / "Input json"
    static_2da_dir = custom_content_root / "arelith_2da"
    tlk_input_dir = custom_content_root / "Tlk input"

    for directory in (input_2da_dir, input_json_dir, static_2da_dir, tlk_input_dir):
        directory.mkdir(parents=True, exist_ok=True)

    _write_original_tlk_seed(tlk_input_dir / "original.json")
    _write_input_2da(
        input_2da_dir / "classes.2da",
        columns=["Label", "Name", "Plural", "Lower"],
        rows=[
            [0, "Wizard", "****", "****"],
        ],
    )
    _write_input_2da(
        input_2da_dir / "racialtypes.2da",
        columns=["Label", "Name", "ConverName", "ConverNameLower", "NamePlural"],
        rows=[
            [0, "Elf", "****", "****", "****"],
        ],
    )
    _write_input_2da(
        input_2da_dir / "feat.2da",
        columns=["FEAT"],
        rows=[
            [0, "PowerAttack"],
        ],
    )
    _write_input_2da(
        input_2da_dir / "iprp_feats.2da",
        columns=["Label", "Name", "FeatIndex"],
        rows=[
            [0, "FeatProperty", "****", "0"],
        ],
    )
    _write_input_2da(
        input_2da_dir / "spells.2da",
        columns=["Label", "Name", "SpellDesc", "FeatID", "UserType"],
        rows=[
            [0, "SpellZero", "****", "****", "****", "1"],
            [1, "SpellOne", "****", "****", "****", "1"],
        ],
    )
    _write_input_2da(
        input_2da_dir / "iprp_spells.2da",
        columns=["Label", "Name", "SpellIndex", "CasterLvl"],
        rows=[
            [0, "SpellProperty", "****", "0", "3"],
        ],
    )
    _write_input_2da(
        static_2da_dir / "static_example.2da",
        columns=["Label", "Value"],
        rows=[
            [0, "STATIC", "1"],
        ],
    )

    _write_json_rows(
        input_json_dir / "classes.json",
        [
            {"id": 0, "Name": "Wizard"},
        ],
    )
    _write_json_rows(
        input_json_dir / "racialtypes.json",
        [
            {"id": 0, "Name": "Elf"},
        ],
    )
    _write_json_rows(
        input_json_dir / "feat.json",
        [
            {"id": 0, "FEAT": "Power Attack Display"},
        ],
    )
    _write_json_rows(input_json_dir / "iprp_feats.json", [])
    _write_json_rows(input_json_dir / "iprp_spells.json", [])
    _write_json_rows(
        input_json_dir / "spells.json",
        [
            {"id": 0, "Name": "Custom Spell", "SpellDesc": "Spell Description Zero"},
            {"id": 1, "SpellDesc": "Spell Description One"},
        ],
    )


def _write_shared_resources(are_resources_dir: Path) -> None:
    """Create shared resources used by the synthetic fixture."""

    write_gff(
        are_resources_dir / "gff" / "shared_area.are",
        gff.Struct(0xFFFFFFFF),
        "ARE ",
    )
    (are_resources_dir / "override" / "shared_override.2da").write_text(
        "2DA V2.0\n\nLABEL VALUE\n0 SHARED 1\n",
        encoding="utf-8",
    )


def _write_original_tlk_seed(path: Path) -> None:
    """Write a sparse JSON TLK seed that exercises blank-id reuse."""

    path.write_text(
        """{
  "language": 0,
  "entries": [
    {"id": 0, "text": "Bad Strref"},
    {"id": 2, "text": "Seeded Label"},
    {"id": 44, "text": "Late Seed"}
  ]
}
""",
        encoding="utf-8",
    )


def _write_input_2da(
    path: Path, *, columns: list[str], rows: Iterable[list[object]]
) -> None:
    """Write a compact synthetic 2DA table."""

    body_lines = ["2DA V2.0", "", " ".join(columns)]
    for row in rows:
        body_lines.append(" ".join(str(value) for value in row))
    path.write_text("\n".join(body_lines) + "\n", encoding="latin-1")


def _write_json_rows(path: Path, rows: list[dict[str, object]]) -> None:
    """Write a JSON row override file used by custom-content tests."""

    import json

    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def _write_module_resources(module_resources_dir: Path) -> None:
    """Create target-specific resources and palette definitions."""

    (module_resources_dir / "settings.txt").write_text(SETTINGS_TEXT, encoding="utf-8")
    (module_resources_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (module_resources_dir / "scripts" / "ignored.nss").write_text(
        "void main() {}", encoding="utf-8"
    )
    (module_resources_dir / "notes.txt").write_text(
        "ignored by link mode", encoding="utf-8"
    )
    write_gff(module_resources_dir / "module_entry.are", gff.Struct(0xFFFFFFFF), "ARE ")

    _write_faction_file(module_resources_dir / "repute.fac")
    _write_creature_palette(module_resources_dir)
    _write_generic_palette(
        module_resources_dir=module_resources_dir,
        palette_type="door",
        directory_name="Default",
        resource_name="gate.utd",
        gff_type="UTD ",
        field_name="LocName",
        display_name="Training Gate",
    )
    _write_generic_palette(
        module_resources_dir=module_resources_dir,
        palette_type="encounter",
        directory_name="Default",
        resource_name="ambush.ute",
        gff_type="UTE ",
        field_name="LocalizedName",
        display_name="Training Ambush",
    )
    _write_generic_palette(
        module_resources_dir=module_resources_dir,
        palette_type="item",
        directory_name="Default",
        resource_name="training_sword.uti",
        gff_type="UTI ",
        field_name="LocalizedName",
        display_name="Training Sword",
    )
    _write_generic_palette(
        module_resources_dir=module_resources_dir,
        palette_type="placeable",
        directory_name="Default",
        resource_name="training_crate.utp",
        gff_type="UTP ",
        field_name="LocName",
        display_name="Training Crate",
    )
    _write_generic_palette(
        module_resources_dir=module_resources_dir,
        palette_type="sound",
        directory_name="Default",
        resource_name="training_sound.uts",
        gff_type="UTS ",
        field_name="LocName",
        display_name="Training Sound",
    )
    _write_generic_palette(
        module_resources_dir=module_resources_dir,
        palette_type="store",
        directory_name="Default",
        resource_name="training_store.utm",
        gff_type="UTM ",
        field_name="LocName",
        display_name="Training Store",
    )
    _write_generic_palette(
        module_resources_dir=module_resources_dir,
        palette_type="trigger",
        directory_name="Default",
        resource_name="training_trigger.utt",
        gff_type="UTT ",
        field_name="LocalizedName",
        display_name="Training Trigger",
    )
    _write_generic_palette(
        module_resources_dir=module_resources_dir,
        palette_type="waypoint",
        directory_name="Default",
        resource_name="train_waypt.utw",
        gff_type="UTW ",
        field_name="LocalizedName",
        display_name="Training Waypoint",
    )


def _write_precompiled_scripts(precompiled_dir: Path) -> None:
    """Create a single precompiled script file for compile-mode verification."""

    (precompiled_dir / "fixture_precompiled.ncs").write_bytes(b"precompiled-script")


def _write_faction_file(path: Path) -> None:
    """Create a minimal ``repute.fac`` file used by creature palette generation."""

    faction_list = gff.List(
        [
            gff.Struct(
                0,
                FactionParentID=gff.Dword(0xFFFFFFFF),
                FactionName=gff.CExoString("PC"),
                FactionGlobal=gff.Int(0),
            ),
            gff.Struct(
                0,
                FactionParentID=gff.Dword(0xFFFFFFFF),
                FactionName=gff.CExoString("Hostile"),
                FactionGlobal=gff.Int(1),
            ),
        ]
    )
    write_gff(
        path,
        gff.Struct(0xFFFFFFFF, FactionList=faction_list, RepList=gff.List()),
        "FAC ",
    )


def _write_creature_palette(module_resources_dir: Path) -> None:
    """Write the creature palette definition and its backing resources."""

    creature_dir = module_resources_dir / "creature" / "Creatures" / "Beasts"
    creature_dir.mkdir(parents=True, exist_ok=True)
    (module_resources_dir / "creature" / "palette.txt").write_text(
        CREATURE_PALETTE, encoding="utf-8"
    )
    write_gff(
        creature_dir / "beast.utc",
        gff.Struct(
            0xFFFFFFFF,
            FirstName=gff.CExoLocString(gff.Dword(10), {}),
            ChallengeRating=gff.Float(5.0),
            FactionID=gff.Int(1),
        ),
        "UTC ",
    )


def _write_generic_palette(
    *,
    module_resources_dir: Path,
    palette_type: str,
    directory_name: str,
    resource_name: str,
    gff_type: str,
    field_name: str,
    display_name: str,
) -> None:
    """Write one non-creature palette type with a single localized resource."""

    resource_dir = module_resources_dir / palette_type / directory_name
    resource_dir.mkdir(parents=True, exist_ok=True)
    (module_resources_dir / palette_type / "palette.txt").write_text(
        GENERIC_PALETTE, encoding="utf-8"
    )
    write_gff(
        resource_dir / resource_name,
        gff.Struct(0xFFFFFFFF, **{field_name: locstring(display_name)}),
        gff_type,
    )
