"""Credential policies and the decrypted-key resolver (``app.api.settings_credentials``).

``/internal/resolve-credential`` returns a *decrypted* API key, so the two
things that matter most are who may call it (the install owner only, never an
API user holding its own Bearer key) and which key the per-user policy lets it
hand out. The policy editors are owner-only too, and refuse settings that would
leave a user with no key behind them.
"""
import pytest

from app.api.auth import hash_api_key
from app.api.settings_crypto import set_encrypted_api_key
from app.core import encryption
from app.db import database

BASE = "/api/v1/settings"
USER_ID = "cred-user"
USER_KEY = "aih_cred_user_key"


@pytest.fixture
def api_user(mock_db):
    database.create_api_user(USER_ID, "Cred User", hash_api_key(USER_KEY))
    return USER_ID


def _seed_fal_policy():
    """A global ``fal_api_key`` policy row (the schema seeds none), written
    through the app's DB layer so it serializes with the app's own writers."""
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO credential_policies (id, policy) VALUES (%s, %s)", ("fal_api_key", "optional"))


def _resolve(client, credential_type="fal_api_key", **params):
    return client.get(f"{BASE}/internal/resolve-credential/{credential_type}", params=params)


# ---------------------------------------------------------------------------
# Authorization: an API user must never read decrypted keys or edit policies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("get", f"{BASE}/internal/resolve-credential/fal_api_key", None),
    ("put", f"{BASE}/credential-policies/fal_api_key", {"policy": "optional"}),
    ("get", f"{BASE}/credential-policies/summary", None),
    ("get", f"{BASE}/users/{USER_ID}/credential-policies", None),
    ("put", f"{BASE}/users/{USER_ID}/credential-policies/fal_api_key", {"policy": "optional"}),
    ("delete", f"{BASE}/users/{USER_ID}/credential-policies/fal_api_key", None),
])
def test_api_user_bearer_cannot_reach_owner_only_credential_routes(client, api_user, method, path, body):
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    kwargs = {"headers": {"Authorization": f"Bearer {USER_KEY}"}}
    if body is not None:
        kwargs["json"] = body
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 403, response.text
    assert "fal-admin-secret" not in response.text


def test_unauthenticated_resolver_is_refused(client):
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    response = _resolve(client)
    assert response.status_code in (401, 403)
    assert "fal-admin-secret" not in response.text


# ---------------------------------------------------------------------------
# Resolution by per-user policy
# ---------------------------------------------------------------------------

def test_unknown_credential_type_is_rejected(authenticated_client):
    assert _resolve(authenticated_client, "aws_root_key").status_code == 400


def test_no_user_defaults_to_user_provided_and_never_falls_back_to_admin(authenticated_client):
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    response = _resolve(authenticated_client)
    assert response.status_code == 404
    assert "User must provide" in response.json()["detail"]
    assert "fal-admin-secret" not in response.text


def test_user_provided_policy_ignores_admin_key(authenticated_client, api_user):
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    database.set_user_credential_policy(api_user, "fal_api_key", "user_provided")
    response = _resolve(authenticated_client, user_id=api_user)
    assert response.status_code == 404
    assert "fal-admin-secret" not in response.text


def test_user_provided_policy_decrypts_the_users_own_key(authenticated_client, api_user):
    database.set_user_credential(api_user, "fal_api_key", encryption.encrypt_value("fal-user-secret"))
    response = _resolve(authenticated_client, user_id=api_user)
    assert response.status_code == 200
    assert response.json() == {"key": "fal-user-secret", "source": "user", "credential_type": "fal_api_key"}


def test_optional_policy_falls_back_to_admin_key(authenticated_client, api_user):
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    database.set_user_credential_policy(api_user, "fal_api_key", "optional")
    response = _resolve(authenticated_client, user_id=api_user)
    assert response.status_code == 200
    assert response.json()["source"] == "admin"
    assert response.json()["key"] == "fal-admin-secret"


