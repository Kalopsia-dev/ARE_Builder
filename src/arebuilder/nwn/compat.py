import importlib.metadata
import io
import struct as structlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from nwn import erf, gff, tlk
from nwn.gff._impl import FieldEntry, FieldKind, Header, StructEntry
from nwn.types import Language

EXPECTED_NWN_VERSION = "0.0.22"
NWN_ENCODING = "windows-1252"
INVALID_STRREF = gff.Dword(0xFFFFFFFF)


@dataclass(slots=True)
class TalkTable:
    """In-memory talk table wrapper with compatibility-focused lookups."""

    entries: list[str]
    language: Language

    def text(self, strref: int) -> str:
        """Return the text for a string reference or raise a bounds error."""

        if strref < 0 or strref >= len(self.entries):
            raise ValueError(f"No such string ID: {strref} (size: {len(self.entries)})")
        return self.entries[strref]


def assert_expected_nwn_version() -> str:
    """Verify that the installed runtime matches the pinned compatibility version."""

    version = importlib.metadata.version("nwn")
    if version != EXPECTED_NWN_VERSION:
        # This module deliberately patches around private nwn behavior, so
        # dependency drift must fail loudly before binary readers/writers run.
        raise RuntimeError(
            f"Expected nwn {EXPECTED_NWN_VERSION}, but imported nwn {version}."
        )
    return version


def load_talk_table(path: Path) -> TalkTable:
    """Load the configured ``dialog.tlk`` file once for the current process."""

    assert_expected_nwn_version()
    with path.open("rb") as handle:
        entries, language = tlk.read(handle)
    return TalkTable(entries=[str(entry) for entry in entries], language=language)


def read_gff(path: Path):
    """Read a GFF resource from disk."""

    with path.open("rb") as handle:
        return _read_gff_fixed(handle)


def read_gff_bytes(data: bytes):
    """Read a GFF resource from an in-memory byte payload."""

    with io.BytesIO(data) as handle:
        return _read_gff_fixed(handle)


def write_gff(path: Path, root: gff.Struct, file_type: str) -> None:
    """Write a GFF resource to disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        _write_gff_fixed(handle, root, file_type)


def write_tlk(
    path: Path, entries: list[object], language: Language = Language.ENGLISH
) -> None:
    """Write a synthetic TLK file used by tests and verification fixtures."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        tlk.write(handle, [_tlk_entry(entry) for entry in entries], language)


