"""Retrieval result types.

``SearchHit.as_dict`` is the shape spec §8 specifies, and it is deliberately
about the *original file*: a hit says "``报告.pdf`` page 12" rather than pointing
at converted text, because that is what the caller needs in order to open the
note and read more.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

BRANCH_VECTOR = "vector"
BRANCH_KEYWORD = "keyword"

# Human-readable branch names for the degraded-result message. Kept here rather
# than derived from the constants so the wording is not glued to the identifiers.
BRANCH_LABELS = {BRANCH_VECTOR: "向量", BRANCH_KEYWORD: "关键词"}

# Sentence-ish terminators, used when a snippet has to be cut short.
BOUNDARY_CHARS = "。！？；\n.!?;"

SNIPPET_MAX_CHARS = 1000

EMPTY_RESULT_MESSAGE = (
    "没有匹配的笔记。可以换个说法重试，或先用 list_notes 看一下都有哪些笔记。"
)


@dataclass(frozen=True, slots=True)
class ChunkCandidate:
    """One branch's opinion about one chunk."""

    chunk_id: int
    document_id: uuid.UUID
    text: str
    source: str
    source_path: str
    title: str | None = None
    heading_path: tuple[str, ...] = ()
    locator: dict | None = None
    score: float = 0.0
    branch: str = ""


@dataclass(frozen=True, slots=True)
class SearchQuery:
    query: str
    limit: int = 25
    tags: tuple[str, ...] = ()
    path_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    """What the caller receives."""

    chunk_id: int
    document_id: uuid.UUID
    path: str
    source: str
    score: float
    snippet: str
    title: str | None = None
    heading_path: tuple[str, ...] = ()
    locator: dict | None = None

    def as_dict(self) -> dict:
        """spec §8's result object — the origin points at the original file."""
        return {
            "path": self.path,
            "title": self.title,
            "source": self.source,
            "heading_path": list(self.heading_path),
            "locator": self.locator,
            "score": round(self.score, 6),
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    query: str
    hits: tuple[SearchHit, ...] = ()
    branches: dict[str, int] = field(default_factory=dict)
    degraded: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.hits

    @property
    def message(self) -> str | None:
        """Why a caller gets no hits — never just "nothing".

        Two distinct situations, and conflating them is the bug this property
        exists to prevent (spec §9). "No matches" tells a model to rephrase; "the
        search did not run" tells it to try again later. A degraded branch that
        returned nothing is *unknown*, not *empty*, so it must not be reported as
        "没有匹配的笔记" — the model would conclude the knowledge base has nothing
        on the subject when in fact nobody looked.
        """
        if not self.is_empty:
            return None
        if self.degraded:
            names = "/".join(BRANCH_LABELS.get(name, name) for name in self.degraded)
            if len(self.degraded) >= len(self.branches):
                return f"检索暂时不可用（{names} 分支失败），请稍后重试，不要据此判断知识库里没有相关内容。"
            return f"检索结果可能不完整（{names} 分支失败），未命中的内容未必不存在，建议稍后重试。"
        return EMPTY_RESULT_MESSAGE

    def as_dict(self) -> dict:
        payload: dict = {"query": self.query, "count": len(self.hits), "results": [hit.as_dict() for hit in self.hits]}
        if self.is_empty:
            payload["message"] = self.message
        if self.degraded:
            payload["degraded"] = list(self.degraded)
        return payload


def make_snippet(text: str, *, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    """Trim a chunk to a presentable snippet, cutting at a boundary when possible.

    Chunks are already bounded by the chunk size ceiling, so this only bites for
    the deliberately-oversized ones (a long code block, say).
    """
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    window = stripped[:max_chars]
    cut = max(window.rfind(char) for char in BOUNDARY_CHARS)
    if cut >= max_chars // 2:
        window = window[: cut + 1]
    return window.rstrip() + "…"


def hits_from_candidates(
    fused: Sequence[tuple[int, float]],
    by_id: dict[int, ChunkCandidate],
) -> list[SearchHit]:
    """Turn fused ``(chunk_id, score)`` pairs into hits, preserving rank order."""
    hits: list[SearchHit] = []
    for chunk_id, score in fused:
        candidate = by_id.get(chunk_id)
        if candidate is None:
            continue
        hits.append(
            SearchHit(
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                path=candidate.source_path,
                source=candidate.source,
                score=score,
                snippet=make_snippet(candidate.text),
                title=candidate.title,
                heading_path=tuple(candidate.heading_path),
                locator=candidate.locator,
            )
        )
    return hits
