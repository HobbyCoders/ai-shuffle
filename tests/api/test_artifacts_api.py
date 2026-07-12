"""Download-route tests for app/api/artifacts.py."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.artifacts import router
from app.api.auth import require_auth
from app.core import artifact_store


@pytest.fixture
def artifacts_dir(tmp_path, monkeypatch):
    d = tmp_path / "artifacts"
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", d)
    return d


@pytest.fixture
def client(artifacts_dir):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    with TestClient(app) as c:
        yield c


def test_download_returns_bytes_and_mime(mock_db, client):
    data = b"the artifact bytes"
    row = artifact_store.store_bytes(data, mime="text/plain", filename="a.txt")

    resp = client.get(f"/api/v1/artifacts/{row['hash']}")
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-type"].startswith("text/plain")


def test_unknown_valid_hash_is_404(mock_db, client):
    resp = client.get(f"/api/v1/artifacts/{'a' * 64}")
    assert resp.status_code == 404


def test_invalid_hash_format_is_404(mock_db, client):
    resp = client.get("/api/v1/artifacts/not-a-real-hash")
    assert resp.status_code == 404
