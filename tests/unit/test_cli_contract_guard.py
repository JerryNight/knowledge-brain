"""Every ``kb`` subcommand has to be able to reach its adapter.

The CLI is the only provisioning path in phase 1 (spec §9) and it had no test at
all, so ``kb status`` shipped calling ``documents.by_status(user_id, statuses)``
positionally against a keyword-only signature. It crashed on its first real run
and nothing caught it: a command function is only ever exercised by typing the
command, and by then the traceback *is* the test.

The mocks are built with ``create_autospec`` from the concrete adapter classes
rather than from hand-written fakes. That is the whole trick — autospec copies
the real signature, so a call the adapter would reject raises ``TypeError`` here
instead of in production. A permissive fake would reproduce the very bug this
file exists to catch, which is why ``test_the_autospec_rejects_a_positional_call``
asserts the trap is actually armed.
"""

from __future__ import annotations

import argparse
import uuid
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from kb import cli
from kb.db.adapters.documents import PostgresDocumentReader
from kb.db.adapters.index_admin import PostgresIndexMaintenance
from kb.db.adapters.repos import PostgresRepoService
from kb.db.adapters.tokens import TokenService
from kb.queue.postgres import PostgresJobQueue

USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
REPO_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TOKEN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _services() -> SimpleNamespace:
    """A ``Services`` stand-in whose every method carries its real signature."""
    tokens = create_autospec(TokenService, instance=True)
    tokens.create_user.return_value = USER_ID
    tokens.issue_token.return_value = SimpleNamespace(plaintext="kb_plaintext", token_id=TOKEN_ID)
    tokens.revoke_token.return_value = True
    tokens.find_user_by_email.return_value = USER_ID

    repos = create_autospec(PostgresRepoService, instance=True)
    repos.list_repos.return_value = [SimpleNamespace(id=REPO_ID, branch="main", last_synced_sha=None, url="/tmp/vault")]
    repos.create_repo.return_value = REPO_ID
    repos.request_sync.return_value = 1
    repos.request_full_rebuild.return_value = 1

    queue = create_autospec(PostgresJobQueue, instance=True)
    queue.counts_for_user.return_value = {}

    documents = create_autospec(PostgresDocumentReader, instance=True)
    documents.by_status.return_value = []

    index_maintenance = create_autospec(PostgresIndexMaintenance, instance=True)
    index_maintenance.count_chunks.return_value = 0

    return SimpleNamespace(
        tokens=tokens,
        repos=repos,
        queue=queue,
        documents=documents,
        index_maintenance=index_maintenance,
        vector_search_enabled=True,
    )


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    stub = _services()
    monkeypatch.setattr(cli, "build_services", lambda: stub)
    return stub


def _args(**overrides: object) -> argparse.Namespace:
    """Arguments the way ``argparse`` produces them: every flag present, mostly None."""
    base: dict[str, object] = {"user_id": None, "email": "someone@example.com"}
    base.update(overrides)
    return argparse.Namespace(**base)


async def test_status_reaches_every_adapter(services: SimpleNamespace, capsys) -> None:
    assert await cli._status(_args()) == 0
    assert "vector_search_enabled: True" in capsys.readouterr().out
    services.repos.list_repos.assert_awaited()
    services.queue.counts_for_user.assert_awaited()
    services.index_maintenance.count_chunks.assert_awaited()
    services.documents.by_status.assert_awaited()


async def test_user_create_prints_the_token_once(services: SimpleNamespace, capsys) -> None:
    args = _args(token_name="default", base_url="https://kb.example.com")
    assert await cli._user_create(args) == 0
    assert "kb_plaintext" in capsys.readouterr().out


async def test_token_issue_and_revoke(services: SimpleNamespace) -> None:
    assert await cli._token_issue(_args(name="laptop")) == 0
    assert await cli._token_revoke(_args(token_id=str(TOKEN_ID))) == 0
    services.tokens.revoke_token.assert_awaited_with(TOKEN_ID)


async def test_repo_add_passes_every_field_by_name(services: SimpleNamespace) -> None:
    args = _args(url="/tmp/vault", branch="main", credential_ref=None, no_sync=False)
    assert await cli._repo_add(args) == 0
    services.repos.create_repo.assert_awaited_with(
        USER_ID, url="/tmp/vault", branch="main", credential_ref=None, sync_now=True
    )


async def test_sync_without_a_repo_id_sweeps_every_repo(services: SimpleNamespace) -> None:
    assert await cli._sync(_args(repo_id=None)) == 0
    services.repos.request_sync.assert_awaited_with(USER_ID, REPO_ID)


async def test_rebuild_enqueues_for_the_tenant(services: SimpleNamespace) -> None:
    assert await cli._rebuild(_args()) == 0
    services.repos.request_full_rebuild.assert_awaited_with(USER_ID)


async def test_the_autospec_rejects_a_positional_call(services: SimpleNamespace) -> None:
    """Without this, a permissive mock would make every assertion above vacuous."""
    with pytest.raises(TypeError):
        await services.documents.by_status(USER_ID, ("failed",))
