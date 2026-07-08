"""Claude account registry database operations.

Backs the "multiple Anthropic (Claude Code OAuth) accounts with in-app
switching" feature. The account LIST is shared across installs on a shared
database; the ACTIVE-account pointer is per-install and stored in
``system_settings`` under ``claude_active_account:{instance_id}`` (see
``app.core.claude_accounts``), mirroring the ``workspace_path:{instance_id}``
precedent — hence there is no ``is_active`` column in this table.

``credentials`` stores an ``enc:``-prefixed Fernet blob of the full
``claudeAiOauth`` JSON object. ``credential_fingerprint`` is the sha256 hex of
the plaintext ``claudeAiOauth`` JSON (sorted keys) so divergence between the
on-disk credentials file and the stored row can be detected without decrypting.
``expires_at`` is a plaintext copy of ``expiresAt`` (unix ms) for display.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list

logger = logging.getLogger(__name__)


class ClaudeAccountRow(TypedDict, total=False):
    """A row from ``claude_accounts``.

    ``credentials`` is only populated when ``include_credentials=True`` on the
    reader (it is the encrypted blob); list reads omit it by default so the
    secret never travels further than it must.
    """
    id: str
    name: str
    email: Optional[str]
    subscription_type: Optional[str]
    credentials: Optional[str]
    credential_fingerprint: Optional[str]
    expires_at: Optional[int]
    has_refresh_token: bool
    sync_pending: bool
    created_at: str
    updated_at: str
    last_used_at: Optional[str]


# Columns exposed on a normal (non-credential) read. ``credentials`` is
# deliberately excluded so the encrypted blob is only fetched on demand.
_PUBLIC_COLUMNS = (
    "id, name, email, subscription_type, credential_fingerprint, expires_at, "
    "has_refresh_token, sync_pending, created_at, updated_at, last_used_at"
)


def create_claude_account(
    account_id: str,
    name: str,
    credentials: str,
    credential_fingerprint: Optional[str],
    expires_at: Optional[int],
    has_refresh_token: bool = True,
    email: Optional[str] = None,
    subscription_type: Optional[str] = None,
) -> Optional[ClaudeAccountRow]:
    """Insert a new Claude account row and return it (without credentials)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO claude_accounts
                (id, name, email, subscription_type, credentials,
                 credential_fingerprint, expires_at, has_refresh_token,
                 sync_pending, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                account_id,
                name,
                email,
                subscription_type,
                credentials,
                credential_fingerprint,
                expires_at,
                has_refresh_token,
                False,
                now,
                now,
            ),
        )
    return get_claude_account(account_id)


def get_claude_account(
    account_id: str, include_credentials: bool = False
) -> Optional[ClaudeAccountRow]:
    """Get one account by id. Credentials blob included only when requested."""
    cols = _PUBLIC_COLUMNS if not include_credentials else _PUBLIC_COLUMNS + ", credentials"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {cols} FROM claude_accounts WHERE id = %s",
            (account_id,),
        )
        return row_to_dict(cursor.fetchone())  # type: ignore[return-value]


def get_all_claude_accounts(include_credentials: bool = False) -> List[ClaudeAccountRow]:
    """List all accounts, most recently used first (then newest created)."""
    cols = _PUBLIC_COLUMNS if not include_credentials else _PUBLIC_COLUMNS + ", credentials"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {cols} FROM claude_accounts "
            "ORDER BY last_used_at DESC NULLS LAST, created_at DESC"
        )
        return rows_to_list(cursor.fetchall())  # type: ignore[return-value]


def update_claude_account_credentials(
    account_id: str,
    credentials: str,
    credential_fingerprint: Optional[str],
    expires_at: Optional[int],
    has_refresh_token: bool,
    clear_sync_pending: bool = True,
) -> Optional[ClaudeAccountRow]:
    """Replace an account's credentials plus the derived display/sync columns.

    Used by the sync-back path when the on-disk credentials file diverges from
    the stored row (e.g. a CLI subprocess self-refreshed the shared file).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        if clear_sync_pending:
            cursor.execute(
                """
                UPDATE claude_accounts
                SET credentials = %s, credential_fingerprint = %s,
                    expires_at = %s, has_refresh_token = %s,
                    sync_pending = FALSE, updated_at = %s
                WHERE id = %s
                """,
                (credentials, credential_fingerprint, expires_at,
                 has_refresh_token, now, account_id),
            )
        else:
            cursor.execute(
                """
                UPDATE claude_accounts
                SET credentials = %s, credential_fingerprint = %s,
                    expires_at = %s, has_refresh_token = %s, updated_at = %s
                WHERE id = %s
                """,
                (credentials, credential_fingerprint, expires_at,
                 has_refresh_token, now, account_id),
            )
        if cursor.rowcount == 0:
            return None
    return get_claude_account(account_id)


def update_claude_account_credentials_only(
    account_id: str, credentials: str
) -> bool:
    """Replace ONLY the encrypted credentials blob.

    Used by the re-encryption flow: the plaintext is unchanged, so the
    fingerprint / expires_at / has_refresh_token columns must be left untouched.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE claude_accounts SET credentials = %s, updated_at = %s WHERE id = %s",
            (credentials, now, account_id),
        )
        return cursor.rowcount > 0


def rename_claude_account(account_id: str, name: str) -> Optional[ClaudeAccountRow]:
    """Rename an account."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE claude_accounts SET name = %s, updated_at = %s WHERE id = %s",
            (name, now, account_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_claude_account(account_id)


def delete_claude_account(account_id: str) -> bool:
    """Delete an account row."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM claude_accounts WHERE id = %s", (account_id,))
        return cursor.rowcount > 0


def mark_claude_account_sync_pending(account_id: str) -> bool:
    """Flag that the file rotated but write-back was blocked (no encryption key).

    Only touches the plaintext ``sync_pending`` column, so it works even when
    encryption is locked.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE claude_accounts SET sync_pending = TRUE, updated_at = %s WHERE id = %s",
            (now, account_id),
        )
        return cursor.rowcount > 0


def touch_claude_account_last_used(account_id: str) -> None:
    """Bump ``last_used_at`` for an account (called on activate/switch)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE claude_accounts SET last_used_at = %s WHERE id = %s",
            (now, account_id),
        )


def count_claude_accounts() -> int:
    """Return the number of Claude account rows."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM claude_accounts")
        row = cursor.fetchone()
        return int(row["n"]) if row else 0