def test_optional_policy_prefers_users_key_over_admin(authenticated_client, api_user):
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    database.set_user_credential_policy(api_user, "fal_api_key", "optional")
    database.set_user_credential(api_user, "fal_api_key", encryption.encrypt_value("fal-user-secret"))
    body = _resolve(authenticated_client, user_id=api_user).json()
    assert (body["key"], body["source"]) == ("fal-user-secret", "user")


def test_admin_provided_policy_skips_users_key(authenticated_client, api_user):
    """Under 'admin_provided' a user's own stored key is not consulted at all."""
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    database.set_user_credential_policy(api_user, "fal_api_key", "admin_provided")
    database.set_user_credential(api_user, "fal_api_key", encryption.encrypt_value("fal-user-secret"))
    body = _resolve(authenticated_client, user_id=api_user).json()
    assert (body["key"], body["source"]) == ("fal-admin-secret", "admin")


def test_admin_provided_without_admin_key_is_404_without_user_hint(authenticated_client, api_user):
    database.set_user_credential_policy(api_user, "fal_api_key", "admin_provided")
    response = _resolve(authenticated_client, user_id=api_user)
    assert response.status_code == 404
    assert "User must provide" not in response.json()["detail"]


def test_github_pat_never_falls_back_to_an_admin_key(authenticated_client, api_user):
    database.set_user_credential_policy(api_user, "github_pat", "optional")
    assert _resolve(authenticated_client, "github_pat", user_id=api_user).status_code == 404


def test_undecryptable_user_key_does_not_leak_ciphertext(authenticated_client, api_user, monkeypatch):
    """A stored user key that fails to decrypt resolves to nothing, never to the
    ciphertext itself."""
    stored = encryption.encrypt_value("fal-user-secret")
    database.set_user_credential(api_user, "fal_api_key", stored)

    def boom(_value):
        raise ValueError("wrong key")

    monkeypatch.setattr(encryption, "decrypt_value", boom)
    response = _resolve(authenticated_client, user_id=api_user)
    assert response.status_code == 404
    assert stored not in response.text


def test_encrypted_user_key_is_not_returned_when_storage_is_locked(authenticated_client, api_user, monkeypatch):
    stored = encryption.encrypt_value("fal-user-secret")
    database.set_user_credential(api_user, "fal_api_key", stored)
    monkeypatch.setattr(encryption, "is_encryption_ready", lambda: False)
    response = _resolve(authenticated_client, user_id=api_user)
    assert response.status_code == 404
    assert stored not in response.text


def test_legacy_plaintext_user_key_still_resolves(authenticated_client, api_user):
    database.set_user_credential(api_user, "fal_api_key", "legacy-plain-key")
    body = _resolve(authenticated_client, user_id=api_user).json()
    assert (body["key"], body["source"]) == ("legacy-plain-key", "user")


# ---------------------------------------------------------------------------
# Global policy editor
# ---------------------------------------------------------------------------

def test_policy_listing_is_readable(authenticated_client):
    response = authenticated_client.get(f"{BASE}/credential-policies")
    assert response.status_code == 200
    ids = {p["id"] for p in response.json()["policies"]}
    assert "openai_api_key" in ids


def test_update_policy_rejects_unknown_policy_value(authenticated_client):
    response = authenticated_client.put(f"{BASE}/credential-policies/openai_api_key", json={"policy": "everyone"})
    assert response.status_code == 400


def test_update_policy_rejects_admin_provided_github_pat(authenticated_client):
    response = authenticated_client.put(f"{BASE}/credential-policies/github_pat", json={"policy": "admin_provided"})
    assert response.status_code == 400
    assert "GitHub PAT" in response.json()["detail"]


def test_update_policy_refuses_admin_provided_without_admin_key(authenticated_client):
    _seed_fal_policy()
    response = authenticated_client.put(f"{BASE}/credential-policies/fal_api_key", json={"policy": "admin_provided"})
    assert response.status_code == 400
    assert database.get_credential_policy("fal_api_key")["policy"] == "optional"


