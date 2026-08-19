"""Client for SuperGrok banked usage-limit reset credits.

Isolated network/contract boundary: credentials are supplied per request,
upstream bodies and tokens are never included in errors, and the bearer token
never leaves ``cli-chat-proxy.grok.com`` (HTTPS + host pin, no redirects).

Primary inventory URL (first 200 + valid wins):
``GET https://cli-chat-proxy.grok.com/v1/billing/reset-credits``
Fallbacks: ``/v1/billing/reset-tokens``, then nested fields on
``GET /v1/billing?format=credits``.

Primary consume URL:
``POST https://cli-chat-proxy.grok.com/v1/billing/reset-credits/consume``
Fallback: ``POST /v1/billing/reset-tokens/{id}/redeem``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlsplit

import httpx

from app.core.http_client import get_http_client

GROK_BILLING_HOST = "cli-chat-proxy.grok.com"
GROK_TOKEN_AUTH = "xai-grok-cli"

RESET_CREDITS_URL = "https://cli-chat-proxy.grok.com/v1/billing/reset-credits"
RESET_TOKENS_URL = "https://cli-chat-proxy.grok.com/v1/billing/reset-tokens"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
CONSUME_RESET_CREDIT_URL = f"{RESET_CREDITS_URL}/consume"

INVENTORY_URLS: Sequence[str] = (
    RESET_CREDITS_URL,
    RESET_TOKENS_URL,
    BILLING_URL,
)

INVENTORY_TIMEOUT = 5.0
CONSUME_TIMEOUT = 10.0

DEFAULT_TITLE = "Usage limit reset"
DEFAULT_DESCRIPTION = (
    "A reset clears your weekly usage once. "
    "Resets don't stack and expire on their date."
)

_RESET_TYPES = frozenset({"grok_rate_limits"})
_CREDIT_STATUSES = frozenset({"available", "redeeming", "redeemed"})
_CREDIT_LIST_KEYS = (
    "credits",
    "tokens",
    "resetTokens",
    "usageResetTokens",
    "reset_credits",
)
_ID_KEYS = ("id", "tokenId", "token_id")
_EXPIRES_KEYS = ("expires_at", "expiresAt", "expiry", "expires_on")
_GRANTED_KEYS = ("granted_at", "grantedAt", "created_at")
_AVAILABLE_COUNT_KEYS = ("available_count", "availableCount")
_STATUS_ALIASES = {
    "available": "available",
    "active": "available",
    "unused": "available",
    "granted": "available",
    "redeeming": "redeeming",
    "consuming": "redeeming",
    "in_progress": "redeeming",
    "redeemed": "redeemed",
    "used": "redeemed",
    "consumed": "redeemed",
}
_OUTCOME_ALIASES = {
    "reset": "reset",
    "ok": "reset",
    "success": "reset",
    "redeemed": "reset",
    "already_redeemed": "already_redeemed",
    "already_used": "already_redeemed",
    "no_credit": "no_credit",
    "not_found": "no_credit",
    "nothing_to_reset": "nothing_to_reset",
    "no_usage": "nothing_to_reset",
}


class GrokResetCreditsError(Exception):
    """Credential-free failure at the Grok reset-credit network boundary."""

    def __init__(self, kind: str, *, status_code: Optional[int] = None) -> None:
        self.kind = kind
        self.status_code = status_code
        super().__init__(kind)

    @property
    def is_unauthorized(self) -> bool:
        return self.status_code == 401


def _contract_error() -> GrokResetCreditsError:
    return GrokResetCreditsError("invalid_contract")


def _url_is_pinned(url: str) -> bool:
    """HTTPS + host pin — the bearer token must never leave cli-chat-proxy.grok.com."""
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == GROK_BILLING_HOST


def _headers(access_token: str, *, content_type: bool = False) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-XAI-Token-Auth": GROK_TOKEN_AUTH,
        "Accept": "application/json",
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _optional_text(value: Any) -> Optional[str]:
    if value is None or not isinstance(value, str):
        return None
    return value


def _rfc3339_to_unix(value: str) -> Optional[int]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _to_unix(value: Any, *, missing: Optional[int]) -> Optional[int]:
    """Parse RFC3339 or unix seconds. ``missing`` is returned when absent/unparseable."""
    if value is None or isinstance(value, bool):
        return missing
    if isinstance(value, (int, float)):
        ts = int(value)
        if ts > 10_000_000_000:
            ts //= 1000
        return ts
    if isinstance(value, str) and value:
        stripped = value.strip()
        if stripped.isdigit() or (
            stripped.startswith("-") and stripped[1:].isdigit()
        ):
            return _to_unix(int(stripped), missing=missing)
        parsed = _rfc3339_to_unix(stripped)
        return parsed if parsed is not None else missing
    return missing


def _first_present(payload: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _credit_list_from(payload: Dict[str, Any]) -> Optional[List[Any]]:
    for key in _CREDIT_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return None


def _looks_like_inventory(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if any(key in payload for key in _AVAILABLE_COUNT_KEYS):
        return True
    if _credit_list_from(payload) is not None:
        return True
    cfg = payload.get("config")
    if isinstance(cfg, dict):
        if any(key in cfg for key in _AVAILABLE_COUNT_KEYS):
            return True
        if _credit_list_from(cfg) is not None:
            return True
    return False


def _unwrap_inventory(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Any]]:
    lst = _credit_list_from(payload)
    if lst is not None:
        return payload, lst
    cfg = payload.get("config")
    if isinstance(cfg, dict):
        lst = _credit_list_from(cfg)
        if lst is not None:
            return cfg, lst
        return cfg, []
    return payload, []


def _read_available_count(
    container: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[int]:
    for source in (container, payload, payload.get("config")):
        if not isinstance(source, dict):
            continue
        for key in _AVAILABLE_COUNT_KEYS:
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _contract_error()
            return value
    return None


def _normalize_status(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return "available"
    mapped = _STATUS_ALIASES.get(raw.lower())
    if mapped:
        return mapped
    if raw in _CREDIT_STATUSES:
        return raw
    return "unknown"


def _normalize_reset_type(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return "grok_rate_limits"
    if raw in _RESET_TYPES:
        return raw
    return "unknown"


def _normalize_credit(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    credit_id = _first_present(raw, _ID_KEYS)
    if isinstance(credit_id, (int, float)) and not isinstance(credit_id, bool):
        credit_id = str(int(credit_id))
    if not isinstance(credit_id, str) or not credit_id:
        return None

    title = _optional_text(_first_present(raw, ("title", "name")))
    description = _optional_text(_first_present(raw, ("description",)))
    granted_at = _to_unix(_first_present(raw, _GRANTED_KEYS), missing=0)
    return {
        "id": credit_id,
        "reset_type": _normalize_reset_type(
            _first_present(raw, ("reset_type", "resetType", "type"))
        ),
        "status": _normalize_status(_first_present(raw, ("status",))),
        "granted_at": 0 if granted_at is None else granted_at,
        "expires_at": _to_unix(_first_present(raw, _EXPIRES_KEYS), missing=None),
        "title": title.strip() if title and title.strip() else DEFAULT_TITLE,
        "description": (
            description.strip()
            if description and description.strip()
            else DEFAULT_DESCRIPTION
        ),
    }


def normalize_inventory(payload: Any) -> Dict[str, Any]:
    """Validate and normalize reset-credit inventory into the public contract."""
    if not isinstance(payload, dict):
        raise _contract_error()

    container, raw_credits = _unwrap_inventory(payload)
    credits: List[Dict[str, Any]] = []
    for raw in raw_credits:
        credit = _normalize_credit(raw)
        if credit is not None:
            credits.append(credit)

    available_count = _read_available_count(container, payload)
    if available_count is None:
        available_count = sum(1 for c in credits if c["status"] == "available")

    return {"available_count": available_count, "credits": credits}


def _map_outcome(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("code", "outcome", "status", "result"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            mapped = _OUTCOME_ALIASES.get(value.lower())
            if mapped:
                return mapped
    if payload.get("success") is True:
        return "reset"
    error = payload.get("error")
    if isinstance(error, str) and error:
        return _OUTCOME_ALIASES.get(error.lower())
    return None


def _read_windows_reset(payload: Dict[str, Any]) -> int:
    value = payload.get("windows_reset", payload.get("windowsReset", 0))
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def normalize_consume(payload: Any) -> Dict[str, Any]:
    """Validate and normalize a reset-credit consume response."""
    if not isinstance(payload, dict):
        raise _contract_error()
    outcome = _map_outcome(payload)
    if outcome is None:
        raise _contract_error()
    return {"outcome": outcome, "windows_reset": _read_windows_reset(payload)}


def _decode_response(response: httpx.Response) -> Any:
    if not 200 <= response.status_code < 300:
        raise GrokResetCreditsError(
            "upstream_status", status_code=response.status_code
        )
    if response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001 - sanitize malformed upstream bodies
        raise _contract_error() from exc


async def _send(
    http_client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    access_token: str,
    timeout: float,
    json_body: Optional[Dict[str, Any]] = None,
) -> httpx.Response:
    if not _url_is_pinned(url):
        raise GrokResetCreditsError("transport")
    headers = _headers(access_token, content_type=json_body is not None)
    try:
        kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": timeout,
            "follow_redirects": False,
        }
        if method == "GET":
            return await http_client.get(url, **kwargs)
        return await http_client.post(url, json=json_body, **kwargs)
    except httpx.TimeoutException as exc:
        raise GrokResetCreditsError("timeout") from exc
    except httpx.RequestError as exc:
        raise GrokResetCreditsError("transport") from exc


async def get_reset_credits(
    access_token: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Fetch and normalize the current reset-credit inventory."""
    http_client = client or get_http_client()
    last_error: Optional[GrokResetCreditsError] = None
    last_index = len(INVENTORY_URLS) - 1
    for index, url in enumerate(INVENTORY_URLS):
        try:
            response = await _send(
                http_client,
                "GET",
                url,
                access_token=access_token,
                timeout=INVENTORY_TIMEOUT,
            )
            payload = _decode_response(response)
        except GrokResetCreditsError as exc:
            if exc.is_unauthorized:
                raise
            last_error = exc
            continue
        if not _looks_like_inventory(payload):
            if index == last_index:
                return {"available_count": 0, "credits": []}
            last_error = _contract_error()
            continue
        try:
            return normalize_inventory(payload)
        except GrokResetCreditsError as exc:
            last_error = exc
            continue
    raise last_error or _contract_error()


