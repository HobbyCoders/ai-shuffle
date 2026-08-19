"""Bridge the app's encrypted ``model_providers`` credentials into fluid's
native provider clients.

Background
----------
fluid's native clients
(``fluid/providers/{openai_responses,openrouter,anthropic}/client.py``) know
nothing about this app: each reads an env var (``OPENAI_API_KEY`` /
``OPENROUTER_API_KEY`` / ``ANTHROPIC_API_KEY``) or takes an ``api_key``
argument, and ``OpenAIResponsesClient`` additionally accepts an ``auth`` object
implementing fluid's ``ResponsesAuth`` protocol (``async get_access_token()``,
``async on_401()``, optional ``account_id``) for ChatGPT-subscription (Codex)
OAuth. Without this module the flag-on native legs are built bare and every
request fails with a missing-credential 401 — the app's encrypted provider rows
were never consulted.

This module is the adapter-side resolver: given the session's bound
``model_providers`` row id and the fluid provider kind, it returns a
:class:`ProviderCredentials` bundle that ``harness_adapter._build_provider_client``
hands straight to the fluid constructor. It reuses the existing crypto service
(``app.core.encryption``) and the existing Codex refresh worker
(``app.core.codex_token_refresh``) — no new credential storage, no new crypto.

Auth per kind
-------------
* ``openai_responses`` + a ChatGPT-subscription row (``provider_type`` /
  ``auth_mode`` == ``chatgpt``) → :class:`CodexSubscriptionAuth`, whose
  ``get_access_token`` decrypts the stored access token on every call (so a
  background/401 rotation is picked up immediately) and whose ``on_401`` drives
  the shared, per-provider-serialized ``refresh_on_401_once``. The client must
  also be pointed at the Codex backend (``use_codex_backend``).
* ``openai_responses`` / ``openrouter`` / ``anthropic`` + an API-key row → the
  decrypted ``auth_token`` as ``api_key``.
* No provider bound to the session → ``None``, and the adapter keeps its
  existing env-var fallback (dev mode).
* A bound provider with no usable credential → :class:`ProviderCredentialError`
  (a ``ProviderConfigError``/``ValueError``, i.e. the channel the query engines
  already turn into a user-visible error) instead of a bare upstream 401.

HARD CONSTRAINT (F1/G9, mirrored from the translator gateway)
-------------------------------------------------------------
A Claude Pro/Max **subscription** OAuth token is NEVER routed into
``AnthropicClient``. Anthropic bans third-party use of subscription tokens;
Claude subscription traffic belongs to the delegated engine (the official
``claude`` CLI subprocess). The anthropic branch below therefore accepts ONLY a
real API key stored on the provider row and refuses any row that advertises a
subscription/OAuth ``auth_mode``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from app.core import encryption
from app.core.environment import ProviderConfigError

logger = logging.getLogger(__name__)


class ProviderCredentialError(ProviderConfigError):
    """A session's bound model provider has no usable credentials.

    Subclasses :class:`app.core.environment.ProviderConfigError` (a
    ``ValueError``) so it travels the same user-visible error channel the
    gateway-env path already uses — the user sees "provider X has no
    credentials configured" instead of an opaque 401 mid-turn.
    """


@dataclass
class ProviderCredentials:
    """What a fluid native client needs to authenticate.

    ``api_key`` and ``auth`` are mutually exclusive: an API-key row yields the
    former, a ChatGPT-subscription row the latter. ``use_codex_backend`` tells
    the caller to point ``OpenAIResponsesClient`` at the Codex responses
    endpoint rather than the OpenAI platform one. ``base_url`` is the provider
    row's endpoint root (set for ``custom`` rows and any anthropic row that
    overrides it) — the caller appends the API path (``/v1/messages``).
    """

    api_key: Optional[str] = None
    auth: Optional[Any] = None
    use_codex_backend: bool = False
    base_url: Optional[str] = None


# ``auth_mode`` values that mean "this row carries a subscription OAuth login,
# not an API key". Only ``chatgpt`` exists today (Codex); the rest are listed so
# a future subscription mode can never silently fall through to the API-key
# branch (and, for anthropic, into the banned subscription-token path).
_SUBSCRIPTION_AUTH_MODES = frozenset(
    {
        "chatgpt",
        "grok",
        "oauth",
        "subscription",
        "claude",
        "claude_subscription",
        "max",
        "pro",
    }
)


def _is_codex_subscription(row: Dict[str, Any]) -> bool:
    """Whether a provider row is a ChatGPT/Codex-subscription OAuth row.

    Mirrors the gateway's mode resolution: ``provider_type == "chatgpt"`` OR
    ``auth_mode == "chatgpt"`` (a ``custom`` row can carry the Codex auth mode).
    """
    return row.get("provider_type") == "chatgpt" or row.get("auth_mode") == "chatgpt"


def _is_grok_subscription(row: Dict[str, Any]) -> bool:
    """Whether a provider row is a SuperGrok-subscription OAuth row.

    ``provider_type == "grok"`` OR ``auth_mode == "grok"``.
    """
    return row.get("provider_type") == "grok" or row.get("auth_mode") == "grok"


def _is_xai_api(row: Dict[str, Any]) -> bool:
    """Whether a provider row is an xAI API-key (Grok) row."""
    return row.get("provider_type") == "xai"


def _decrypt_auth_token(row: Dict[str, Any]) -> Optional[str]:
    """Decrypt a provider row's ``auth_token`` blob (or pass plaintext through).

    Uses the app's crypto service only (ADMIN_PASSWORD-derived Fernet key); the
    plaintext branch is the same migration compatibility the gateway keeps.
    """
    raw = row.get("auth_token")
    if not raw:
        return None
    if encryption.is_encrypted(raw):
        return encryption.decrypt_value(raw)
    return raw


def _provider_label(row: Dict[str, Any], provider_id: str) -> str:
    return f"{row.get('name') or provider_id!r}"


async def _load_codex_access_token(provider_id: str) -> Tuple[str, Optional[str]]:
    """Return ``(access_token, account_id)`` for a Codex provider, or raise.

    Reads the encrypted bundle fresh on every call so a rotation performed by
    the refresh worker (or by a peer's ``on_401``) is picked up without
    rebuilding the client.
    """
    from app.db import database as db
    from app.db.async_database import run_sync

    stored = await run_sync(db.get_codex_tokens, provider_id)
    if not stored:
        raise ProviderCredentialError(
            f"Model provider {provider_id!r} has no credentials configured: no "
            "ChatGPT/Codex account is connected."
        )
    if stored.get("needs_reauth"):
        raise ProviderCredentialError(
            f"The ChatGPT account for model provider {provider_id!r} needs to be "
            "reconnected before it can be used."
        )
    enc_access = stored.get("codex_access_token")
    access = encryption.decrypt_value(enc_access) if enc_access else None
    if not access:
        raise ProviderCredentialError(
            f"The ChatGPT access token for model provider {provider_id!r} is "
            "missing; reconnect the account."
        )
    return access, stored.get("codex_account_id")


async def _load_grok_access_token(provider_id: str) -> str:
    """Return the decrypted Grok access token, or raise.

    Reads the encrypted bundle fresh on every call so a rotation performed by
    the refresh worker (or by a peer's ``on_401``) is picked up without
    rebuilding the client. Does NOT return account_id — Grok inference must
    not send Codex's ``chatgpt-account-id`` header.
    """
    from app.db import database as db
    from app.db.async_database import run_sync

    stored = await run_sync(db.get_codex_tokens, provider_id)
    if not stored:
        raise ProviderCredentialError(
            f"Model provider {provider_id!r} has no credentials configured: no "
            "Grok account is connected."
        )
    if stored.get("needs_reauth"):
        raise ProviderCredentialError(
            f"The Grok account for model provider {provider_id!r} needs to be "
            "reconnected before it can be used."
        )
    enc_access = stored.get("codex_access_token")
    access = encryption.decrypt_value(enc_access) if enc_access else None
    if not access:
        raise ProviderCredentialError(
            f"The Grok access token for model provider {provider_id!r} is "
            "missing; reconnect the account."
        )
    return access


class GrokSubscriptionAuth:
    """fluid ``ResponsesAuth`` implementation for a SuperGrok provider row.

    Same protocol as :class:`CodexSubscriptionAuth`, but ``account_id`` stays
    ``None`` so the fluid client never attaches Codex-only headers, and
    ``on_401`` drives ``xai_token_refresh.refresh_on_401_once``.
    """

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.account_id = None
        self._last_access_token: Optional[str] = None

    async def get_access_token(self) -> str:
        access = await _load_grok_access_token(self.provider_id)
        self._last_access_token = access
        return access

    async def on_401(self) -> None:
        """Refresh once after an upstream 401 (serialized per provider)."""
        from app.core import xai_token_refresh

        refreshed = await xai_token_refresh.refresh_on_401_once(
            self.provider_id, stale_access_token=self._last_access_token
        )
        if not refreshed:
            logger.warning(
                "grok auth: 401 refresh produced no fresh token for provider %s",
                self.provider_id,
            )


class CodexSubscriptionAuth:
    """fluid ``ResponsesAuth`` implementation backed by a ``model_providers`` row.

    Satisfies the protocol ``OpenAIResponsesClient`` expects: ``account_id``
    attribute, ``async get_access_token()``, ``async on_401()``. Token storage,
    decryption and rotation all stay in the existing app services — this is a
    thin shim, deliberately holding no plaintext beyond the last token handed
    out (which ``on_401`` needs so a peer's rotation is detected instead of
    triggering a second, reuse-tripping refresh POST).
    """

    def __init__(self, provider_id: str, account_id: Optional[str] = None) -> None:
        self.provider_id = provider_id
        self.account_id = account_id
        self._last_access_token: Optional[str] = None

    async def get_access_token(self) -> str:
        access, account_id = await _load_codex_access_token(self.provider_id)
        if account_id:
            self.account_id = account_id
        self._last_access_token = access
        return access

    async def on_401(self) -> None:
        """Refresh once after an upstream 401 (serialized per provider)."""
        from app.core import codex_token_refresh

        refreshed = await codex_token_refresh.refresh_on_401_once(
            self.provider_id, stale_access_token=self._last_access_token
        )
        if not refreshed:
            logger.warning(
                "codex auth: 401 refresh produced no fresh token for provider %s",
                self.provider_id,
            )


async def resolve_provider_credentials(
    kind: str, provider_id: str
) -> ProviderCredentials:
    """Resolve credentials for the session's bound provider row.

    ``kind`` is the fluid provider kind (``openai_responses`` / ``openrouter`` /
    ``anthropic``); ``provider_id`` is a ``model_providers`` row id. Raises
    :class:`ProviderCredentialError` when the row is missing, disabled, or has
    no usable credential — never returns a half-authenticated bundle.
    """
    from app.db import database as db
    from app.db.async_database import run_sync

    row = await run_sync(db.get_model_provider, provider_id, True)
    if not row:
        raise ProviderCredentialError(
            f"Model provider {provider_id!r} was not found; the session is bound "
            "to a provider that no longer exists."
        )
    if not row.get("enabled", True):
        raise ProviderCredentialError(
            f"Model provider {_provider_label(row, provider_id)} is disabled; "
            "re-enable it or pick another provider for this session."
        )

    label = _provider_label(row, provider_id)

    if kind == "openai_responses" and _is_codex_subscription(row):
        auth = CodexSubscriptionAuth(provider_id, row.get("codex_account_id"))
        # Eagerly resolve once so a missing/needs-reauth account surfaces as a
        # clear config error at connect time rather than a 401 mid-turn.
        await auth.get_access_token()
        return ProviderCredentials(auth=auth, use_codex_backend=True)

    if kind == "openai_responses" and _is_grok_subscription(row):
        auth = GrokSubscriptionAuth(provider_id)
        await auth.get_access_token()
        base_url = (row.get("base_url") or "").strip() or "https://api.x.ai/v1"
        return ProviderCredentials(
            auth=auth, base_url=base_url, use_codex_backend=False
        )

    if kind == "anthropic":
        # HARD CONSTRAINT (see module docstring): a Claude Pro/Max subscription
        # OAuth token must NEVER reach AnthropicClient — that traffic stays on
        # the delegated `claude` CLI engine. Only a real API key on the provider
        # row is acceptable here.
        auth_mode = (row.get("auth_mode") or "").strip().lower()
        if auth_mode in _SUBSCRIPTION_AUTH_MODES:
            raise ProviderCredentialError(
                f"Model provider {label} uses subscription auth "
                f"({auth_mode!r}), which cannot drive the direct Anthropic API. "
                "Claude subscription sessions must run on the delegated Claude "
                "CLI engine; use an Anthropic API key provider for the native "
                "path."
            )

    api_key = _decrypt_auth_token(row)
    if not api_key:
        raise ProviderCredentialError(
            f"Model provider {label} has no credentials configured — add an API "
            "key for it (or connect its ChatGPT or Grok account) before starting "
            "a session on it."
        )
    base_url = (row.get("base_url") or "").strip() or None
    if kind == "openai_responses" and _is_xai_api(row) and not base_url:
        base_url = "https://api.x.ai/v1"
    return ProviderCredentials(api_key=api_key, base_url=base_url)
