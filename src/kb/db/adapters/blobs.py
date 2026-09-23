"""Where a document's original bytes come from (``BlobSource``).

Two channels, one interface — the indexer never learns which it is talking to
(spec §4).

* **Git** — the blob is read straight out of the bare partial clone at the
  revision the job was queued for. Under ``--filter=blob:none`` this is the call
  that triggers a lazy fetch, which is exactly the intent: only files that
  actually changed are ever downloaded.
* **Upload** — the original lands in a directory-backed store on the service
  host. Spec §7 约束 6 puts conversion on the server, so the original has to be
  kept: re-converting after a parser swap must not require asking the user to
  upload again.

The upload store is deliberately keyed by **path**, not by content hash: the
indexer asks for "the bytes at this path", and a content-keyed store could not
answer that question without a database round trip. Paths are confined to a
per-tenant directory; ``..`` and absolute paths are rejected rather than
normalised, because a silently-mangled path is worse than a refusal.

**The git ambiguity, stated honestly.** ``BlobSource.read`` receives only
``(user_id, source, path)``, but a tenant may register several repositories, and
a path can exist in more than one. The reader tries each enabled repository at
its own ``last_synced_sha`` and returns the first hit. Phase 1 is
single-repository-per-user, where this is exact; a multi-repo deployment should
carry ``repo_id`` on the job payload and read by that instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path, PurePosixPath

from kb.config import get_settings
from kb.retrieval import query_builder as qb
from kb.sync.git import GitClient, GitError
from kb.sync.ports import RepoRef

LOGGER = logging.getLogger(__name__)

GitFactory = Callable[[RepoRef], GitClient]

DEFAULT_WORKDIR_ROOT = "var/git"


class UnsafeBlobPath(ValueError):
    """An upload name tried to escape its tenant directory."""


def safe_relative_path(name: str) -> PurePosixPath:
    """Validate an upload name, returning it as a relative POSIX path.

    Rejects anything with a drive letter, a leading slash, or a ``..`` segment.
    Namespacing the store by ``user_id`` is what keeps one tenant's uploads out
    of another's directory, and this check is what keeps a single tenant from
    escaping its own.
    """
    candidate = PurePosixPath(name.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise UnsafeBlobPath(f"upload name escapes the tenant directory: {name!r}")
    if not candidate.parts or candidate.parts[0] in ("", "."):
        raise UnsafeBlobPath(f"upload name is empty: {name!r}")
    return candidate


class LocalBlobStore:
    """A directory-backed store for uploaded originals, namespaced by tenant."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)

    @classmethod
    def from_settings(cls) -> LocalBlobStore:
        return cls(get_settings().blob_store_path)

    def path_for(self, *, user_id: uuid.UUID, name: str) -> Path:
        return self._root / str(user_id) / str(safe_relative_path(name))

    async def put(self, *, user_id: uuid.UUID, name: str, blob: bytes) -> Path:
        path = self.path_for(user_id=user_id, name=name)
        await asyncio.to_thread(self._write, path, blob)
        return path

    async def get(self, *, user_id: uuid.UUID, name: str) -> bytes | None:
        path = self.path_for(user_id=user_id, name=name)
        return await asyncio.to_thread(self._read, path)

    async def delete(self, *, user_id: uuid.UUID, name: str) -> bool:
        path = self.path_for(user_id=user_id, name=name)
        return await asyncio.to_thread(self._unlink, path)

    @staticmethod
    def _write(path: Path, blob: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename so a crashed upload cannot leave a half file that a
        # later read would happily treat as the real content.
        temporary = path.with_name(path.name + ".part")
        temporary.write_bytes(blob)
        temporary.replace(path)

    @staticmethod
    def _read(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _unlink(path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


class UploadBlobSource:
    """``BlobSource`` over the upload store."""

    def __init__(self, store: LocalBlobStore) -> None:
        self._store = store

    async def read(self, *, user_id: uuid.UUID, source: str, source_path: str) -> bytes | None:
        return await self._store.get(user_id=user_id, name=source_path)


class RoutingBlobSource:
    """Picks a ``BlobSource`` by the document's ``source`` column.

    The indexer is handed one object and asks it for bytes; which channel those
    bytes come from is decided here, at the edge, so the indexer keeps knowing
    nothing about git or uploads (spec §4). An unknown source is a ``None`` read
    rather than an exception — the same treatment a missing file gets.
    """

    def __init__(self, sources: dict[str, object]) -> None:
        self._sources = dict(sources)

    def register(self, source: str, blob_source: object) -> None:
        self._sources[source] = blob_source

    async def read(self, *, user_id: uuid.UUID, source: str, source_path: str) -> bytes | None:
        blob_source = self._sources.get(source)
        if blob_source is None:
            LOGGER.warning("no blob source registered for source=%r", source)
            return None
        return await blob_source.read(user_id=user_id, source=source, source_path=source_path)


class GitBlobSource:
    """``BlobSource`` over a tenant's git repositories.

    Failed reads are not fatal: a missing path returns ``None`` and the indexer
    records a skip, which is the same treatment a deleted file gets.
    """

    def __init__(
        self,
        sessionmaker,
        *,
        git_factory: GitFactory,
        repository_loader: Callable[[], Awaitable[Iterable[RepoRef]]] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._git_factory = git_factory
        self._load_repos = repository_loader or self._default_repo_loader
        self._clients: dict[uuid.UUID, GitClient] = {}
        self._prepared: set[uuid.UUID] = set()

    async def read(self, *, user_id: uuid.UUID, source: str, source_path: str) -> bytes | None:
        for repo in await self._load_repos():
            if repo.user_id != user_id:
                continue
            git = self._client(repo)
            try:
                await self._prepare(repo, git)
                revision = await self._revision(repo, git)
                return await git.read_blob(revision, source_path)
            except GitError as exc:
                LOGGER.debug("blob not available for %s in repo %s: %s", source_path, repo.id, exc)
                continue
        return None

    # -- internals ----------------------------------------------------------

    def _client(self, repo: RepoRef) -> GitClient:
        client = self._clients.get(repo.id)
        if client is None:
            client = self._git_factory(repo)
            self._clients[repo.id] = client
        return client

    async def _prepare(self, repo: RepoRef, git: GitClient) -> None:
        """Clone/fetch once per process, not once per blob read."""
        if repo.id in self._prepared:
            return
        await git.ensure_repo()
        if repo.last_synced_sha is None:
            # No diff starting point yet: the only revision we can name is the
            # remote head, so fetch to make it resolvable.
            await git.fetch()
        self._prepared.add(repo.id)

    async def _revision(self, repo: RepoRef, git: GitClient) -> str:
        """The commit the job was queued for.

        ``last_synced_sha`` is advanced to the new head only after the sync's
        batches commit, so by the time an indexing job runs, this *is* the
        revision whose blob the job described. If the file changed again since,
        the indexer's ``content_sha`` check notices and skips the job.
        """
        return repo.last_synced_sha or await git.remote_head()

    async def _default_repo_loader(self) -> list[RepoRef]:
        """Every enabled repository, read on the admin path.

        Cross-tenant by necessity — the worker has to find work before it knows
        whose it is (see ``query_builder.all_enabled_repos_for_worker``). Repo
        *rows* carry no document content, and each subsequent read is scoped to
        the ``user_id`` compared above.
        """
        from kb.db.engine import get_admin_sessionmaker

        async with get_admin_sessionmaker()() as session:
            rows = (await session.execute(qb.all_enabled_repos_for_worker())).scalars().all()
        return [
            RepoRef(
                id=row.id,
                user_id=row.user_id,
                url=row.url,
                branch=row.branch,
                last_synced_sha=row.last_synced_sha,
                credential_ref=row.credential_ref,
            )
            for row in rows
        ]


def env_credential_resolver(credential_ref: str | None) -> str | None:
    """Resolve a ``credential_ref`` from the environment (spec §10).

    The reference is a variable *name*; the secret itself never reaches the
    database (``repos.credential_ref`` stores only this reference). Git receives
    it through its own environment, never on the command line.
    """
    if not credential_ref:
        return None
    return os.environ.get(credential_ref)


def default_git_factory(*, workdir_root: str | os.PathLike[str] = DEFAULT_WORKDIR_ROOT) -> GitFactory:
    """Build per-repository ``GitClient`` instances under ``workdir_root``."""

    root = Path(workdir_root)

    def factory(repo: RepoRef) -> GitClient:
        return GitClient(
            repo.url,
            root / str(repo.id),
            branch=repo.branch,
            credential=env_credential_resolver(repo.credential_ref),
        )

    return factory


__all__ = [
    "GitBlobSource",
    "GitFactory",
    "LocalBlobStore",
    "RoutingBlobSource",
    "UnsafeBlobPath",
    "UploadBlobSource",
    "default_git_factory",
    "env_credential_resolver",
    "safe_relative_path",
]
