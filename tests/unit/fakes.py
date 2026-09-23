"""In-memory doubles for the sync/upload/retrieval/API ports.

These exist so the expensive-to-arrange behaviours can still be tested: a failure
*between* enqueue batches, a rename that git proves is content-identical, a
``.kbignore`` edit that must widen indexing, one retrieval branch being down. All
of those are one-line setups against these doubles and awkward against a database.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from kb.db.adapters.documents import DocumentContent, DocumentSummary
from kb.db.adapters.repos import RepoInfo
from kb.indexer.embedding import EmbeddingError
from kb.queue.base import JobSpec
from kb.retrieval.types import ChunkCandidate, SearchQuery, SearchResult
from kb.sync.diff import ChangeEvent
from kb.sync.ports import (
    DocumentRecord,
    JobPayload,  # noqa: F401 - re-exported for test convenience
    NewDocument,
)


def document_id() -> uuid.UUID:
    return uuid.uuid4()


class FakeGitClient:
    """Stands in for ``GitClient``.

    Blobs are keyed by path only; the revision argument is recorded but not
    resolved, because the pipeline is what decides which revision to read.
    """

    def __init__(
        self,
        *,
        head: str = "head0001",
        files: list[str] | None = None,
        events: list[ChangeEvent] | None = None,
        blobs: dict[str, bytes] | None = None,
        kbignore: str | None = None,
    ) -> None:
        self.head = head
        self.files = files or []
        self.events = events or []
        self.blobs = blobs or {}
        self.kbignore = kbignore
        self.fetched = 0
        self.cloned = 0
        self.diff_calls: list[tuple[str, str]] = []
        self.blob_reads: list[str] = []
        self.size_queries: list[str] = []
        self.revisions: dict[str, str] = {}

    async def ensure_repo(self) -> None:
        self.cloned += 1

    async def fetch(self) -> None:
        self.fetched += 1

    async def remote_head(self) -> str:
        return self.head

    async def rev_parse(self, revision: str) -> str:
        return self.head

    async def list_files(self, sha: str) -> list[str]:
        self.revisions["list_files"] = sha
        return list(self.files)

    async def diff(self, old_sha: str, new_sha: str) -> list[ChangeEvent]:
        self.diff_calls.append((old_sha, new_sha))
        return list(self.events)

    async def read_blob(self, revision: str, path: str) -> bytes:
        self.blob_reads.append(path)
        self.revisions["read_blob"] = revision
        return self.blobs[path]

    async def read_file_text(self, revision: str, path: str) -> str | None:
        if path == ".kbignore":
            return self.kbignore
        data = self.blobs.get(path)
        return data.decode("utf-8") if data else None

    async def blob_size(self, revision: str, path: str) -> int | None:
        self.size_queries.append(path)
        data = self.blobs.get(path)
        return len(data) if data is not None else None


class FakeVaultStore:
    """Dict-backed ``VaultStore`` that records the order of its calls."""

    def __init__(self, documents: list[DocumentRecord] | None = None) -> None:
        self.documents: dict[tuple[uuid.UUID, str, str], DocumentRecord] = {}
        self.synced_sha: dict[uuid.UUID, str] = {}
        self.calls: list[str] = []
        for record in documents or []:
            self.documents[(record.user_id, record.source, record.source_path)] = record

    async def find_document(self, *, user_id, source: str, source_path: str) -> DocumentRecord | None:
        self.calls.append(f"find:{source_path}")
        return self.documents.get((user_id, source, source_path))

    async def list_document_paths(self, *, user_id, source: str) -> list[str]:
        self.calls.append("list_paths")
        return sorted(path for (_, doc_source, path) in self.documents if doc_source == source)

    async def upsert_document(self, document: NewDocument) -> DocumentRecord:
        self.calls.append(f"upsert:{document.source_path}")
        record = self.preview_upsert(document)
        self.documents[(document.user_id, document.source, document.source_path)] = record
        return record

    def write_document(self, record: DocumentRecord) -> None:
        """Store a record that a caller already holds.

        A ``UnitOfWorkScope`` hands the row back before the transaction commits,
        so the commit has to store *that* row. Re-deriving it here would mint a
        second id and break the identity the caller was given.
        """
        self.calls.append(f"upsert:{record.source_path}")
        self.documents[(record.user_id, record.source, record.source_path)] = record

    def preview_upsert(self, document: NewDocument) -> DocumentRecord:
        """The record an upsert would produce, without writing it."""
        key = (document.user_id, document.source, document.source_path)
        existing = self.documents.get(key)
        return DocumentRecord(
            id=existing.id if existing else document_id(),
            user_id=document.user_id,
            source=document.source,
            source_path=document.source_path,
            content_sha=document.content_sha,
            converted_sha=None,
            conversion_status="ok",
            size_bytes=document.size_bytes,
        )

    async def delete_document(self, *, user_id, source: str, source_path: str) -> bool:
        self.calls.append(f"delete:{source_path}")
        return self.documents.pop((user_id, source, source_path), None) is not None

    async def rename_document(self, *, user_id, source: str, old_path: str, new_path: str) -> bool:
        self.calls.append(f"rename:{old_path}->{new_path}")
        record = self.documents.pop((user_id, source, old_path), None)
        if record is None:
            return False
        self.documents[(user_id, source, new_path)] = replace(record, source_path=new_path)
        return True

    async def set_last_synced_sha(self, *, user_id, repo_id: uuid.UUID, sha: str) -> None:
        self.calls.append(f"advance_sha:{sha}")
        self.synced_sha[repo_id] = sha


class FakeScope:
    """A ``UnitOfWorkScope`` that stages its writes until the scope commits.

    Staging is the point of the double. A real scope rolls back everything when
    its body raises, so a fake that wrote straight through would let a test
    assert a document row that production had already discarded — which is the
    one failure these tests exist to detect.
    """

    def __init__(self, store: FakeVaultStore, scope_id: int, *, fail_on_enqueue: bool = False) -> None:
        self._store = store
        self.scope_id = scope_id
        self._fail_on_enqueue = fail_on_enqueue
        self.staged: list[tuple[NewDocument, DocumentRecord]] = []
        self.enqueued_in_scope: list[JobSpec] = []
        self.committed = False

    @property
    def staged_documents(self) -> list[NewDocument]:
        return [document for document, _record in self.staged]

    # -- reads go straight through; nothing has been written yet ------------

    async def find_document(self, *, user_id, source: str, source_path: str) -> DocumentRecord | None:
        return await self._store.find_document(user_id=user_id, source=source, source_path=source_path)

    async def list_document_paths(self, *, user_id, source: str) -> list[str]:
        return await self._store.list_document_paths(user_id=user_id, source=source)

    # -- writes are staged -------------------------------------------------

    async def upsert_document(self, document: NewDocument) -> DocumentRecord:
        record = self._store.preview_upsert(document)
        self.staged.append((document, record))
        return record

    async def enqueue(self, spec: JobSpec) -> int:
        if self._fail_on_enqueue and not self.enqueued_in_scope:
            raise RuntimeError("queue unavailable")
        self.enqueued_in_scope.append(spec)
        return len(self.enqueued_in_scope)

    # -- commit ------------------------------------------------------------

    async def commit(self) -> None:
        """Apply the staged writes. Called by ``FakeUnitOfWork``, never by tests."""
        for _document, record in self.staged:
            self._store.write_document(record)
        self.committed = True


class FakeUnitOfWork:
    """Opens scopes that share one store and one job log.

    ``fail_on_scope`` makes the *n*-th scope blow up on its first enqueue, which
    is how the red-line test simulates a batch that never lands.
    """

    def __init__(self, store: FakeVaultStore | None = None, *, fail_on_scope: int | None = None) -> None:
        self.store = store or FakeVaultStore()
        self.jobs: list[JobSpec] = []
        self.scopes: list[FakeScope] = []
        self.fail_on_scope = fail_on_scope

    @asynccontextmanager
    async def begin(self, user_id: uuid.UUID):
        index = len(self.scopes)
        scope = FakeScope(self.store, index + 1, fail_on_enqueue=(self.fail_on_scope == index))
        self.scopes.append(scope)
        # Nothing below runs when the body raises, which is what rollback means.
        yield scope
        await scope.commit()
        self.jobs.extend(scope.enqueued_in_scope)


# ---------------------------------------------------------------------------
# retrieval / API doubles
# ---------------------------------------------------------------------------


def candidate(
    chunk_id: int,
    *,
    document_id: uuid.UUID | None = None,
    text: str = "正文片段",
    path: str = "notes/a.md",
    branch: str = "",
    score: float = 0.0,
) -> ChunkCandidate:
    """A ``ChunkCandidate`` with only the fields a test usually cares about."""
    return ChunkCandidate(
        chunk_id=chunk_id,
        document_id=document_id or uuid.uuid4(),
        text=text,
        source="git",
        source_path=path,
        title="标题",
        heading_path=("第一章",),
        locator=None,
        score=score,
        branch=branch,
    )


def summary(
    path: str = "notes/a.md",
    *,
    status: str = "ok",
    source: str = "git",
    title: str | None = None,
    size_bytes: int | None = 128,
) -> DocumentSummary:
    """A ``DocumentSummary`` with only the fields a test usually cares about."""
    return DocumentSummary(
        id=uuid.uuid4(),
        source=source,
        source_path=path,
        title=title if title is not None else path,
        conversion_status=status,
        conversion_error=None,
        size_bytes=size_bytes,
    )


class FakeSearchBackend:
    """Both retrieval branches, each independently failable.

    ``fail_*`` exists so the "one branch down is not a failed query" behaviour is
    a testable input rather than something arranged by monkeypatching.
    """

    def __init__(
        self,
        *,
        vector: list[ChunkCandidate] | None = None,
        keyword: list[ChunkCandidate] | None = None,
        fail_vector: bool = False,
        fail_keyword: bool = False,
    ) -> None:
        self.vector = vector or []
        self.keyword = keyword or []
        self.fail_vector = fail_vector
        self.fail_keyword = fail_keyword
        self.calls: list[tuple[str, int, object]] = []

    async def vector_search(self, *, user_id, embedding, limit, tags=None, path_prefix=None):
        self.calls.append(("vector", limit, (tuple(tags) if tags else (), path_prefix)))
        if self.fail_vector:
            raise RuntimeError("vector branch unavailable")
        return list(self.vector)

    async def keyword_search(self, *, user_id, query, limit, tags=None, path_prefix=None):
        self.calls.append(("keyword", limit, (tuple(tags) if tags else (), path_prefix)))
        if self.fail_keyword:
            raise RuntimeError("keyword branch unavailable")
        return list(self.keyword)


class FakeQueryEmbedder:
    """Embeds queries deterministically, and can be made to fail."""

    def __init__(self, *, dim: int = 4, fail: bool = False) -> None:
        self.dim = dim
        self.fail = fail
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        if self.fail:
            raise EmbeddingError("provider is rate limited")
        return [0.5] * self.dim


class FakeTokenService:
    """Maps a plaintext token straight to a user id."""

    def __init__(self, tokens: dict[str, uuid.UUID] | None = None) -> None:
        self.tokens = dict(tokens or {})
        self.checked: list[str | None] = []

    async def authenticate(self, token: str) -> uuid.UUID | None:
        self.checked.append(token)
        return self.tokens.get(token)


def make_settings(**overrides):
    """A stand-in for ``kb.config.Settings`` with every attribute the app reads.

    A real ``Settings`` refuses to load without a ``DATABASE_URL``; these tests
    are about behaviour above the database, so they should not need one.
    """
    values = {
        "api_host": "127.0.0.1",
        "api_port": 8000,
        "public_base_url": "http://localhost:8000",
        "embedding_provider": "openai",
        "embedding_model": "text-embedding-3-small",
        "embedding_dim": 1024,
        "embedding_api_key": "test-key",
        "embedding_batch_size": 20,
        "embedding_max_concurrency": 8,
        "query_cache_size": 128,
        "queue_lock_timeout_seconds": 900,
        "queue_max_attempts": 5,
        "sync_poll_interval_seconds": 300,
        "sync_batch_size": 500,
        "max_file_size_bytes": 52_428_800,
        "git_workdir_root": "var/git",
        "blob_store_path": "var/blobs",
        "retrieval_default_limit": 25,
        "retrieval_max_limit": 50,
        "rrf_k": 60,
        "per_document_chunk_limit": 3,
        "chunker_version": 1,
        "chunk_min_tokens": 120,
        "chunk_max_tokens": 800,
        "chunk_overlap_ratio": 0.15,
        "embed_skip_min_tokens": 10,
        "embed_skip_max_tokens": 8000,
        "git_credential_ref": "",
        "log_level": "INFO",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_services(**overrides):
    """A ``Services``-shaped namespace built from doubles.

    ``create_app`` and the MCP tools only ever read attributes, so a namespace is
    enough — and it keeps these tests from needing engines, a DSN, or a database.
    """
    services = SimpleNamespace(
        settings=make_settings(),
        tokens=FakeTokenService(),
        retrieval=None,
        search_backend=FakeSearchBackend(),
        documents=None,
        repos=None,
        queue=None,
        vault=None,
        unit_of_work=None,
        upload=None,
        indexer=None,
        index_writer=None,
        index_config=None,
        index_maintenance=None,
        conversion_cache=None,
        converter=None,
        blob_store=None,
        blob_router=None,
        upload_blobs=None,
        git_blobs=None,
        app_sessionmaker=None,
        admin_sessionmaker=None,
        vector_search_enabled=True,
    )
    for key, value in overrides.items():
        setattr(services, key, value)
    return services


class FakeRetrievalService:
    """Returns a canned ``SearchResult`` and records the query it was given."""

    def __init__(self, result: SearchResult | None = None) -> None:
        self.result = result or SearchResult(query="", hits=())
        self.queries: list[SearchQuery] = []
        self.user_ids: list[uuid.UUID] = []

    async def search(self, query: SearchQuery, *, user_id: uuid.UUID) -> SearchResult:
        self.queries.append(query)
        self.user_ids.append(user_id)
        return replace(self.result, query=query.query)


class FakeDocumentReader:
    """Serves ``read_note`` / ``list_notes`` from in-memory summaries."""
    def __init__(
        self,
        *,
        summaries: list[DocumentSummary] | None = None,
        contents: dict[str, str] | None = None,
    ) -> None:
        self.summaries = list(summaries or [])
        self.contents = dict(contents or {})
        self.browsed: list[tuple[str | None, int]] = []

    async def browse(self, *, user_id, prefix=None, limit=100):
        self.browsed.append((prefix, limit))
        return [s for s in self.summaries if prefix is None or s.source_path.startswith(prefix)][:limit]

    async def find(self, *, user_id, path: str):
        return next((s for s in self.summaries if s.source_path == path), None)

    async def read(self, *, user_id, path: str):
        summary = await self.find(user_id=user_id, path=path)
        if summary is None:
            return None
        return DocumentContent(
            document=summary, text=self.contents.get(path, ""), chunks=1
        )

    async def by_status(self, *, user_id, statuses=("failed", "no_text"), limit=200):
        return [s for s in self.summaries if s.conversion_status in statuses][:limit]

    async def all_documents(self, *, user_id):
        return list(self.summaries)


class FakeRepoService:
    """Records sync/rebuild requests so the REST layer can be tested without a queue."""

    def __init__(self, repos: list[RepoInfo] | None = None) -> None:
        self.repos = list(repos or [])
        self.synced: list[uuid.UUID] = []
        self.rebuilds: list[uuid.UUID] = []
        self.created: list[dict] = []
        self._next_job = 1

    async def list_repos(self, user_id):
        return list(self.repos)

    async def get_repo(self, user_id, repo_id):
        return next((repo for repo in self.repos if repo.id == repo_id), None)

    async def request_sync(self, user_id, repo_id):
        self.synced.append(repo_id)
        return self._job()

    async def request_full_rebuild(self, user_id):
        self.rebuilds.append(user_id)
        return self._job()

    async def create_repo(self, user_id, *, url, branch="main", credential_ref=None, sync_now=True):
        self.created.append({"url": url, "branch": branch, "credential_ref": credential_ref})
        repo_id = uuid.uuid4()
        self.repos.append(
            RepoInfo(
                id=repo_id,
                user_id=user_id,
                url=url,
                branch=branch,
                last_synced_sha=None,
                sync_enabled=True,
                credential_ref=credential_ref,
            )
        )
        return repo_id

    def _job(self) -> int:
        job_id = self._next_job
        self._next_job += 1
        return job_id


def repo_info(**overrides) -> RepoInfo:
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "url": "git@github.com:you/vault.git",
        "branch": "main",
        "last_synced_sha": None,
        "sync_enabled": True,
        "credential_ref": None,
    }
    values.update(overrides)
    return RepoInfo(**values)


class FakeQueueCounts:
    """The slice of ``PostgresJobQueue`` the status endpoint uses."""

    def __init__(self, counts: dict[str, int] | None = None) -> None:
        self.counts = dict(counts or {"pending": 2, "done": 7})
        self.asked_for: list[uuid.UUID] = []

    async def counts_for_user(self, user_id):
        self.asked_for.append(user_id)
        return dict(self.counts)


class FakeIndexConfig:
    """Returns a stored stamp, or ``None`` for a fresh installation."""

    def __init__(self, stored=None) -> None:
        self.stored = stored

    async def load(self):
        return self.stored
