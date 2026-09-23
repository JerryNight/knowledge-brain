"""Token estimation — deliberately not a model tokenizer.

Chunk sizing and the embedding skip window (spec §8) need a token count, but
they need it to be *deterministic, offline and fast*: the chunker runs over
every file at index time, and the skip window is a cost-control rule, not an
exact billing calculation.

So this is a heuristic:

* every CJK ideograph counts as one token (close to how zh tokenizers behave),
* every run of latin letters / digits / underscore counts as one token
  (a small underestimate for English prose, roughly words x 1.3).

Both are within the tolerance the thresholds are chosen with (120 / 800 / 10 /
8000 are round numbers, not measured constants).
"""

from __future__ import annotations

import re

# Han, Hangul-adjacent CJK extensions, and the CJK compatibility ideographs.
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]")

# Identifiers matter here: code spans like `SKIP LOCKED` or `chunk_min_tokens`
# are single retrieval units, and counting them as one token matches how they
# are queried.
WORD_PATTERN = re.compile(r"[A-Za-z0-9_]+")


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text``. Zero for empty or whitespace-only input."""
    if not text:
        return 0
    return len(CJK_PATTERN.findall(text)) + len(WORD_PATTERN.findall(text))


def is_blank(text: str) -> bool:
    """True when a fragment carries no indexable content at all."""
    return not text or not text.strip()
