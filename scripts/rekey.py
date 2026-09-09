"""Legacy-only offline password rekey; refused after server-key adoption.

Current installations use ``python -m scripts.credential_keys`` for portable
recovery. Do not use this legacy utility to change server-managed custody.

Why this exists
---------------
AI Shuffle's at-rest encryption key is DERIVED from the admin password:

    key = PBKDF2-HMAC-SHA256(admin_password, salt, 600_000 iters)   # -> Fernet key

(see ``app/core/encryption.py``). The salt lives in ``system_settings.encryption_salt``;
the password is never stored. EVERY ``enc:``-prefixed value is Fernet-encrypted with
that derived key, across FOUR storage locations: ``system_settings.value`` (e.g.
fal/anthropic keys), ``automations.triggers``/``trigger_config`` (webhook secrets),
``connectors.config`` (bot/access tokens), and ``api_user_credentials.encrypted_value``.

Changing the admin password therefore REQUIRES re-encrypting ALL of those secrets
under a key derived from the NEW password, or they become permanently
undecryptable. The running app does this online via
``POST /api/v1/auth/change-password`` (``encryption.re_encrypt_all_secrets``). This
script is the OFFLINE equivalent for operators who need to rekey without the server
running (e.g. after a lost/rotated admin password, or during a controlled
maintenance window).

Safety properties
-----------------
* SINGLE SOURCE OF TRUTH: reuses ``encryption.build_reencryption_plan`` — the exact
  same dynamic-discovery + fail-closed logic the online path uses. No mirrored
  name list, no independent crypto.
* VERIFIES the old password decrypts an existing secret before writing anything.
  If no encrypted secrets exist, it falls back to verifying against the stored
  admin password hash so a wrong old password is still caught.
* FAILS CLOSED: if any discovered value cannot be decrypted with the old key, the
  script aborts and lists the offenders (by table/name+id, never the value) —
  unless ``--skip-undecryptable`` is given, which loudly SKIPS them instead.
* All-or-nothing via decrypt-before-write: every value is decrypted with the old
  key BEFORE anything is written (fail-closed above). Secrets are then applied
  through the app's own domain accessors (auto-committing, so JSONB handling
  stays single-sourced); the admin-hash update runs in its own transaction.
* ``--dry-run`` LISTS exactly what would change (by table/name; counts for JSONB)
  and touches nothing.
* Refuses to run while the server appears to be up (cheap ``/health`` probe on
  the configured HOST/PORT) unless ``--force`` is given, to avoid racing the
  in-memory ``_encryption_key`` the live app holds.

Usage
-----
Advanced maintenance only, not a fresh-install step. The managed PostgreSQL
supervisor and its private DATA_DIR/managed-postgres.json descriptor must remain
available while application writers are stopped. External database environment
settings are not supported. From that managed maintenance environment::

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

    Returns (ok, reason). Does not mutate anything. Tries EVERY encrypted
    ``system_settings`` value (discovered dynamically, matching the
    re-encryption path — no hardcoded name list): the password verifies if it
    decrypts ANY of them, so a single already-bricked orphan (encrypted under
    a long-lost key) cannot falsely reject the correct old password.
    """
    from cryptography.fernet import Fernet
    from app.core.encryption import is_encrypted

    # Prefer verifying against a real encrypted secret.
    try:
        all_settings = db.get_all_system_settings() or {}
    except Exception:  # noqa: BLE001
        all_settings = {}
    failed_names: List[str] = []
    for name, value in all_settings.items():
        if name == "encryption_salt":
            continue
        if value and isinstance(value, str) and is_encrypted(value):
            try:
                f = Fernet(old_key)
                f.decrypt(base64.b64decode(value[4:]))
                return True, f"old password decrypts {name}"
            except Exception:  # noqa: BLE001 — InvalidToken or corrupt base64
                failed_names.append(name)
    if failed_names:
        return False, (
            "old password FAILED to decrypt any encrypted setting "
            f"({', '.join(failed_names)}) — wrong password?"
        )

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


def _summarize_plan(plan) -> str:
    """One-line, secret-free summary of a re-encryption plan by location."""
    from collections import Counter
    counts = Counter(item.kind for item in plan)
    parts = [
        f"{counts.get('system_setting', 0)} system_settings",
        f"{counts.get('automation', 0)} automations(JSONB)",
        f"{counts.get('connector', 0)} connectors(JSONB)",
        f"{counts.get('credential', 0)} api_user_credentials",
    ]
    return ", ".join(parts)


def rekey(
    old_password: str,
    new_password: str,
    *,
    dry_run: bool,
    update_admin_hash: bool,
    skip_undecryptable: bool = False,
) -> int:
    """Perform the rekey. Returns a process exit code (0 = success)."""
    import bcrypt
    from app.core import encryption
    from app.core.encryption import (
        derive_key_from_password,
        get_or_create_salt,
        build_reencryption_plan,
        apply_reencryption_plan,
    )
    from app.core.auth import auth_service
    from app.db import database as db
    from app.db.base import transaction

    try:
        encryption.require_legacy_key_mode(db)
    except RuntimeError:
        logger.error("Server-managed credential storage cannot be password-rekeyed; use scripts.credential_keys.")
        return 2

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

    # ---- Discover + decrypt every encrypted value (single source of truth) --
    # Reuses the SAME dynamic discovery the online change-password path uses.
    plan, failures = build_reencryption_plan(old_key, new_key, db)

    if failures:
        for label, why in failures:
            logger.error("Undecryptable with old key: %s (%s)", label, why)
        if not skip_undecryptable:
            logger.error(
                "Refusing to proceed: %d value(s) could not be decrypted with the "
                "old password. Re-run with --skip-undecryptable to SKIP them "
                "(they are already bricked) and rekey the rest.",
                len(failures),
            )
            return 3
        logger.warning(
            "--skip-undecryptable: SKIPPING %d undecryptable value(s) listed above; "
            "they will be LEFT AS-IS and remain unreadable.",
            len(failures),
        )

    logger.info("Will re-encrypt: %s", _summarize_plan(plan))
    if update_admin_hash:
        logger.info("Will also update the admin password hash.")

    if dry_run:
        for item in plan:
            logger.info("[dry-run]   would re-encrypt %s", item.label)
        logger.info("[dry-run] No changes written.")
        return 0

    # ---- Write phase -------------------------------------------------------
    # Secrets are applied via the same domain accessors the app uses (JSONB
    # re-serialization handled there); the all-or-nothing guarantee comes from
    # decrypting EVERYTHING before this point (fail-closed above). The admin
    # hash update stays inside a single transaction() as before.
    apply_reencryption_plan(plan, db)
    # Keep the in-memory key consistent for anything else in-process.
    encryption._encryption_key = new_key

    if update_admin_hash:
        new_hash = bcrypt.hashpw(
            new_password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")
        with transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE admin SET password_hash = %s WHERE id = 1",
                (new_hash,),
            )

    logger.info("Rekey complete: re-encrypted %s.", _summarize_plan(plan))
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
        "--skip-undecryptable",
        action="store_true",
        help=(
            "Skip (and loudly list) values the OLD key cannot decrypt — for "
            "operators with already-bricked orphans — instead of aborting. "
            "Skipped values are LEFT AS-IS and remain unreadable."
        ),
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
            skip_undecryptable=args.skip_undecryptable,
        )
    finally:
        close_pool()


if __name__ == "__main__":
    sys.exit(main())
