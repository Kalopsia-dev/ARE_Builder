import hashlib
import math
from pathlib import Path

from nwn import gff

from arebuilder.config.module_settings import ModuleSettings
from arebuilder.nwn.compat import locstring

EVENT_FIELD_MAP = {
    "OnAcquireItem": "Mod_OnAcquirItem",
    "OnActivateItem": "Mod_OnActvtItem",
    "OnClientEnter": "Mod_OnClientEntr",
    "OnClientLeave": "Mod_OnClientLeav",
    "OnCutsceneAbort": "Mod_OnCutsnAbort",
    "OnHeartbeat": "Mod_OnHeartbeat",
    "OnModuleLoad": "Mod_OnModLoad",
    "OnPlayerChat": "Mod_OnPlrChat",
    "OnPlayerDeath": "Mod_OnPlrDeath",
    "OnPlayerDying": "Mod_OnPlrDying",
    "OnPlayerEquipItem": "Mod_OnPlrEqItm",
    "OnPlayerLevelUp": "Mod_OnPlrLvlUp",
    "OnPlayerRest": "Mod_OnPlrRest",
    "OnPlayerUnEquipItem": "Mod_OnPlrUnEqItm",
    "OnRespawn": "Mod_OnSpawnBtnDn",
    "OnUnAcquireItem": "Mod_OnUnAqreItem",
    "OnUserDefined": "Mod_OnUsrDefined",
}


def build_module_ifo(settings: ModuleSettings, included_files: dict[str, Path]):
    """Build the module.ifo GFF structure from parsed module settings and included files."""

    area_names = [
        Path(basename).stem
        for basename in included_files
        if Path(basename).suffix.lower() == ".are"
    ]

    hak_structs = [
        gff.Struct(8, Mod_Hak=gff.CExoString(hak_name)) for hak_name in settings.haks
    ]
    area_structs = [
        gff.Struct(6, Area_Name=gff.ResRef(area_name)) for area_name in area_names
    ]

    variable_structs = []
    # NWN stores module variables in one list with numeric type tags, so the
    # typed settings dictionaries are normalized into the shared VarTable shape.
    for key, value in settings.ints.items():
        variable_structs.append(
            gff.Struct(
                0,
                Name=gff.CExoString(key),
                Type=gff.Dword(1),
                Value=gff.Int(value),
            )
        )
    for key, value in settings.floats.items():
        variable_structs.append(
            gff.Struct(
                0,
                Name=gff.CExoString(key),
                Type=gff.Dword(2),
                Value=gff.Float(value),
            )
        )
    for key, value in settings.strings.items():
        variable_structs.append(
            gff.Struct(
                0,
                Name=gff.CExoString(key),
                Type=gff.Dword(3),
                Value=gff.CExoString(value),
            )
        )

    facing_radians = math.pi * settings.entry_facing / 180.0
    # Event fields are stored under historical GFF labels that do not match the
    # human-readable settings names one-to-one.
    event_fields = {
        field_name: gff.ResRef(settings.events.get(event_name, "") or "")
        for event_name, field_name in EVENT_FIELD_MAP.items()
    }

    module_id = hashlib.md5(
        "".join(area_names).encode("utf-8"), usedforsecurity=False
    ).hexdigest()

    return gff.Struct(
        0xFFFFFFFF,
        Expansion_Pack=gff.Word(3),
        Mod_Area_list=gff.List(area_structs),
        Mod_CacheNSSList=gff.List(),
        Mod_Creator_ID=gff.Int(2),
        Mod_CustomTlk=gff.CExoString(settings.custom_tlk),
        Mod_CutSceneList=gff.List(),
        Mod_DawnHour=gff.Byte(settings.dawn_hour),
        Mod_Description=locstring(settings.description),
        Mod_DuskHour=gff.Byte(settings.dusk_hour),
        Mod_Entry_Area=gff.ResRef(settings.entry_area),
        Mod_Entry_Dir_X=gff.Float(math.cos(facing_radians)),
        Mod_Entry_Dir_Y=gff.Float(math.sin(facing_radians)),
        Mod_Entry_X=gff.Float(settings.entry_x),
        Mod_Entry_Y=gff.Float(settings.entry_y),
        Mod_Entry_Z=gff.Float(settings.entry_z),
        Mod_Expan_List=gff.List(),
        Mod_GVar_List=gff.List(),
        Mod_HakList=gff.List(hak_structs),
        Mod_ID=gff.VOID(module_id.encode("ascii")),
        Mod_IsSaveGame=gff.Byte(0),
        Mod_MinGameVer=gff.CExoString("1.69"),
        Mod_MinPerHour=gff.Byte(settings.min_per_hour),
        Mod_Name=locstring(settings.name),
        Mod_OnModStart=gff.ResRef(""),
        Mod_StartDay=gff.Byte(settings.start_day),
        Mod_StartHour=gff.Byte(settings.start_hour),
        Mod_StartMonth=gff.Byte(settings.start_month),
        Mod_StartMovie=gff.ResRef(settings.movie or ""),
        Mod_StartYear=gff.Dword(settings.start_year),
        Mod_Tag=gff.CExoString(settings.tag),
        Mod_Version=gff.Dword(3),
        Mod_XPScale=gff.Byte(settings.xp_scale),
        VarTable=gff.List(variable_structs),
        **event_fields,
    )
