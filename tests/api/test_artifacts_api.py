"""Download-route tests for app/api/artifacts.py."""

import hashlib
import hmac
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api import artifacts as artifacts_mod
from app.api.artifacts import _require_auth_or_signed, router
from app.api.projects import MAX_UPLOAD_SIZE
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
def client(artifacts_dir, mock_db):
    from app.db import api_users, sessions
    api_users.create_api_user('fixture-user', 'Fixture user', 'fixture-hash')
    sessions.create_session('s', 'default', api_user_id='fixture-user')
    app = FastAPI()
    app.include_router(router)
    def authenticate(request: Request):
        request.state.api_user = {'id': request.headers.get('x-fixture-user', 'fixture-user')}
        return 'test-token'
    app.dependency_overrides[_require_auth_or_signed] = authenticate
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


@pytest.mark.parametrize('reference_type', ['artifact.created', 'tool.use'])
def test_shared_hash_requires_accessible_session_with_durable_reference(mock_db, client, reference_type):
    from app.db import api_users, sessions, session_events
    api_users.create_api_user('second-user', 'Second user', 'second-hash')
    sessions.create_session('second-session', 'default', api_user_id='second-user')
    sessions.create_session('unrelated-session', 'default', api_user_id='second-user')
    row = artifact_store.store_bytes(b'shared screenshot', mime='image/png', created_by_session='s')
    digest = row['hash']
    url = f'/api/v1/artifacts/{digest}'
    headers = {'x-fixture-user': 'second-user'}
    assert client.get(url, headers=headers).status_code == 404
    assert client.get(url + '?session_id=second-session', headers=headers).status_code == 404
    payload = {'hash': digest} if reference_type == 'artifact.created' else {'screenshot': url}
    session_events.append_events('second-session', [{'type': reference_type, 'payload': payload}])
    assert client.get(url + '?session_id=second-session', headers=headers).content == b'shared screenshot'
    assert client.get(url + '?session_id=unrelated-session', headers=headers).status_code == 404
    assert client.get(url + '?session_id=second-session').status_code == 404
    assert client.get(url).content == b'shared screenshot'


def test_paired_account_cannot_read_another_accounts_screenshot(mock_db, artifacts_dir, monkeypatch):
    from app.db import sessions, mobile_tool_identity
    from app.core import artifact_access
    sessions.create_session('account-image', 'default')
    mobile_tool_identity.bind('account-image', dict(owner='account:account-A', kind='paired',
        principal_id='phone-A', account_id='account-A', credential_hash='fixture'), new=True)
    row = artifact_store.store_bytes(b'private account image', mime='image/png', created_by_session='account-image')
    monkeypatch.setattr(artifact_access.mobile_hub, 'ownership', lambda **kw: {'account_id': kw['paired_client_id']})
    app = FastAPI()
    app.include_router(router)
    def authenticate(request: Request):
        request.state.paired_client = {'id': request.headers.get('x-fixture-account')}
        return 'test-token'
    app.dependency_overrides[_require_auth_or_signed] = authenticate
    with TestClient(app) as c:
        url = f"/api/v1/artifacts/{row['hash']}?session_id=account-image"
        assert c.get(url, headers={'x-fixture-account': 'account-A'}).status_code == 200
        assert c.get(url, headers={'x-fixture-account': 'account-B'}).status_code == 404


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
    monkeypatch.setattr(settings, "cluster_artifact_proxy", False)
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
    monkeypatch.setattr(settings, "cluster_artifact_proxy", False)
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


# ---------------------------------------------------------------------------
# Serving-install proxy (cluster_artifact_proxy, default on).
# Holder HTTP is mocked — no network. stream() only; never .get().
# ---------------------------------------------------------------------------

class _StreamClient:
    """httpx stand-in whose stream() is the only legal fetch path."""

    def __init__(self, cm):
        self._cm = cm
        self.calls = []

    def stream(self, method, url, timeout=None, follow_redirects=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "timeout": timeout,
                "follow_redirects": follow_redirects,
            }
        )
        return self._cm

    def get(self, *args, **kwargs):
        raise AssertionError("download_artifact must use stream(), not get()")


class _ChunkStream:
    """Async stream context that yields ``chunks`` at status ``status_code``."""

    def __init__(self, chunks, status_code=200):
        self._chunks = chunks
        self.status_code = status_code
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        self.closed = True

    async def aiter_bytes(self, chunk_size=None):
        for chunk in self._chunks:
            if self.closed:
                return
            yield chunk


def _install_stream(monkeypatch, cm):
    fake = _StreamClient(cm)
    monkeypatch.setattr(artifacts_mod, "get_http_client", lambda: fake)
    return fake


def _remote_row(h, mime="image/png", filename="s.png"):
    upsert_artifact(
        hash=h,
        size=42,
        mime=mime,
        filename=filename,
        created_by_session="s",
        runner_id=HOLDER,
    )


