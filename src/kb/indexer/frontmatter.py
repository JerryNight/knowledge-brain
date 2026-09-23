"""YAML frontmatter extraction for markdown notes (spec §6).

`title` and `tags` become columns on `documents`, where they are usable as
retrieval filters. Everything else in the frontmatter is ignored on purpose:
the frontmatter is still part of the markdown that gets indexed, so unknown
keys are not lost. Obsidian-specific syntax (``[[wikilinks]]``, callouts,
dataview blocks) is never rewritten — spec §6 requires it to pass through
untouched, and this module only reads the leading block.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

DELIMITER = "---"


@dataclass(frozen=True, slots=True)
class Frontmatter:
    """Parsed frontmatter plus the body it was stripped from."""

    body: str
    title: str | None = None
    tags: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def parse_frontmatter(text: str) -> Frontmatter:
    """Split a markdown document into frontmatter metadata and body.

    A document without frontmatter, or with malformed frontmatter, is returned
    unchanged with no metadata. Malformed YAML must not fail a sync: the note is
    still perfectly indexable, so the failure mode is "no title/tags", not
    "document rejected".
    """
    if not text.startswith(DELIMITER):
        return Frontmatter(body=text)

    lines = text.splitlines(keepends=True)
    end = _closing_delimiter_index(lines)
    if end is None:
        return Frontmatter(body=text)

    block = "".join(lines[1:end])
    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError:
        return Frontmatter(body=text)
    if not isinstance(loaded, dict):
        return Frontmatter(body=text)

    body = "".join(lines[end + 1 :]).lstrip("\n")
    return Frontmatter(
        body=body,
        title=_as_title(loaded.get("title")),
        tags=_as_tags(loaded.get("tags")),
        raw=loaded,
    )


def derive_title(markdown_body: str) -> str | None:
    """Fall back to the first ATX heading when there is no frontmatter title."""
    for line in markdown_body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading
        elif stripped and not stripped.startswith(("[", "!", "<!--", "|", ">", "```")):
            return stripped[:200]
    return None


def _closing_delimiter_index(lines: list[str]) -> int | None:
    """Index of the ``---`` line that closes the frontmatter block.

    Only the leading block counts, so a ``---`` used as a horizontal rule in the
    body cannot accidentally be read as a terminator... except when it is the
    first such line after the opening delimiter, which is exactly what the
    format requires.
    """
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == DELIMITER:
            return index
    return None


def _as_title(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _as_tags(value: object) -> list[str]:
    """Accept both ``tags: [a, b]`` and the multi-line/single-string forms."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(",", " ").split()]
        return _dedupe(parts)
    if isinstance(value, (list, tuple, set)):
        return _dedupe([str(item).strip() for item in value if str(item).strip()])
    return []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
