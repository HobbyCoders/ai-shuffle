"""Unit tests for cluster auth primitives and the advertise-URL resolver.

Covers the security-critical surface of cross-install trust:
- ``verify_cluster_secret`` is a constant-time compare that rejects empties.
- ``require_cluster_auth`` gates peer endpoints on a Bearer cluster secret.
- ``mint_artifact_token``/``verify_artifact_token`` sign a cross-install
  artifact-download redirect and reject tampered/expired/garbage inputs.
- ``cluster_headers`` presents the Bearer scheme + install id to peers.
- ``advertise_url`` resolves env override → per-install DB override → host
  guess → none, and the DB override is scoped per install (never leaks across
  installs sharing one database).

The secret is never returned by any helper here — only compared. Everything is
mocked at the secret/registry boundary; no DB, no network.
"""

import hashlib
import hmac
import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.core import cluster


class FakeRequest:
    """Minimal stand-in exposing the ``.headers`` mapping the deps read."""

    def __init__(self, headers: dict):
        self.headers = headers


# ---------------------------------------------------------------------------
# verify_cluster_secret — constant-time compare, empty rejection
# ---------------------------------------------------------------------------

def test_verify_cluster_secret_accepts_exact_match():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        assert cluster.verify_cluster_secret("s3cr3t") is True


def test_verify_cluster_secret_rejects_wrong_and_empty():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        assert cluster.verify_cluster_secret("nope") is False
        assert cluster.verify_cluster_secret("") is False
        assert cluster.verify_cluster_secret(None) is False


# ---------------------------------------------------------------------------
# require_cluster_auth — Bearer-gated peer dependency
# ---------------------------------------------------------------------------

def test_require_cluster_auth_accepts_valid_secret_and_returns_install_id():
    req = FakeRequest({"Authorization": "Bearer s3cr3t", "X-Install-Id": "peer-1"})
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        assert cluster.require_cluster_auth(req) == "peer-1"


def test_require_cluster_auth_returns_empty_install_id_when_header_absent():
    req = FakeRequest({"Authorization": "Bearer s3cr3t"})
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        assert cluster.require_cluster_auth(req) == ""


def test_require_cluster_auth_rejects_missing_and_wrong_secret():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic s3cr3t"}):
            with pytest.raises(HTTPException) as exc:
                cluster.require_cluster_auth(FakeRequest(headers))
            assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# mint/verify_artifact_token — signed cross-install download redirect
# ---------------------------------------------------------------------------

def test_artifact_token_roundtrip():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        h = "a" * 64
        exp, sig = cluster.mint_artifact_token(h)
        assert cluster.verify_artifact_token(h, str(exp), sig) is True


def test_artifact_token_rejects_wrong_hash():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        exp, sig = cluster.mint_artifact_token("a" * 64)
        assert cluster.verify_artifact_token("b" * 64, str(exp), sig) is False


def test_artifact_token_rejects_tampered_signature():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        h = "a" * 64
        exp, sig = cluster.mint_artifact_token(h)
        flipped = ("f" if sig[0] != "f" else "0") + sig[1:]
        assert cluster.verify_artifact_token(h, str(exp), flipped) is False


def test_artifact_token_rejects_non_digit_exp():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        h = "a" * 64
        _exp, sig = cluster.mint_artifact_token(h)
        assert cluster.verify_artifact_token(h, "not-a-number", sig) is False


