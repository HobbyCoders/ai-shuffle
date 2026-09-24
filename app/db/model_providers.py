"""Model provider registry database operations.

Backs the "run non-Anthropic models through the Claude Code harness" feature:
each row is an admin-configured model gateway (OpenRouter or a custom
Anthropic-compatible endpoint) whose env vars are injected into a spawned
session. The provider list is shared across installs on a shared database.

``auth_token`` stores an ``enc:``-prefixed Fernet blob of the gateway key and is
NEVER returned on a normal read — it is fetched only when ``include_token=True``
(secrets are never on a default read). The
``models_json`` (a curated ``[{id, label}]`` list) and ``extra_env_json`` (an
extra-env map) columns are TEXT-encoded JSON, serialized/parsed via
``json.dumps`` / ``json.loads`` (same convention as ``db/profiles.py``).

Codex/ChatGPT-subscription OAuth (phase-2, plan amendment A1 — one account per
provider, stored 1:1 on this same row, NOT a separate pool table):
``codex_access_token`` / ``codex_refresh_token`` are two MORE ``enc:``-prefixed
write-only blobs handled exactly like ``auth_token`` — excluded from the public
columns, returned only under ``include_token=True``, and re-keyed on a password
change (see ``app/core/encryption.py`` locations 7 & 8). The non-secret Codex
fields (``auth_mode``, ``codex_account_id``, ``codex_id_token_expires_at``,
``codex_last_refresh``, ``needs_reauth``) ARE public. :func:`set_codex_token_bundle`
persists the whole rotated bundle in one atomic UPDATE (a later refresh worker
relies on that atomicity because the Codex refresh token rotates); there is no
public API write path for the token blobs in this chunk.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list

logger = logging.getLogger(__name__)


class ModelProviderRow(TypedDict, total=False):
    """A row from ``model_providers``.

    ``auth_token`` is only populated when ``include_token=True`` on the reader
    (it is the encrypted blob); list/detail reads omit it by default so the
    secret never travels further than it must. ``models_json`` is parsed to a
    ``list`` and ``extra_env_json`` to a ``dict`` on read.
    """
    id: str
    name: str
    provider_type: str
    base_url: str
    api_dialect: Optional[str]
    auth_token: Optional[str]
    models_json: List[Any]
    default_model: Optional[str]
    small_fast_model: Optional[str]
    extra_env_json: Dict[str, Any]
    context_window: Optional[int]
    max_output_tokens: Optional[int]
    enabled: bool
    auth_mode: Optional[str]
    codex_access_token: Optional[str]
    codex_refresh_token: Optional[str]
    codex_account_id: Optional[str]
    codex_id_token_expires_at: Optional[str]
    codex_last_refresh: Optional[str]
    needs_reauth: bool
    openai_billing_org_id: Optional[str]
    openai_billing_instance_id: Optional[str]
    created_at: str
    updated_at: str
    revision: Optional[str]


# Columns exposed on a normal (non-token) read. ``auth_token`` and the two Codex
# token blobs (``codex_access_token`` / ``codex_refresh_token``) are deliberately
# excluded so the encrypted blobs are only fetched on demand. The non-secret
# Codex fields (auth_mode, codex_account_id, expiry/last_refresh timestamps,
# needs_reauth) ARE public.
_PUBLIC_COLUMNS = (
    "id, name, provider_type, base_url, api_dialect, models_json, "
    "default_model, small_fast_model, extra_env_json, context_window, "
    "max_output_tokens, enabled, auth_mode, codex_account_id, "
    "codex_id_token_expires_at, codex_last_refresh, needs_reauth, "
    "openai_billing_org_id, openai_billing_instance_id, "
    "created_at, updated_at, revision"
)

# The encrypted write-only token blobs, appended to the SELECT only when a caller
# passes ``include_token=True`` (server-side use: env overlay / re-encryption /
# the Codex refresh worker). Kept off ``_PUBLIC_COLUMNS`` so they never travel on
# a normal read.
_TOKEN_COLUMNS = "auth_token, codex_access_token, codex_refresh_token, openai_admin_key"

# Columns that update_model_provider accepts as direct partial updates. JSON
# columns are handled specially (see _dump_json_value); auth_token and the two
# Codex token blobs are here so a caller CAN update them, but they are only
# touched when explicitly passed (the re-encryption plan writes one blob at a
# time; set_codex_token_bundle writes the whole bundle atomically).
_UPDATABLE_COLUMNS = (
    "name",
    "provider_type",
    "base_url",
    "api_dialect",
    "auth_token",
    "models_json",
    "default_model",
    "small_fast_model",
    "extra_env_json",
    "context_window",
    "max_output_tokens",
    "enabled",
    "auth_mode",
    "codex_access_token",
    "codex_refresh_token",
    "openai_admin_key",
    "codex_account_id",
    "codex_id_token_expires_at",
    "codex_last_refresh",
    "needs_reauth",
)


def _parse_json_columns(row: Optional[ModelProviderRow]) -> Optional[ModelProviderRow]:
    """Parse the TEXT-encoded JSON columns in-place into Python structures."""
    if not row:
        return row
    if "models_json" in row:
        val = row["models_json"]
        row["models_json"] = (
            json.loads(val) if isinstance(val, str) else (val if val is not None else [])
        )
    if "extra_env_json" in row:
        val = row["extra_env_json"]
        row["extra_env_json"] = (
            json.loads(val) if isinstance(val, str) else (val if val is not None else {})
        )
    return row


def _dump_json_value(column: str, value: Any) -> Any:
    """Serialize a value destined for a JSON TEXT column.

    Accepts either an already-serialized string or a Python list/dict; leaves
    anything else (e.g. None) untouched so the DB default applies.
    """
    if column in ("models_json", "extra_env_json") and not isinstance(value, (str, type(None))):
        return json.dumps(value)
    return value


def create_model_provider(
    provider_id: str,
    name: str,
    provider_type: str,
    base_url: str,
    auth_token: Optional[str] = None,
    models: Optional[List[Any]] = None,
    default_model: Optional[str] = None,
    small_fast_model: Optional[str] = None,
    extra_env: Optional[Dict[str, Any]] = None,
    context_window: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    enabled: bool = True,
    api_dialect: Optional[str] = None,
) -> Optional[ModelProviderRow]:
    """Insert a new model-provider row and return it (without the token).

    ``models`` (a ``[{id, label}]`` list) and ``extra_env`` (a map) are stored
    as TEXT-encoded JSON. ``auth_token`` is expected to already be an
    ``enc:``-prefixed blob (encryption happens at the API layer).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO model_providers
                (id, name, provider_type, base_url, api_dialect, auth_token,
                 models_json, default_model, small_fast_model, extra_env_json,
                 context_window, max_output_tokens, enabled,
                 created_at, updated_at, revision)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                provider_id,
                name,
                provider_type,
                base_url,
                api_dialect,
                auth_token,
                json.dumps(_normalize_model_capabilities(models if models is not None else [])),
                default_model,
                small_fast_model,
                json.dumps(extra_env if extra_env is not None else {}),
                context_window,
                max_output_tokens,
                enabled,
                now,
                now,
                str(uuid4()),
            ),
        )
    return get_model_provider(provider_id)


