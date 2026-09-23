"""M9 preflight: does the configured embedding provider actually behave?

The provider switch (provider / model / dim / batch size) can fail in ways no
unit test sees, because the fake transport under ``tests/unit`` never touches
the network. Every one of these is silent until a real ingest:

* a stale key or a wrong ``api_base`` — only a real call finds out;
* a ``dimensions`` the provider ignores instead of rejecting. One provider
  answers such a request with HTTP 200 and its *default* width, so the column
  quietly fills with wrong-width vectors and only cosine distance notices;
* a batch size above the provider's per-request ceiling — 400 on the first real
  ingest, long after the setting looked reasonable.

So it asserts four things, roughly in the order they fail:

  1. ``from_settings()`` builds an embedder at all (the key is picked up);
  2. a real call returns vectors of exactly ``EMBEDDING_DIM`` — read from the
     ORM constant rather than from the config, so a config/schema drift shows up
     here instead of as a pgvector type error later;
  3. a multi-batch request comes back whole across the provider's ceiling;
  4. ``build_services()`` reports ``vector_search_enabled`` — i.e. retrieval has
     an embedder rather than quietly degrading to keyword-only.

**This spends real money and needs the network.** It is a preflight to run when
the provider configuration changes, not a test for CI.

Exit codes: 0 pass · 1 the provider did not match the configuration · 2 no key.

Usage::

    uv run python scripts/embedding_smoke.py
    uv run python scripts/embedding_smoke.py --texts 40 --skip-services
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from kb.config import get_settings
from kb.indexer.embedding import OpenAICompatibleEmbedder
from kb.models.chunk import EMBEDDING_DIM
from kb.wiring import build_services

# Mixed scripts on purpose: a Chinese-only batch would not catch a provider that
# mangles non-ASCII input, and the keyword-ish ones exercise the tokenizer path.
PROBES = [
    "量子纠缠与贝尔不等式的实验验证",
    "红烧肉的做法与火候控制",
    "PostgreSQL 的 HNSW 索引参数调优",
    "Obsidian 双链笔记的组织方式",
    "RLS 行级安全策略对查询计划的影响",
]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--texts", type=int, default=25, help="texts to batch in step 3")
    parser.add_argument(
        "--skip-services",
        action="store_true",
        help="skip step 4 (does not need a database)",
    )
    args = parser.parse_args()

    settings = get_settings()
    print(f"provider   = {settings.embedding_provider}")
    print(f"model      = {settings.embedding_model}")
    print(f"dim        = {settings.embedding_dim} (schema expects {EMBEDDING_DIM})")
    print(f"api_base   = {settings.embedding_api_base}")
    print(f"batch_size = {settings.embedding_batch_size}")
    print(f"key set    = {bool(settings.embedding_api_key)}")
    print("-" * 60)

    if not settings.embedding_api_key:
        print("EMBEDDING_API_KEY is empty — nothing to probe (exit 2)")
        return 2
    if settings.embedding_dim != EMBEDDING_DIM:
        print(
            f"config dim {settings.embedding_dim} != ORM dim {EMBEDDING_DIM}: vectors would not fit the column (exit 1)"
        )
        return 1

    embedder = OpenAICompatibleEmbedder.from_settings()
    try:
        vectors = await embedder.embed(PROBES[:2])
        widths = {len(vector) for vector in vectors}
        print(f"[1][2] 2 texts -> {len(vectors)} vectors, widths={widths}, dim={embedder.dim}")
        if len(vectors) != 2 or widths != {EMBEDDING_DIM}:
            print(f"expected 2 vectors of width {EMBEDDING_DIM} (exit 1)")
            return 1
        norm = sum(value * value for value in vectors[0]) ** 0.5
        print(f"       L2 norm of vector 0 = {norm:.4f}")
        if norm < 0.1:
            print("vector looks degenerate — all near zero (exit 1)")
            return 1

        repeats = args.texts // len(PROBES) + 1
        many = [f"{text} #{i}" for i, text in enumerate(PROBES * repeats)][: args.texts]
        batches = -(-args.texts // max(settings.embedding_batch_size, 1))
        big = await embedder.embed(many)
        big_widths = {len(vector) for vector in big}
        print(
            f"[3] {len(many)} texts -> {len(big)} vectors, widths={big_widths}"
            f" ({batches} request(s) at batch_size={settings.embedding_batch_size})"
        )
        if len(big) != len(many) or big_widths != {EMBEDDING_DIM}:
            print("multi-batch request did not come back whole (exit 1)")
            return 1
    except Exception as exc:  # noqa: BLE001 — the point is to report, not to handle
        print(f"provider call failed: {type(exc).__name__}: {exc} (exit 1)")
        return 1
    finally:
        await embedder.aclose()

    if args.skip_services:
        print("SMOKE_OK (service wiring not checked)")
        return 0

    print("-" * 60)
    services = build_services()
    print(f"[4] vector_search_enabled = {services.vector_search_enabled}")
    if services.vector_search_enabled is not True:
        print("retrieval would be keyword-only — check the key and the DSNs (exit 1)")
        return 1

    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
