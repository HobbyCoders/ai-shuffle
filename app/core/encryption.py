"""
Encryption utilities for securing sensitive data at rest.

Uses Fernet symmetric encryption with keys derived from the admin password.
The encryption key is never stored - it's derived at runtime from the password.

Alternatively, set ADMIN_PASSWORD env var to auto-derive the key on startup.
"""

import base64
import os
import secrets
import logging
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Global cache for the encryption key (only in memory, never persisted)
_encryption_key: Optional[bytes] = None
_key_salt: Optional[bytes] = None


def init_encryption_from_env(db_module) -> bool:
    """
    Initialize encryption key from ADMIN_PASSWORD environment variable.
    This allows headless operation without requiring admin login.

    Call this at app startup to enable API key decryption.

    Returns:
        True if encryption was initialized, False if env var not set
    """
    admin_password = os.environ.get("ADMIN_PASSWORD")
    # Also check settings (which loads from .env file)
    if not admin_password:
        try:
            from app.core.config import settings
            admin_password = settings.admin_password
        except Exception:
            pass
    if not admin_password:
        return False

    set_encryption_key(admin_password, db_module)
    logger.info("Encryption key initialized from ADMIN_PASSWORD environment variable")
    return True


def get_or_create_salt(db_module) -> bytes:
    """
    Get the salt from database or create a new one.
    The salt is stored in the database but is useless without the password.
    """
    global _key_salt

    if _key_salt:
        return _key_salt

    salt_b64 = db_module.get_system_setting("encryption_salt")
    if salt_b64:
        _key_salt = base64.b64decode(salt_b64)
    else:
        # Generate new salt (only happens once, on first encryption setup)
        _key_salt = secrets.token_bytes(32)
        db_module.set_system_setting("encryption_salt", base64.b64encode(_key_salt).decode())
        logger.info("Generated new encryption salt")

    return _key_salt