def get_model_provider(
    provider_id: str, include_token: bool = False
) -> Optional[ModelProviderRow]:
    """Get one provider by id. Token blob included only when requested."""
    cols = _PUBLIC_COLUMNS if not include_token else _PUBLIC_COLUMNS + ", " + _TOKEN_COLUMNS
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {cols} FROM model_providers WHERE id = %s",
            (provider_id,),
        )
        return _parse_json_columns(row_to_dict(cursor.fetchone()))  # type: ignore[return-value]


def get_all_model_providers(include_token: bool = False) -> List[ModelProviderRow]:
    """List all providers, newest created first. Token omitted by default."""
    cols = _PUBLIC_COLUMNS if not include_token else _PUBLIC_COLUMNS + ", " + _TOKEN_COLUMNS
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {cols} FROM model_providers ORDER BY created_at DESC"
        )
        rows = rows_to_list(cursor.fetchall())
        return [_parse_json_columns(r) for r in rows]  # type: ignore[return-value]


def get_enabled_model_providers(include_token: bool = False) -> List[ModelProviderRow]:
    """List only enabled providers, newest created first.

    Token omitted by default (public/registry reads); the env overlay passes
    ``include_token=True`` to obtain the encrypted blob for decryption.
    """
    cols = _PUBLIC_COLUMNS if not include_token else _PUBLIC_COLUMNS + ", " + _TOKEN_COLUMNS
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {cols} FROM model_providers WHERE enabled = TRUE "
            "ORDER BY created_at DESC"
        )
        rows = rows_to_list(cursor.fetchall())
        return [_parse_json_columns(r) for r in rows]  # type: ignore[return-value]


