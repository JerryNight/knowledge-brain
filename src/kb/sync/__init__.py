"""Sync package — git pull, diff, and the idempotence rules (spec §6)."""

from kb.sync.diff import ChangeEvent, additions_from_paths, parse_diff_raw, parse_name_status
from kb.sync.git import GitClient, GitError
from kb.sync.ignore import DEFAULT_IGNORES, KBIGNORE_FILENAME, IgnoreRules, needs_full_rescan, parse_kbignore
from kb.sync.pipeline import SyncOutcome, SyncPipeline
from kb.sync.ports import (
    SOURCE_GIT,
    SOURCE_UPLOAD,
    DocumentRecord,
    JobEnqueuer,
    JobPayload,
    NewDocument,
    RepoRef,
    UnitOfWork,
    UnitOfWorkScope,
    VaultStore,
)

__all__ = [
    "DEFAULT_IGNORES",
    "KBIGNORE_FILENAME",
    "SOURCE_GIT",
    "SOURCE_UPLOAD",
    "ChangeEvent",
    "DocumentRecord",
    "GitClient",
    "GitError",
    "IgnoreRules",
    "JobEnqueuer",
    "JobPayload",
    "NewDocument",
    "RepoRef",
    "SyncOutcome",
    "SyncPipeline",
    "UnitOfWork",
    "UnitOfWorkScope",
    "VaultStore",
    "additions_from_paths",
    "needs_full_rescan",
    "parse_diff_raw",
    "parse_kbignore",
    "parse_name_status",
]
