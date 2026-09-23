"""Indexer package — markdown to chunks to embeddings (spec §8).

Nothing in here knows where the markdown came from: git and upload both arrive
through the same door. The pieces are deliberately separable:

* ``tokens`` / ``tables`` / ``locator`` — pure helpers, no I/O,
* ``frontmatter`` — markdown metadata,
* ``chunker`` — the splitting rules from spec §8,
* ``embedding`` — provider interface, batching, retry, query cache,
* ``service`` — orchestration against ports.
"""

from kb.indexer.chunker import ChunkDraft, ChunkOptions, chunk_markdown
from kb.indexer.frontmatter import Frontmatter, derive_title, parse_frontmatter
from kb.indexer.locator import MarkedLine, iter_marked_lines, marker, parse_marker, strip_markers
from kb.indexer.tokens import estimate_tokens

__all__ = [
    "ChunkDraft",
    "ChunkOptions",
    "Frontmatter",
    "MarkedLine",
    "chunk_markdown",
    "derive_title",
    "estimate_tokens",
    "iter_marked_lines",
    "marker",
    "parse_frontmatter",
    "parse_marker",
    "strip_markers",
]
