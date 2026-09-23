"""Origin markers — how a chunk keeps pointing at the original file (spec §7 约束 4).

The target is: a search hit says "from ``报告.pdf`` page 12", not "from some text
we produced once". The page number therefore has to travel from the converter to
the chunker, and it has to survive the conversion cache — which stores markdown
and nothing else.

So the locator is carried *inside* the markdown, as an HTML comment::

    <!-- kb:page=12 -->

HTML comments are invisible in rendered markdown, harmless to any consumer that
does not care, and — the point — they round-trip through the cache for free. The
alternative (a side table of offsets) would need a schema change to
``conversion_cache`` and would silently degrade to locator-less chunks on every
cache hit.

The chunker strips markers before storing chunk text, so users never see them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass

MARKER_RE = re.compile(r"^<!--\s*kb:(?P<body>.*?)\s*-->\s*$")
FIELD_RE = re.compile(r'(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>"(?:[^"\\]|\\.)*"|\S+)')

# Keys whose values are line numbers rather than text.
_INT_KEYS = frozenset({"page", "row_start", "row_end", "sheet_index"})


def marker(**fields: object) -> str:
    """Render a marker line. ``None`` values are dropped.

    Strings are always quoted. Leaving them bare would make the round trip
    lossy: an unquoted value that happens to look like a number comes back as an
    ``int``, and a spreadsheet named ``2`` is not exotic.
    """
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            parts.append(f"{key}={str(value).lower()}")
        elif isinstance(value, str):
            parts.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
        else:
            parts.append(f"{key}={value}")
    return f"<!-- kb:{' '.join(parts)} -->"


def parse_marker(line: str) -> dict[str, object] | None:
    """Parse a marker line into a locator dict, or ``None`` if it is not one."""
    match = MARKER_RE.match(line.strip())
    if not match:
        return None

    locator: dict[str, object] = {}
    for field in FIELD_RE.finditer(match.group("body")):
        locator[field.group("key")] = _coerce(field.group("value"))
    return locator or None


def _coerce(raw: str) -> object:
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if raw in _INT_KEYS or raw.lstrip("-").isdigit():
        try:
            return int(raw)
        except ValueError:
            return raw
    return raw


@dataclass(slots=True)
class MarkedLine:
    """One line of markdown plus the locator in force at that point."""

    text: str
    locator: dict[str, object] | None


def iter_marked_lines(markdown: str) -> Iterator[MarkedLine]:
    """Walk markdown, attaching the active locator to every content line.

    A locator stays in force until the next marker replaces it (later keys win),
    which is exactly the semantics of "this page's text follows".
    """
    current: dict[str, object] | None = None
    for raw in markdown.splitlines():
        parsed = parse_marker(raw)
        if parsed is not None:
            current = {**(current or {}), **parsed}
            continue
        yield MarkedLine(text=raw, locator=current)


def strip_markers(markdown: str) -> str:
    """Markdown with every marker line removed."""
    return "\n".join(line.text for line in iter_marked_lines(markdown))


def shift_rows(locator: dict[str, object] | None, offset: int) -> dict[str, object] | None:
    """Move a row-addressed locator down by ``offset`` rows.

    Used when a wide table is split into groups: group *k* starts ``offset`` rows
    past the top of the table, so its locator must say so, otherwise every group
    would claim to come from the same spreadsheet row.
    """
    if locator is None or offset == 0:
        return locator
    shifted = dict(locator)
    for key in ("row_start", "row_end"):
        value = shifted.get(key)
        if isinstance(value, int):
            shifted[key] = value + offset
    return shifted
