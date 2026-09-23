"""Unit tests for authentication and the REST surface (spec §9).

Driven through a real ASGI call rather than by calling handlers directly, because
the thing being tested *is* the pipeline: middleware resolves the token, the
principal lands in a context variable, and the handler reads it back. Calling the
handler in isolation would skip the only step that can be wrong.

``TestClient`` is used without its context manager on purpose — that runs requests
without starting the application lifespan, which is precisely the scope of these
tests. The MCP session manager's startup is exercised by the deployment, not here.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.app import create_app
from kb.auth import generate_token
from kb.context import current_principal
from tests.unit.fakes import (
    FakeDocumentReader,
    FakeIndexConfig,
    FakeQueueCounts,
    FakeRepoService,
    FakeTokenService,
    make_services,
    repo_info,
    summary,
)

USER = uuid.UUID("55555555-5555-5555-5555-555555555555")
TOKEN = generate_token()


def services(**overrides):
    base = {
        "tokens": FakeTokenService({TOKEN: USER}),
        "repos": FakeRepoService(),
        "queue": FakeQueueCounts(),
        "documents": FakeDocumentReader(),
        "index_config": FakeIndexConfig(),
    }
    base.update(overrides)
    return make_services(**base)


def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------


def test_health_needs_no_token() -> None:
    """A liveness probe that needs a credential reports the wrong thing when the
    credential store is the thing that broke."""
    response = client(create_app(services())).get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_protected_endpoint_rejects_a_missing_token() -> None:
    response = client(create_app(services())).get("/api/repos")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_protected_endpoint_rejects_an_unknown_token() -> None:
    response = client(create_app(services())).get(
        "/api/repos", headers={"Authorization": f"Bearer {generate_token()}"}
    )
    assert response.status_code == 401


def test_protected_endpoint_rejects_a_malformed_header() -> None:
    response = client(create_app(services())).get("/api/repos", headers={"Authorization": "Basic abc"})
    assert response.status_code == 401


def test_mcp_endpoint_is_protected_too() -> None:
    response = client(create_app(services())).post("/mcp", json={})
    assert response.status_code == 401


def test_a_valid_token_is_accepted_and_the_principal_reaches_the_handler() -> None:
    app = create_app(services())

    @app.get("/api/_whoami")
    async def whoami() -> dict:
        principal = current_principal()
        return {"user_id": str(principal.user_id) if principal else None}

    response = client(app).get("/api/_whoami", headers=auth())
    assert response.status_code == 200
    assert response.json()["user_id"] == str(USER)


def test_the_principal_does_not_leak_into_the_next_request() -> None:
    """The contextvar is reset per request, so an authenticated call cannot colour a later one."""
    app = create_app(services())

    @app.get("/api/_whoami")
    async def whoami() -> dict:
        principal = current_principal()
        return {"user_id": str(principal.user_id) if principal else None}

    http = client(app)
    assert http.get("/api/_whoami", headers=auth()).json()["user_id"] == str(USER)
    assert http.get("/api/_whoami").status_code == 401


# ---------------------------------------------------------------------------
# repos
# ---------------------------------------------------------------------------


def test_listing_repos_returns_the_callers_repos() -> None:
    repo = repo_info(user_id=USER, last_synced_sha="a" * 40)
    response = client(create_app(services(repos=FakeRepoService([repo])))).get(
        "/api/repos", headers=auth()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["repos"][0]["last_synced_sha"] == "a" * 40


def test_sync_enqueues_rather_than_synchronising_inline() -> None:
    repo = repo_info(user_id=USER)
    repo_service = FakeRepoService([repo])
    response = client(create_app(services(repos=repo_service))).post(
        f"/api/repos/{repo.id}/sync", headers=auth()
    )
    assert response.status_code == 202
    assert repo_service.synced == [repo.id]


def test_syncing_someone_elses_repo_is_a_404_not_a_403() -> None:
    """The tenant-scoped query does not see it, so "not found" is the honest answer
    and it leaks nothing about other tenants' repository ids."""
    response = client(create_app(services())).post(
        f"/api/repos/{uuid.uuid4()}/sync", headers=auth()
    )
    assert response.status_code == 404


