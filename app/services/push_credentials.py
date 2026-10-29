"""Write-only, encrypted FCM sender configuration. Never reads an env file."""
import json

from app.core import encryption
from app.db import settings as settings_db

SETTING_KEY = "fcm_service_account"


def validate_account(raw: str) -> dict:
    # Validate locally, without contacting Google or echoing secret material.
    if len(raw) > 32768:
        raise ValueError("Service-account JSON is too large")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("type") != "service_account":
            raise ValueError
        if not all(isinstance(value.get(k), str) and value[k].strip()
                   for k in ("project_id", "private_key", "client_email", "token_uri")):
            raise ValueError
        # Do not permit a pasted document to redirect OAuth credentials.
        if value["token_uri"] != "https://oauth2.googleapis.com/token":
            raise ValueError
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        if not isinstance(load_pem_private_key(value["private_key"].encode(), password=None), RSAPrivateKey):
            raise ValueError
    except Exception:
        raise ValueError("Enter a Firebase sender service-account JSON with a valid RSA private key") from None
    return value


def save_account(raw: str) -> None:
    value = validate_account(raw)
    encrypted = encryption.encrypt_value(json.dumps(value))
    if not encryption.is_encrypted(encrypted):
        raise RuntimeError("Secret storage is locked")
    settings_db.set_system_setting(SETTING_KEY, encrypted)


def load_account() -> tuple[str | None, dict | None]:
    encrypted = settings_db.get_system_setting(SETTING_KEY)
    if not encrypted:
        return None, None
    if not encryption.is_encrypted(encrypted):
        raise RuntimeError("FCM credential is not encrypted")
    return encrypted, json.loads(encryption.decrypt_value(encrypted))