def derive_key_from_password(password: str, salt: bytes) -> bytes:
    """
    Derive a Fernet-compatible encryption key from a password using PBKDF2.

    Args:
        password: The admin password
        salt: Random salt (stored in DB)

    Returns:
        32-byte key suitable for Fernet encryption
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600000,  # OWASP recommended minimum for PBKDF2-SHA256
    )
    key = kdf.derive(password.encode())
    # Fernet requires base64-encoded 32-byte key
    return base64.urlsafe_b64encode(key)


def set_encryption_key(password: str, db_module) -> None:
    """
    Set the encryption key in memory by deriving it from the admin password.
    Called when admin logs in successfully.
    """
    global _encryption_key
    salt = get_or_create_salt(db_module)
    _encryption_key = derive_key_from_password(password, salt)
    logger.info("Encryption key loaded into memory")


def clear_encryption_key() -> None:
    """Clear the encryption key from memory (e.g., on logout or shutdown)."""
    global _encryption_key
    _encryption_key = None
    logger.info("Encryption key cleared from memory")


def is_encryption_ready() -> bool:
    """Check if encryption key is available in memory."""
    return _encryption_key is not None


def encrypt_value(plaintext: str) -> str:
    """
    Encrypt a string value.

    Args:
        plaintext: The value to encrypt

    Returns:
        Base64-encoded encrypted value with 'enc:' prefix

    Raises:
        RuntimeError: If encryption key is not loaded
    """
    if not _encryption_key:
        raise RuntimeError("Encryption key not loaded. Admin must be logged in.")

    f = Fernet(_encryption_key)
    encrypted = f.encrypt(plaintext.encode())
    # Prefix with 'enc:' to identify encrypted values
    return f"enc:{base64.b64encode(encrypted).decode()}"


def decrypt_value(encrypted: str) -> str:
    """
    Decrypt a string value.

    Args:
        encrypted: The encrypted value (with 'enc:' prefix)

    Returns:
        Decrypted plaintext

    Raises:
        RuntimeError: If encryption key is not loaded
        ValueError: If decryption fails (wrong key or corrupted data)
    """
    if not _encryption_key:
        raise RuntimeError("Encryption key not loaded. Admin must be logged in.")

    if not encrypted.startswith("enc:"):
        # Not encrypted, return as-is (for migration compatibility)
        return encrypted

    try:
        encrypted_bytes = base64.b64decode(encrypted[4:])  # Remove 'enc:' prefix
        f = Fernet(_encryption_key)
        decrypted = f.decrypt(encrypted_bytes)
        return decrypted.decode()
    except InvalidToken:
        raise ValueError("Decryption failed - invalid key or corrupted data")


def is_encrypted(value: str) -> bool:
    """Check if a value is encrypted (has 'enc:' prefix)."""
    return value.startswith("enc:") if value else False


# ---------------------------------------------------------------------------
# Dynamic secret discovery + re-encryption
# ---------------------------------------------------------------------------
#
# The at-rest encryption key is derived from the admin password, so a password
# change requires re-encrypting EVERY ``enc:``-prefixed value under a key derived
# from the new password, or those values become permanently undecryptable.
#
# The app Fernet-encrypts values in FOUR storage locations (all sharing the same
# admin-password-derived key and ``enc:`` prefix):
#   1. ``system_settings.value``           — e.g. fal_api_key, anthropic_api_key
#      (plus any orphaned/legacy keys such as cloud_install_api_key).
#   2. ``automations.triggers`` / ``trigger_config`` (JSONB) — webhook secrets.
#   3. ``connectors.config`` (JSONB)       — bot tokens / access tokens.
#   4. ``api_user_credentials.encrypted_value``.
#
# Rather than a hardcoded name list (which historically missed 1/2/3/4 entirely
# and bricked them on every password change), we DISCOVER encrypted values
# dynamically by scanning every relevant row for the ``enc:`` prefix.


class ReencryptPlanItem:
    """One value to re-encrypt, with enough context to write it back.

    ``kind`` is one of: ``"system_setting"``, ``"automation"``, ``"connector"``,
    ``"credential"``. ``ref`` identifies the row (setting key, automation id,
    connector id, or (api_user_id, credential_type) tuple). ``label`` is a
    human/log-safe identifier that NEVER contains the secret value. For JSONB
    kinds the plan carries the FULL re-encrypted object so the writer can persist
    it wholesale (``new_obj``); scalar kinds carry ``new_value``.
    """

    __slots__ = ("kind", "ref", "label", "new_value", "new_obj")

    def __init__(self, kind, ref, label, new_value=None, new_obj=None):
        self.kind = kind
        self.ref = ref
        self.label = label
        self.new_value = new_value
        self.new_obj = new_obj


def _reencrypt_json(obj, old_fernet, new_fernet, failures, label):
    """Recursively walk a JSON-ish structure, re-encrypting ``enc:`` strings.

    Returns ``(new_obj, changed)``. Any ``enc:``-prefixed string is decrypted
    with ``old_fernet`` and re-encrypted with ``new_fernet``. Decrypt failures
    are appended to ``failures`` (as ``(label, reason)``) and the offending value
    is left unchanged; the caller treats a non-empty ``failures`` as fatal.
    The secret value itself is never logged or stored in ``failures``.
    """
    changed = False
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nv, c = _reencrypt_json(v, old_fernet, new_fernet, failures, f"{label}.{k}")
            out[k] = nv
            changed = changed or c
        return out, changed
    if isinstance(obj, list):
        out = []
        for i, v in enumerate(obj):
            nv, c = _reencrypt_json(v, old_fernet, new_fernet, failures, f"{label}[{i}]")
            out.append(nv)
            changed = changed or c
        return out, changed
    if isinstance(obj, str) and is_encrypted(obj):
        try:
            plaintext = old_fernet.decrypt(base64.b64decode(obj[4:])).decode()
        except Exception as e:  # noqa: BLE001 — reason kept, value never logged
            failures.append((label, f"decrypt failed: {type(e).__name__}"))
            return obj, False
        reenc = new_fernet.encrypt(plaintext.encode())
        return f"enc:{base64.b64encode(reenc).decode()}", True
    return obj, False


def build_reencryption_plan(old_key: bytes, new_key: bytes, db_module):
    """Discover every encrypted value and build a re-encryption plan.

    FAIL-CLOSED: decrypts every discovered ``enc:`` value with ``old_key`` up
    front. Returns ``(plan, failures)`` where ``plan`` is a list of
    :class:`ReencryptPlanItem` and ``failures`` is a list of
    ``(label, reason)`` for values the old key could NOT decrypt. Callers MUST
    refuse to write anything when ``failures`` is non-empty (unless an operator
    has explicitly opted to skip undecryptable values).

    Discovery queries (all via existing db-module accessors):
      * ``get_all_system_settings()``  -> every row whose value ``is_encrypted``
        (skips ``encryption_salt`` and any plaintext).
      * ``get_all_automations()``      -> recursive walk of ``triggers`` +
        ``trigger_config``.
      * ``get_all_connectors()``       -> recursive walk of ``config``.
      * ``get_all_api_users()`` x ``get_all_user_credentials()`` -> every
        credential whose ``encrypted_value`` ``is_encrypted``.

    Nothing here mutates the database or the in-memory key.
    """
    old_fernet = Fernet(old_key)
    new_fernet = Fernet(new_key)

    plan: list = []
    failures: list = []

    # (1) system_settings — every encrypted value (fal/anthropic/orphans/future).
    try:
        all_settings = db_module.get_all_system_settings() or {}
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to enumerate system_settings for re-encryption: %s", e)
        all_settings = {}
    for key, value in all_settings.items():
        if key == "encryption_salt":
            continue
        if not (isinstance(value, str) and is_encrypted(value)):
            continue
        try:
            plaintext = old_fernet.decrypt(base64.b64decode(value[4:])).decode()
        except Exception as e:  # noqa: BLE001
            failures.append((f"system_settings:{key}", f"decrypt failed: {type(e).__name__}"))
            continue
        reenc = new_fernet.encrypt(plaintext.encode())
        plan.append(ReencryptPlanItem(
            "system_setting", key, f"system_settings:{key}",
            new_value=f"enc:{base64.b64encode(reenc).decode()}",
        ))

    # (2) automations — recursive walk of triggers + trigger_config JSONB.
    try:
        automations = db_module.get_all_automations() or []
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to enumerate automations for re-encryption: %s", e)
        automations = []
    for a in automations:
        aid = a.get("id")
        new_triggers, ch1 = _reencrypt_json(
            a.get("triggers"), old_fernet, new_fernet, failures, f"automation:{aid}.triggers")
        new_tc, ch2 = _reencrypt_json(
            a.get("trigger_config"), old_fernet, new_fernet, failures, f"automation:{aid}.trigger_config")
        if ch1 or ch2:
            plan.append(ReencryptPlanItem(
                "automation", aid, f"automations:{aid}",
                new_obj={"triggers": new_triggers, "trigger_config": new_tc},
            ))

    # (3) connectors — recursive walk of config JSONB.
    try:
        connectors = db_module.get_all_connectors() or []
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to enumerate connectors for re-encryption: %s", e)
        connectors = []
    for c in connectors:
        cid = c.get("id")
        new_cfg, changed = _reencrypt_json(
            c.get("config"), old_fernet, new_fernet, failures, f"connector:{cid}.config")
        if changed:
            plan.append(ReencryptPlanItem(
                "connector", cid, f"connectors:{cid}", new_obj={"config": new_cfg},
            ))

    # (4) api_user_credentials — every encrypted_value across all users.
    try:
        users = db_module.get_all_api_users() or []
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to enumerate api_users for re-encryption: %s", e)
        users = []
    for u in users:
        uid = u.get("id")
        try:
            creds = db_module.get_all_user_credentials(uid) or []
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to enumerate credentials for user %s: %s", uid, e)
            continue
        for cred in creds:
            value = cred.get("encrypted_value")
            ctype = cred.get("credential_type")
            if not (isinstance(value, str) and is_encrypted(value)):
                continue
            label = f"api_user_credentials:{uid}/{ctype}"
            try:
                plaintext = old_fernet.decrypt(base64.b64decode(value[4:])).decode()
            except Exception as e:  # noqa: BLE001
                failures.append((label, f"decrypt failed: {type(e).__name__}"))
                continue
            reenc = new_fernet.encrypt(plaintext.encode())
            plan.append(ReencryptPlanItem(
                "credential", (uid, ctype), label,
                new_value=f"enc:{base64.b64encode(reenc).decode()}",
            ))

    return plan, failures


def apply_reencryption_plan(plan, db_module) -> None:
    """Persist a re-encryption plan via the existing db-module accessors.

    Each item is written with the same domain accessor the app uses for that
    location, so JSONB re-serialization and any invariants are handled by the
    normal write path. Mirrors the auto-committing shape the online
    ``change-password`` flow already relied on; the all-or-nothing guarantee
    comes from decrypting everything BEFORE this is called (see
    :func:`build_reencryption_plan`).
    """
    for item in plan:
        if item.kind == "system_setting":
            db_module.set_system_setting(item.ref, item.new_value)
        elif item.kind == "automation":
            db_module.update_automation(
                item.ref,
                triggers=item.new_obj["triggers"],
                trigger_config=item.new_obj["trigger_config"],
            )
        elif item.kind == "connector":
            db_module.update_connector(item.ref, config=item.new_obj["config"])
        elif item.kind == "credential":
            uid, ctype = item.ref
            db_module.set_user_credential(uid, ctype, item.new_value)
        else:  # pragma: no cover — defensive
            raise ValueError(f"Unknown re-encryption plan item kind: {item.kind}")
        logger.debug("Re-encrypted %s", item.label)


def re_encrypt_all_secrets(old_password: str, new_password: str, db_module) -> bool:
    """
    Re-encrypt ALL at-rest secrets under a new admin password.
    Called when the admin changes their password.

    Discovers every ``enc:``-prefixed value across all four storage locations
    (system_settings, automations triggers/trigger_config, connectors config,
    api_user_credentials) — see :func:`build_reencryption_plan` — rather than a
    hardcoded name list. FAILS CLOSED: if ANY discovered value cannot be
    decrypted with the old key, every failure is logged (by table+id, never the
    value) and the function returns ``False`` WITHOUT writing anything, leaving
    the in-memory key untouched.

    Args:
        old_password: The current admin password
        new_password: The new admin password
        db_module: Database module for reading/writing secrets

    Returns:
        True if successful, False otherwise
    """
    global _encryption_key, _key_salt

    salt = get_or_create_salt(db_module)
    old_key = derive_key_from_password(old_password, salt)
    new_key = derive_key_from_password(new_password, salt)

    plan, failures = build_reencryption_plan(old_key, new_key, db_module)

    if failures:
        for label, reason in failures:
            logger.error("Cannot re-encrypt %s: %s", label, reason)
        logger.error(
            "Refusing to re-encrypt: %d value(s) could not be decrypted with the "
            "old password. No changes written.",
            len(failures),
        )
        return False

    apply_reencryption_plan(plan, db_module)

    # Update in-memory key only after every write has succeeded.
    _encryption_key = new_key

    logger.info(
        "Successfully re-encrypted all secrets with new password (%d value(s))",
        len(plan),
    )
    return True


def encrypt_existing_plaintext_keys(db_module) -> int:
    """
    Encrypt any existing plaintext API keys in the database.
    Called during migration or when encryption is first enabled.

    NOTE: this is a DIFFERENT concern from :func:`re_encrypt_all_secrets`. It is
    an opportunistic one-time migration of legacy PLAINTEXT ``system_settings``
    keys to encrypted form, and intentionally stays scoped to the fixed list
    below — it does NOT walk automations/connectors/credentials JSONB.

    Returns:
        Number of keys encrypted
    """
    if not _encryption_key:
        raise RuntimeError("Encryption key not loaded. Admin must be logged in.")

    sensitive_settings = [
        "openai_api_key",
        "gemini_api_key",
        "image_api_key",
        "video_api_key",
        "anthropic_api_key",
    ]

    count = 0
    for setting_name in sensitive_settings:
        value = db_module.get_system_setting(setting_name)
        if value and not is_encrypted(value):
            # This is a plaintext key, encrypt it
            encrypted = encrypt_value(value)
            db_module.set_system_setting(setting_name, encrypted)
            logger.debug(f"Encrypted existing plaintext key: {setting_name}")
            count += 1

    return count
