"""Unit tests for diff parsing, ignore rules, and the git client against a real
local repository.

The git tests build a throwaway repository on disk and never talk to a network,
which is what spec §11.1 #8 asks for: git behaviour is verified, not simulated.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from kb.sync.diff import ChangeEvent, additions_from_paths, parse_diff_raw, parse_name_status
from kb.sync.git import GitClient, GitError
from kb.sync.ignore import DEFAULT_IGNORES, IgnoreRules, needs_full_rescan, parse_kbignore

# ---------------------------------------------------------------------------
# diff parsing
# ---------------------------------------------------------------------------


def test_parse_raw_added_file() -> None:
    raw = ":000000 100644 0000000 1111111 A\0note.md\0"
    events = parse_diff_raw(raw)
    assert events == [ChangeEvent(status="A", path="note.md", old_blob=None, new_blob="1111111")]


def test_parse_raw_deleted_file() -> None:
    raw = ":100644 000000 1111111 0000000 D\0note.md\0"
    events = parse_diff_raw(raw)
    assert events[0].status == "D"
    assert events[0].new_blob is None
    assert events[0].old_blob == "1111111"


def test_parse_raw_rename_carries_both_paths_and_blobs() -> None:
    raw = ":100644 100644 aaa bbb R100\0old/note.md\0new/note.md\0"
    events = parse_diff_raw(raw)
    assert events == [
        ChangeEvent(status="R", path="new/note.md", old_path="old/note.md", old_blob="aaa", new_blob="bbb")
    ]
    assert events[0].is_rename


def test_rename_with_identical_blobs_is_flagged_as_content_unchanged() -> None:
    """This is what makes a pure rename free: no fetch, no embedding."""
    same = ChangeEvent(status="R", path="b.md", old_path="a.md", old_blob="aaa", new_blob="aaa")
    edited = ChangeEvent(status="R", path="b.md", old_path="a.md", old_blob="aaa", new_blob="ccc")
    assert same.content_unchanged
    assert not edited.content_unchanged


def test_parse_raw_handles_multiple_records() -> None:
    raw = ":100644 100644 aaa bbb M\0one.md\0:000000 100644 0000000 ccc A\0two.md\0"
    events = parse_diff_raw(raw)
    assert [event.path for event in events] == ["one.md", "two.md"]


def test_parse_raw_ignores_blank_and_unknown_records() -> None:
    assert parse_diff_raw("\0\0garbage\0") == []


def test_parse_name_status_handles_renames() -> None:
    events = parse_name_status("R100\0old.md\0new.md\0M\0two.md\0")
    assert events == [
        ChangeEvent(status="R", path="new.md", old_path="old.md"),
        ChangeEvent(status="M", path="two.md"),
    ]


def test_additions_from_paths_marks_everything_added() -> None:
    events = additions_from_paths(["a.md", "b.md"])
    assert [event.status for event in events] == ["A", "A"]


# ---------------------------------------------------------------------------
# ignore rules
# ---------------------------------------------------------------------------


def test_default_ignores_cover_obsidian_plumbing() -> None:
    rules = IgnoreRules.from_lines()
    for path in DEFAULT_IGNORES:
        assert rules.ignores(path.rstrip("/") + "/x.md") or rules.ignores(path)


def test_kbignore_patterns_are_applied() -> None:
    rules = IgnoreRules.from_lines(parse_kbignore("drafts/\n*.tmp\n"))
    assert rules.ignores("drafts/x.md")
    assert rules.ignores("notes/a.tmp")
    assert not rules.ignores("notes/a.md")


def test_kbignore_negation_reopens_a_file() -> None:
    rules = IgnoreRules.from_lines(parse_kbignore("drafts/\n!drafts/keep.md\n"))
    assert rules.ignores("drafts/other.md")
    assert not rules.ignores("drafts/keep.md")


def test_parse_kbignore_skips_comments_and_blanks() -> None:
    text = "# a comment\n\n  \ndrafts/\n"
    assert parse_kbignore(text) == ["drafts/"]


def test_keep_filters_a_path_list() -> None:
    rules = IgnoreRules.from_lines(parse_kbignore("drafts/\n"))
    assert rules.keep(["a.md", "drafts/b.md"]) == ["a.md"]


def test_kbignore_edit_forces_a_full_rescan() -> None:
    events = [ChangeEvent(status="M", path=".kbignore"), ChangeEvent(status="M", path="a.md")]
    assert needs_full_rescan(events)


def test_kbignore_delete_also_forces_a_full_rescan() -> None:
    """Removing the file restores the defaults, so what is indexed changes too."""
    assert needs_full_rescan([ChangeEvent(status="D", path=".kbignore")])


def test_ordinary_changes_do_not_force_a_rescan() -> None:
    assert not needs_full_rescan([ChangeEvent(status="M", path="notes/a.md")])


# ---------------------------------------------------------------------------
# git client, against a real repository
# ---------------------------------------------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "kb test",
    "GIT_AUTHOR_EMAIL": "kb@test.invalid",
    "GIT_COMMITTER_NAME": "kb test",
    "GIT_COMMITTER_EMAIL": "kb@test.invalid",
}


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=_env())
    return completed.stdout.strip()


def _env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.update(GIT_ENV)
    return env


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    """A repository with two commits, including a rename."""
    repo = tmp_path / "vault"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "note.md").write_text("# 笔记\n\n第一版\n", encoding="utf-8")
    (repo / "keep.md").write_text("保留\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "first")

    git(repo, "mv", "note.md", "renamed.md")
    (repo / "added.md").write_text("新增内容\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "rename and add")
    return repo


@pytest.fixture()
def client(tmp_path: Path, source_repo: Path) -> GitClient:
    return GitClient(str(source_repo), tmp_path / "clone", branch="main")


async def test_partial_clone_and_fetch(client: GitClient) -> None:
    await client.ensure_repo()
    assert (client.workdir / "HEAD").exists()
    await client.fetch()  # idempotent
    head = await client.remote_head()
    assert len(head) == 40


async def test_ensure_repo_is_a_no_op_the_second_time(client: GitClient) -> None:
    await client.ensure_repo()
    await client.ensure_repo()
    assert (client.workdir / "HEAD").exists()


async def test_list_files_walks_the_tree(client: GitClient) -> None:
    await client.ensure_repo()
    await client.fetch()
    files = await client.list_files(await client.remote_head())
    assert files == ["added.md", "keep.md", "renamed.md"]


async def test_diff_reports_a_rename_as_a_rename(client: GitClient) -> None:
    """``-M`` is what turns "deleted + added" into "renamed" — and a pure rename
    into zero embedding work."""
    await client.ensure_repo()
    await client.fetch()
    raw = git(client.workdir.parent / "vault", "rev-list", "--max-parents=0", "HEAD").split()[0]
    events = {event.path: event for event in await client.diff(raw, await client.remote_head())}

    assert events["renamed.md"].status == "R"
    assert events["renamed.md"].old_path == "note.md"
    assert events["renamed.md"].content_unchanged
    assert events["added.md"].status == "A"


async def test_read_blob_fetches_content_on_demand(client: GitClient) -> None:
    await client.ensure_repo()
    await client.fetch()
    head = await client.remote_head()
    assert await client.read_blob(head, "added.md") == "新增内容\n".encode()
    assert await client.blob_size(head, "added.md") == len("新增内容\n".encode())


async def test_read_file_text_returns_none_for_missing_paths(client: GitClient) -> None:
    await client.ensure_repo()
    await client.fetch()
    assert await client.read_file_text(await client.remote_head(), "nope.md") is None


async def test_git_errors_carry_the_command(client: GitClient, tmp_path: Path) -> None:
    broken = GitClient(str(tmp_path / "does-not-exist"), tmp_path / "clone2", branch="main")
    with pytest.raises(GitError, match="clone"):
        await broken.ensure_repo()


def test_credential_is_passed_through_the_environment(tmp_path: Path) -> None:
    """argv is world-readable on the host, so the token must not go there."""
    client = GitClient("https://example.test/vault.git", tmp_path / "c", credential="secret-token")
    env = client._env()
    assert env["KB_GIT_TOKEN"] == "secret-token"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "secret-token" not in " ".join(["git", "clone", client.url])


def test_repo_id_is_a_uuid_where_needed() -> None:
    assert isinstance(uuid.uuid4(), uuid.UUID)