def _consume_urls(credit_id: str) -> Sequence[str]:
    encoded = quote(credit_id, safe="")
    return (
        CONSUME_RESET_CREDIT_URL,
        f"{RESET_TOKENS_URL}/{encoded}/redeem",
    )


async def consume_reset_credit(
    access_token: str,
    *,
    credit_id: str,
    idempotency_key: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Consume one specific reset credit and normalize the outcome."""
    http_client = client or get_http_client()
    body = {
        "credit_id": credit_id,
        "token_id": credit_id,
        "id": credit_id,
        "redeem_request_id": idempotency_key,
        "idempotency_key": idempotency_key,
    }
    last_error: Optional[GrokResetCreditsError] = None
    for url in _consume_urls(credit_id):
        try:
            response = await _send(
                http_client,
                "POST",
                url,
                access_token=access_token,
                timeout=CONSUME_TIMEOUT,
                json_body=body,
            )
            payload = _decode_response(response)
        except GrokResetCreditsError as exc:
            if exc.is_unauthorized:
                raise
            last_error = exc
            continue
        if payload == {}:
            return {"outcome": "reset", "windows_reset": 0}
        try:
            return normalize_consume(payload)
        except GrokResetCreditsError as exc:
            last_error = exc
            continue
    raise last_error or _contract_error()
