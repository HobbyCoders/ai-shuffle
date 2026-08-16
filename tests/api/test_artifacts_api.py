"""Download-route tests for app/api/artifacts.py."""

import hashlib
import hmac
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.artifacts import _require_auth_or_signed, router
from app.core import artifact_store, cluster
from app.core.config import settings
from app.db.artifacts import upsert_artifact
from app.db.base import get_db
from app.db.installs import register_install

CLUSTER_SECRET = "test-cluster-secret"


@pytest.fixture(autouse=True)
def pinned_cluster_secret(monkeypatch):
    """Pin the shared secret so mint/verify are deterministic (no DB round-trip)."""
    monkeypatch.setattr(cluster, "get_cluster_secret", lambda: CLUSTER_SECRET)


@pytest.fixture
def artifacts_dir(tmp_path, monkeypatch):
    d = tmp_path / "artifacts"
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", d)
    return d


@pytest.fixture
def client(artifacts_dir):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_require_auth_or_signed] = lambda: "test-token"
    with TestClient(app) as c:
        yield c


@pytest.fixture
def unauth_client(artifacts_dir):
    """Same app, but with the auth dependency left in place."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _sign(hash: str, exp: int) -> str:
    """Recompute the redirect signature exactly as ``mint_artifact_token`` does."""
    return hmac.new(
        CLUSTER_SECRET.encode(),
        f"artifact-download:{hash}:{exp}".encode(),
        hashlib.sha256,
    ).hexdigest()


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


def _assert_signed_redirect(location: str, h: str):
    """The 307 target is the holder's URL plus a valid cluster-signed exp/sig."""
    parsed = urlparse(location)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        f"{HOLDER_URL}/api/v1/artifacts/{h}"
    )
    q = parse_qs(parsed.query)
    exp, sig = q["exp"][0], q["sig"][0]
    assert cluster.verify_artifact_token(h, exp, sig) is True


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
    _assert_signed_redirect(resp.headers["location"], h)


def test_redirect_strips_trailing_slash_on_base_url(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = "c" * 64
    upsert_artifact(hash=h, size=42, mime="image/png", filename="s.png",
                    created_by_session="s", runner_id=HOLDER)
    _make_online_holder(HOLDER_URL + "/")

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 307
    _assert_signed_redirect(resp.headers["location"], h)


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


# ---------------------------------------------------------------------------
# Signed redirect on the receiving end — a cookieless browser that followed the
# 307 is authorized by the exp/sig pair alone; anything else still 401s.
# ---------------------------------------------------------------------------

def test_unauthenticated_download_is_401(mock_db, unauth_client):
    data = b"secret bytes"
    row = artifact_store.store_bytes(data, mime="text/plain", filename="a.txt")

    resp = unauth_client.get(f"/api/v1/artifacts/{row['hash']}")
    assert resp.status_code == 401


def test_valid_signed_token_serves_bytes_without_auth(mock_db, unauth_client):
    data = b"cross-install bytes"
    row = artifact_store.store_bytes(data, mime="text/plain", filename="a.txt")
    h = row["hash"]
    exp, sig = cluster.mint_artifact_token(h)

    resp = unauth_client.get(f"/api/v1/artifacts/{h}?exp={exp}&sig={sig}")
    assert resp.status_code == 200
    assert resp.content == data


def test_expired_signed_token_is_401(mock_db, unauth_client):
    data = b"cross-install bytes"
    row = artifact_store.store_bytes(data, mime="text/plain", filename="a.txt")
    h = row["hash"]
    exp = int(time.time()) - 10
    sig = _sign(h, exp)

    resp = unauth_client.get(f"/api/v1/artifacts/{h}?exp={exp}&sig={sig}")
    assert resp.status_code == 401


def test_tampered_signature_is_401(mock_db, unauth_client):
    data = b"cross-install bytes"
    row = artifact_store.store_bytes(data, mime="text/plain", filename="a.txt")
    h = row["hash"]
    exp, sig = cluster.mint_artifact_token(h)
    flipped = ("f" if sig[0] != "f" else "0") + sig[1:]

    resp = unauth_client.get(f"/api/v1/artifacts/{h}?exp={exp}&sig={flipped}")
    assert resp.status_code == 401
