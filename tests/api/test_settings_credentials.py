"""The decrypted-key resolver (``app.api.settings_credentials``).

``/internal/resolve-credential`` returns a *decrypted* API key, so the two
things that matter most are who may call it (the install owner only, never an
API user holding its own Bearer key) and which key the per-user policy lets it
hand out. The policy rows are written straight to the database here: the
policy editor routes had no client and were removed.
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


def _resolve(client, credential_type="fal_api_key", headers=None, **params):
    return client.get(f"{BASE}/internal/resolve-credential/{credential_type}", params=params, headers=headers)


# ---------------------------------------------------------------------------
# Authorization: an API user must never read decrypted keys or edit policies
# ---------------------------------------------------------------------------

def test_api_user_bearer_cannot_reach_the_resolver(client, api_user):
    set_encrypted_api_key("fal_api_key", "fal-admin-secret")
    response = _resolve(client, headers={"Authorization": f"Bearer {USER_KEY}"})
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
