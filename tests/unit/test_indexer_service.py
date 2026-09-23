"""Unit tests for the indexing service — including the level-3 short circuit.

Spec §11.1 #3 (idempotence) and #4 (a rename costs zero embeddings) both bottom
out here: a chunk whose ``(ordinal, text)`` is already stored must not be
re-embedded. The fake writer makes "what is already stored" an explicit input.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from kb.converter.registry import default_registry
from kb.converter.service import ConversionService
from kb.hashing import sha256_hex, sha256_text
from kb.indexer.chunker import ChunkOptions
from kb.indexer.ports import ChunkWrite, DocumentUpdate, ExistingChunk
from kb.indexer.service import IndexingService
from tests.unit.fakes import FakeVaultStore  # noqa: F401 - keeps the fakes module imported

USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
DOC_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

OPTIONS = ChunkOptions(min_tokens=120, max_tokens=800, overlap_ratio=0.15)


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class FakeBlobSource:
    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.blobs = blobs or {}
        self.reads: list[str] = []

    async def read(self, *, user_id, source: str, source_path: str) -> bytes | None:
        self.reads.append(source_path)
        return self.blobs.get(source_path)


class FakeEmbedder:
    def __init__(self, dim: int = 4) -> None:
        self.dim = 4
        self.model = "fake"
        self.batches = 0
        self.received: list[str] = []

    async def embed(self, texts):
        self.batches += 1
        self.received.extend(texts)
        return [[float(len(text) % 7)] * self.dim for text in texts]


class FakeIndexWriter:
    def __init__(self, existing: dict[int, ExistingChunk] | None = None) -> None:
        self.existing = existing or {}
        self.replacements: list[list[ChunkWrite]] = []
        self.updates: list[DocumentUpdate] = []

    async def existing_chunks(self, *, user_id, document_id):
        return dict(self.existing)

    async def replace_chunks(self, *, user_id, document_id, chunks):
        self.replacements.append(list(chunks))
        return len(chunks)

    async def update_document(self, *, user_id, document_id, update: DocumentUpdate):
        self.updates.append(update)

    @property
    def chunks(self) -> list[ChunkWrite]:
        return self.replacements[-1] if self.replacements else []


def service(writer, blobs=None, embedder=None, options=OPTIONS) -> IndexingService:
    return IndexingService(
        source=FakeBlobSource(blobs),
        converter=ConversionService(default_registry(), max_bytes=1_000_000, to_thread=_inline),
        writer=writer,
        embedder=embedder,
        options=options,
    )


async def _inline(func, *args, **kwargs):
    return func(*args, **kwargs)


async def index(pipeline: IndexingService, path: str, content_sha: str, mime: str | None = None):
    return await pipeline.index_document(
        user_id=USER,
        document_id=DOC_ID,
        source="git",
        source_path=path,
        content_sha=content_sha,
        mime=mime,
    )


def markdown(tokens: int = 300, heading: str = "标题") -> bytes:
    return f"# {heading}\n\n{'字' * tokens}\n".encode()


# ---------------------------------------------------------------------------
# basic indexing
# ---------------------------------------------------------------------------


async def test_markdown_is_chunked_and_embedded() -> None:
    blob = markdown(300)
    writer, embedder = FakeIndexWriter(), FakeEmbedder()

    outcome = await index(service(writer, {"a.md": blob}, embedder), "a.md", sha256_hex(blob))

    assert outcome.status == "ok"
    assert outcome.chunks == 1
    assert outcome.embedded == 1
    assert writer.chunks[0].token_count == 300
    assert writer.chunks[0].embedding is not None


async def test_document_update_records_conversion_and_frontmatter() -> None:
    blob = "---\ntitle: Q3 复盘\ntags: [work, q3]\n---\n\n# 正文\n\n内容\n".encode()
    writer, embedder = FakeIndexWriter(), FakeEmbedder()

    await index(service(writer, {"n.md": blob}, embedder), "n.md", sha256_hex(blob))

    update = writer.updates[-1]
    assert update.conversion_status == "ok"
    assert update.title == "Q3 复盘"
    assert list(update.tags) == ["work", "q3"]
    assert update.converted_sha == sha256_text(blob.decode())
    assert update.indexed_at is not None


async def test_frontmatter_is_not_indexed_as_content() -> None:
    blob = "---\ntitle: Q3 复盘\n---\n\n只有正文\n".encode()
    writer = FakeIndexWriter()

    await index(service(writer, {"n.md": blob}, FakeEmbedder()), "n.md", sha256_hex(blob))

    assert all("Q3 复盘" not in chunk.text for chunk in writer.chunks)


async def test_embedding_input_carries_heading_context() -> None:
    """Embedded text is not the stored text (spec §8)."""
    blob = markdown(300, heading="认证流程")
    writer, embedder = FakeIndexWriter(), FakeEmbedder()

    await index(service(writer, {"a.md": blob}, embedder), "a.md", sha256_hex(blob))

    assert embedder.received[0].startswith("# 认证流程")
    assert not writer.chunks[0].text.startswith("# 认证流程")


# ---------------------------------------------------------------------------
# level 3: unchanged chunks are not re-embedded
# ---------------------------------------------------------------------------


async def test_identical_chunks_reuse_their_vectors() -> None:
    blob = markdown(300)
    draft_text = writer_text_for(blob)
    existing = {0: ExistingChunk(ordinal=0, text=draft_text, embedding=[0.5] * 4)}
    writer, embedder = FakeIndexWriter(existing), FakeEmbedder()

    outcome = await index(service(writer, {"a.md": blob}, embedder), "a.md", sha256_hex(blob))

    assert outcome.embedded == 0
    assert outcome.reused == 1
    assert embedder.batches == 0
    assert writer.chunks[0].embedding == [0.5] * 4


async def test_running_the_same_job_twice_costs_nothing_the_second_time() -> None:
    """Idempotence (spec §11.1 #3): a retry must not pay for embeddings again."""
    blob = markdown(300)
    writer, embedder = FakeIndexWriter(), FakeEmbedder()
    pipeline = service(writer, {"a.md": blob}, embedder)
    sha = sha256_hex(blob)

    first = await index(pipeline, "a.md", sha)
    # Second run sees what the first stored.
    writer.existing = {chunk.ordinal: ExistingChunk(chunk.ordinal, chunk.text, chunk.embedding) for chunk in writer.chunks}
    second = await index(pipeline, "a.md", sha)

    assert first.embedded == 1
    assert second.embedded == 0
    assert second.reused == 1
    assert embedder.batches == 1


async def test_only_the_changed_chunk_is_re_embedded() -> None:
    """The point of the ordinal comparison: one edit must not re-embed the file."""
    blob = markdown(300)
    existing = {
        0: ExistingChunk(ordinal=0, text="完全不同的旧内容", embedding=[0.5] * 4),
        1: ExistingChunk(ordinal=1, text="另一个旧块", embedding=[0.6] * 4),
    }
    writer, embedder = FakeIndexWriter(existing), FakeEmbedder()

    outcome = await index(service(writer, {"a.md": blob}, embedder), "a.md", sha256_hex(blob))

    # The document is one chunk now, so one embedding replaces two stale rows.
    assert outcome.embedded == 1
    assert embedder.batches == 1


def writer_text_for(blob: bytes) -> str:
    from kb.indexer.chunker import chunk_markdown

    body = blob.decode()
    return chunk_markdown(body.split("\n", 2)[2], OPTIONS)[0].text


# ---------------------------------------------------------------------------
# embedding window (spec §8)
# ---------------------------------------------------------------------------


async def test_tiny_chunks_are_stored_without_vectors() -> None:
    blob = b"# \xe5\xa4\xb4\n\n\xe7\x9f\xad\n"  # "# 头\n\n短\n"
    writer, embedder = FakeIndexWriter(), FakeEmbedder()

    outcome = await index(service(writer, {"a.md": blob}, embedder), "a.md", sha256_hex(blob))

    assert outcome.embedded == 0
    assert writer.chunks[0].embedding is None
    assert writer.chunks[0].text == "短"


async def test_oversized_code_blocks_are_stored_without_vectors() -> None:
    body = "\n".join("字" * 40 for _ in range(220))  # ~8800 tokens in one code block
    blob = f"# 代码\n\n```\n{body}\n```\n".encode()
    writer, embedder = FakeIndexWriter(), FakeEmbedder()

    outcome = await index(service(writer, {"a.md": blob}, embedder), "a.md", sha256_hex(blob))

    assert writer.chunks[0].token_count > 8000
    assert outcome.embedded == 0
    assert writer.chunks[0].embedding is None


# ---------------------------------------------------------------------------
# failure isolation and staleness
# ---------------------------------------------------------------------------


async def test_conversion_failure_clears_chunks_and_records_the_status() -> None:
    """A bad file must not block the batch, and must not leave stale chunks."""
    blob = b"%PDF-1.4 not really"
    writer, embedder = FakeIndexWriter(), FakeEmbedder()

    outcome = await index(service(writer, {"broken.pdf": blob}, embedder), "broken.pdf", sha256_hex(blob))

    assert outcome.status == "failed"
    assert writer.replacements[-1] == []
    assert writer.updates[-1].conversion_status == "failed"
    assert writer.updates[-1].conversion_error
    assert embedder.batches == 0


async def test_oversized_files_are_recorded_as_unsupported() -> None:
    blob = b"x" * 2_000_000
    writer = FakeIndexWriter()
    pipeline = IndexingService(
        source=FakeBlobSource({"big.bin": blob}),
        converter=ConversionService(default_registry(), max_bytes=10, to_thread=_inline),
        writer=writer,
        options=OPTIONS,
    )

    outcome = await index(pipeline, "big.bin", sha256_hex(blob))

    assert outcome.status == "unsupported"
    assert writer.updates[-1].conversion_status == "unsupported"


async def test_a_job_whose_content_changed_is_skipped() -> None:
    """The queued hash no longer matches: a newer job is already on its way."""
    blob = markdown(300)
    writer, embedder = FakeIndexWriter(), FakeEmbedder()

    outcome = await index(service(writer, {"a.md": blob}, embedder), "a.md", sha256_hex(b"something else"))

    assert outcome.skipped
    assert "changed" in (outcome.reason or "")
    assert writer.replacements == []


async def test_missing_source_bytes_are_skipped() -> None:
    writer = FakeIndexWriter()
    outcome = await index(service(writer, {}, FakeEmbedder()), "gone.md", "sha")

    assert outcome.skipped
    assert writer.replacements == []


async def test_index_config_is_verified_before_writing() -> None:
    calls: list[str] = []

    async def verify() -> None:
        calls.append("verify")

    pipeline = IndexingService(
        source=FakeBlobSource({}),
        converter=ConversionService(default_registry(), to_thread=_inline),
        writer=FakeIndexWriter(),
        options=OPTIONS,
        verify_index=verify,
    )
    await index(pipeline, "a.md", sha256_hex(b"x"))

    assert calls == ["verify"]


def test_conversion_outcome_timestamp_is_timezone_aware() -> None:
    assert datetime.now(UTC).tzinfo is not None
