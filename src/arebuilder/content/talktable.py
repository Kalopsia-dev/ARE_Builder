import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nwn.types import Language

from arebuilder.content.twoda import TwoDAFile, load_2da, write_2da
from arebuilder.nwn.compat import (
    load_talk_table,
    read_erf_members,
    write_erf_archive,
    write_tlk,
)

CUSTOM_TLK_OFFSET = 16_777_216
DEFAULT_SPELL_NAME_DESC_OFFSET = 5000


class CustomContentError(ValueError):
    """Raised when custom-content generation cannot proceed safely."""


@dataclass(slots=True)
class CustomContentPaths:
    """Resolved input paths for custom TLK and HAK generation."""

    root: Path
    input_2da_dir: Path
    input_json_dir: Path
    static_2da_dir: Path
    reference_path: Path


@dataclass(slots=True)
class CustomContentBuildResult:
    """Report generated TLK/HAK paths and archive members."""

    tlk_path: Path
    hak_path: Path
    member_names: list[str]


@dataclass(slots=True)
class _CustomContentLayout:
    """Separate labeled inputs, static fallbacks, and static 2DAs for one content build."""

    labeled_input_names: list[str]
    fallback_static_input_names: list[str]
    static_names: list[str]


class CustomTalkTable:
    """Mutable talk-table allocator for custom TLK offset strrefs."""

    def __init__(self, *, language_id: int = 0):
        """Initialize the instance with its required collaborators and state."""

        self.language_id = language_id
        self._entries: list[str | None] = []
        self._blank_ids: set[int] = set()
        self._text_to_id: dict[str, int] = {}

    @classmethod
    def from_reference(cls, path: Path) -> "CustomTalkTable":
        """Load a reference talk table seed from either tlkify JSON or a binary TLK file."""

        if not path.exists():
            raise FileNotFoundError(f"Unable to locate TLK reference at {path}")
        if path.suffix.lower() == ".json":
            return cls._from_json(path)
        if path.suffix.lower() == ".tlk":
            return cls._from_tlk(path)
        raise CustomContentError(
            f"Unsupported TLK reference format: {path}. Expected .json or .tlk."
        )

    @classmethod
    def _from_json(cls, path: Path) -> "CustomTalkTable":
        """Create a mutable talk table from tlkify's JSON seed format."""

        payload = _load_json_file(path)
        if not isinstance(payload, dict) or set(payload) != {"language", "entries"}:
            raise CustomContentError(
                f"Invalid TLK JSON format in {path}. Expected keys language and entries."
            )
        table = cls(language_id=int(payload["language"]))
        entries = payload["entries"]
        if not isinstance(entries, list):
            raise CustomContentError(f"Invalid TLK entries in {path}.")
        max_id = max((int(entry["id"]) for entry in entries), default=-1)
        table._entries = [None] * (max_id + 1)
        for entry in entries:
            raw_id = int(entry["id"])
            text = str(entry["text"])
            table._entries[raw_id] = text
            table._text_to_id[text] = raw_id
        table._blank_ids = {
            raw_id for raw_id, text in enumerate(table._entries) if text is None
        }
        return table

    @classmethod
    def _from_tlk(cls, path: Path) -> "CustomTalkTable":
        """Create mutable allocator state from a binary TLK file, treating blank strings as reusable IDs."""

        talk_table = load_talk_table(path)
        table = cls(language_id=int(talk_table.language))
        table._entries = [
            entry if entry != "" else None for entry in talk_table.entries
        ]
        table._blank_ids = {
            raw_id for raw_id, text in enumerate(table._entries) if text is None
        }
        for raw_id, text in enumerate(table._entries):
            if text is not None:
                table._text_to_id[text] = raw_id
        return table

    def add(self, text: str) -> int:
        """Add one dynamic talk-table entry and return its offset strref."""

        if text in self._text_to_id:
            return self._text_to_id[text] + CUSTOM_TLK_OFFSET
        if self._blank_ids:
            # tlkify-compatible builds reuse the lowest blank slot before
            # appending so generated strrefs remain stable across rebuilds.
            raw_id = min(self._blank_ids)
            self._blank_ids.remove(raw_id)
        else:
            raw_id = len(self._entries)
            self._entries.append(None)
        self._set_entry(raw_id, text)
        return raw_id + CUSTOM_TLK_OFFSET

    def add_at(self, raw_id: int, text: str) -> int:
        """Add one entry at a fixed raw TLK id used by spell labels."""

        if raw_id <= self.highest_used_id:
            raise CustomContentError(
                f"ID {raw_id} must be greater than the current maximum of {self.highest_used_id}."
            )
        current_length = len(self._entries)
        if raw_id >= current_length:
            self._entries.extend([None] * (raw_id + 1 - current_length))
        self._blank_ids.update(range(current_length, raw_id))
        self._set_entry(raw_id, text)
        return raw_id + CUSTOM_TLK_OFFSET

    @property
    def highest_used_id(self) -> int:
        """Return the highest raw TLK ID that currently contains non-blank text."""

        for raw_id in range(len(self._entries) - 1, -1, -1):
            if self._entries[raw_id] is not None:
                return raw_id
        return -1

    def to_entries(self) -> list[str]:
        """Return entries as dense strings because the TLK writer cannot serialize holes."""

        return [entry or "" for entry in self._entries]

    @property
    def language(self) -> Language:
        """Return the NWN language enum used by the final TLK file."""

        return Language(self.language_id)

    def _set_entry(self, raw_id: int, text: str) -> None:
        """Update allocator indexes after writing text at a raw TLK ID."""

        existing_text = self._entries[raw_id]
        if existing_text is not None:
            self._text_to_id.pop(existing_text, None)
        self._entries[raw_id] = text
        self._text_to_id[text] = raw_id
        self._blank_ids.discard(raw_id)


