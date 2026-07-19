"""In-process session-token routing registry for the translator gateway (G4).

A Codex/OpenAI-subscription session is spawned with
``ANTHROPIC_BASE_URL=http://127.0.0.1:<gateway port>`` and
``ANTHROPIC_AUTH_TOKEN=<opaque routing token>``. The CLI presents that token to
the loopback gateway as ``Authorization: Bearer <token>``. This registry maps
that opaque token back to the *provider* it should route to, so the gateway can
decrypt the real upstream credentials at request time and never has to trust
anything the CLI sends about which account to use.

Security properties (per Chunk A brief / F1 / G9):
  * Tokens are ``secrets.token_urlsafe(32)`` — unguessable.
  * Tokens live only in this process's memory for the process lifetime; they are
    NEVER persisted and NEVER logged.
  * A routing token carries only a provider id (+ optional account id) — it does
    NOT carry, and this registry never stores, any credential material.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class RouteBinding:
    """What an opaque gateway token resolves to.

    ``provider_account_id`` is optional context (the Codex ``chatgpt-account-id``
    when known at mint time); the gateway re-reads authoritative credentials from
    the provider row at request time regardless.
    """

    session_id: Optional[str]
    provider_id: str
    provider_account_id: Optional[str] = None


class SessionTokenRegistry:
    """Thread-safe opaque-token <-> RouteBinding map.

    Re-minting for the same live ``session_id`` + provider returns the SAME token
    (a session re-spawn must not leak an unbounded number of live tokens); if the
    provider/account for a session changes, the stale token is dropped.
    """

    def __init__(self) -> None:
        self._by_token: Dict[str, RouteBinding] = {}
        self._by_session: Dict[str, str] = {}
        self._lock = threading.Lock()

    def mint_session_token(
        self,
        session_id: Optional[str],
        provider_id: str,
        provider_account_id: Optional[str] = None,
    ) -> str:
        """Mint (or reuse) an opaque routing token bound to ``provider_id``."""
        binding = RouteBinding(
            session_id=session_id or None,
            provider_id=provider_id,
            provider_account_id=provider_account_id,
        )
        with self._lock:
            if binding.session_id is not None:
                existing = self._by_session.get(binding.session_id)
                if existing is not None:
                    prior = self._by_token.get(existing)
                    if prior == binding:
                        return existing
                    # Provider/account for this session changed — drop the stale
                    # token so it can no longer route.
                    self._by_token.pop(existing, None)
            token = secrets.token_urlsafe(32)
            self._by_token[token] = binding
            if binding.session_id is not None:
                self._by_session[binding.session_id] = token
            return token

    def resolve(self, token: Optional[str]) -> Optional[RouteBinding]:
        """Return the binding for ``token``, or ``None`` if unknown/missing."""
        if not token:
            return None
        return self._by_token.get(token)

    def revoke_session(self, session_id: Optional[str]) -> None:
        """Drop any token bound to ``session_id`` (best-effort cleanup)."""
        if not session_id:
            return
        with self._lock:
            token = self._by_session.pop(session_id, None)
            if token is not None:
                self._by_token.pop(token, None)

    def clear(self) -> None:
        """Drop all bindings (test helper)."""
        with self._lock:
            self._by_token.clear()
            self._by_session.clear()


# Process-wide singleton. The gateway server and the spawn-env writer share this
# one registry instance (they run in the same process).
_REGISTRY = SessionTokenRegistry()


def get_registry() -> SessionTokenRegistry:
    return _REGISTRY


def mint_session_token(
    session_id: Optional[str],
    provider_id: str,
    provider_account_id: Optional[str] = None,
) -> str:
    return _REGISTRY.mint_session_token(session_id, provider_id, provider_account_id)


def resolve(token: Optional[str]) -> Optional[RouteBinding]:
    return _REGISTRY.resolve(token)
