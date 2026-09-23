"""Ignore rules for the vault (spec §6).

Two sources, applied together:

* hard-coded defaults — ``.obsidian/``, ``.trash/``, ``.git/``, ``.DS_Store``;
* the repository's own ``.kbignore``, in gitignore syntax.

The subtle part is in ``needs_full_rescan``: editing ``.kbignore`` is itself a
file change, and it is not an ordinary one. Widening the rules must index files
that were previously skipped; narrowing them must delete rows that are now
excluded. Treating the edit as a normal file change is how a user ends up
saying "I un-ignored it and it still doesn't show up".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import pathspec

from kb.sync.diff import ChangeEvent

KBIGNORE_FILENAME = ".kbignore"

# pathspec's name for gitignore-syntax matching. It was called `gitwildmatch`
# before pathspec 1.0, and the old name is deprecated rather than removed — this
# project pins >=1.0 (see pyproject) so the current name is used directly.
PATTERN_FACTORY = "gitignore"

DEFAULT_IGNORES: tuple[str, ...] = (
    ".obsidian/",
    ".trash/",
    ".git/",
    ".DS_Store",
)


@dataclass(slots=True)
class IgnoreRules:
    """A compiled matcher plus the raw lines, for reporting and debugging."""

    spec: pathspec.PathSpec
    lines: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_lines(cls, extra: Sequence[str] = ()) -> IgnoreRules:
        """Defaults first, then the repository's own rules.

        Order matters for negations: a later ``!keep.md`` must be able to win
        over an earlier ``*.md``, which is how gitignore-style matching works
        and therefore what a user writing ``.kbignore`` expects.
        """
        lines = [*DEFAULT_IGNORES, *extra]
        return cls(spec=pathspec.PathSpec.from_lines(PATTERN_FACTORY, lines), lines=tuple(lines))

    def ignores(self, path: str) -> bool:
        return self.spec.match_file(path)

    def keep(self, paths: Iterable[str]) -> list[str]:
        return [path for path in paths if not self.ignores(path)]

    @property
    def kbignore_is_ignored(self) -> bool:
        return self.ignores(KBIGNORE_FILENAME)


def parse_kbignore(text: str) -> list[str]:
    """Read ``.kbignore`` content into pattern lines, dropping comments/blanks."""
    patterns: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def needs_full_rescan(events: Sequence[ChangeEvent]) -> bool:
    """Whether this change set includes a ``.kbignore`` edit.

    Any status counts, including a delete: removing the file restores the
    defaults, which also changes what should be indexed.
    """
    for event in events:
        if event.path == KBIGNORE_FILENAME or event.old_path == KBIGNORE_FILENAME:
            return True
    return False