class CustomContentBuilder:
    """Generate rewritten 2DAs, a custom TLK, and a custom HAK."""

    def __init__(
        self,
        *,
        paths: CustomContentPaths,
        spell_name_desc_offset: int = DEFAULT_SPELL_NAME_DESC_OFFSET,
    ):
        """Initialize the instance with its required collaborators and state."""

        self.paths = paths
        self.spell_name_desc_offset = spell_name_desc_offset
        self.talk_table = CustomTalkTable.from_reference(paths.reference_path)
        self._label_cache: dict[str, dict[int, dict[str, str]]] = {}
        self._input_cache: dict[str, TwoDAFile] = {}
        self._labeled_input_names: set[str] = set()

    def build(
        self,
        *,
        custom_tlk_name: str,
        hak_dir: Path,
        tlk_dir: Path,
    ) -> CustomContentBuildResult:
        """Generate and write TLK and HAK outputs for one custom content set."""

        if not custom_tlk_name:
            raise CustomContentError("custom_tlk_name is required.")

        layout = _classify_custom_content_layout(self.paths)
        self._labeled_input_names = set(layout.labeled_input_names)

        processed_inputs = {
            f"{name}.2da": self._apply_labels(name, self._load_input_2da(name))
            for name in layout.labeled_input_names
        }
        # Static 2DAs are copied into the HAK without relabeling; they provide
        # baseline content that the labeled inputs can override by basename.
        processed_static = {
            f"{name}.2da": load_2da(
                self.paths.static_2da_dir / f"{name}.2da",
                validate_index=False,
                warn_on_reindex=False,
            )
            for name in layout.static_names
        }
        # Input 2DAs without matching JSON are still included, but treated as
        # static fallbacks so missing labels do not cause row-order warnings.
        processed_static_inputs = {
            f"{name}.2da": load_2da(
                self.paths.input_2da_dir / f"{name}.2da",
                validate_index=False,
                warn_on_reindex=False,
            )
            for name in layout.fallback_static_input_names
        }
        all_members = dict(processed_static)
        all_members.update(processed_static_inputs)
        all_members.update(processed_inputs)

        tlk_path = tlk_dir / f"{custom_tlk_name}.tlk"
        hak_path = hak_dir / f"{custom_tlk_name}_hak.hak"

        write_tlk(tlk_path, self.talk_table.to_entries(), self.talk_table.language)

        with tempfile.TemporaryDirectory(
            prefix="arebuilder-custom-content-"
        ) as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            for member_name, table in all_members.items():
                write_2da(temp_dir / member_name, table)
            # The ERF writer consumes file bytes, so 2DAs are normalized through a
            # temporary directory before being packed into the final HAK archive.
            write_erf_archive(
                hak_path,
                b"HAK ",
                [
                    (member_name, (temp_dir / member_name).read_bytes())
                    for member_name in sorted(all_members)
                ],
            )

        return CustomContentBuildResult(
            tlk_path=tlk_path,
            hak_path=hak_path,
            member_names=sorted(all_members),
        )

    def _apply_labels(self, name: str, table: TwoDAFile) -> TwoDAFile:
        """Apply JSON label overrides and synthesized label rules to one input 2DA."""

        label_rows = self._synthesize_missing_labels(
            name, table, self._load_labels(name)
        )
        if not label_rows:
            return table
        if name == "spells":
            return self._apply_spell_labels(table, label_rows)

        updated_table = table.clone()
        row_map = updated_table.row_map()
        for row_id, labels in sorted(label_rows.items()):
            row = row_map.get(row_id)
            if row is None:
                continue
            for column_name, text in labels.items():
                if column_name not in updated_table.columns:
                    continue
                row.values[column_name] = str(self.talk_table.add(text))
        return updated_table

    def _apply_spell_labels(
        self, table: TwoDAFile, label_rows: dict[int, dict[str, str]]
    ) -> TwoDAFile:
        """Apply spell-specific label rules with stable fixed IDs for name and description."""

        updated_table = table.clone()
        row_map = updated_table.row_map()
        # Name and description columns use deterministic fixed IDs to preserve
        # compatibility with existing custom spell references.
        static_columns = (
            ("Name", "SpellDesc") if self.spell_name_desc_offset > 0 else ()
        )
        for row_id, labels in sorted(label_rows.items()):
            row = row_map.get(row_id)
            if row is None:
                continue
            for column_name, text in labels.items():
                if (
                    column_name not in updated_table.columns
                    or column_name in static_columns
                ):
                    continue
                row.values[column_name] = str(self.talk_table.add(text))

        for row_id, labels in sorted(label_rows.items()):
            row = row_map.get(row_id)
            if row is None:
                continue
            for offset, column_name in enumerate(static_columns):
                text = labels.get(column_name)
                if text is None or column_name not in updated_table.columns:
                    continue
                raw_id = (
                    self.spell_name_desc_offset
                    + offset
                    + (len(static_columns) * row_id)
                )
                row.values[column_name] = str(self.talk_table.add_at(raw_id, text))
        return updated_table

    def _synthesize_missing_labels(
        self,
        name: str,
        table: TwoDAFile,
        label_rows: dict[int, dict[str, str]],
    ) -> dict[int, dict[str, str]]:
        """Apply tlkify's derived-label rules to one label mapping."""

        if name == "classes":
            return _populate_class_labels(label_rows)
        if name == "racialtypes":
            return _populate_racialtype_labels(label_rows)
        if name == "iprp_spells":
            return self._populate_iprp_spell_labels(table, label_rows)
        if name == "iprp_feats":
            return self._populate_iprp_feat_labels(table, label_rows)
        return label_rows

    def _populate_iprp_spell_labels(
        self, table: TwoDAFile, label_rows: dict[int, dict[str, str]]
    ) -> dict[int, dict[str, str]]:
        """Generate missing ``iprp_spells`` names from ``spells`` metadata."""

        try:
            spell_table = self._load_input_2da("spells")
        except FileNotFoundError:
            print(
                "W: spells.2da: File not found. iprp_spells may be missing labels.",
                flush=True,
            )
            return label_rows
        spell_labels = self._load_labels("spells")
        spell_rows = spell_table.row_map()

        valid_spell_names: dict[int, str] = {}
        for row_id, labels in spell_labels.items():
            row = spell_rows.get(row_id)
            if row is None:
                continue
            # tlkify only derives item-property spell names for player-facing
            # spells that have no FeatID and use the conventional UserType.
            if row.values.get("FeatID") == "****" and row.values.get("UserType") == "1":
                name_text = labels.get("Name")
                if name_text is not None:
                    valid_spell_names[row_id] = name_text

        updated = {row_id: dict(values) for row_id, values in label_rows.items()}
        for row in table.rows:
            spell_index_text = row.values.get("SpellIndex", "****")
            if spell_index_text == "****":
                continue
            try:
                spell_index = int(spell_index_text)
            except ValueError:
                continue
            current = updated.setdefault(row.index, {})
            if "Name" in current:
                if "****" in current["Name"]:
                    updated.pop(row.index, None)
                continue
            spell_name = valid_spell_names.get(spell_index)
            if spell_name is None:
                updated.pop(row.index, None)
                continue
            generated_name = f"{spell_name} ({row.values.get('CasterLvl', '****')})"
            if "****" not in generated_name:
                current["Name"] = generated_name
        return updated

    def _populate_iprp_feat_labels(
        self, table: TwoDAFile, label_rows: dict[int, dict[str, str]]
    ) -> dict[int, dict[str, str]]:
        """Generate missing ``iprp_feats`` names from ``feat`` metadata."""

        feat_labels = self._load_labels("feat")
        valid_feat_names = {
            row_id: labels["FEAT"]
            for row_id, labels in feat_labels.items()
            if "FEAT" in labels
        }

        updated = {row_id: dict(values) for row_id, values in label_rows.items()}
        for row in table.rows:
            feat_index_text = row.values.get("FeatIndex", "****")
            if feat_index_text == "****":
                continue
            try:
                feat_index = int(feat_index_text)
            except ValueError:
                continue
            current = updated.setdefault(row.index, {})
            if "Name" not in current:
                feat_name = valid_feat_names.get(feat_index)
                if feat_name is not None:
                    current["Name"] = feat_name
        return updated

    def _load_input_2da(self, name: str) -> TwoDAFile:
        """Load and cache one input 2DA by stem, cloning it before callers mutate rows."""

        if name not in self._input_cache:
            is_labeled_input = name in self._labeled_input_names
            self._input_cache[name] = load_2da(
                self.paths.input_2da_dir / f"{name}.2da",
                validate_index=is_labeled_input,
                warn_on_reindex=is_labeled_input,
            )
        return self._input_cache[name].clone()

    def _load_labels(self, name: str) -> dict[int, dict[str, str]]:
        """Load and cache one JSON label mapping by stem, returning a mutable copy."""

        if name not in self._label_cache:
            self._label_cache[name] = load_label_rows(
                self.paths.input_json_dir / f"{name}.json"
            )
        return {
            row_id: dict(values) for row_id, values in self._label_cache[name].items()
        }


