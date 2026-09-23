"""Git diff parsing — ``git diff --raw -z`` into change events (spec §6).

``--raw`` rather than ``--name-status`` because raw output carries the blob
SHAs. That is what makes the second short-circuit cheap: when Git reports a
rename, ``old_sha == new_sha`` proves the content did not change, so the file is
re-pathed without fetching its blob, converting it, or re-embedding a single
chunk. With ``--name-status`` we would have to fetch the blob to find out.

``-z`` because it NUL-separates records instead of quoting paths: a note named
``预算 "2026".md`` would otherwise arrive C-escaped and need un-escaping, and
non-ASCII paths would depend on ``core.quotePath`` settings.
"""

from __future__ import annotations

from dataclasses import dataclass

# Statuses we act on. `C` (copy) is treated as an addition: the new path needs
# indexing and the source path is untouched.
STATUS_ADDED = "A"
STATUS_MODIFIED = "M"
STATUS_DELETED = "D"
STATUS_RENAMED = "R"
STATUS_COPIED = "C"
STATUS_TYPECHANGE = "T"

NUL = "\0"


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One file-level change between two commits.

    ``old_blob``/``new_blob`` are git object ids, present only when the caller
    asked for raw output. ``old_path`` is set for renames and copies.
    """

    status: str
    path: str
    old_path: str | None = None
    old_blob: str | None = None
    new_blob: str | None = None

    @property
    def is_rename(self) -> bool:
        return self.status == STATUS_RENAMED

    @property
    def content_unchanged(self) -> bool:
        """True when the blob ids match, i.e. only the path moved.

        A rename with an edit is a modification; a rename without one is a path
        update and nothing else.
        """
        return bool(self.old_blob and self.new_blob and self.old_blob == self.new_blob)


def parse_diff_raw(raw: str) -> list[ChangeEvent]:
    """Parse NUL-separated ``git diff --raw -z`` output.

    Each record is either::

        :<old_mode> <new_mode> <old_sha> <new_sha> <status>\\0<path>\\0

    or, for renames and copies, one path pair::

        ...\\0<old_path>\\0<new_path>\\0
    """
    tokens = [token for token in raw.split(NUL) if token != ""]
    events: list[ChangeEvent] = []
    index = 0

    while index < len(tokens):
        header = tokens[index]
        if not header.startswith(":"):
            index += 1
            continue

        fields = header[1:].split()
        if len(fields) < 5:
            index += 1
            continue

        old_blob, new_blob, raw_status = fields[2], fields[3], fields[4]
        status = raw_status[0]

        if status in (STATUS_RENAMED, STATUS_COPIED):
            if index + 2 >= len(tokens):
                break
            old_path, new_path = tokens[index + 1], tokens[index + 2]
            index += 3
        else:
            if index + 1 >= len(tokens):
                break
            old_path, new_path = None, tokens[index + 1]
            index += 2

        events.append(
            ChangeEvent(
                status=status,
                path=new_path,
                old_path=old_path,
                old_blob=_clean_blob(old_blob),
                new_blob=_clean_blob(new_blob),
            )
        )
    return events


def parse_name_status(raw: str) -> list[ChangeEvent]:
    """Parse ``git diff --name-status -z`` output. Kept for callers that only
    need paths (no blob ids, so no free rename short-circuit)."""
    tokens = [token for token in raw.split(NUL) if token != ""]
    events: list[ChangeEvent] = []
    index = 0

    while index < len(tokens):
        status = tokens[index][0]
        if status in (STATUS_RENAMED, STATUS_COPIED):
            if index + 2 >= len(tokens):
                break
            events.append(ChangeEvent(status=status, path=tokens[index + 2], old_path=tokens[index + 1]))
            index += 3
        else:
            if index + 1 >= len(tokens):
                break
            events.append(ChangeEvent(status=status, path=tokens[index + 1]))
            index += 2
    return events


def additions_from_paths(paths: list[str]) -> list[ChangeEvent]:
    """Turn a file listing into ``A`` events — the first-full-sync shape."""
    return [ChangeEvent(status=STATUS_ADDED, path=path) for path in paths]


def _clean_blob(value: str) -> str | None:
    """Zero-filled object ids mean "no such side" (added or deleted files)."""
    if not value or set(value) == {"0"}:
        return None
    return value