def count_configured_model_providers() -> int:
    """Count enabled providers that carry a usable stored credential.

    "Usable" here means the row is enabled AND holds at least one non-empty
    credential blob: the API-key ``auth_token`` (OpenRouter / custom /
    Anthropic-API rows) or the ChatGPT-subscription ``codex_access_token``.
    Rows that are disabled, or that were created without any credential, do
    not count. Deliberately reads only the presence of the blobs — no
    decryption, no network validation — so this stays a cheap gate for
    "does this install have any way to run a model at all" (PA10).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) AS n FROM model_providers
            WHERE enabled = TRUE
              AND (
                    (auth_token IS NOT NULL AND auth_token <> '')
                 OR (codex_access_token IS NOT NULL AND codex_access_token <> '')
              )
            """
        )
        row = cursor.fetchone()
        return int(row["n"]) if row else 0


def _normalize_model_capabilities(models):
    """Whitelist canonical capability data on every catalog write, including legacy imports."""
    from app.core.model_effort import sanitize_effort_metadata
    if isinstance(models, str):
        models = json.loads(models)
    if not isinstance(models, list):
        return models
    normalized = []
    for entry in models:
        if isinstance(entry, dict):
            metadata = sanitize_effort_metadata(entry)
            capability = metadata.get("reasoning_capabilities", {})
            if "reasoning_capabilities" in entry or capability.get("effort_levels") is not None:
                entry = {**entry, **metadata}
        normalized.append(entry)
    return normalized


def update_model_provider(
    provider_id: str, *, expected_revision: Optional[str] = None, **kwargs: Any
) -> Optional[ModelProviderRow]:
    """Partially update a provider by id and return the fresh row.

    Only the columns present in ``kwargs`` (and in ``_UPDATABLE_COLUMNS``) are
    written. ``auth_token`` is NEVER touched unless explicitly passed, so a
    metadata-only edit leaves the stored token intact. JSON columns accept
    either a serialized string or a Python list/dict.
    ``expected_revision`` rejects an observation fetched before any concurrent
    binding or capability edit, including writes made by another worker.
    """
    if "models_json" in kwargs:
        kwargs["models_json"] = _normalize_model_capabilities(kwargs["models_json"])
    updates: List[str] = []
    values: List[Any] = []
    for column in _UPDATABLE_COLUMNS:
        if column in kwargs:
            updates.append(f"{column} = %s")
            values.append(_dump_json_value(column, kwargs[column]))

    if not updates:
        return get_model_provider(provider_id)

    updates.append("updated_at = %s")
    values.append(datetime.now(timezone.utc).isoformat())
    updates.append("revision = %s")
    values.append(str(uuid4()))
    values.append(provider_id)
    condition = " AND revision = %s" if expected_revision is not None else ""
    if expected_revision is not None:
        values.append(expected_revision)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE model_providers SET {', '.join(updates)} WHERE id = %s{condition} RETURNING {_PUBLIC_COLUMNS}",
            values,
        )
        # Return this write's revision, not an unrelated edit committed between
        # our UPDATE and a second SELECT (used by capability cooldown keys).
        return _parse_json_columns(row_to_dict(cursor.fetchone()))