def build_custom_content(
    *,
    custom_tlk_name: str,
    custom_content_root: Path,
    hak_dir: Path,
    tlk_dir: Path,
    reference_path: Path | None = None,
    spell_name_desc_offset: int = DEFAULT_SPELL_NAME_DESC_OFFSET,
) -> CustomContentBuildResult | None:
    """Generate custom TLK and HAK outputs when a module enables ``custom_tlk``."""

    if not custom_tlk_name:
        return None
    paths = resolve_custom_content_paths(
        custom_content_root=custom_content_root,
        reference_path=reference_path,
    )
    builder = CustomContentBuilder(
        paths=paths, spell_name_desc_offset=spell_name_desc_offset
    )
    return builder.build(
        custom_tlk_name=custom_tlk_name,
        hak_dir=hak_dir,
        tlk_dir=tlk_dir,
    )


def resolve_custom_content_paths(
    *, custom_content_root: Path, reference_path: Path | None = None
) -> CustomContentPaths:
    """Resolve custom-content directories and reference TLK path, then verify they exist."""

    root = custom_content_root
    resolved_reference = reference_path or (root / "Tlk input" / "original.json")
    paths = CustomContentPaths(
        root=root,
        input_2da_dir=root / "Input 2das",
        input_json_dir=root / "Input json",
        static_2da_dir=root / "arelith_2da",
        reference_path=resolved_reference,
    )
    missing_paths = [
        path
        for path in (
            paths.root,
            paths.input_2da_dir,
            paths.input_json_dir,
            paths.static_2da_dir,
            paths.reference_path,
        )
        if not path.exists()
    ]
    if missing_paths:
        raise FileNotFoundError(
            "Missing custom-content path(s): "
            + ", ".join(str(path) for path in missing_paths)
        )
    return paths


