"""Client for Claude subscription usage resets (weekly and 5-hour).

Claude offers two kinds of reset, both claimed through one endpoint and read
from the OAuth usage endpoint the Claude Code CLI uses (contract read from CLI
2.1.287):

* Weekly resets (program ``cedar_ember``): banked grants, each with a number
  of resets left and a use-by date. A claim names the grant and carries a
  request id, so a retried claim cannot spend a second reset.
  ``GET /api/oauth/usage?cedar_ember=1&skip_spend=1`` returns a
  ``cedar_ember`` block; ``POST /api/organizations/{org}/reset_rate_limits``
  with ``{"program": "cedar_ember", "grant_id", "request_id"}`` claims one.
* The 5-hour reset (program ``juniper_tide``): clears the current session
  window, a set number of times per week, and counts against the weekly
  limit. ``GET /api/oauth/usage?at_wall=1&skip_spend=1`` returns a
  ``juniper_tide`` block; the claim body is ``{"program": "juniper_tide"}``.

Credentials: the user signs in to the stock Claude Code CLI. The org id comes
from ``claude auth status``; the access token is read from the CLI's own
credential file, only when the usage tab asks for reset status or the user
presses Reset. It is never refreshed, written, cached, logged or returned.
An expired token is reported as such and the user is told to run a Claude
turn, which lets the CLI refresh its own sign-in. On macOS the CLI keeps the
token in the Keychain, which this module does not read.

This module is an isolated network and contract boundary: URLs are fixed to
``api.anthropic.com``, redirects are not followed, and upstream bodies never
appear in errors.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.core.http_client import get_http_client

API_BASE = "https://api.anthropic.com"
WEEKLY_STATUS_URL = f"{API_BASE}/api/oauth/usage?cedar_ember=1&skip_spend=1"
SESSION_STATUS_URL = f"{API_BASE}/api/oauth/usage?at_wall=1&skip_spend=1"
OAUTH_BETA = "oauth-2025-04-20"
PROFILE_SCOPE = "user:profile"

WEEKLY_PROGRAM = "cedar_ember"
SESSION_PROGRAM = "juniper_tide"

STATUS_TIMEOUT = 5.0
CLAIM_TIMEOUT = 25.0
# A token this close to expiry is treated as expired: the claim could outlive it.
EXPIRY_MARGIN_SECONDS = 60

GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

WINDOW_TYPES = (
    "five_hour",
    "seven_day",
    "seven_day_overage_included",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_cowork",
    "seven_day_omelette",
    "seven_day_oauth_apps",
)
_WEEKLY_INELIGIBLE = frozenset({
    "config_off", "tier", "seat", "mobile", "surface", "cli_version", "no_grant",
    "tenure", "other_experiment", "unavailable", "unknown",
})
_WEEKLY_CLAIM_REASONS = _WEEKLY_INELIGIBLE | frozenset({
    "paused", "expired", "unknown_grant", "not_next_grant", "grant_id_required",
    "not_limited", "already_used", "cooldown", "stamp_indeterminate",
    "reset_unconfirmed",
})
_SESSION_INELIGIBLE = frozenset({
    "tier", "tenure", "surface", "mobile", "cli_version", "not_at_wall",
    "weekly_limit", "no_weekly_limit", "other_experiment", "extra_usage",
    "unavailable", "unknown",
})
_WEEKLY_RESULTS = frozenset({
    "reset", "already_used", "not_limited", "cooldown", "ineligible", "unavailable",
})
_SESSION_RESULTS = frozenset({
    "reset", "already_used", "not_limited", "ineligible", "unavailable",
})


class ClaudeResetsError(Exception):
    """Credential-free failure at the Claude reset boundary.

    ``kind`` is one of ``not_signed_in``, ``token_unavailable``,
    ``token_expired``, ``missing_scope``, ``unauthorized``, ``rate_limited``,
    ``upstream_status``, ``timeout``, ``transport``, ``invalid_contract``.
    """

    def __init__(self, kind: str, *, status_code: Optional[int] = None) -> None:
        self.kind = kind
        self.status_code = status_code
        super().__init__(kind)


# ─── Credentials (read-only) ────────────────────────────────────────────────


def credentials_path(config_dir: Optional[str]) -> Path:
    """Where the CLI keeps its sign-in: its config dir, else ~/.claude."""
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / ".credentials.json"


def read_cli_access_token(config_dir: Optional[str], *, now: Optional[float] = None) -> str:
    """The CLI's current access token, read once and never written or refreshed."""
    path = credentials_path(config_dir)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ClaudeResetsError("token_unavailable") from exc
    except (OSError, ValueError) as exc:
        raise ClaudeResetsError("token_unavailable") from exc
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        raise ClaudeResetsError("token_unavailable")
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise ClaudeResetsError("token_unavailable")
    scopes = oauth.get("scopes")
    if isinstance(scopes, list) and PROFILE_SCOPE not in scopes:
        raise ClaudeResetsError("missing_scope")
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        current = time.time() if now is None else now
        if expires_at / 1000 <= current + EXPIRY_MARGIN_SECONDS:
            raise ClaudeResetsError("token_expired")
    return token.strip()


