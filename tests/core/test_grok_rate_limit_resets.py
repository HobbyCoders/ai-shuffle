"""Unit coverage for ``app.core.grok_rate_limit_resets``.

All network I/O uses ``httpx.MockTransport``. No test reaches the live network.
Wire fixtures mirror the live gRPC-web response captured from grok.com on
2026-08-20 (one ``restok_*`` token with validity_start/validity_end).
"""

from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from app.core import grok_rate_limit_resets as grok
from app.core.grok_rate_limit_resets import (
    CONSUME_TIMEOUT,
    DEFAULT_DESCRIPTION,
    DEFAULT_TITLE,
    GET_REMAINING_RESETS_URL,
    GRPC_WEB_CONTENT_TYPE,
    INVENTORY_TIMEOUT,
    REDEEM_RESET_URL,
    GrokResetCreditsError,
    decode_reset_tokens,
    encode_redeem_request,
    frame_grpc_web,
    normalize_consume,
    normalize_tokens,
    parse_grpc_web_response,
)

ACCESS = "grok-access-token-secret"
NOW = 1_787_238_000  # 2026-08-20T15:00:00Z
START = 1_786_560_540
END = 1_789_238_940


# ---------------------------------------------------------------------------
# Wire helpers (independent encoder so the module's decoder is actually tested)
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _ld(field: int, payload: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def _timestamp(seconds: int) -> bytes:
    return _varint((1 << 3) | 0) + _varint(seconds)


def token_msg(token_id: str, start=START, end=END) -> bytes:
    body = _ld(10, token_id.encode())
    if start is not None:
        body += _ld(20, _timestamp(start))
    if end is not None:
        body += _ld(30, _timestamp(end))
    return body


def tokens_resp(*tokens: bytes) -> bytes:
    return b"".join(_ld(10, t) for t in tokens)


def trailers(status: int = 0, message: str = "") -> bytes:
    block = f"grpc-status:{status}\r\n".encode()
    if message:
        block += f"grpc-message:{message}\r\n".encode()
    return b"\x80" + len(block).to_bytes(4, "big") + block


def grpc_ok(message: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        content=frame_grpc_web(message) + trailers(0),
        headers={"content-type": GRPC_WEB_CONTENT_TYPE},
    )


def grpc_trailers_only(status: int, message: str = "") -> httpx.Response:
    """grok.com returns errors as HTTP 200 with grpc-status in the headers."""
    return httpx.Response(
        200,
        content=b"",
        headers={
            "content-type": "application/grpc",
            "grpc-status": str(status),
            "grpc-message": message,
        },
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# Byte-for-byte capture of the live GetRemainingResets body (token id trimmed).
LIVE_BODY = (
    b"\x00\x00\x00\x00#R!R\rrestok_vpYDqo\xa2\x01\x06\x08\x9c\x80\xf3\xd3\x06"
    b"\xf2\x01\x06\x08\x9c\xbd\x96\xd5\x06"
    b"\x80\x00\x00\x00\x0fgrpc-status:0\r\n"
)


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------


class TestCodec:
    def test_decodes_live_capture(self):
        messages, tr = parse_grpc_web_response(LIVE_BODY)
        assert tr == {"grpc-status": "0"}
        assert len(messages) == 1
        assert decode_reset_tokens(messages[0]) == [
            {
                "token_id": "restok_vpYDqo",
                "validity_start": START,
                "validity_end": END,
            }
        ]

    def test_roundtrip_multiple_tokens_and_missing_timestamps(self):
        msg = tokens_resp(
            token_msg("a"), token_msg("b", start=None, end=None), _ld(10, b"")
        )
        assert decode_reset_tokens(msg) == [
            {"token_id": "a", "validity_start": START, "validity_end": END},
            {"token_id": "b", "validity_start": None, "validity_end": None},
        ]

    def test_skips_unknown_fields(self):
        extra = _varint((99 << 3) | 0) + _varint(7) + _ld(98, b"zzz")
        msg = _ld(10, extra + token_msg("x")) + extra
        assert [t["token_id"] for t in decode_reset_tokens(msg)] == ["x"]

    def test_truncated_frame_is_contract_error(self):
        with pytest.raises(GrokResetCreditsError) as exc:
            parse_grpc_web_response(b"\x00\x00\x00\x00\x10abc")
        assert exc.value.kind == "invalid_contract"

    def test_truncated_varint_is_contract_error(self):
        with pytest.raises(GrokResetCreditsError):
            decode_reset_tokens(b"\xff\xff")

    def test_redeem_request_encoding(self):
        assert encode_redeem_request("restok_1") == b"R\x08restok_1"

    def test_frame(self):
        assert frame_grpc_web(b"") == b"\x00\x00\x00\x00\x00"
        assert frame_grpc_web(b"ab") == b"\x00\x00\x00\x00\x02ab"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_available_token(self):
        out = normalize_tokens(
            [{"token_id": "restok_1", "validity_start": START, "validity_end": END}],
            now=NOW,
        )
        assert out == {
            "available_count": 1,
            "credits": [
                {
                    "id": "restok_1",
                    "reset_type": "grok_rate_limits",
                    "status": "available",
                    "granted_at": START,
                    "expires_at": END,
                    "title": DEFAULT_TITLE,
                    "description": DEFAULT_DESCRIPTION,
                }
            ],
        }

    def test_expired_tokens_are_hidden_like_the_web_app(self):
        out = normalize_tokens(
            [
                {"token_id": "old", "validity_start": 1, "validity_end": NOW - 1},
                {"token_id": "edge", "validity_start": 1, "validity_end": NOW},
                {"token_id": "new", "validity_start": 1, "validity_end": NOW + 1},
            ],
            now=NOW,
        )
        assert out["available_count"] == 1
        assert [c["id"] for c in out["credits"]] == ["new"]

    def test_missing_timestamps_default(self):
        out = normalize_tokens(
            [{"token_id": "t", "validity_start": None, "validity_end": None}],
            now=NOW,
        )
        assert out["credits"][0]["granted_at"] == 0
        assert out["credits"][0]["expires_at"] is None
        assert out["available_count"] == 1

    def test_consume_reset_when_token_gone(self):
        assert normalize_consume(tokens_resp(token_msg("other")), credit_id="x") == {
            "outcome": "reset",
            "windows_reset": 1,
        }
        assert normalize_consume(b"", credit_id="x") == {
            "outcome": "reset",
            "windows_reset": 1,
        }

    def test_consume_nothing_to_reset_when_token_still_listed(self):
        assert normalize_consume(tokens_resp(token_msg("x")), credit_id="x") == {
            "outcome": "nothing_to_reset",
            "windows_reset": 0,
        }


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


class TestNetwork:
    @pytest.fixture(autouse=True)
    def fixed_inventory_clock(self, monkeypatch):
        # Keep the recorded wire fixture valid without changing expiry policy
        # or replacing the process-wide time module used by other code.
        monkeypatch.setattr(grok, "time", SimpleNamespace(time=lambda: NOW))

    def test_urls_are_pinned_to_grok_com(self):
        for url in (GET_REMAINING_RESETS_URL, REDEEM_RESET_URL):
            parts = urlsplit(url)
            assert parts.scheme == "https"
            assert parts.hostname == "grok.com"
        assert GET_REMAINING_RESETS_URL.endswith(
            "/prod_mc_billing.ConsumerUiSvc/GetRemainingResets"
        )
        assert REDEEM_RESET_URL.endswith("/prod_mc_billing.ConsumerUiSvc/RedeemReset")

    async def test_get_sends_grpc_web_post_with_bearer(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return grpc_ok(tokens_resp(token_msg("restok_1")))

        async with _client(handler) as client:
            out = await grok.get_reset_credits(ACCESS, client=client)

        assert out["available_count"] == 1
        assert out["credits"][0]["id"] == "restok_1"
        req = captured["request"]
        assert req.method == "POST"
        assert str(req.url) == GET_REMAINING_RESETS_URL
        assert req.content == b"\x00\x00\x00\x00\x00"
        assert req.headers["authorization"] == f"Bearer {ACCESS}"
        assert req.headers["content-type"] == GRPC_WEB_CONTENT_TYPE
        assert req.headers["x-grpc-web"] == "1"
        assert req.extensions["timeout"]["read"] == INVENTORY_TIMEOUT

    async def test_trailers_only_ok_is_empty_inventory(self):
        async with _client(lambda r: grpc_trailers_only(0)) as client:
            out = await grok.get_reset_credits(ACCESS, client=client)
        assert out == {"available_count": 0, "credits": []}

    async def test_empty_tokens_is_empty_inventory(self):
        async with _client(lambda r: grpc_ok(b"")) as client:
            out = await grok.get_reset_credits(ACCESS, client=client)
        assert out == {"available_count": 0, "credits": []}

    async def test_unauthenticated_grpc_status_is_unauthorized(self):
        async with _client(
            lambda r: grpc_trailers_only(16, "invalid token")
        ) as client:
            with pytest.raises(GrokResetCreditsError) as exc:
                await grok.get_reset_credits(ACCESS, client=client)
        assert exc.value.is_unauthorized
        assert exc.value.grpc_status == 16
        assert "invalid token" not in str(exc.value)

    async def test_http_401_is_unauthorized(self):
        async with _client(lambda r: httpx.Response(401, text="nope")) as client:
            with pytest.raises(GrokResetCreditsError) as exc:
                await grok.get_reset_credits(ACCESS, client=client)
        assert exc.value.is_unauthorized
        assert exc.value.status_code == 401

    async def test_other_grpc_error_is_not_unauthorized(self):
        async with _client(lambda r: grpc_trailers_only(13, "internal")) as client:
            with pytest.raises(GrokResetCreditsError) as exc:
                await grok.get_reset_credits(ACCESS, client=client)
        assert not exc.value.is_unauthorized
        assert exc.value.kind == "grpc_status"

    async def test_http_5xx_is_upstream_status(self):
        async with _client(lambda r: httpx.Response(503, text="secret")) as client:
            with pytest.raises(GrokResetCreditsError) as exc:
                await grok.get_reset_credits(ACCESS, client=client)
        assert exc.value.kind == "upstream_status"
        assert exc.value.status_code == 503
        assert "secret" not in str(exc.value)

    async def test_timeout_and_transport_errors(self):
        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        def transport(request):
            raise httpx.ConnectError("down", request=request)

        async with _client(timeout) as client:
            with pytest.raises(GrokResetCreditsError) as exc:
                await grok.get_reset_credits(ACCESS, client=client)
            assert exc.value.kind == "timeout"
        async with _client(transport) as client:
            with pytest.raises(GrokResetCreditsError) as exc:
                await grok.get_reset_credits(ACCESS, client=client)
            assert exc.value.kind == "transport"

    async def test_redirects_are_not_followed(self):
        captured = {}

        def handler(request):
            captured["follow"] = request.extensions.get("follow_redirects", None)
            return httpx.Response(
                302, headers={"location": "https://evil.invalid/"}
            )

        async with _client(handler) as client:
            with pytest.raises(GrokResetCreditsError) as exc:
                await grok.get_reset_credits(ACCESS, client=client)
        assert exc.value.status_code == 302

    async def test_consume_posts_token_id_and_maps_success(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return grpc_ok(tokens_resp())

        async with _client(handler) as client:
            out = await grok.consume_reset_credit(
                ACCESS, credit_id="restok_1", idempotency_key="idem", client=client
            )

        assert out == {"outcome": "reset", "windows_reset": 1}
        req = captured["request"]
        assert str(req.url) == REDEEM_RESET_URL
        assert req.content == frame_grpc_web(encode_redeem_request("restok_1"))
        assert req.headers["authorization"] == f"Bearer {ACCESS}"
        assert req.extensions["timeout"]["read"] == CONSUME_TIMEOUT

    @pytest.mark.parametrize(
        ("grpc_status", "outcome"),
        [(5, "no_credit"), (6, "already_redeemed"), (9, "already_redeemed")],
    )
    async def test_consume_maps_grpc_failures_to_outcomes(self, grpc_status, outcome):
        async with _client(lambda r: grpc_trailers_only(grpc_status)) as client:
            out = await grok.consume_reset_credit(
                ACCESS, credit_id="restok_1", idempotency_key="idem", client=client
            )
        assert out == {"outcome": outcome, "windows_reset": 0}

    async def test_consume_unauthenticated_propagates(self):
        async with _client(lambda r: grpc_trailers_only(16)) as client:
            with pytest.raises(GrokResetCreditsError) as exc:
                await grok.consume_reset_credit(
                    ACCESS, credit_id="restok_1", idempotency_key="idem", client=client
                )
        assert exc.value.is_unauthorized