def _classify_custom_content_layout(paths: CustomContentPaths) -> _CustomContentLayout:
    """Classify input/static 2DAs and emit any missing-JSON warnings."""

    input_2da_names = {path.stem for path in paths.input_2da_dir.glob("*.2da")}
    static_2da_names = {path.stem for path in paths.static_2da_dir.glob("*.2da")}
    input_json_names = {path.stem for path in paths.input_json_dir.glob("*.json")}

    duplicate_2da_names = sorted(input_2da_names & static_2da_names)
    missing_input_json = sorted(input_2da_names - input_json_names)
    if missing_input_json:
        # Keep tlkify's warning-before-fallback behavior so operators can repair
        # labels without blocking a build that still has valid static 2DAs.
        for name in missing_input_json:
            print(
                f"W: {name}.2da: Missing Input json/{name}.json.",
                flush=True,
            )
    if duplicate_2da_names:
        raise CustomContentError(
            "\n".join(
                [
                    f"W: {name}.2da: Appears in both Input 2das and arelith_2da."
                    for name in duplicate_2da_names
                ]
            )
        )

    return _CustomContentLayout(
        labeled_input_names=sorted(input_2da_names - set(missing_input_json)),
        fallback_static_input_names=missing_input_json,
        static_names=sorted(static_2da_names),
    )