def test_creating_a_repo_registers_and_optionally_queues_sync() -> None:
    repo_service = FakeRepoService()
    response = client(create_app(services(repos=repo_service))).post(
        "/api/repos",
        headers=auth(),
        json={"url": "git@github.com:you/vault.git", "sync_now": False},
    )
    assert response.status_code == 201
    assert repo_service.created == [
        {"url": "git@github.com:you/vault.git", "branch": "main", "credential_ref": None}
    ]
    assert repo_service.synced == []


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------


def test_rebuild_enqueues_a_job() -> None:
    repo_service = FakeRepoService()
    response = client(create_app(services(repos=repo_service))).post(
        "/api/admin/rebuild", headers=auth()
    )
    assert response.status_code == 202
    assert repo_service.rebuilds == [USER]


def test_status_reports_jobs_scoped_to_the_caller() -> None:
    queue = FakeQueueCounts({"pending": 3})
    response = client(create_app(services(queue=queue))).get("/api/admin/status", headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert body["jobs"] == {"pending": 3}
    # Scoped, not the installation-wide count: sync_jobs has no RLS, so the
    # predicate has to be explicit.
    assert queue.asked_for == [USER]


def test_status_surfaces_files_that_are_known_but_unsearchable() -> None:
    reader = FakeDocumentReader(summaries=[summary("扫描件.pdf", status="no_text")])
    response = client(create_app(services(documents=reader))).get("/api/admin/status", headers=auth())
    body = response.json()
    assert [item["path"] for item in body["unsearchable"]] == ["扫描件.pdf"]


def test_status_reports_the_stored_index_stamp_so_a_mismatch_is_visible() -> None:
    response = client(create_app(services())).get("/api/admin/status", headers=auth())
    index = response.json()["index"]
    assert index["configured"]["chunker_version"] == 1
    assert index["stored"] is None


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


def test_upload_rejects_an_empty_body() -> None:
    response = client(create_app(services())).post(
        "/api/documents", headers=auth(), params={"path": "a.md"}, content=b""
    )
    assert response.status_code == 400


def test_upload_rejects_a_path_that_escapes_the_tenant_directory() -> None:
    app = create_app(services())
    app.state.services.blob_store = _RejectingBlobStore()
    response = _upload(app, "a.md", b"hello")
    assert response.status_code == 400


def test_upload_stores_bytes_then_writes_the_document_and_job() -> None:
    store = _RecordingBlobStore()
    upload = _RecordingUpload()
    app = create_app(services())
    app.state.services.blob_store = store
    app.state.services.upload = upload
    app.state.services.settings.max_file_size_bytes = 1024

    response = _upload(app, "报告.pdf", b"%PDF-1.4 bytes")

    assert response.status_code == 202
    assert store.stored == [("报告.pdf", b"%PDF-1.4 bytes")]
    assert upload.requests[0].filename == "报告.pdf"
    assert response.json()["job_id"] == 99


def test_upload_refuses_an_oversized_file_before_writing_anything() -> None:
    store = _RecordingBlobStore()
    app = create_app(services())
    app.state.services.blob_store = store
    app.state.services.upload = _RecordingUpload()
    app.state.services.settings.max_file_size_bytes = 4

    response = _upload(app, "big.pdf", b"way more than four bytes")

    assert response.status_code == 413
    assert store.stored == []


# -- helpers ----------------------------------------------------------------


def _upload(app: FastAPI, path: str, body: bytes):
    return client(app).post(
        "/api/documents",
        headers={**auth(), "content-type": "application/pdf"},
        params={"path": path},
        content=body,
    )


class _RecordingBlobStore:
    def __init__(self) -> None:
        self.stored: list[tuple[str, bytes]] = []

    async def put(self, *, user_id, name: str, blob: bytes):
        self.stored.append((name, blob))


class _RejectingBlobStore:
    async def put(self, *, user_id, name: str, blob: bytes):
        from kb.db.adapters.blobs import UnsafeBlobPath

        raise UnsafeBlobPath(f"upload name escapes the tenant directory: {name!r}")


class _RecordingUpload:
    def __init__(self) -> None:
        self.requests: list = []

    async def upload(self, request):
        from kb.upload.service import UploadOutcome

        self.requests.append(request)
        return UploadOutcome(content_sha="deadbeef", document_id=uuid.uuid4(), job_id=99)
