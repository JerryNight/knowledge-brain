"""Git client — partial clone, fetch, diff, and on-demand blob reads (spec §6).

**Why partial clone.** ``--depth=1`` cannot diff (no history), and a full clone
of a vault with attachments can be enormous. ``--filter=blob:none`` keeps the
full commit history but downloads blobs only when they are asked for, which is
exactly the access pattern here: we diff commits, then read the few changed
files.

The repository is kept **bare**: nothing needs a working tree, and diffing,
listing and blob reads all work without one.

**Credentials.** The token is passed through the environment (``GIT_CONFIG_*``
plus a variable read by a ``credential.helper`` shell snippet), never on the
command line — argv is visible to every process on the host.

All git invocations run in a worker thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from kb.sync.diff import ChangeEvent, parse_diff_raw


class GitError(RuntimeError):
    """A git command failed. Carries the command and its stderr."""


class GitClient:
    def __init__(
        self,
        url: str,
        workdir: Path,
        *,
        branch: str = "main",
        credential: str | None = None,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.url = url
        self.workdir = Path(workdir)
        self.branch = branch
        self.credential = credential
        self.timeout_seconds = timeout_seconds

    # -- repository lifecycle ----------------------------------------------

    async def ensure_repo(self) -> None:
        """Clone as a bare partial clone if the working directory is empty."""
        if (self.workdir / "HEAD").exists():
            return
        self.workdir.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            "clone",
            "--bare",
            "--filter=blob:none",
            "--quiet",
            self.url,
            str(self.workdir),
        )

    async def fetch(self) -> None:
        """Fetch the tracked branch, keeping the blob filter in force."""
        await self._run("fetch", "--quiet", "--filter=blob:none", "origin", self.branch)

    async def rev_parse(self, revision: str) -> str:
        """Resolve a revision to a commit SHA."""
        return (await self._run("rev-parse", revision)).strip()

    async def remote_head(self) -> str:
        """SHA of the tracked branch's remote head."""
        return await self.rev_parse(f"refs/heads/{self.branch}")

    # -- inspection ---------------------------------------------------------

    async def list_files(self, sha: str) -> list[str]:
        """Every blob path in a commit's tree, recursively."""
        raw = await self._run("ls-tree", "-r", "-z", "--name-only", sha)
        return sorted(path for path in raw.split("\0") if path)

    async def diff(self, old_sha: str, new_sha: str) -> list[ChangeEvent]:
        """Change events between two commits, with rename detection.

        ``-M`` is what makes a moved file a rename instead of a delete plus an
        add — which is the difference between updating a path and re-embedding a
        directory.
        """
        raw = await self._run("diff", "--raw", "-z", "-M", old_sha, new_sha)
        return parse_diff_raw(raw)

    async def read_blob(self, revision: str, path: str) -> bytes:
        """Read one file's bytes, fetching its blob on demand.

        This is the call that triggers a lazy fetch under partial clone, which
        is why the pipeline only reaches for files that actually changed.
        """
        return await self._run_bytes("cat-file", "blob", f"{revision}:{path}")

    async def read_file_text(self, revision: str, path: str) -> str | None:
        """Read a text file, or ``None`` when it does not exist in that revision."""
        try:
            data = await self.read_blob(revision, path)
        except GitError:
            return None
        return data.decode("utf-8", errors="replace")

    async def blob_size(self, revision: str, path: str) -> int | None:
        """Size of a file without downloading it."""
        try:
            raw = await self._run("cat-file", "-s", f"{revision}:{path}")
        except GitError:
            return None
        return int(raw.strip())

    # -- plumbing -----------------------------------------------------------

    async def _run(self, *args: str) -> str:
        result = await asyncio.to_thread(self._invoke, args)
        return result.decode("utf-8", errors="replace")

    async def _run_bytes(self, *args: str) -> bytes:
        return await asyncio.to_thread(self._invoke, args)

    def _invoke(self, args: Sequence[str]) -> bytes:
        command = ["git", *args]
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=str(self.workdir) if self.workdir.exists() else None,
            capture_output=True,
            env=self._env(),
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed with {completed.returncode}: "
                f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
            )
        return completed.stdout

    def _env(self) -> dict[str, str]:
        """Environment for every invocation.

        ``GIT_TERMINAL_PROMPT=0`` turns a missing credential into an error
        instead of a worker hanging forever on a password prompt.
        """
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if self.credential:
            env["KB_GIT_TOKEN"] = self.credential
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "credential.helper"
            env["GIT_CONFIG_VALUE_0"] = '!f() { echo "username=kb"; echo "password=$KB_GIT_TOKEN"; }; f'
        return env
