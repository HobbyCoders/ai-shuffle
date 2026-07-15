"""Download-route tests for app/api/artifacts.py."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.artifacts import router
from app.api.auth import require_auth
from app.core import artifact_store
from app.core.config import settings
from app.db.artifacts import upsert_artifact
from app.db.base import get_db
from app.db.installs import register_install


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


def test_hash_re_rejects_trailing_newline():
    """The download-route regex must use the ``\\Z`` anchor: a 64-hex hash with a
    trailing newline is REJECTED (``$`` would have matched before the newline,
    admitting a smuggled newline into the content-hash comparison)."""
    from app.api.artifacts import _HASH_RE

    valid = "a" * 64
    assert _HASH_RE.match(valid) is not None
    assert _HASH_RE.match(valid + "\n") is None


# ---------------------------------------------------------------------------
# Cross-install serving — local hold vs 307 holder redirect vs honest 404.
# ---------------------------------------------------------------------------

HOLDER = "install-B"
HOLDER_URL = "http://10.0.0.99:8000"


def _make_online_holder(base_url):
    register_install(HOLDER, "Device B", base_url, "linux", "1.0", {})


def _make_holder_offline():
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE installs SET last_seen = %s WHERE id = %s",
            ("2000-01-01T00:00:00+00:00", HOLDER),
        )


def test_serves_locally_even_when_runner_is_another_install(mock_db, client, monkeypatch):
    # We physically hold the bytes (path present) — serve directly regardless of
    # the recorded holder attribution.
    monkeypatch.setattr(settings, "instance_id", "install-A")
    data = b"locally-held-image"
    row = artifact_store.store_bytes(data, mime="image/png", filename="s.png",
                                     runner_id=HOLDER)
    resp = client.get(f"/api/v1/artifacts/{row['hash']}")
    assert resp.status_code == 200
    assert resp.content == data


def test_redirects_to_online_holder(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = "b" * 64
    # Metadata row only — no local bytes on this install's disk.
    upsert_artifact(hash=h, size=42, mime="image/png", filename="s.png",
                    created_by_session="s", runner_id=HOLDER)
    _make_online_holder(HOLDER_URL)

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"{HOLDER_URL}/api/v1/artifacts/{h}"


def test_redirect_strips_trailing_slash_on_base_url(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = "c" * 64
    upsert_artifact(hash=h, size=42, mime="image/png", filename="s.png",
                    created_by_session="s", runner_id=HOLDER)
    _make_online_holder(HOLDER_URL + "/")

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"{HOLDER_URL}/api/v1/artifacts/{h}"


def test_no_redirect_when_holder_has_no_base_url(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = "d" * 64
    upsert_artifact(hash=h, size=42, mime="image/png", filename="s.png",
                    created_by_session="s", runner_id=HOLDER)
    _make_online_holder(None)

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 404


def test_no_redirect_when_holder_offline(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = "e" * 64
    upsert_artifact(hash=h, size=42, mime="image/png", filename="s.png",
                    created_by_session="s", runner_id=HOLDER)
    _make_online_holder(HOLDER_URL)
    _make_holder_offline()

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 404


def test_no_redirect_to_self_when_bytes_missing(mock_db, client, monkeypatch):
    # We are recorded as the holder but the bytes are gone from disk — an honest
    # 404, never a redirect loop back to ourselves.
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = "f" * 64
    upsert_artifact(hash=h, size=42, mime="image/png", filename="s.png",
                    created_by_session="s", runner_id="install-A")

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 404
