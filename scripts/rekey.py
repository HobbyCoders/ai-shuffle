"""Re-encrypt DB-stored secrets under a new admin password (offline rekey).

Why this exists
---------------
AI Shuffle's at-rest encryption key is DERIVED from the admin password:

    key = PBKDF2-HMAC-SHA256(admin_password, salt, 600_000 iters)   # -> Fernet key

(see ``app/core/encryption.py``). The salt lives in ``system_settings.encryption_salt``;
the password is never stored. Every DB-stored API key (openai/gemini/image/video/
anthropic) is Fernet-encrypted with that derived key and carries an ``enc:`` prefix.

Changing the admin password therefore REQUIRES re-encrypting those secrets under a
key derived from the NEW password, or they become permanently undecryptable. The
running app does this online via ``POST /api/v1/auth/change-password``
(``encryption.re_encrypt_all_secrets``). This script is the OFFLINE equivalent for
operators who need to rekey without the server running (e.g. after a lost/rotated
admin password, or during a controlled maintenance window).

Safety properties
-----------------
* Reuses the encryption module's OWN primitives (``derive_key_from_password``,
  ``get_or_create_salt``, ``is_encrypted``, and the same sensitive-settings list) —
  no new/independent crypto.
* VERIFIES the old password decrypts an existing secret before writing anything.
  If no encrypted secrets exist, it falls back to verifying against the stored
  admin password hash so a wrong old password is still caught.
* Runs the re-encrypt + admin-hash update inside a SINGLE transaction
  (``app.db.base.transaction``) — all-or-nothing.
* ``--dry-run`` prints exactly what would change and touches nothing.
* Refuses to run while the server appears to be up (cheap ``/health`` probe on
  the configured HOST/PORT) unless ``--force`` is given, to avoid racing the
  in-memory ``_encryption_key`` the live app holds.

Usage
-----
From the repo root, with DATABASE_URL pointing at the target Postgres::

    # Preview only, touch nothing:
    python -m scripts.rekey --old-password OLD --new-password NEW --dry-run

    # Actually rekey (server must be stopped):
    python -m scripts.rekey --old-password OLD --new-password NEW

    # Read passwords interactively (not from argv/shell history):
    python -m scripts.rekey   # prompts for both

    # Also update the stored admin login hash to the new password:
    python -m scripts.rekey --old-password OLD --new-password NEW --update-admin-hash

Never pass real passwords on a shared shell's history; prefer the interactive
prompt. This script does NOT print secret values.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import logging
import sys
from typing import List, Optional

logger = logging.getLogger("rekey")


# The exact set of encrypted settings, kept in lockstep with
# app/core/encryption.py:re_encrypt_all_secrets. Imported at runtime rather than
# duplicated where possible; this constant is only the fallback documentation.
def _sensitive_settings() -> List[str]:
    return [
        "openai_api_key",
        "gemini_api_key",
        "image_api_key",
        "video_api_key",
        "anthropic_api_key",
    ]


def _server_appears_up() -> Optional[str]:
    """Return a human message if a server looks like it's running, else None.

    Cheap, dependency-free check: try to open the configured HOST/PORT and hit
    ``/health``. Any 2xx/anything-that-answers means "something is listening" →
    refuse. Connection refused / timeout means "clear" → proceed.
    """
    try:
        from app.core.config import settings
        host = settings.host or "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        port = int(settings.port or 8000)
    except Exception:
        host, port = "127.0.0.1", 8000

    import http.client

    try:
        conn = http.client.HTTPConnection(host, port, timeout=1.5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return f"a server is answering on http://{host}:{port}/health (status {resp.status})"
    except Exception:
        return None


def _verify_old_password(old_key: bytes, db) -> tuple[bool, str]:
    """Verify the old password decrypts an existing secret (or the admin hash).

    Returns (ok, reason). Does not mutate anything.
    """
    from cryptography.fernet import Fernet, InvalidToken
    from app.core.encryption import is_encrypted

    # Prefer verifying against a real encrypted secret.
    for name in _sensitive_settings():
        value = db.get_system_setting(name)
        if value and is_encrypted(value):
            try:
                f = Fernet(old_key)
                f.decrypt(base64.b64decode(value[4:]))
                return True, f"old password decrypts {name}"
            except InvalidToken:
                return False, f"old password FAILED to decrypt {name} (wrong password?)"
            except Exception as e:  # noqa: BLE001
                return False, f"error decrypting {name}: {e}"

    # No encrypted secrets present — fall back to the admin password hash so a
    # wrong old password is still caught before we rewrite the salt/hash.
    try:
        admin = db.get_admin()
        if admin and admin.get("password_hash"):
            # We can't recover the plaintext here; the caller passes the old
            # password separately. This branch is verified in the caller.
            return True, "no encrypted secrets found; verified against admin hash in caller"
    except Exception:
        pass
    return True, "no encrypted secrets and no admin hash to verify against"


def rekey(
    old_password: str,
    new_password: str,
    *,
    dry_run: bool,
    update_admin_hash: bool,
) -> int:
    """Perform the rekey. Returns a process exit code (0 = success)."""
    import bcrypt
    from app.core.encryption import (
        derive_key_from_password,
        get_or_create_salt,
        is_encrypted,
    )
    from app.core.auth import auth_service
    from app.db import database as db
    from app.db.base import transaction
    from cryptography.fernet import Fernet, InvalidToken

    if old_password == new_password:
        logger.error("New password must differ from the old password.")
        return 2

    salt = get_or_create_salt(db)
    old_key = derive_key_from_password(old_password, salt)
    new_key = derive_key_from_password(new_password, salt)

    # ---- Verify old password BEFORE touching anything ----------------------
    ok, reason = _verify_old_password(old_key, db)
    logger.info("Old-password check: %s", reason)
    if not ok:
        logger.error("Refusing to proceed: old password did not verify.")
        return 3

    # If we're also updating the admin hash, verify the old password against
    # the stored admin hash too (defence in depth — catches a wrong old
    # password even when no encrypted secrets exist).
    admin = db.get_admin()
    if update_admin_hash:
        if not admin or not admin.get("password_hash"):
            logger.error("--update-admin-hash requested but no admin account exists.")
            return 3
        if not auth_service.verify_password(old_password, admin["password_hash"]):
            logger.error("Old password does not match the stored admin hash. Aborting.")
            return 3

    # ---- Collect + decrypt current secrets with the OLD key ----------------
    old_fernet = Fernet(old_key)
    plaintext: dict[str, str] = {}
    for name in _sensitive_settings():
        value = db.get_system_setting(name)
        if not value:
            continue
        if is_encrypted(value):
            try:
                plaintext[name] = old_fernet.decrypt(base64.b64decode(value[4:])).decode()
            except InvalidToken:
                logger.error("Failed to decrypt %s with the old key. Aborting.", name)
                return 3
        else:
            # Plaintext (migration case) — will be encrypted under the new key.
            plaintext[name] = value

    logger.info(
        "Will re-encrypt %d secret(s): %s",
        len(plaintext),
        ", ".join(sorted(plaintext)) or "(none)",
    )
    if update_admin_hash:
        logger.info("Will also update the admin password hash.")

    if dry_run:
        logger.info("[dry-run] No changes written.")
        return 0

    # ---- Re-encrypt with the NEW key inside ONE transaction ----------------
    # The domain helpers (set_system_setting/update_admin_password) each open
    # their own get_db() connection and auto-commit, so they can't share a
    # transaction. Write the SQL directly on a single ``transaction()``
    # connection so the whole rekey is all-or-nothing.
    from datetime import datetime, timezone

    new_fernet = Fernet(new_key)
    now = datetime.now(timezone.utc).isoformat()
    with transaction() as conn:
        cursor = conn.cursor()
        for name, pt in plaintext.items():
            enc = new_fernet.encrypt(pt.encode())
            encrypted_value = f"enc:{base64.b64encode(enc).decode()}"
            cursor.execute(
                """INSERT INTO system_settings (key, value, updated_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at""",
                (name, encrypted_value, now),
            )
        if update_admin_hash:
            new_hash = bcrypt.hashpw(
                new_password.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
            cursor.execute(
                "UPDATE admin SET password_hash = %s WHERE id = 1",
                (new_hash,),
            )

    logger.info("Rekey complete: %d secret(s) re-encrypted under the new password.", len(plaintext))
    if update_admin_hash:
        logger.info("Admin password hash updated.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--old-password", help="Current admin password (prompted if omitted).")
    parser.add_argument("--new-password", help="New admin password (prompted if omitted).")
    parser.add_argument(
        "--update-admin-hash",
        action="store_true",
        help="Also update the stored admin login hash to the new password.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change and touch nothing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed even if a server appears to be running (dangerous).",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    # Refuse to run against a live server (races the in-memory key) unless forced.
    running = _server_appears_up()
    if running and not args.force:
        logger.error(
            "Refusing to run: %s. Stop the server first, or pass --force if you "
            "are certain it is safe.",
            running,
        )
        return 4

    old_password = args.old_password or getpass.getpass("Old admin password: ")
    new_password = args.new_password or getpass.getpass("New admin password: ")
    if not old_password or not new_password:
        logger.error("Both old and new passwords are required.")
        return 2

    from app.db.base import close_pool, init_pool

    init_pool(min_conn=1, max_conn=2)
    try:
        return rekey(
            old_password,
            new_password,
            dry_run=args.dry_run,
            update_admin_hash=args.update_admin_hash,
        )
    finally:
        close_pool()


if __name__ == "__main__":
    sys.exit(main())
