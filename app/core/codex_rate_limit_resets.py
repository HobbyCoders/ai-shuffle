"""Client for the official ChatGPT/Codex rate-limit reset-credit endpoints.

This module is an isolated network and contract boundary. Endpoint URLs are
fixed, credentials are supplied by the caller for one request, and upstream
bodies and headers are never included in errors.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import httpx

from app.core.http_client import get_http_client
from app.core.translator_gateway.upstream import CODEX_USER_AGENT

RESET_CREDITS_URL = (
    "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
)
CONSUME_RESET_CREDIT_URL = f"{RESET_CREDITS_URL}/consume"
INVENTORY_TIMEOUT = 5.0
CONSUME_TIMEOUT = 10.0

_RESET_TYPES = frozenset({"codex_rate_limits"})
_CREDIT_STATUSES = frozenset({"available", "redeeming", "redeemed"})
_CONSUME_OUTCOMES = frozenset(
    {"reset", "nothing_to_reset", "no_credit", "already_redeemed"}
)


class CodexResetCreditsError(Exception):
    """Credential-free failure at the reset-credit network boundary."""

    def __init__(self, kind: str, *, status_code: Optional[int] = None) -> None:
        self.kind = kind
        self.status_code = status_code
        super().__init__(kind)

    @property
    def is_unauthorized(self) -> bool:
        return self.status_code == 401


def _headers(
    access_token: str,
    account_id: Optional[str],
    *,
    content_type: bool = False,
) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": CODEX_USER_AGENT,
    }
    if isinstance(account_id, str) and account_id.strip():
        headers["ChatGPT-Account-Id"] = account_id
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _contract_error() -> CodexResetCreditsError:
    return CodexResetCreditsError("invalid_contract")


def _required_nonnegative_int(payload: Dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _contract_error()
    return value


def _optional_text(payload: Dict[str, Any], key: str) -> Optional[str]:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise _contract_error()
    return value


def _rfc3339_to_unix(value: Any) -> int:
    if not isinstance(value, str) or not value:
        raise _contract_error()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise _contract_error() from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _contract_error()
    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError) as exc:
        raise _contract_error() from exc


def normalize_inventory(payload: Any) -> Dict[str, Any]:
    """Validate and normalize the reset-credit inventory contract."""
    if not isinstance(payload, dict):
        raise _contract_error()

    available_count = _required_nonnegative_int(payload, "available_count")
    raw_credits = payload.get("credits")
    if not isinstance(raw_credits, list):
        raise _contract_error()

    credits = []
    for raw in raw_credits:
        if not isinstance(raw, dict):
            raise _contract_error()
        credit_id = raw.get("id")
        reset_type = raw.get("reset_type")
        credit_status = raw.get("status")
        if not isinstance(credit_id, str) or not credit_id:
            raise _contract_error()
        if not isinstance(reset_type, str) or not isinstance(credit_status, str):
            raise _contract_error()

        expires_raw = raw.get("expires_at")
        expires_at = None
        if expires_raw is not None:
            expires_at = _rfc3339_to_unix(expires_raw)

        credits.append(
            {
                "id": credit_id,
                "reset_type": (
                    reset_type if reset_type in _RESET_TYPES else "unknown"
                ),
                "status": (
                    credit_status
                    if credit_status in _CREDIT_STATUSES
                    else "unknown"
                ),
                "granted_at": _rfc3339_to_unix(raw.get("granted_at")),
                "expires_at": expires_at,
                "title": _optional_text(raw, "title"),
                "description": _optional_text(raw, "description"),
            }
        )

    return {"available_count": available_count, "credits": credits}


def normalize_consume(payload: Any) -> Dict[str, Any]:
    """Validate and normalize a reset-credit consume response."""
    if not isinstance(payload, dict):
        raise _contract_error()
    code = payload.get("code")
    if not isinstance(code, str) or code not in _CONSUME_OUTCOMES:
        raise _contract_error()
    windows_reset = payload.get("windows_reset", 0)
    if (
        isinstance(windows_reset, bool)
        or not isinstance(windows_reset, int)
        or windows_reset < 0
    ):
        raise _contract_error()
    return {"outcome": code, "windows_reset": windows_reset}


def _decode_response(response: httpx.Response) -> Any:
    if not 200 <= response.status_code < 300:
        raise CodexResetCreditsError(
            "upstream_status", status_code=response.status_code
        )
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001 - sanitize malformed upstream bodies
        raise _contract_error() from exc


async def get_reset_credits(
    access_token: str,
    account_id: Optional[str],
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Fetch and normalize the current reset-credit inventory."""
    http_client = client or get_http_client()
    try:
        response = await http_client.get(
            RESET_CREDITS_URL,
            headers=_headers(access_token, account_id),
            timeout=INVENTORY_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise CodexResetCreditsError("timeout") from exc
    except httpx.RequestError as exc:
        raise CodexResetCreditsError("transport") from exc
    return normalize_inventory(_decode_response(response))


async def consume_reset_credit(
    access_token: str,
    account_id: Optional[str],
    *,
    credit_id: str,
    idempotency_key: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Consume one specific reset credit and normalize the outcome."""
    http_client = client or get_http_client()
    try:
        response = await http_client.post(
            CONSUME_RESET_CREDIT_URL,
            headers=_headers(access_token, account_id, content_type=True),
            json={
                "credit_id": credit_id,
                "redeem_request_id": idempotency_key,
            },
            timeout=CONSUME_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise CodexResetCreditsError("timeout") from exc
    except httpx.RequestError as exc:
        raise CodexResetCreditsError("transport") from exc
    return normalize_consume(_decode_response(response))