def test_update_policy_allows_admin_provided_once_admin_key_exists(authenticated_client):
    _seed_fal_policy()
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    response = authenticated_client.put(f"{BASE}/credential-policies/fal_api_key", json={"policy": "admin_provided"})
    assert response.status_code == 200
    assert database.get_credential_policy("fal_api_key")["policy"] == "admin_provided"
    summary = authenticated_client.get(f"{BASE}/credential-policies/summary").json()["policies"]
    fal = next(p for p in summary if p["id"] == "fal_api_key")
    assert (fal["admin_has_key"], fal["effective_status"]) == (True, "admin_provides")


def test_update_unknown_policy_id_is_404(authenticated_client):
    response = authenticated_client.put(f"{BASE}/credential-policies/nope_key", json={"policy": "optional"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Per-user overrides
# ---------------------------------------------------------------------------

def test_user_policies_for_missing_user_is_404(authenticated_client):
    assert authenticated_client.get(f"{BASE}/users/ghost/credential-policies").status_code == 404
    assert authenticated_client.put(
        f"{BASE}/users/ghost/credential-policies/fal_api_key", json={"policy": "optional"}).status_code == 404
    assert authenticated_client.delete(f"{BASE}/users/ghost/credential-policies/fal_api_key").status_code == 404


def test_user_policy_defaults_then_reflects_override(authenticated_client, api_user):
    before = authenticated_client.get(f"{BASE}/users/{api_user}/credential-policies").json()
    fal = next(p for p in before["policies"] if p["credential_type"] == "fal_api_key")
    assert (fal["policy"], fal["source"]) == ("user_provided", "default")

    assert authenticated_client.put(
        f"{BASE}/users/{api_user}/credential-policies/fal_api_key", json={"policy": "optional"}).status_code == 200
    after = authenticated_client.get(f"{BASE}/users/{api_user}/credential-policies").json()
    fal = next(p for p in after["policies"] if p["credential_type"] == "fal_api_key")
    assert (fal["policy"], fal["source"]) == ("optional", "user")

    reset = authenticated_client.delete(f"{BASE}/users/{api_user}/credential-policies/fal_api_key").json()
    assert reset["deleted"] is True and reset["fallback_policy"] == "user_provided"
    assert database.get_user_credential_policy(api_user, "fal_api_key") is None


@pytest.mark.parametrize("credential_type,policy,fragment", [
    ("fal_api_key", "everyone", "Invalid policy"),
    ("aws_root_key", "optional", "Unknown credential type"),
    ("github_pat", "admin_provided", "GitHub PAT"),
    ("fal_api_key", "admin_provided", "admin has not configured"),
])
def test_user_policy_override_validation(authenticated_client, api_user, credential_type, policy, fragment):
    response = authenticated_client.put(
        f"{BASE}/users/{api_user}/credential-policies/{credential_type}", json={"policy": policy})
    assert response.status_code == 400
    assert fragment in response.json()["detail"]
    assert database.get_user_credential_policy(api_user, credential_type) is None


def test_user_policy_override_admin_provided_with_admin_key(authenticated_client, api_user):
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    response = authenticated_client.put(
        f"{BASE}/users/{api_user}/credential-policies/fal_api_key", json={"policy": "admin_provided"})
    assert response.status_code == 200
    assert database.get_user_credential_policy(api_user, "fal_api_key")["policy"] == "admin_provided"


def test_policy_listing_is_readable_by_an_active_api_user_only(client, api_user):
    """``require_auth`` admits a live API key; deactivating the user closes it."""
    headers = {"Authorization": f"Bearer {USER_KEY}"}
    assert client.get(f"{BASE}/credential-policies", headers=headers).status_code == 200
    database.update_api_user(api_user, is_active=False)
    assert client.get(f"{BASE}/credential-policies", headers=headers).status_code == 401
    assert client.get(f"{BASE}/credential-policies",
                      headers={"Authorization": "Bearer aih_never_issued"}).status_code == 401