def test_proxy_returns_200_and_does_not_307(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    data = b"proxied-artifact-bytes"
    h = hashlib.sha256(data).hexdigest()
    _remote_row(h, mime="image/png", filename="s.png")
    _make_online_holder(HOLDER_URL)
    fake = _install_stream(monkeypatch, _ChunkStream([data]))

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.content == data
    assert "location" not in resp.headers
    assert fake.calls[0]["method"] == "GET"
    assert fake.calls[0]["timeout"] == 10.0
    assert fake.calls[0]["follow_redirects"] is False
    parsed = urlparse(fake.calls[0]["url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        f"{HOLDER_URL}/api/v1/artifacts/{h}"
    )
    q = parse_qs(parsed.query)
    assert cluster.verify_artifact_token(h, q["exp"][0], q["sig"][0]) is True


def test_proxy_does_not_follow_holder_307(mock_db, client, monkeypatch):
    """A holder 307 whose follow-target would hash-match must still 404.

    The shared client defaults to follow_redirects=True. If the call site
    forgets the override (or uses .get()), httpx would land on the matching
    body and this test would see 200.
    """
    monkeypatch.setattr(settings, "instance_id", "install-A")
    data = b"matching-body-if-followed"
    h = hashlib.sha256(data).hexdigest()
    _remote_row(h)
    _make_online_holder(HOLDER_URL)

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.rstrip("/").endswith("redirect-target"):
            return httpx.Response(200, content=data)
        return httpx.Response(
            307, headers={"location": f"{HOLDER_URL}/redirect-target"}
        )

    mock_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    monkeypatch.setattr(artifacts_mod, "get_http_client", lambda: mock_http)

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 404
    assert "location" not in resp.headers
    assert "/redirect-target" not in seen


def test_proxy_overflow_without_prebuilt_content_is_404(mock_db, client, monkeypatch):
    """Cap is enforced while iterating chunks — not against a pre-built body."""
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = "1" * 64
    _remote_row(h)
    _make_online_holder(HOLDER_URL)

    chunk = b"x" * (1024 * 1024)
    stream = _ChunkStream([])
    stream.iterated = False

    async def _overflow_chunks(chunk_size=None):
        stream.iterated = True
        sent = 0
        while sent <= MAX_UPLOAD_SIZE:
            yield chunk
            sent += len(chunk)

    # Generator yields past the cap without assembling a `content=` body.
    stream.aiter_bytes = _overflow_chunks
    fake = _install_stream(monkeypatch, stream)

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 404
    assert fake.calls, "must call stream(), not get()"
    assert stream.iterated is True
    assert stream.closed is True


def test_proxy_hash_mismatch_is_404(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = hashlib.sha256(b"expected-bytes").hexdigest()
    _remote_row(h)
    _make_online_holder(HOLDER_URL)
    _install_stream(monkeypatch, _ChunkStream([b"not-the-right-bytes"]))

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 404


def test_flag_off_still_307s(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    monkeypatch.setattr(settings, "cluster_artifact_proxy", False)
    h = "2" * 64
    _remote_row(h)
    _make_online_holder(HOLDER_URL)

    def _must_not_fetch(*args, **kwargs):
        raise AssertionError("flag off must 307, not proxy")

    monkeypatch.setattr(artifacts_mod, "get_http_client", _must_not_fetch)

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 307
    _assert_signed_redirect(resp.headers["location"], h)


@pytest.mark.parametrize("filename", ["shot.png", 'café "x".png'])
def test_proxy_headers_match_local_file_response(mock_db, client, monkeypatch, filename):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    data = b"header-compare-bytes:" + filename.encode()
    local = artifact_store.store_bytes(
        data, mime="image/png", filename=filename, runner_id=HOLDER
    )
    local_resp = client.get(f"/api/v1/artifacts/{local['hash']}")
    assert local_resp.status_code == 200

    # Drop the local bytes so the same hash takes the proxy path.
    path = artifact_store.artifact_path(local["hash"])
    assert path is not None
    path.unlink()
    _make_online_holder(HOLDER_URL)
    _install_stream(monkeypatch, _ChunkStream([data]))

    proxy_resp = client.get(
        f"/api/v1/artifacts/{local['hash']}", follow_redirects=False
    )
    assert proxy_resp.status_code == 200
    assert proxy_resp.content == data
    assert proxy_resp.headers["content-type"] == local_resp.headers["content-type"]
    assert (
        proxy_resp.headers["content-disposition"]
        == local_resp.headers["content-disposition"]
    )


def test_proxy_rejects_unsafe_base_url(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = "3" * 64
    _remote_row(h)
    _make_online_holder("http://user:pass@10.0.0.99:8000")

    def _must_not_fetch(*args, **kwargs):
        raise AssertionError("must not fetch an unvalidated base_url")

    monkeypatch.setattr(artifacts_mod, "get_http_client", _must_not_fetch)

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 404


def test_proxy_malformed_ipv6_base_url_is_404(mock_db, client, monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "install-A")
    h = "4" * 64
    _remote_row(h)
    _make_online_holder("http://[")

    def _must_not_fetch(*args, **kwargs):
        raise AssertionError("must not fetch an unvalidated base_url")

    monkeypatch.setattr(artifacts_mod, "get_http_client", _must_not_fetch)

    resp = client.get(f"/api/v1/artifacts/{h}", follow_redirects=False)
    assert resp.status_code == 404