def set_openai_billing_binding(provider_id: str, organization_id: str, instance_id: str, admin_key: Optional[str] = None) -> Optional[ModelProviderRow]:
    """Bind credit tracking without changing model credentials."""
    from urllib.parse import urlsplit

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT provider_type, base_url, auth_mode, enabled, openai_billing_instance_id FROM model_providers WHERE id = %s FOR UPDATE",
            (provider_id,),
        )
        row = cursor.fetchone()
        if not row or row["provider_type"] != "openai" or not row["enabled"]:
            return None
        if row.get("openai_billing_instance_id") not in (None, instance_id):
            return None
        try:
            url = urlsplit(row["base_url"])
            if (
                url.scheme != "https" or url.hostname != "api.openai.com"
                or url.port not in (None, 443) or url.username or url.password
                or row.get("auth_mode") in {"codex", "chatgpt"}
            ):
                return None
        except ValueError:
            return None
        cursor.execute(
            "UPDATE model_providers SET openai_billing_org_id = %s, openai_billing_instance_id = %s, openai_admin_key = %s, updated_at = %s, revision = %s WHERE id = %s",
            (organization_id, instance_id, admin_key, datetime.now(timezone.utc).isoformat(), str(uuid4()), provider_id),
        )
        cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s", (provider_id,))
    return get_model_provider(provider_id)


def clear_openai_billing_binding(
    provider_id: str, expected_instance_id: Optional[str] = None,
) -> Optional[ModelProviderRow]:
    """Remove local Admin API access and its usage snapshot, not the model key."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, openai_billing_instance_id FROM model_providers WHERE id = %s FOR UPDATE", (provider_id,)
        )
        row = cursor.fetchone()
        if not row or (expected_instance_id and row.get("openai_billing_instance_id") not in (None, expected_instance_id)):
            return None
        cursor.execute(
            "UPDATE model_providers SET openai_billing_org_id = NULL, openai_billing_instance_id = NULL, openai_admin_key = NULL, updated_at = %s, revision = %s WHERE id = %s",
            (datetime.now(timezone.utc).isoformat(), str(uuid4()), provider_id),
        )
        cursor.execute("SELECT raw FROM provider_usage_snapshots WHERE provider_id = %s", (provider_id,))
        snapshot = cursor.fetchone()
        raw = snapshot.get("raw") if snapshot else None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        if isinstance(raw, dict) and raw.get("source") in {"browser_platform", "openai_admin"}:
            cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s", (provider_id,))
    return get_model_provider(provider_id)


def delete_model_provider(provider_id: str) -> bool:
    """Delete a provider row."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM model_providers WHERE id = %s", (provider_id,))
        deleted = cursor.rowcount > 0
        cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s", (provider_id,))
        cursor.execute("DELETE FROM system_settings WHERE key = %s", ("openrouter_billing_key:" + provider_id,))
        cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s", ("openrouter-account:" + provider_id,))
        return deleted