def load_label_rows(path: Path) -> dict[int, dict[str, str]]:
    """Load tlkify-style JSON row overrides keyed by 2DA row id."""

    if not path.is_file():
        return {}
    payload = _load_json_file(path)
    if not isinstance(payload, list):
        raise CustomContentError(f"Expected a JSON list in {path}.")

    rows: dict[int, dict[str, str]] = {}
    duplicate_ids: list[int] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise CustomContentError(f"Expected object entries in {path}.")
        if "id" not in entry:
            raise CustomContentError(
                f"Unable to proceed due to missing ID column in JSON file: {path}"
            )
        row_id = int(entry["id"])
        values = {
            str(column_name): str(value)
            for column_name, value in entry.items()
            if column_name != "id" and value is not None
        }
        if row_id in rows:
            duplicate_ids.append(row_id)
        # Last duplicate wins, matching tlkify's effective behavior while still
        # warning the user that the source data is ambiguous.
        rows[row_id] = values

    if duplicate_ids:
        print(
            f"W: {path.name}: Duplicate entries for 2DA row(s): {sorted(set(duplicate_ids))}",
            flush=True,
        )
    return dict(sorted(rows.items()))


def _load_json_file(path: Path) -> Any:
    """Load JSON using UTF-8 first and UTF-8-BOM when exported files include a BOM."""

    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return json.loads(path.read_text(encoding=encoding))
        except UnicodeDecodeError:
            continue
        except json.JSONDecodeError:
            continue
    raise CustomContentError(f"Unable to parse JSON file: {path}")


def _populate_class_labels(
    label_rows: dict[int, dict[str, str]],
) -> dict[int, dict[str, str]]:
    """Populate derived class labels using tlkify's plural and lowercase rules."""

    if not any("Name" in labels for labels in label_rows.values()):
        print(
            'W: classes.json: Unable to add additional labels. Missing "Name" column.',
            flush=True,
        )
        return label_rows
    updated = {row_id: dict(values) for row_id, values in label_rows.items()}
    for labels in updated.values():
        name_text = labels.get("Name")
        if name_text is None:
            continue
        labels.setdefault("Plural", _dynamic_plural(name_text))
        labels["Lower"] = name_text.lower()
    return updated


def _populate_racialtype_labels(
    label_rows: dict[int, dict[str, str]],
) -> dict[int, dict[str, str]]:
    """Populate derived racial labels using tlkify's adjective rules."""

    if not any("Name" in labels for labels in label_rows.values()):
        print(
            'W: racialtypes.json: Unable to add additional labels. Missing "Name" column.',
            flush=True,
        )
        return label_rows
    updated = {row_id: dict(values) for row_id, values in label_rows.items()}
    for labels in updated.values():
        name_text = labels.get("Name")
        if name_text is None:
            continue
        labels.setdefault("NamePlural", _dynamic_plural(name_text))
        labels.setdefault("ConverName", _dynamic_adjective(name_text))
        labels.setdefault("ConverNameLower", labels["ConverName"].lower())
        if not labels.get("ConverNameLower"):
            labels["ConverNameLower"] = name_text.lower()
    return updated


def _dynamic_plural(text: str) -> str:
    """Return tlkify-compatible plural text for generated class and racial labels."""

    if text.endswith(("ch", "is", "sh")):
        return text + "es"
    if text.endswith("fe"):
        return text[:-2] + "ves"
    if text.endswith("lf"):
        return text[:-1] + "ves"
    if text.endswith(("s", "x", "z", "o")):
        return text + "es"
    if text.endswith("f"):
        return text[:-1] + "ves"
    if text.endswith("y") and len(text) > 1 and text[-2] not in "aeiou":
        return text[:-1] + "ies"
    return text + "s"


def _dynamic_adjective(text: str) -> str:
    """Return tlkify-compatible adjective text for generated racial labels."""

    if text.endswith("f"):
        return text[:-1] + "ven"
    return text


__all__ = [
    "CUSTOM_TLK_OFFSET",
    "DEFAULT_SPELL_NAME_DESC_OFFSET",
    "CustomContentBuildResult",
    "CustomContentBuilder",
    "CustomContentPaths",
    "CustomTalkTable",
    "build_custom_content",
    "load_label_rows",
    "read_erf_members",
    "resolve_custom_content_paths",
]
