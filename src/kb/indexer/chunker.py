"""Markdown-aware chunking — spec §8, implemented as pure functions.

Markdown structure is free information, so the splitter uses heading boundaries
rather than a fixed character count. The rules, in the order they are applied:

1. Sections are cut at H1 / H2 / H3 boundaries. A deeper heading (H4+) stays
   inside its section as content — it labels that part of the text but does not
   start a new section.
2. A section under 120 tokens is merged upward into the previous one — fragments
   pollute retrieval more than they help it.
3. A section over 800 tokens is re-split by paragraph into a sliding window with
   15% overlap.
4. Code blocks are never cut. A window that would split one keeps it whole and
   lets that chunk run long.
5. Small tables stay whole; wide tables are grouped by rows **with the header
   repeated** in every group, otherwise the tail of a spreadsheet is a wall of
   bare numbers.
6. Chunks under 10 or over 8000 tokens are not embedded at all — they only enter
   the full-text index (spec §8's cost-control rule).

The result carries both halves of the embedding/storage split from spec §8:
``text`` is the clean original for the user, ``embed_text`` prefixes the heading
context that the vector actually needs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from kb.indexer.locator import MarkedLine, iter_marked_lines
from kb.indexer.tables import MarkdownTable, escape_cell, is_table_line, is_wide, parse_markdown_table
from kb.indexer.tokens import estimate_tokens

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

BLOCK_TEXT = "text"
BLOCK_HEADING = "heading"
BLOCK_CODE = "code"
BLOCK_TABLE = "table"


@dataclass(frozen=True, slots=True)
class ChunkOptions:
    """Chunking parameters. Defaults mirror spec §8; production reads settings."""

    min_tokens: int = 120
    max_tokens: int = 800
    overlap_ratio: float = 0.15
    embed_skip_min_tokens: int = 10
    embed_skip_max_tokens: int = 8000
    max_heading_level: int = 3
    max_table_columns: int = 8
    chunker_version: int = 1

    @property
    def overlap_tokens(self) -> int:
        return int(self.max_tokens * self.overlap_ratio)

    @classmethod
    def from_settings(cls) -> ChunkOptions:
        """Build from application settings. Imported lazily so this module stays
        importable without environment variables."""
        from kb.config import get_settings

        settings = get_settings()
        return cls(
            min_tokens=settings.chunk_min_tokens,
            max_tokens=settings.chunk_max_tokens,
            overlap_ratio=settings.chunk_overlap_ratio,
            embed_skip_min_tokens=settings.embed_skip_min_tokens,
            embed_skip_max_tokens=settings.embed_skip_max_tokens,
            chunker_version=settings.chunker_version,
        )


@dataclass(slots=True)
class ChunkDraft:
    """One chunk, before it has a database row."""

    ordinal: int
    text: str
    heading_path: list[str] = field(default_factory=list)
    locator: dict | None = None
    token_count: int = 0

    @property
    def embed_text(self) -> str:
        """What gets vectorized: heading context plus the text (spec §8).

        "Configure the timeout" is meaningless without its headings, so the
        heading path is prepended for the embedding call only. What the user is
        shown (``text``) stays clean.
        """
        context = render_heading_context(self.heading_path)
        return f"{context}\n\n{self.text}" if context else self.text

    def needs_embedding(self, options: ChunkOptions) -> bool:
        """Whether this chunk falls inside the embedding window (spec §8).

        Evaluated on ``token_count``, i.e. the clean text, so the decision is
        stable and explainable rather than shifting with heading depth.
        """
        return options.embed_skip_min_tokens <= self.token_count <= options.embed_skip_max_tokens


def render_heading_context(heading_path: Sequence[str]) -> str:
    """Render a heading path as markdown headings, one level deeper per entry."""
    return "\n".join(f"{'#' * min(index + 1, 6)} {title}" for index, title in enumerate(heading_path) if title)


def chunk_markdown(markdown: str, options: ChunkOptions | None = None) -> list[ChunkDraft]:
    """Split markdown into ordered chunks. Ordinals start at 0 and are contiguous."""
    opts = options or ChunkOptions()
    blocks = _scan_blocks(markdown)
    if not blocks:
        return []

    drafts: list[ChunkDraft] = []
    for group in _group_sections(_sectionise(blocks, opts), opts):
        drafts.extend(_emit(group, opts))
    for ordinal, draft in enumerate(drafts):
        draft.ordinal = ordinal
    return drafts


# ---------------------------------------------------------------------------
# Scanning: lines -> blocks
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Block:
    kind: str
    lines: list[str]
    locator: dict | None = None
    level: int = 0
    title: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip("\n")


def _scan_blocks(markdown: str) -> list[_Block]:
    """Turn markdown into structural blocks, keeping code and tables intact.

    Fenced code and table rows are collected as single blocks here so that no
    later stage can accidentally cut through them.
    """
    lines = list(iter_marked_lines(markdown))
    blocks: list[_Block] = []
    buffer: list[str] = []
    buffer_locator: dict | None = None

    def flush() -> None:
        nonlocal buffer, buffer_locator
        if buffer:
            blocks.append(_Block(kind=BLOCK_TEXT, lines=buffer, locator=buffer_locator))
            buffer = []
            buffer_locator = None

    index = 0
    while index < len(lines):
        current = lines[index]
        text = current.text

        fence = FENCE_RE.match(text)
        if fence:
            flush()
            char, length = fence.group(1)[0], len(fence.group(1))
            code_lines = [text]
            locator = current.locator
            index += 1
            while index < len(lines):
                code_lines.append(lines[index].text)
                closing = FENCE_RE.match(lines[index].text)
                if closing and closing.group(1)[0] == char and len(closing.group(1)) >= length:
                    index += 1
                    break
                index += 1
            blocks.append(_Block(kind=BLOCK_CODE, lines=code_lines, locator=locator))
            continue

        heading = HEADING_RE.match(text)
        if heading:
            flush()
            blocks.append(
                _Block(
                    kind=BLOCK_HEADING,
                    lines=[text],
                    locator=current.locator,
                    level=len(heading.group(1)),
                    title=heading.group(2),
                )
            )
            index += 1
            continue

        if is_table_line(text):
            table_lines = [text]
            locator = current.locator
            index += 1
            while index < len(lines) and is_table_line(lines[index].text):
                table_lines.append(lines[index].text)
                index += 1
            blocks.append(_Block(kind=BLOCK_TABLE, lines=table_lines, locator=locator))
            continue

        if not text.strip():
            flush()
            index += 1
            continue

        if buffer_locator is None:
            buffer_locator = current.locator
        buffer.append(text)
        index += 1

    flush()
    return blocks


# ---------------------------------------------------------------------------
# Sections -> groups
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Section:
    heading_path: list[str]
    heading_line: str = ""
    blocks: list[_Block] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(estimate_tokens(block.text) for block in self.blocks)

    @property
    def locator(self) -> dict | None:
        for block in self.blocks:
            if block.locator is not None:
                return block.locator
        return None


def _sectionise(blocks: list[_Block], options: ChunkOptions) -> list[_Section]:
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    current = _Section(heading_path=[])

    for block in blocks:
        if block.kind == BLOCK_HEADING:
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.title))
            if block.level <= options.max_heading_level:
                if current.blocks:
                    sections.append(current)
                current = _Section(
                    heading_path=[title for _, title in stack if title],
                    heading_line=block.lines[0],
                )
                continue
            # Deeper than H3: it labels its section but does not start one.
        current.blocks.append(block)

    if current.blocks:
        sections.append(current)
    return sections


@dataclass(slots=True)
class _Group:
    heading_path: list[str]
    blocks: list[_Block] = field(default_factory=list)
    _tokens: int = 0
    merged: bool = False

    @property
    def tokens(self) -> int:
        return self._tokens

    @property
    def locator(self) -> dict | None:
        for block in self.blocks:
            if block.locator is not None:
                return block.locator
        return None

    def extend(self, section: _Section, *, keep_heading: bool) -> None:
        """Absorb another section.

        ``keep_heading`` re-attaches the absorbed section's heading line as
        content. A merged section's heading is no longer part of the group's
        ``heading_path`` (which belongs to the section it joined), so dropping
        the line would strip the only label its content had.
        """
        extra = 0
        if keep_heading and section.heading_line:
            self.blocks.append(_Block(kind=BLOCK_HEADING, lines=[section.heading_line]))
            extra = estimate_tokens(section.heading_line)
        self.blocks.extend(section.blocks)
        self._tokens += section.tokens + extra
        self.merged = self.merged or keep_heading

    @classmethod
    def of(cls, section: _Section) -> _Group:
        return cls(heading_path=list(section.heading_path), blocks=list(section.blocks), _tokens=section.tokens)


def _group_sections(sections: list[_Section], options: ChunkOptions) -> list[_Group]:
    """Merge short sections upward (rule 2).

    A short section joins the *previous* group, not the next: the previous group
    is its parent in document order, which is where a stray one-liner belongs.
    The merge is refused when it would push the group over the size ceiling —
    that chunk would be split again immediately, and the section heading would
    end up detached from its own content.
    """
    groups: list[_Group] = []
    for section in sections:
        if not section.blocks:
            continue
        if groups and section.tokens < options.min_tokens and groups[-1].tokens + section.tokens <= options.max_tokens:
            # The merged-in section keeps its heading line as content, since its
            # heading is no longer part of the group's heading_path.
            groups[-1].extend(section, keep_heading=True)
            continue
        groups.append(_Group.of(section))

    # A short leading group has no predecessor, so it merges forward instead.
    if len(groups) >= 2 and groups[0].tokens < options.min_tokens:
        first, second = groups[0], groups[1]
        if first.tokens + second.tokens <= options.max_tokens:
            merged = _Group(heading_path=second.heading_path)
            merged.blocks.extend(first.blocks)
            merged.blocks.extend(second.blocks)
            merged._tokens = first.tokens + second.tokens
            merged.merged = True
            groups = [merged, *groups[2:]]

    return groups


# ---------------------------------------------------------------------------
# Groups -> chunk drafts
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Piece:
    text: str
    tokens: int
    locator: dict | None = None
    standalone: bool = False


def _emit(group: _Group, options: ChunkOptions) -> list[ChunkDraft]:
    pieces = _pieces(group, options)
    drafts: list[ChunkDraft] = []
    run: list[_Piece] = []

    def flush() -> None:
        if not run:
            return
        for window in _pack(run, options.max_tokens, options.overlap_tokens):
            drafts.append(_draft(window, group.heading_path))
        run.clear()

    for piece in pieces:
        if piece.standalone:
            flush()
            drafts.append(_draft([piece], group.heading_path))
        else:
            run.append(piece)
    flush()
    return [draft for draft in drafts if draft.text.strip()]


def _draft(pieces: list[_Piece], heading_path: Sequence[str]) -> ChunkDraft:
    text = "\n\n".join(piece.text.strip() for piece in pieces if piece.text.strip())
    locator = next((piece.locator for piece in pieces if piece.locator is not None), None)
    return ChunkDraft(
        ordinal=0,
        text=text,
        heading_path=list(heading_path),
        locator=locator,
        token_count=estimate_tokens(text),
    )


def _pieces(group: _Group, options: ChunkOptions) -> list[_Piece]:
    pieces: list[_Piece] = []
    for block in group.blocks:
        text = block.text
        if not text.strip():
            continue
        if block.kind == BLOCK_TABLE:
            table = parse_markdown_table(block.lines)
            if table is not None:
                pieces.extend(_table_pieces(table, block.locator, options))
                continue
        pieces.append(_Piece(text=text, tokens=estimate_tokens(text), locator=block.locator))
    return pieces


def _table_pieces(table: MarkdownTable, locator: dict | None, options: ChunkOptions) -> list[_Piece]:
    """Whole table when it fits, otherwise row groups with a repeated header (rule 5).

    Applies to tall narrow tables too, not only wide ones: once a table exceeds
    the size ceiling, repeating the header is the only thing that makes each
    piece readable on its own.
    """
    rows = table.rows
    header_tokens = estimate_tokens(table.render(rows=[]))
    total = header_tokens + sum(_row_tokens(row) for row in rows)

    if not rows or (total <= options.max_tokens and not is_wide(table, max_columns=options.max_table_columns)):
        return [_Piece(text=table.render(), tokens=total, locator=locator)]

    groups: list[tuple[int, list[list[str]]]] = []
    current: list[list[str]] = []
    current_tokens = header_tokens
    for offset, row in enumerate(rows):
        row_tokens = _row_tokens(row)
        if current and current_tokens + row_tokens > options.max_tokens:
            groups.append((offset - len(current), current))
            current = []
            current_tokens = header_tokens
        current.append(row)
        current_tokens += row_tokens
    if current:
        groups.append((len(rows) - len(current), current))

    pieces: list[_Piece] = []
    for offset, rows_in_group in groups:
        pieces.append(
            _Piece(
                text=table.render(rows=rows_in_group),
                tokens=header_tokens + sum(_row_tokens(row) for row in rows_in_group),
                locator=_group_locator(locator, offset, len(rows_in_group)),
                standalone=True,
            )
        )
    return pieces


def _group_locator(locator: dict | None, offset: int, count: int) -> dict | None:
    """Row-addressed locator for one table group.

    ``row_start`` in the marker is the header row, so a group's first data row is
    ``row_start + offset + 1``; every subsequent group is shifted by the rows
    already emitted. Without this every group would claim to come from the top
    of the sheet.

    Always returns a fresh dict. Sharing the block's locator here once produced
    group two of every table pointing at group-one-plus-one — a mutating
    ``shift_rows`` call that looked like pure arithmetic.
    """
    if locator is None:
        return None
    start = locator.get("row_start")
    if not isinstance(start, int):
        return dict(locator)
    shifted = dict(locator)
    shifted["row_start"] = start + offset + 1
    shifted["row_end"] = start + offset + count
    return shifted


def _row_tokens(row: Sequence[str]) -> int:
    return estimate_tokens(" | ".join(escape_cell(cell) for cell in row))


def _pack(pieces: list[_Piece], max_tokens: int, overlap_tokens: int) -> list[list[_Piece]]:
    """Sliding-window packing over indivisible pieces (rules 3 and 4).

    A piece larger than the ceiling — a long code block, most likely — is
    emitted alone rather than split; spec §8 prefers an oversized chunk over a
    cut code block. Progress is guaranteed: the next window always starts at
    least one piece further along.
    """
    windows: list[list[_Piece]] = []
    start = 0
    total_pieces = len(pieces)

    while start < total_pieces:
        end = start
        tokens = 0
        while end < total_pieces and (tokens + pieces[end].tokens <= max_tokens or end == start):
            tokens += pieces[end].tokens
            end += 1
        windows.append(pieces[start:end])
        if end >= total_pieces:
            break

        budget = overlap_tokens
        rewind = end
        while rewind > start + 1 and pieces[rewind - 1].tokens <= budget:
            budget -= pieces[rewind - 1].tokens
            rewind -= 1
        start = rewind
    return windows


__all__ = [
    "ChunkDraft",
    "ChunkOptions",
    "MarkedLine",
    "chunk_markdown",
    "render_heading_context",
]