def set_model_provider_enabled(provider_id: str, enabled: bool) -> Optional[ModelProviderRow]:
    """Toggle a provider's ``enabled`` flag and return the fresh row."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE model_providers SET enabled = %s, updated_at = %s, revision = %s WHERE id = %s",
            (enabled, now, str(uuid4()), provider_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_model_provider(provider_id)


# ---------------------------------------------------------------------------
# Codex/ChatGPT-subscription OAuth token bundle (phase-2, amendment A1).
#
# Stored 1:1 on the provider's single-credential row. The token blobs are
# ENCRYPTED WRITE-ONLY (``enc:``-prefixed Fernet, encryption happens at the call
# site — mirroring ``auth_token``) and are returned only via the explicit
# include-token reader below, never on a public read. There is NO public API
# write path for the blobs in this chunk; these are internal setters/readers a
# later device-code enrollment flow + refresh worker call.
# ---------------------------------------------------------------------------

def set_codex_token_bundle(
    provider_id: str,
    *,
    access_token: Optional[str],
    refresh_token: Optional[str],
    account_id: Optional[str],
    id_token_expires_at: Optional[str] = None,
    last_refresh: Optional[str] = None,
    needs_reauth: bool = False,
    auth_mode: str = "chatgpt",
) -> Optional[ModelProviderRow]:
    """Atomically persist an OAuth token bundle on a provider row.

    Writes ``codex_access_token`` + ``codex_refresh_token`` +
    ``codex_account_id`` + ``codex_id_token_expires_at`` + ``codex_last_refresh``
    in ONE UPDATE, and sets ``auth_mode`` (default ``'chatgpt'`` so existing
    Codex callers are unchanged; Grok enrollment passes ``'grok'``) +
    ``needs_reauth`` (default cleared) alongside. The Codex refresh token
    ROTATES on every refresh, so a later refresh worker relies on this
    single-statement persist to avoid a torn write between the new access and
    refresh tokens. ``access_token`` / ``refresh_token`` are expected to already
    be ``enc:``-prefixed blobs (encryption at the call site, same contract as
    ``auth_token``).

    Returns the fresh public row (no blobs), or ``None`` if the provider is gone.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE model_providers
               SET auth_mode = %s,
                   codex_access_token = %s,
                   codex_refresh_token = %s,
                   codex_account_id = %s,
                   codex_id_token_expires_at = %s,
                   codex_last_refresh = %s,
                   needs_reauth = %s,
                   updated_at = %s,
                   revision = %s
             WHERE id = %s
            """,
            (
                auth_mode,
                access_token,
                refresh_token,
                account_id,
                id_token_expires_at,
                last_refresh,
                needs_reauth,
                now,
                str(uuid4()),
                provider_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
    return get_model_provider(provider_id)


def get_codex_tokens(provider_id: str) -> Optional[Dict[str, Any]]:
    """Explicit include-token reader for the Codex bundle (server-side only).

    Returns the encrypted ``codex_access_token`` / ``codex_refresh_token`` blobs
    plus the non-secret bundle fields (``auth_mode``, ``codex_account_id``,
    ``codex_id_token_expires_at``, ``codex_last_refresh``, ``needs_reauth``).
    These blobs are NEVER returned by the public readers — this is the Codex
    analogue of ``get_model_provider(include_token=True)``. Returns ``None`` when
    the provider does not exist.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT auth_mode, codex_access_token, codex_refresh_token, "
            "codex_account_id, codex_id_token_expires_at, codex_last_refresh, "
            "needs_reauth FROM model_providers WHERE id = %s",
            (provider_id,),
        )
        return row_to_dict(cursor.fetchone())


def try_claim_codex_refresh(
    provider_id: str, owner: str, ttl_seconds: int = 120
) -> bool:
    """Try to take the durable cross-install refresh lease on a provider row.

    The Codex refresh token ROTATES on every use: the moment two installs
    sharing this database POST a refresh for the same provider, the loser gets
    ``refresh_token_reused`` and the credential is permanently dead. An
    in-process ``asyncio.Lock`` only serializes ONE process, so the refresh
    worker claims the row here first — a single conditional UPDATE that at most
    one install can win.

    The lease is evaluated entirely on the DATABASE clock (``NOW()``), exactly
    like ``installs._ONLINE_EXPR``: install clocks on a shared DB drift, so a
    process wall clock must never decide who holds the lease. The claim succeeds
    when the row is unleased, when the previous lease has expired (crash
    backstop — nobody has to release it), or when WE already hold it. Same-owner
    reclaim is deliberate: after a crash-restart the same instance may need to
    re-claim inside the TTL, and its in-process lock still serializes locally.

    Returns ``True`` iff this call took (or kept) the lease.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE model_providers
               SET codex_refresh_lease_owner = %s,
                   codex_refresh_lease_expires_at = NOW() + make_interval(secs => %s)
             WHERE id = %s
               AND (codex_refresh_lease_expires_at IS NULL
                    OR codex_refresh_lease_expires_at < NOW()
                    OR codex_refresh_lease_owner = %s)
            """,
            (owner, ttl_seconds, provider_id, owner),
        )
        return cursor.rowcount > 0


def release_codex_refresh_claim(provider_id: str, owner: str) -> None:
    """Release the Codex refresh lease — but only if ``owner`` still holds it.

    Owner-scoped on purpose: if our TTL already expired and a peer install
    claimed the row, an unconditional clear would silently strip THEIR lease and
    re-open the double-refresh window this lease exists to close. Releasing a
    lease we no longer hold is therefore a no-op; TTL expiry remains the backstop
    for a process that dies mid-refresh.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE model_providers
               SET codex_refresh_lease_owner = NULL,
                   codex_refresh_lease_expires_at = NULL
             WHERE id = %s
               AND codex_refresh_lease_owner = %s
            """,
            (provider_id, owner),
        )
