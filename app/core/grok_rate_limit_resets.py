"""Client for SuperGrok banked usage-limit reset credits.

Isolated network/contract boundary: credentials are supplied per request,
upstream bodies and tokens are never included in errors, and the bearer token
never leaves ``grok.com`` (HTTPS + host pin, no redirects).

Live-verified 2026-08-20 against the grok.com web app: there is NO reset
endpoint on ``cli-chat-proxy.grok.com`` (the guessed ``/v1/billing/reset-*``
paths 404). The web UI's "Usage limit reset" card uses the gRPC-web service
``prod_mc_billing.ConsumerUiSvc`` served from ``https://grok.com``, which
accepts the same OAuth bearer the Grok CLI holds:

    POST /prod_mc_billing.ConsumerUiSvc/GetRemainingResets
        ConsumerGetRemainingResetsReq {}
        -> ConsumerGetRemainingResetsResp { repeated ConsumerResetToken tokens = 10 }
    POST /prod_mc_billing.ConsumerUiSvc/RedeemReset
        ConsumerRedeemResetReq { string token_id = 10 }
        -> ConsumerRedeemResetResp { repeated ConsumerResetToken still_redeemable = 10 }

    ConsumerResetToken {
        string token_id = 10;
        google.protobuf.Timestamp validity_start = 20;
        google.protobuf.Timestamp validity_end = 30;
    }

Messages are tiny, so the protobuf + gRPC-web framing is hand-rolled here
rather than pulling in ``protobuf``/``grpcio``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from app.core.http_client import get_http_client

GROK_RESET_HOST = "grok.com"
GROK_RESET_SERVICE = "prod_mc_billing.ConsumerUiSvc"
GET_REMAINING_RESETS_URL = (
    f"https://{GROK_RESET_HOST}/{GROK_RESET_SERVICE}/GetRemainingResets"
)
REDEEM_RESET_URL = f"https://{GROK_RESET_HOST}/{GROK_RESET_SERVICE}/RedeemReset"

GRPC_WEB_CONTENT_TYPE = "application/grpc-web+proto"

INVENTORY_TIMEOUT = 5.0
CONSUME_TIMEOUT = 10.0

DEFAULT_TITLE = "Usage limit reset"
DEFAULT_DESCRIPTION = (
    "A reset clears your weekly usage once. "
    "Resets don't stack and expire on their date."
)

# Proto field numbers (see module docstring).
_F_TOKENS = 10
_F_TOKEN_ID = 10
_F_VALIDITY_START = 20
_F_VALIDITY_END = 30
_F_TS_SECONDS = 1

# gRPC status codes we map to specific outcomes.
_GRPC_OK = 0
_GRPC_NOT_FOUND = 5
_GRPC_ALREADY_EXISTS = 6
_GRPC_PERMISSION_DENIED = 7
_GRPC_FAILED_PRECONDITION = 9
_GRPC_UNAUTHENTICATED = 16
_UNAUTHORIZED_GRPC = frozenset({_GRPC_UNAUTHENTICATED})


class GrokResetCreditsError(Exception):
    """Credential-free failure at the Grok reset-credit network boundary."""

    def __init__(
        self,
        kind: str,
        *,
        status_code: int | None = None,
        grpc_status: int | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.grpc_status = grpc_status
        super().__init__(kind)

    @property
    def is_unauthorized(self) -> bool:
        if self.status_code == 401:
            return True
        return self.grpc_status in _UNAUTHORIZED_GRPC


def _contract_error() -> GrokResetCreditsError:
    return GrokResetCreditsError("invalid_contract")


def _url_is_pinned(url: str) -> bool:
    """HTTPS + host pin — the bearer token must never leave grok.com."""
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == GROK_RESET_HOST


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": GRPC_WEB_CONTENT_TYPE,
        "Accept": GRPC_WEB_CONTENT_TYPE,
        "X-Grpc-Web": "1",
    }


# ---------------------------------------------------------------------------
# Minimal protobuf wire codec
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise _contract_error()
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _encode_bytes_field(field: int, payload: bytes) -> bytes:
    return _encode_varint((field << 3) | 2) + _encode_varint(len(payload)) + payload


def _iter_fields(data: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, value)`` for a serialized message."""
    pos = 0
    while pos < len(data):
        key, pos = _decode_varint(data, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _decode_varint(data, pos)
        elif wire == 1:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = _decode_varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise _contract_error()
        if pos > len(data):
            raise _contract_error()
        yield field, wire, value


def _decode_timestamp(data: bytes) -> int | None:
    seconds: int | None = None
    for field, wire, value in _iter_fields(data):
        if field == _F_TS_SECONDS and wire == 0:
            seconds = value
    return seconds


def _decode_reset_token(data: bytes) -> dict[str, Any] | None:
    token_id = ""
    start: int | None = None
    end: int | None = None
    for field, wire, value in _iter_fields(data):
        if wire != 2:
            continue
        if field == _F_TOKEN_ID:
            try:
                token_id = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _contract_error() from exc
        elif field == _F_VALIDITY_START:
            start = _decode_timestamp(value)
        elif field == _F_VALIDITY_END:
            end = _decode_timestamp(value)
    if not token_id:
        return None
    return {"token_id": token_id, "validity_start": start, "validity_end": end}


def decode_reset_tokens(message: bytes) -> list[dict[str, Any]]:
    """Decode the repeated ``tokens`` / ``still_redeemable`` field (= 10)."""
    tokens: list[dict[str, Any]] = []
    for field, wire, value in _iter_fields(message):
        if field == _F_TOKENS and wire == 2:
            token = _decode_reset_token(value)
            if token is not None:
                tokens.append(token)
    return tokens


def encode_redeem_request(token_id: str) -> bytes:
    return _encode_bytes_field(_F_TOKEN_ID, token_id.encode("utf-8"))


# ---------------------------------------------------------------------------
# gRPC-web framing
# ---------------------------------------------------------------------------


def frame_grpc_web(message: bytes) -> bytes:
    """Wrap one protobuf message in a gRPC-web data frame."""
    return b"\x00" + len(message).to_bytes(4, "big") + message


def _parse_trailers(block: bytes) -> dict[str, str]:
    trailers: dict[str, str] = {}
    for line in block.split(b"\r\n"):
        if not line or b":" not in line:
            continue
        name, _, value = line.partition(b":")
        trailers[name.strip().lower().decode("latin-1")] = (
            value.strip().decode("latin-1")
        )
    return trailers


def parse_grpc_web_response(body: bytes) -> tuple[list[bytes], dict[str, str]]:
    """Split a gRPC-web body into data messages and parsed trailers."""
    messages: list[bytes] = []
    trailers: dict[str, str] = {}
    pos = 0
    while pos < len(body):
        if pos + 5 > len(body):
            raise _contract_error()
        flag = body[pos]
        length = int.from_bytes(body[pos + 1:pos + 5], "big")
        pos += 5
        chunk = body[pos:pos + length]
        if len(chunk) != length:
            raise _contract_error()
        pos += length
        if flag & 0x80:
            trailers.update(_parse_trailers(chunk))
        elif flag == 0:
            messages.append(chunk)
        else:
            raise _contract_error()
    return messages, trailers


def _grpc_status_from(
    response: httpx.Response, trailers: dict[str, str]
) -> int:
    raw = trailers.get("grpc-status")
    if raw is None:
        raw = response.headers.get("grpc-status")
    if raw is None:
        return _GRPC_OK
    try:
        return int(unquote(raw).strip())
    except ValueError as exc:
        raise _contract_error() from exc


def _decode_response(response: httpx.Response) -> bytes:
    """Return the single response message, raising on HTTP/gRPC failure."""
    if not 200 <= response.status_code < 300:
        raise GrokResetCreditsError(
            "upstream_status", status_code=response.status_code
        )
    messages, trailers = parse_grpc_web_response(response.content)
    grpc_status = _grpc_status_from(response, trailers)
    if grpc_status != _GRPC_OK:
        raise GrokResetCreditsError("grpc_status", grpc_status=grpc_status)
    if not messages:
        # Trailers-only OK with no message: treat as an empty message.
        return b""
    return messages[0]


# ---------------------------------------------------------------------------
# Normalization into the shared banked-reset contract
# ---------------------------------------------------------------------------


def normalize_tokens(
    tokens: list[dict[str, Any]], *, now: int | None = None
) -> dict[str, Any]:
    """Map decoded reset tokens onto the public inventory contract.

    Mirrors the grok.com web app: tokens whose ``validity_end`` has passed are
    not shown at all, and every remaining token is redeemable.
    """
    current = int(time.time()) if now is None else now
    credits: list[dict[str, Any]] = []
    for token in tokens:
        end = token.get("validity_end")
        if isinstance(end, int) and end <= current:
            continue
        start = token.get("validity_start")
        credits.append(
            {
                "id": token["token_id"],
                "reset_type": "grok_rate_limits",
                "status": "available",
                "granted_at": start if isinstance(start, int) else 0,
                "expires_at": end if isinstance(end, int) else None,
                "title": DEFAULT_TITLE,
                "description": DEFAULT_DESCRIPTION,
            }
        )
    return {"available_count": len(credits), "credits": credits}


def normalize_inventory(message: bytes) -> dict[str, Any]:
    """Decode a ``ConsumerGetRemainingResetsResp`` into the public contract."""
    return normalize_tokens(decode_reset_tokens(message))


def normalize_consume(message: bytes, *, credit_id: str) -> dict[str, Any]:
    """Decode a ``ConsumerRedeemResetResp`` into the public consume contract.

    A gRPC OK means the reset was applied; upstream returns the tokens still
    redeemable afterwards. If the redeemed id is still listed the upstream did
    not consume it, which we surface as ``nothing_to_reset``.
    """
    still = decode_reset_tokens(message)
    if any(t["token_id"] == credit_id for t in still):
        return {"outcome": "nothing_to_reset", "windows_reset": 0}
    return {"outcome": "reset", "windows_reset": 1}


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


async def _call(
    http_client: httpx.AsyncClient,
    url: str,
    *,
    access_token: str,
    message: bytes,
    timeout: float,
) -> httpx.Response:
    if not _url_is_pinned(url):
        raise GrokResetCreditsError("transport")
    try:
        return await http_client.post(
            url,
            content=frame_grpc_web(message),
            headers=_headers(access_token),
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.TimeoutException as exc:
        raise GrokResetCreditsError("timeout") from exc
    except httpx.RequestError as exc:
        raise GrokResetCreditsError("transport") from exc


async def get_reset_credits(
    access_token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch and normalize the current reset-credit inventory."""
    http_client = client or get_http_client()
    response = await _call(
        http_client,
        GET_REMAINING_RESETS_URL,
        access_token=access_token,
        message=b"",
        timeout=INVENTORY_TIMEOUT,
    )
    return normalize_inventory(_decode_response(response))


async def consume_reset_credit(
    access_token: str,
    *,
    credit_id: str,
    idempotency_key: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Redeem one specific reset token and normalize the outcome.

    ``idempotency_key`` is part of the shared API contract; the Grok RPC has
    no idempotency field, so it is accepted and ignored here (redeeming an
    already-used token id is reported by upstream as ``no_credit`` /
    ``already_redeemed``).
    """
    del idempotency_key
    http_client = client or get_http_client()
    response = await _call(
        http_client,
        REDEEM_RESET_URL,
        access_token=access_token,
        message=encode_redeem_request(credit_id),
        timeout=CONSUME_TIMEOUT,
    )
    try:
        message = _decode_response(response)
    except GrokResetCreditsError as exc:
        if exc.grpc_status == _GRPC_NOT_FOUND:
            return {"outcome": "no_credit", "windows_reset": 0}
        if exc.grpc_status in (_GRPC_ALREADY_EXISTS, _GRPC_FAILED_PRECONDITION):
            return {"outcome": "already_redeemed", "windows_reset": 0}
        raise
    return normalize_consume(message, credit_id=credit_id)