def write_erf_archive(
    path: Path, file_type: bytes, members: Iterable[tuple[str, bytes]]
) -> None:
    """Write an ERF-like archive using the pinned ``nwn`` writer."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        with erf.Writer(handle, file_type=file_type) as writer:
            for filename, data in members:
                writer.add_file_data(filename, data)


def read_erf_members(path: Path) -> dict[str, bytes]:
    """Read all members from an ERF-like archive into memory."""

    with path.open("rb") as handle:
        reader = erf.Reader(handle)
        return {
            filename: reader.read_file(filename)
            for filename in sorted(reader.filenames)
        }


def normalize_gff(value) -> Any:
    """Convert GFF values into plain Python data for semantic comparisons."""

    if isinstance(value, gff.Struct):
        normalized = {"_struct_id": value.struct_id}
        normalized.update({key: normalize_gff(inner) for key, inner in value.items()})
        return normalized
    if isinstance(value, gff.List):
        return [normalize_gff(item) for item in value]
    if isinstance(value, gff.CExoLocString):
        return {
            "strref": int(value.strref),
            "entries": normalize_locstring_entries(value.entries),
        }
    if isinstance(
        value,
        (
            gff.Byte,
            gff.Char,
            gff.Word,
            gff.Short,
            gff.Dword,
            gff.Int,
            gff.Dword64,
            gff.Int64,
        ),
    ):
        return int(value)
    if isinstance(value, (gff.Float, gff.Double)):
        return float(value)
    if isinstance(value, gff.VOID):
        return bytes(value)
    return str(value)


def locstring(text: str) -> gff.CExoLocString:
    """Create a CExoLocString with inline English text and no external strref."""

    return gff.CExoLocString(INVALID_STRREF, {Language.ENGLISH: text})


def normalize_locstring_entries(entries: Mapping[object, str]) -> dict[int, str]:
    """Return localized-string entries keyed by raw engine entry IDs."""

    return {
        _locstring_entry_id(language_id): text for language_id, text in entries.items()
    }


def _tlk_entry(entry: object):
    """Return an nwn 0.0.22 TLK entry while accepting older plain-string callers."""

    if hasattr(entry, "text"):
        return entry
    return tlk.Entry(str(entry))


def _locstring_entry_id(language_id: object) -> int:
    """Convert nwn language key variants to raw CExoLocString entry IDs."""

    if hasattr(language_id, "to_id"):
        return int(language_id.to_id())
    return int(language_id)


def _read_gff_fixed(file_handle):
    """Read GFF data while preserving raw localized-string entry IDs.

    The pinned ``nwn`` reader raises ``ValueError`` when a
    ``CExoLocString`` contains an entry ID outside its small ``Language`` enum.
    Real NWN assets can contain additional IDs, so we keep those raw integers
    instead of rejecting the whole resource.
    """

    root_offset = file_handle.tell()
    labels: list[str] = []
    fields: list[FieldEntry] = []
    structs: list[StructEntry] = []
    list_indices: list[int] = []
    field_indices: list[int] = []
    resolved_structs: dict[int, gff.Struct] = {}
    struct_parents: dict[int, object] = {}

    header = Header(
        file_handle.read(4).decode("ascii"),
        file_handle.read(4).decode("ascii"),
        *structlib.unpack("<12i", file_handle.read(48)),
    )
    if header.file_version != "V3.2":
        raise ValueError(f"Unsupported GFF version: {header.file_version}")

    file_handle.seek(root_offset + header.label_offset)
    for _ in range(header.label_count):
        labels.append(file_handle.read(16).split(b"\x00")[0].decode("ascii"))

    file_handle.seek(root_offset + header.field_offset)
    for _ in range(header.field_count):
        field_type, label_index, data_or_offset = structlib.unpack(
            "<III", file_handle.read(12)
        )
        # Normalize raw integer kinds immediately; later decode paths can then
        # branch on FieldKind instead of magic numbers from the file.
        fields.append(FieldEntry(FieldKind(field_type), label_index, data_or_offset))

    file_handle.seek(root_offset + header.field_indices_offset)
    for _ in range(header.field_indices_size // 4):
        field_indices.append(structlib.unpack("<I", file_handle.read(4))[0])

    file_handle.seek(root_offset + header.list_indices_offset)
    for _ in range(header.list_indices_size // 4):
        list_indices.append(structlib.unpack("<I", file_handle.read(4))[0])

    file_handle.seek(root_offset + header.struct_offset)
    for _ in range(header.struct_count):
        structs.append(StructEntry(*structlib.unpack("<III", file_handle.read(12))))

    def _read_field_value(field: FieldEntry):
        """Read a GFF field payload using the fixed reader compatibility rules."""

        simple_types = {
            FieldKind.BYTE: ("B", gff.Byte),
            FieldKind.CHAR: ("b", gff.Char),
            FieldKind.WORD: ("H", gff.Word),
            FieldKind.SHORT: ("h", gff.Short),
            FieldKind.DWORD: ("I", gff.Dword),
            FieldKind.INT: ("i", gff.Int),
            FieldKind.FLOAT: ("f", gff.Float),
        }

        if field.type in simple_types:
            unpack_spec, value_class = simple_types[field.type]
            # Small scalar values are stored directly inside data_or_offset in
            # the GFF field table rather than in the field-data blob.
            data = structlib.pack("<I", field.data_or_offset)[
                : structlib.calcsize(unpack_spec)
            ]
            return value_class(structlib.unpack("<" + unpack_spec, data)[0])

        file_handle.seek(root_offset + header.field_data_offset + field.data_or_offset)

        if field.type == FieldKind.DOUBLE:
            return gff.Double(structlib.unpack("<d", file_handle.read(8))[0])
        if field.type == FieldKind.DWORD64:
            return gff.Dword64(structlib.unpack("<Q", file_handle.read(8))[0])
        if field.type == FieldKind.INT64:
            return gff.Int64(structlib.unpack("<q", file_handle.read(8))[0])
        if field.type == FieldKind.CEXOSTRING:
            size = structlib.unpack("<I", file_handle.read(4))[0]
            if size > 0xFFFF:
                raise ValueError("String too long")
            return gff.CExoString(file_handle.read(size).decode(NWN_ENCODING))
        if field.type == FieldKind.RESREF:
            size = structlib.unpack("<b", file_handle.read(1))[0]
            if size > 16:
                raise ValueError("Resref too long")
            return gff.ResRef(file_handle.read(size).decode(NWN_ENCODING))
        if field.type == FieldKind.CEXOLOCSTRING:
            _ = structlib.unpack("<I", file_handle.read(4))[0]
            strref = gff.Dword(structlib.unpack("<I", file_handle.read(4))[0])
            count = structlib.unpack("<I", file_handle.read(4))[0]
            entries: dict[int, str] = {}
            for _ in range(count):
                # Preserve raw localized-string IDs instead of coercing to the
                # small Language enum; live assets can use IDs nwn rejects.
                entry_id = structlib.unpack("<I", file_handle.read(4))[0]
                size = structlib.unpack("<I", file_handle.read(4))[0]
                entries[entry_id] = file_handle.read(size).decode(NWN_ENCODING)
            return gff.CExoLocString(strref, entries)
        if field.type == FieldKind.VOID:
            size = structlib.unpack("<I", file_handle.read(4))[0]
            return gff.VOID(file_handle.read(size))
        if field.type == FieldKind.LIST:
            offset = field.data_or_offset // 4
            size = list_indices[offset]
            start = offset + 1
            end = start + size
            return gff.List(
                [_read_struct(field, index) for index in list_indices[start:end]]
            )
        if field.type == FieldKind.STRUCT:
            return _read_struct(field, field.data_or_offset)
        raise NotImplementedError(f"Field {field} not implemented")

    def _read_struct(parent, struct_index: int) -> gff.Struct:
        """Read a GFF struct and recursively attach its decoded fields."""

        if struct_index in resolved_structs:
            if struct_parents[struct_index] != parent:
                raise ValueError("Struct already resolved with different parent")
            return resolved_structs[struct_index]

        struct_entry = structs[struct_index]
        if struct_entry.field_count == 1:
            # One field index is stored inline; multiple indexes live in the
            # shared field-index table.
            field_array_indices = [struct_entry.data_or_offset]
        else:
            start = struct_entry.data_or_offset // 4
            end = start + struct_entry.field_count
            if end < start:
                raise ValueError("Field index array out of bounds")
            field_array_indices = field_indices[start:end]

        resolved_structs[struct_index] = gff.Struct(
            struct_entry.id,
            **{
                labels[field_entry.label_index]: _read_field_value(field_entry)
                for field_entry in (fields[index] for index in field_array_indices)
            },
        )
        struct_parents[struct_index] = parent
        return resolved_structs[struct_index]

    return _read_struct(None, 0), header.file_type


def _write_gff_fixed(file_handle, root: gff.Struct, file_type: str) -> None:
    """Write GFF data with corrected list offsets for the pinned nwn runtime."""

    labels: list[bytes] = []
    fields: list[FieldEntry] = []
    structs: list[StructEntry | None] = []
    list_indices: list[int] = []
    field_indices: list[int] = []
    field_data = bytearray()
    label_to_index: dict[bytes, int] = {}

    def _add_label(label: str) -> int:
        """Deduplicate a label string and return its index."""

        # Labels are fixed-width ASCII slots. Reusing existing slots keeps output
        # compact and matches the binary GFF table layout.
        encoded = label.encode("ascii")[0:16].ljust(16, b"\x00")
        if encoded in label_to_index:
            return label_to_index[encoded]
        label_to_index[encoded] = len(labels)
        labels.append(encoded)
        return label_to_index[encoded]

    def _process_field(name: str, value) -> int:
        """Serialize one GFF field and collect any labels, data, or nested indexes it needs."""

        data_or_offset = len(field_data)

        if isinstance(value, gff.Byte):
            data_or_offset = int(value)
        elif isinstance(value, gff.Char):
            data_or_offset = structlib.unpack(
                "<I", structlib.pack("<b", int(value)) + b"\x00" * 3
            )[0]
        elif isinstance(value, gff.Word):
            data_or_offset = int(value)
        elif isinstance(value, gff.Short):
            data_or_offset = structlib.unpack(
                "<I", structlib.pack("<h", int(value)) + b"\x00" * 2
            )[0]
        elif isinstance(value, gff.Dword):
            data_or_offset = int(value)
        elif isinstance(value, gff.Int):
            data_or_offset = structlib.unpack("<I", structlib.pack("<i", int(value)))[0]
        elif isinstance(value, gff.Dword64):
            field_data.extend(structlib.pack("<Q", int(value)))
        elif isinstance(value, gff.Int64):
            field_data.extend(structlib.pack("<q", int(value)))
        elif isinstance(value, gff.Float):
            data_or_offset = structlib.unpack("<I", structlib.pack("<f", float(value)))[
                0
            ]
        elif isinstance(value, gff.Double):
            field_data.extend(structlib.pack("<d", float(value)))
        elif isinstance(value, gff.CExoString):
            encoded = value.encode(NWN_ENCODING)
            field_data.extend(structlib.pack("<I", len(encoded)))
            field_data.extend(encoded)
        elif isinstance(value, gff.ResRef):
            encoded = value.encode(NWN_ENCODING)
            if len(encoded) > 16:
                raise ValueError("Resref too long")
            field_data.extend(structlib.pack("<B", len(encoded)))
            field_data.extend(encoded)
        elif isinstance(value, gff.CExoLocString):
            field_data.extend(structlib.pack("<I", 0))
            field_data.extend(structlib.pack("<I", int(value.strref)))
            entries = normalize_locstring_entries(value.entries)
            field_data.extend(structlib.pack("<I", len(entries)))
            for language_id, text in entries.items():
                # Pair with the fixed reader by preserving raw entry IDs exactly
                # as supplied by callers.
                encoded = text.encode(NWN_ENCODING)
                field_data.extend(structlib.pack("<I", language_id))
                field_data.extend(structlib.pack("<I", len(encoded)))
                field_data.extend(encoded)
        elif isinstance(value, gff.VOID):
            field_data.extend(structlib.pack("<I", len(value)))
            field_data.extend(value)
        elif isinstance(value, gff.List):
            data_or_offset = _process_list(value)
        elif isinstance(value, gff.Struct):
            data_or_offset = _process_struct(value)
        else:
            raise ValueError(f"Field type {type(value)} cannot be serialized to GFF")

        fields.append(
            FieldEntry(
                type=value.__class__.FIELD_KIND,
                label_index=_add_label(name),
                data_or_offset=data_or_offset,
            )
        )
        return len(fields) - 1

    def _process_struct(struct_obj: gff.Struct) -> int:
        """Serialize a GFF struct header and recursively process its child fields."""

        structs.append(None)
        struct_id = len(structs) - 1
        struct_field_indices = [
            _process_field(name, value) for name, value in struct_obj.items()
        ]

        if len(struct_field_indices) == 1:
            structs[struct_id] = StructEntry(
                id=struct_obj.struct_id,
                data_or_offset=struct_field_indices[0],
                field_count=1,
            )
        else:
            offset = len(field_indices)
            field_indices.extend(struct_field_indices)
            structs[struct_id] = StructEntry(
                id=struct_obj.struct_id,
                data_or_offset=offset * 4,
                field_count=len(struct_field_indices),
            )
        return struct_id

    def _process_list(list_obj: gff.List) -> int:
        """Serialize a GFF list by writing child struct indexes into the list table."""

        offset = len(list_indices)
        list_indices.append(len(list_obj))
        list_indices.extend([0] * len(list_obj))
        for index, struct_obj in enumerate(list_obj):
            list_indices[offset + 1 + index] = _process_struct(struct_obj)
        return offset * 4

    _process_struct(root)

    header_size = 56
    struct_offset = header_size
    field_offset = struct_offset + len(structs) * 12
    label_offset = field_offset + len(fields) * 12
    field_data_offset = label_offset + len(labels) * 16
    field_indices_offset = field_data_offset + len(field_data)
    list_indices_offset = field_indices_offset + len(field_indices) * 4

    # The section order below mirrors the offset layout advertised by the GFF
    # V3.2 header.
    file_handle.write(file_type.encode("ascii"))
    file_handle.write(b"V3.2")
    file_handle.write(
        structlib.pack(
            "<12i",
            struct_offset,
            len(structs),
            field_offset,
            len(fields),
            label_offset,
            len(labels),
            field_data_offset,
            len(field_data),
            field_indices_offset,
            len(field_indices) * 4,
            list_indices_offset,
            len(list_indices) * 4,
        )
    )

    for struct_entry in structs:
        assert struct_entry is not None
        file_handle.write(
            structlib.pack(
                "<III",
                struct_entry.id,
                struct_entry.data_or_offset,
                struct_entry.field_count,
            )
        )

    for field_entry in fields:
        file_handle.write(
            structlib.pack(
                "<III",
                int(field_entry.type),
                field_entry.label_index,
                field_entry.data_or_offset,
            )
        )

    for label in labels:
        file_handle.write(label)

    file_handle.write(field_data)

    for index in field_indices:
        file_handle.write(structlib.pack("<I", index))

    for index in list_indices:
        file_handle.write(structlib.pack("<I", index))
