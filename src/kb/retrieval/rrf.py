"""Reciprocal Rank Fusion and the per-document cap (spec §8).

RRF instead of a weighted score sum because the two branches produce numbers on
incomparable scales — cosine distance and ``ts_rank``. Normalising them would be
guesswork; RRF only looks at positions, so there is no weight to tune.

Two warnings from spec §11.2 shaped this module:

* the fusion constant ``k`` is a real parameter, not a detail. The reference
  project measured a 2.3pp *drop* from moving it from 45 to 20, which is why it
  is configurable (``RRF_K``) rather than hard-coded;
* nothing here is measured except by the evaluation set. A change that looks
  obvious can be a regression.
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[tuple[int, float]]:
    """Fuse ranked id lists into ``[(id, score), ...]``, best first.

    ``score = sum over branches of 1 / (k + rank)``, with ``rank`` starting at 1
    (spec §8). Appearing in both branches therefore beats topping one of them,
    which is the behaviour that makes hybrid retrieval worth the extra branch.

    Ties break on id so results are stable across runs — a retrieval endpoint
    that reshuffles equal-scoring hits is impossible to debug.
    """
    if k <= 0:
        raise ValueError("k must be positive")

    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)

    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def cap_per_document(
    ranked: Sequence[object],
    *,
    per_document_limit: int,
    limit: int,
    document_of,
) -> list:
    """Keep at most ``per_document_limit`` items per document, then cut to ``limit``.

    Without this, one long note with many similar chunks fills the whole result
    set — the "one file dominates the ranking" failure. Because it runs *before*
    the final cut, the later slots go to other documents instead of being wasted.
    """
    if per_document_limit <= 0:
        raise ValueError("per_document_limit must be positive")

    counts: dict[object, int] = {}
    kept: list = []
    for item in ranked:
        key = document_of(item)
        seen = counts.get(key, 0)
        if seen >= per_document_limit:
            continue
        counts[key] = seen + 1
        kept.append(item)
        if len(kept) >= limit:
            break
    return kept
