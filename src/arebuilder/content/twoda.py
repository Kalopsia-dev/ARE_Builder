from dataclasses import dataclass
from pathlib import Path


class TwoDAError(ValueError):
    """Raised when a ``.2da`` file cannot be parsed or serialized."""


@dataclass(slots=True)
class TwoDARow:
    """One indexed row within a ``.2da`` table."""

    index: int
    values: dict[str, str]


@dataclass(slots=True)
class TwoDAFile:
    """Represent a parsed 2DA table with columns, indexed rows, source path, and default value."""

    columns: list[str]
    rows: list[TwoDARow]
    source_path: Path | None = None
    default_value: str | None = None

    def clone(self) -> "TwoDAFile":
        """Return a deep copy suitable for isolated mutation."""

        return TwoDAFile(
            columns=list(self.columns),
            rows=[
                TwoDARow(index=row.index, values=dict(row.values)) for row in self.rows
            ],
            source_path=self.source_path,
            default_value=self.default_value,
        )

    def row_map(self) -> dict[int, TwoDARow]:
        """Return rows keyed by their integer row index."""

        return {row.index: row for row in self.rows}


def load_2da(
    path: Path,
    *,
    validate_index: bool = True,
    warn_on_reindex: bool = True,
) -> TwoDAFile:
    """Validate and load a latin-1 NWN 2DA file from disk."""

    if not path.is_file() or path.suffix.lower() != ".2da":
        raise FileNotFoundError(
            f"Unable to proceed due to invalid 2DA file path: {path}"
        )
    return parse_2da_text(
        path.read_text(encoding="latin-1"),
        source_path=path,
        validate_index=validate_index,
        warn_on_reindex=warn_on_reindex,
    )


def parse_2da_text(
    text: str,
    *,
    source_path: Path | None = None,
    validate_index: bool = True,
    warn_on_reindex: bool = True,
) -> TwoDAFile:
    """Parse raw 2DA V2.0 text into normalized rows and optional default value metadata."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "2DA V2.0":
        raise TwoDAError("Expected 2DA V2.0 header.")

    content_lines = lines[1:]
    default_value: str | None = None

    # NWN 2DAs commonly include blank padding after the header and optional
    # DEFAULT line. Those lines are structural noise, not table rows.
    while content_lines and not content_lines[0].strip():
        content_lines = content_lines[1:]
    if content_lines and content_lines[0].lstrip().startswith("DEFAULT:"):
        default_value = content_lines[0].split(":", 1)[1].strip()
        content_lines = content_lines[1:]
    while content_lines and not content_lines[0].strip():
        content_lines = content_lines[1:]

    if not content_lines:
        raise TwoDAError("Unable to parse 2DA without a column header row.")

    columns = _tokenize_2da_line(content_lines[0], source_path)
    if not columns:
        raise TwoDAError("Unable to parse 2DA column header row.")

    rows: list[TwoDARow] = []
    for raw_line in content_lines[1:]:
        if not raw_line.strip():
            continue
        tokens = _tokenize_2da_line(raw_line, source_path)
        if not tokens:
            continue
        try:
            row_index = int(tokens[0])
        except ValueError as exc:
            raise TwoDAError(f"Invalid 2DA row index {tokens[0]!r}.") from exc
        value_tokens = tokens[1:]
        if len(value_tokens) > len(columns):
            source_label = source_path.name if source_path else "<memory>"
            raise TwoDAError(
                f"{source_label}: Row {row_index} has {len(value_tokens)} values, "
                f"but only {len(columns)} columns were declared."
            )
        if len(value_tokens) < len(columns):
            # Missing trailing cells are represented by NWN's "****" sentinel.
            value_tokens.extend(["****"] * (len(columns) - len(value_tokens)))
        rows.append(
            TwoDARow(
                index=row_index,
                values={
                    column_name: value_tokens[offset]
                    for offset, column_name in enumerate(columns)
                },
            )
        )

    index_break = _find_first_index_break(rows) if validate_index else None
    if index_break is not None:
        previous_row, offending_row = index_break
        if warn_on_reindex:
            source_label = source_path.name if source_path else "2DA"
            print(
                f"W: {source_label}: Row indices stop ascending at row "
                f"{offending_row} (previous row {previous_row}). Reindexing...",
                flush=True,
            )
        # Legacy tables sometimes contain duplicate or descending row labels. The
        # builder normalizes them to positional indexes before writing outputs.
        rows = [
            TwoDARow(index=offset, values=dict(row.values))
            for offset, row in enumerate(rows)
        ]

    return TwoDAFile(
        columns=columns,
        rows=rows,
        source_path=source_path,
        default_value=default_value,
    )


def write_2da(path: Path, table: TwoDAFile) -> None:
    """Write a ``.2da`` table to disk using normalized spacing."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="latin-1", newline="\n") as handle:
        handle.write("2DA V2.0\n\n")
        if table.default_value is not None:
            handle.write(f"DEFAULT: {_format_2da_value(table.default_value)}\n")
        handle.write(" ".join(table.columns) + "\n")
        for row in table.rows:
            values = [
                _format_2da_value(row.values.get(column_name, "****"))
                for column_name in table.columns
            ]
            handle.write(f"{row.index} {' '.join(values)}\n")


def _tokenize_2da_line(line: str, source_path: Path | None) -> list[str]:
    """Split a 2DA line on whitespace while preserving double-quoted fields.

    The NWN 2DA format only gives special meaning to double quotes. Apostrophes
    are ordinary characters, so a shell-style tokenizer such as ``shlex`` is
    too strict for real data like ``FRAZ-URB'LUU``.
    """

    tokens: list[str] = []
    length = len(line)
    offset = 0

    while offset < length:
        while offset < length and line[offset].isspace():
            offset += 1
        if offset >= length:
            break

        if line[offset] == '"':
            token, offset = _parse_quoted_token(line, offset + 1, source_path)
        else:
            token, offset = _parse_unquoted_token(line, offset)
        tokens.append(token)

    return tokens


def _find_first_index_break(rows: list[TwoDARow]) -> tuple[int, int] | None:
    """Return the first descending row-index transition, if any."""

    for earlier, later in zip(rows, rows[1:]):
        if later.index < earlier.index:
            return earlier.index, later.index
    return None


def _parse_quoted_token(
    line: str, offset: int, source_path: Path | None
) -> tuple[str, int]:
    """Parse one double-quoted token, honoring escaped quotes and backslashes."""

    characters: list[str] = []
    length = len(line)

    while offset < length:
        character = line[offset]
        if character == "\\" and offset + 1 < length:
            next_character = line[offset + 1]
            if next_character in {'"', "\\"}:
                characters.append(next_character)
                offset += 2
                continue
        if character == '"':
            return "".join(characters), offset + 1
        characters.append(character)
        offset += 1

    source_label = source_path.name if source_path else "<memory>"
    raise TwoDAError(f"{source_label}: Unable to parse line: {line!r}")


def _parse_unquoted_token(line: str, offset: int) -> tuple[str, int]:
    """Parse one whitespace-delimited token."""

    start = offset
    length = len(line)
    while offset < length and not line[offset].isspace():
        offset += 1
    return line[start:offset], offset


def _format_2da_value(value: str) -> str:
    """Render one cell value using minimal quoting."""

    if value == "":
        return '""'
    if any(character.isspace() for character in value) or '"' in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value