def test_artifact_token_rejects_expired_exp():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        h = "a" * 64
        past = int(time.time()) - 10
        sig = hmac.new(
            b"s3cr3t",
            f"artifact-download:{h}:{past}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert cluster.verify_artifact_token(h, str(past), sig) is False


def test_artifact_token_verify_never_raises_on_garbage():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"):
        h = "a" * 64
        assert cluster.verify_artifact_token(h, None, None) is False
        assert cluster.verify_artifact_token(h, "", "") is False
        assert cluster.verify_artifact_token(h, "123", None) is False
        assert cluster.verify_artifact_token(h, None, "deadbeef") is False
        assert cluster.verify_artifact_token(h, 12345, "deadbeef") is False


# ---------------------------------------------------------------------------
# cluster_headers — Bearer scheme + install identity
# ---------------------------------------------------------------------------

def test_cluster_headers_use_bearer_scheme_and_install_id():
    with patch.object(cluster, "get_cluster_secret", return_value="s3cr3t"), \
         patch.object(cluster, "settings") as mock_settings:
        mock_settings.effective_instance_id = "me-install"
        headers = cluster.cluster_headers()
    assert headers["Authorization"] == "Bearer s3cr3t"
    assert headers["X-Install-Id"] == "me-install"


# ---------------------------------------------------------------------------
# advertise_url — env → DB override → host guess → none
# ---------------------------------------------------------------------------

def test_advertise_url_prefers_env_override():
    with patch.object(cluster, "settings") as mock_settings, \
         patch.object(cluster.database, "get_system_setting", return_value="http://ignored"):
        mock_settings.cluster_advertise_url = "https://env.example/"
        url, source = cluster.advertise_url()
    assert url == "https://env.example"  # trailing slash stripped
    assert source == "config"


def test_advertise_url_uses_db_override_when_no_env():
    with patch.object(cluster, "settings") as mock_settings, \
         patch.object(cluster.database, "get_system_setting", return_value="http://admin.example/"):
        mock_settings.cluster_advertise_url = ""
        url, source = cluster.advertise_url()
    assert url == "http://admin.example"
    assert source == "config"


def test_advertise_url_prefers_primary_lan_ip():
    # The LAN IP is routable from peers and sidesteps the unresolvable-hostname
    # trap, so it wins over the host-name guess when available.
    with patch.object(cluster, "settings") as mock_settings, \
         patch.object(cluster.database, "get_system_setting", return_value=None), \
         patch.object(cluster, "_primary_lan_ip", return_value="10.0.0.135"), \
         patch.object(cluster.socket, "getfqdn", return_value="worker.lan"):
        mock_settings.cluster_advertise_url = ""
        mock_settings.port = 1455
        url, source = cluster.advertise_url()
    assert url == "http://10.0.0.135:1455"
    assert source == "guess"


def test_advertise_url_falls_back_to_host_guess():
    # When no usable LAN IP is found, fall back to the host name.
    with patch.object(cluster, "settings") as mock_settings, \
         patch.object(cluster.database, "get_system_setting", return_value=None), \
         patch.object(cluster, "_primary_lan_ip", return_value=None), \
         patch.object(cluster.socket, "getfqdn", return_value="worker.lan"):
        mock_settings.cluster_advertise_url = ""
        mock_settings.port = 1455
        url, source = cluster.advertise_url()
    assert url == "http://worker.lan:1455"
    assert source == "guess"


def test_advertise_url_none_when_host_is_localhost():
    with patch.object(cluster, "settings") as mock_settings, \
         patch.object(cluster.database, "get_system_setting", return_value=None), \
         patch.object(cluster, "_primary_lan_ip", return_value=None), \
         patch.object(cluster.socket, "getfqdn", return_value="localhost"):
        mock_settings.cluster_advertise_url = ""
        url, source = cluster.advertise_url()
    assert url is None
    assert source == "none"


# ---------------------------------------------------------------------------
# advertise override is scoped PER INSTALL — the regression this guards against:
# an address entered on one install showing up as every install's address
# because the override lived under a single global key in the shared DB.
# ---------------------------------------------------------------------------

def _fake_db(store, install_row=None):
    """Return (get, set, delete, get_install) closures over an in-memory dict."""
    return (
        lambda k: store.get(k),
        lambda k, v: store.__setitem__(k, v),
        lambda k: store.pop(k, None) is not None,
        lambda _iid: install_row,
    )


def _patch_db(store, install_row=None):
    get, setv, dele, get_install = _fake_db(store, install_row)
    return patch.multiple(
        cluster.database,
        get_system_setting=get,
        set_system_setting=setv,
        delete_system_setting=dele,
        get_install=get_install,
    )


def test_override_is_scoped_per_install():
    # DesktopWorker stored its override; Unraid (a different install) must not see it.
    store = {"cluster_advertise_url:DESKTOPWORKER": "http://10.0.0.135:8000"}
    with patch.object(cluster, "settings") as s, _patch_db(store):
        s.cluster_advertise_url = ""
        s.effective_instance_id = "UNRAID"
        assert cluster._get_advertise_override() is None
        s.effective_instance_id = "DESKTOPWORKER"
        assert cluster._get_advertise_override() == "http://10.0.0.135:8000"


def test_set_override_writes_per_install_key():
    store: dict = {}
    with patch.object(cluster, "settings") as s, _patch_db(store), \
         patch.object(cluster, "register_self"):
        s.cluster_advertise_url = ""
        s.effective_instance_id = "UNRAID"
        cluster.set_advertise_url_override("http://10.0.0.200:8000/")  # slash stripped
    assert store == {"cluster_advertise_url:UNRAID": "http://10.0.0.200:8000"}


def test_legacy_global_override_claimed_only_by_publishing_owner():
    # The install whose registry base_url matches the legacy global value claims
    # it into its per-install key and removes the leaking global key.
    store = {"cluster_advertise_url": "http://10.0.0.135:8000"}
    owner_row = {"base_url": "http://10.0.0.135:8000"}
    with patch.object(cluster, "settings") as s, _patch_db(store, owner_row):
        s.cluster_advertise_url = ""
        s.effective_instance_id = "DESKTOPWORKER"
        assert cluster._get_advertise_override() == "http://10.0.0.135:8000"
    assert store.get("cluster_advertise_url:DESKTOPWORKER") == "http://10.0.0.135:8000"
    assert "cluster_advertise_url" not in store  # global key removed


def test_legacy_global_override_ignored_by_non_owner():
    # A peer that did not publish the legacy value leaves it untouched (so the
    # real owner can still claim it) and reports no override of its own.
    store = {"cluster_advertise_url": "http://10.0.0.135:8000"}
    other_row = {"base_url": "http://10.0.0.200:8000"}
    with patch.object(cluster, "settings") as s, _patch_db(store, other_row):
        s.cluster_advertise_url = ""
        s.effective_instance_id = "UNRAID"
        assert cluster._get_advertise_override() is None
    assert store == {"cluster_advertise_url": "http://10.0.0.135:8000"}


def test_explicitly_cleared_override_does_not_fall_back_to_legacy():
    # An install that cleared its override ("") must stay cleared, not inherit
    # the stale global value.
    store = {
        "cluster_advertise_url:UNRAID": "",
        "cluster_advertise_url": "http://10.0.0.135:8000",
    }
    with patch.object(cluster, "settings") as s, _patch_db(store, {"base_url": "http://10.0.0.135:8000"}):
        s.cluster_advertise_url = ""
        s.effective_instance_id = "UNRAID"
        assert cluster._get_advertise_override() is None
    assert store.get("cluster_advertise_url") == "http://10.0.0.135:8000"
