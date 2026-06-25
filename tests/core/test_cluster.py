"""Unit tests for cluster auth primitives and the advertise-URL resolver.

Covers the security-critical surface of cross-install trust:
- ``verify_cluster_secret`` is a constant-time compare that rejects empties.
- ``require_cluster_auth`` gates peer endpoints on a Bearer cluster secret.
- ``cluster_headers`` presents the Bearer scheme + install id to peers.
- ``advertise_url`` resolves env override → DB override → host guess → none.

The secret is never returned by any helper here — only compared. Everything is
mocked at the secret/registry boundary; no DB, no network.
"""

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


def test_advertise_url_falls_back_to_host_guess():
    with patch.object(cluster, "settings") as mock_settings, \
         patch.object(cluster.database, "get_system_setting", return_value=None), \
         patch.object(cluster.socket, "getfqdn", return_value="worker.lan"):
        mock_settings.cluster_advertise_url = ""
        mock_settings.port = 1455
        url, source = cluster.advertise_url()
    assert url == "http://worker.lan:1455"
    assert source == "guess"


def test_advertise_url_none_when_host_is_localhost():
    with patch.object(cluster, "settings") as mock_settings, \
         patch.object(cluster.database, "get_system_setting", return_value=None), \
         patch.object(cluster.socket, "getfqdn", return_value="localhost"):
        mock_settings.cluster_advertise_url = ""
        url, source = cluster.advertise_url()
    assert url is None
    assert source == "none"