def valid_org_id(org_id: Optional[str]) -> str:
    if not isinstance(org_id, str) or not _ORG_ID_RE.match(org_id):
        raise ClaudeResetsError("not_signed_in")
    return org_id


def _headers(access_token: str, cli_version: Optional[str], *, content_type: bool = False) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": f"claude-cli/{cli_version or 'unknown'} (external, cli)",
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


# ─── Normalizers (lenient, like the CLI: bad fields fall back, bad grants drop) ─


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _flag(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _windows(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [w for w in value if isinstance(w, str) and w in WINDOW_TYPES]


def _choice(value: Any, allowed: frozenset, fallback: Optional[str] = "unknown") -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else fallback


def _grant(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    grant_id = raw.get("id")
    resets_left = _count(raw.get("resets_left"))
    if not isinstance(grant_id, str) or not GRANT_ID_RE.match(grant_id) or resets_left is None:
        return None
    return {
        "id": grant_id,
        "label": raw.get("label") if isinstance(raw.get("label"), str) else "",
        "resets_total": _count(raw.get("resets_total")) or 0,
        "resets_left": resets_left,
        "starts_at": _text(raw.get("starts_at")),
        "ends_at": _text(raw.get("ends_at")),
        "clears": _windows(raw.get("clears")),
        "paused": _flag(raw.get("paused")),
        "usable_now": _flag(raw.get("usable_now")),
        "use_requires_limit": _flag(raw.get("use_requires_limit"), True),
        "blocking": _windows(raw.get("blocking")),
    }


def normalize_weekly(block: Any) -> Optional[Dict[str, Any]]:
    """The ``cedar_ember`` status block, or None when absent or unreadable."""
    if not isinstance(block, dict) or not isinstance(block.get("eligible"), bool):
        return None
    grants = [g for g in map(_grant, block.get("grants") or []) if g is not None] \
        if isinstance(block.get("grants"), list) else []
    next_id = block.get("next_grant_id")
    return {
        "eligible": block["eligible"],
        "ineligible_reason": _choice(block.get("ineligible_reason"), _WEEKLY_INELIGIBLE),
        "at_limit": _flag(block.get("at_limit")),
        "exhausted": _windows(block.get("exhausted")),
        "grants": grants,
        "next_grant_id": next_id if any(g["id"] == next_id for g in grants) else None,
        "weekly_resets_at": _text(block.get("weekly_resets_at")),
        "cooldown_until": _text(block.get("cooldown_until")),
    }


def normalize_session(block: Any) -> Optional[Dict[str, Any]]:
    """The ``juniper_tide`` status block, or None when absent or unreadable."""
    if not isinstance(block, dict) or not isinstance(block.get("eligible"), bool):
        return None
    per_week = _count(block.get("resets_per_week"))
    return {
        "eligible": block["eligible"],
        "ineligible_reason": _choice(block.get("ineligible_reason"), _SESSION_INELIGIBLE),
        "available": _flag(block.get("available")),
        "next_available_at": _text(block.get("next_available_at")),
        "weekly_resets_at": _text(block.get("weekly_resets_at")),
        "resets_per_week": per_week if per_week else 1,
    }


def normalize_weekly_claim(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ClaudeResetsError("invalid_contract")
    return {
        "result": _choice(payload.get("result"), _WEEKLY_RESULTS, "unavailable") or "unavailable",
        "reason": _choice(payload.get("reason"), _WEEKLY_CLAIM_REASONS),
        "resets_left": _count(payload.get("resets_left")),
        "cleared": _windows(payload.get("cleared")),
        "weekly_resets_at": _text(payload.get("weekly_resets_at")),
        "cooldown_until": _text(payload.get("cooldown_until")),
    }


def normalize_session_claim(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ClaudeResetsError("invalid_contract")
    return {
        "result": _choice(payload.get("result"), _SESSION_RESULTS, "unavailable") or "unavailable",
        "next_available_at": _text(payload.get("next_available_at")),
    }


# ─── Requests ───────────────────────────────────────────────────────────────


def _decode(response: httpx.Response) -> Dict[str, Any]:
    code = response.status_code
    if code in (401, 403):
        raise ClaudeResetsError("unauthorized", status_code=code)
    if code == 429:
        raise ClaudeResetsError("rate_limited", status_code=code)
    if not 200 <= code < 300:
        raise ClaudeResetsError("upstream_status", status_code=code)
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - sanitize malformed upstream bodies
        raise ClaudeResetsError("invalid_contract") from exc
    # The usage endpoint reports some errors in-band as an empty object.
    if not isinstance(body, dict) or not body:
        raise ClaudeResetsError("invalid_contract")
    return body


async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
    try:
        response = await client.request(method, url, follow_redirects=False, **kwargs)
    except httpx.TimeoutException as exc:
        raise ClaudeResetsError("timeout") from exc
    except httpx.RequestError as exc:
        raise ClaudeResetsError("transport") from exc
    return _decode(response)


async def get_status(
    access_token: str,
    cli_version: Optional[str],
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Weekly and 5-hour reset status, read in parallel."""
    http = client or get_http_client()
    headers = _headers(access_token, cli_version)
    weekly, session = await asyncio.gather(
        _send(http, "GET", WEEKLY_STATUS_URL, headers=headers, timeout=STATUS_TIMEOUT),
        _send(http, "GET", SESSION_STATUS_URL, headers=headers, timeout=STATUS_TIMEOUT),
    )
    return {
        "weekly": normalize_weekly(weekly.get(WEEKLY_PROGRAM)),
        "session": normalize_session(session.get(SESSION_PROGRAM)),
    }


def _claim_url(org_id: str) -> str:
    return f"{API_BASE}/api/organizations/{valid_org_id(org_id)}/reset_rate_limits"


async def claim_weekly(
    access_token: str,
    cli_version: Optional[str],
    org_id: str,
    *,
    grant_id: str,
    request_id: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Spend one reset from a weekly grant. ``request_id`` makes retries safe."""
    if not GRANT_ID_RE.match(grant_id) or not REQUEST_ID_RE.match(request_id):
        raise ValueError("malformed grant or request id")
    body = await _send(
        client or get_http_client(),
        "POST",
        _claim_url(org_id),
        headers=_headers(access_token, cli_version, content_type=True),
        json={"program": WEEKLY_PROGRAM, "grant_id": grant_id, "request_id": request_id},
        timeout=CLAIM_TIMEOUT,
    )
    return normalize_weekly_claim(body)


async def claim_session(
    access_token: str,
    cli_version: Optional[str],
    org_id: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Use the 5-hour reset. A repeat answers ``already_used``."""
    body = await _send(
        client or get_http_client(),
        "POST",
        _claim_url(org_id),
        headers=_headers(access_token, cli_version, content_type=True),
        json={"program": SESSION_PROGRAM},
        timeout=CLAIM_TIMEOUT,
    )
    return normalize_session_claim(body)
