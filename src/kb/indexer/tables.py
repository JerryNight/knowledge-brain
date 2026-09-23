"""Markdown table parsing and rendering.

Two consumers, one implementation:

* the spreadsheet converter renders a sheet as a markdown table, and
* the chunker re-renders wide tables in row groups, repeating the header
  (spec §8) so the tail of a big sheet is not a wall of bare numbers.

Keeping both on the same code path is what makes "repeated header" trustworthy:
the chunker parses what the converter emitted instead of guessing at its shape.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

# A cell that is nothing but dashes (with optional alignment colons) is the
# separator row of a markdown table.
_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")


def is_table_line(line: str) -> bool:
    """True for a line that looks like a markdown table row."""
    stripped = line.strip()
    return len(stripped) >= 2 and stripped.startswith("|") and stripped.endswith("|")


def split_row(line: str) -> list[str]:
    """Split one markdown table row into cells, honouring ``\\|`` escapes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in stripped:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def _is_separator_row(cells: Sequence[str]) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL.match(cell.strip()) for cell in cells)


def escape_cell(value: object) -> str:
    """Make an arbitrary value safe inside a markdown table cell."""
    if value is None:
        return ""
    text = str(value).replace("\\", "\\\\").replace("|", "\\|")
    return " ".join(text.split())


def render_markdown_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a markdown table. The header is repeated by the caller as needed."""
    width = len(header)
    lines = [
        "| " + " | ".join(escape_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in rows:
        padded = list(row[:width]) + [""] * max(0, width - len(row))
        lines.append("| " + " | ".join(escape_cell(cell) for cell in padded) + " |")
    return "\n".join(lines)


@dataclass(slots=True)
class MarkdownTable:
    """A parsed table: header cells plus data rows, position-free by design."""

    header: list[str]
    rows: list[list[str]] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        return len(self.header)

    def render(self, rows: Sequence[Sequence[str]] | None = None) -> str:
        """Render the whole table, or just ``rows`` under a repeated header."""
        return render_markdown_table(self.header, self.rows if rows is None else rows)


def parse_markdown_table(lines: Sequence[str]) -> MarkdownTable | None:
    """Parse consecutive markdown table lines into a table, or ``None``.

    Returns ``None`` when the block is not a table — a single ``|``-delimited
    line is not enough, and the separator row is required. That matters because
    Obsidian notes contain plenty of lines starting with ``|`` that are not
    tables, and misclassifying them would make the chunker rewrite user text.
    """
    block = [line for line in lines if line.strip()]
    if len(block) < 2 or not all(is_table_line(line) for line in block):
        return None

    header = split_row(block[0])
    separator = split_row(block[1])
    if not _is_separator_row(separator):
        return None

    rows = [split_row(line) for line in block[2:]]
    return MarkdownTable(header=header, rows=rows)


def is_wide(table: MarkdownTable, *, max_columns: int) -> bool:
    """True when a table has more columns than a chunk can carry meaningfully.

    Column count, not row count, is the signal: a two-column table with ten
    thousand rows slices cleanly, whereas fourteen columns produce rows that read
    as noise once truncated. The actual decision to split is made on token
    count; this is the cheap pre-filter.
    """
    return table.column_count > max_columns
