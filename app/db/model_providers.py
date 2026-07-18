"""Model provider registry database operations.

Backs the "run non-Anthropic models through the Claude Code harness" feature:
each row is an admin-configured model gateway (OpenRouter or a custom
Anthropic-compatible endpoint) whose env vars are injected into a spawned
session. The provider list is shared across installs on a shared database.

``auth_token`` stores an ``enc:``-prefixed Fernet blob of the gateway key and is
NEVER returned on a normal read — it is fetched only when ``include_token=True``
(mirroring ``claude_accounts.credentials`` / ``include_credentials``). The
``models_json`` (a curated ``[{id, label}]`` list) and ``extra_env_json`` (an
extra-env map) columns are TEXT-encoded JSON, serialized/parsed via
``json.dumps`` / ``json.loads`` (same convention as ``db/profiles.py``).
"""

from __future__ import annotations

import json
import logging
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
    auth_token: Optional[str]
    models_json: List[Any]
    default_model: Optional[str]
    small_fast_model: Optional[str]
    extra_env_json: Dict[str, Any]
    context_window: Optional[int]
    max_output_tokens: Optional[int]
    enabled: bool
    created_at: str
    updated_at: str


# Columns exposed on a normal (non-token) read. ``auth_token`` is deliberately
# excluded so the encrypted blob is only fetched on demand.
_PUBLIC_COLUMNS = (
    "id, name, provider_type, base_url, models_json, default_model, "
    "small_fast_model, extra_env_json, context_window, max_output_tokens, "
    "enabled, created_at, updated_at"
)

# Columns that update_model_provider accepts as direct partial updates. JSON
# columns are handled specially (see _dump_json_value); auth_token is here so a
# caller CAN update it, but it is only touched when explicitly passed.
_UPDATABLE_COLUMNS = (
    "name",
    "provider_type",
    "base_url",
    "auth_token",
    "models_json",
    "default_model",
    "small_fast_model",
    "extra_env_json",
    "context_window",
    "max_output_tokens",
    "enabled",
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
                (id, name, provider_type, base_url, auth_token, models_json,
                 default_model, small_fast_model, extra_env_json,
                 context_window, max_output_tokens, enabled,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                provider_id,
                name,
                provider_type,
                base_url,
                auth_token,
                json.dumps(models if models is not None else []),
                default_model,
                small_fast_model,
                json.dumps(extra_env if extra_env is not None else {}),
                context_window,
                max_output_tokens,
                enabled,
                now,
                now,
            ),
        )
    return get_model_provider(provider_id)


def get_model_provider(
    provider_id: str, include_token: bool = False
) -> Optional[ModelProviderRow]:
    """Get one provider by id. Token blob included only when requested."""
    cols = _PUBLIC_COLUMNS if not include_token else _PUBLIC_COLUMNS + ", auth_token"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {cols} FROM model_providers WHERE id = %s",
            (provider_id,),
        )
        return _parse_json_columns(row_to_dict(cursor.fetchone()))  # type: ignore[return-value]


def get_all_model_providers(include_token: bool = False) -> List[ModelProviderRow]:
    """List all providers, newest created first. Token omitted by default."""
    cols = _PUBLIC_COLUMNS if not include_token else _PUBLIC_COLUMNS + ", auth_token"
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
    cols = _PUBLIC_COLUMNS if not include_token else _PUBLIC_COLUMNS + ", auth_token"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {cols} FROM model_providers WHERE enabled = TRUE "
            "ORDER BY created_at DESC"
        )
        rows = rows_to_list(cursor.fetchall())
        return [_parse_json_columns(r) for r in rows]  # type: ignore[return-value]


def update_model_provider(provider_id: str, **kwargs: Any) -> Optional[ModelProviderRow]:
    """Partially update a provider by id and return the fresh row.

    Only the columns present in ``kwargs`` (and in ``_UPDATABLE_COLUMNS``) are
    written. ``auth_token`` is NEVER touched unless explicitly passed, so a
    metadata-only edit leaves the stored token intact. JSON columns accept
    either a serialized string or a Python list/dict.
    """
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
    values.append(provider_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE model_providers SET {', '.join(updates)} WHERE id = %s",
            values,
        )
        if cursor.rowcount == 0:
            return None
    return get_model_provider(provider_id)


def delete_model_provider(provider_id: str) -> bool:
    """Delete a provider row."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM model_providers WHERE id = %s", (provider_id,))
        return cursor.rowcount > 0


def set_model_provider_enabled(provider_id: str, enabled: bool) -> Optional[ModelProviderRow]:
    """Toggle a provider's ``enabled`` flag and return the fresh row."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE model_providers SET enabled = %s, updated_at = %s WHERE id = %s",
            (enabled, now, provider_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_model_provider(provider_id)
