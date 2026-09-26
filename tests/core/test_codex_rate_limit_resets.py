"""Reset-credit client: contract validation and a credential-free error boundary.

Network calls go through ``httpx.MockTransport``; nothing leaves the process.
"""

import json

import httpx
import pytest

from app.core import codex_rate_limit_resets as crl
from app.core.codex_rate_limit_resets import CodexResetCreditsError

GRANTED = "2026-01-02T03:04:05Z"
GRANTED_TS = 1767323045


def _credit(**over):
    base = {"id": "c1", "reset_type": "codex_rate_limits", "status": "available", "granted_at": GRANTED}
    base.update(over)
    return base


def _kind(fn, *a):
    with pytest.raises(CodexResetCreditsError) as exc:
        fn(*a)
    return exc.value.kind


# ---------------------------------------------------------------------------
# Inventory contract
# ---------------------------------------------------------------------------


def test_inventory_normalizes_unknowns_and_times():
    out = crl.normalize_inventory({
        "available_count": 2,
        "credits": [
            _credit(expires_at="2026-01-03T03:04:05+00:00", title="T", description=None),
            _credit(id="c2", reset_type="something_new", status="pending_review"),
        ],
    })
    assert out["available_count"] == 2
    first, second = out["credits"]
    assert first == {
        "id": "c1", "reset_type": "codex_rate_limits", "status": "available",
        "granted_at": GRANTED_TS, "expires_at": GRANTED_TS + 86400,
        "title": "T", "description": None,
    }
    assert second["reset_type"] == "unknown" and second["status"] == "unknown"
    assert second["expires_at"] is None


@pytest.mark.parametrize("payload", [
    None,
    [],
    {"credits": []},
    {"available_count": -1, "credits": []},
    {"available_count": True, "credits": []},
    {"available_count": "3", "credits": []},
    {"available_count": 0, "credits": {}},
    {"available_count": 0, "credits": ["x"]},
    {"available_count": 0, "credits": [_credit(id="")]},
    {"available_count": 0, "credits": [_credit(id=5)]},
    {"available_count": 0, "credits": [_credit(reset_type=None)]},
    {"available_count": 0, "credits": [_credit(status=1)]},
    {"available_count": 0, "credits": [_credit(granted_at=None)]},
    {"available_count": 0, "credits": [_credit(granted_at="")]},
    {"available_count": 0, "credits": [_credit(granted_at="yesterday")]},
    # A naive timestamp has no defined instant.
    {"available_count": 0, "credits": [_credit(granted_at="2026-01-02T03:04:05")]},
    {"available_count": 0, "credits": [_credit(expires_at=12345)]},
    {"available_count": 0, "credits": [_credit(title=3)]},
    {"available_count": 0, "credits": [_credit(description=["x"])]},
])
def test_inventory_rejects_contract_violations(payload):
    assert _kind(crl.normalize_inventory, payload) == "invalid_contract"


# ---------------------------------------------------------------------------
# Consume contract
# ---------------------------------------------------------------------------


def test_consume_outcomes():
    assert crl.normalize_consume({"code": "reset", "windows_reset": 2}) == {"outcome": "reset", "windows_reset": 2}
    assert crl.normalize_consume({"code": "no_credit"}) == {"outcome": "no_credit", "windows_reset": 0}


@pytest.mark.parametrize("payload", [
    "reset",
    {"code": "exploded"},
    {"code": None},
    {"code": "reset", "windows_reset": -1},
    {"code": "reset", "windows_reset": False},
    {"code": "reset", "windows_reset": 1.5},
])
def test_consume_rejects_contract_violations(payload):
    assert _kind(crl.normalize_consume, payload) == "invalid_contract"


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def test_headers_account_and_content_type():
    h = crl._headers("tok", "acct-1", content_type=True)
    assert h["Authorization"] == "Bearer tok"
    assert h["ChatGPT-Account-Id"] == "acct-1"
    assert h["Content-Type"] == "application/json"
    assert h["User-Agent"] == crl.CODEX_USER_AGENT
    assert "ChatGPT-Account-Id" not in crl._headers("tok", "   ")
    assert "ChatGPT-Account-Id" not in crl._headers("tok", None)
    assert "Content-Type" not in crl._headers("tok", None)


def test_unauthorized_flag():
    assert CodexResetCreditsError("upstream_status", status_code=401).is_unauthorized
    assert not CodexResetCreditsError("upstream_status", status_code=403).is_unauthorized
    assert not CodexResetCreditsError("timeout").is_unauthorized


# ---------------------------------------------------------------------------
# Network boundary
# ---------------------------------------------------------------------------


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_get_reset_credits_success_sends_expected_request():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["acct"] = request.headers.get("chatgpt-account-id")
        return httpx.Response(200, json={"available_count": 0, "credits": []})

    async with _client(handler) as client:
        out = await crl.get_reset_credits("tok", "acct", client=client)
    assert out == {"available_count": 0, "credits": []}
    assert seen == {"url": crl.RESET_CREDITS_URL, "auth": "Bearer tok", "acct": "acct"}


async def test_consume_sends_idempotency_key():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["ctype"] = request.headers.get("content-type")
        return httpx.Response(200, json={"code": "already_redeemed"})

    async with _client(handler) as client:
        out = await crl.consume_reset_credit(
            "tok", None, credit_id="c1", idempotency_key="idem-1", client=client
        )
    assert out == {"outcome": "already_redeemed", "windows_reset": 0}
    assert seen["url"] == crl.CONSUME_RESET_CREDIT_URL
    assert seen["body"] == {"credit_id": "c1", "redeem_request_id": "idem-1"}
    assert seen["ctype"] == "application/json"


@pytest.mark.parametrize("call", ["get", "consume"])
async def test_status_timeout_transport_and_bad_body(call):
    async def invoke(handler):
        async with _client(handler) as client:
            if call == "get":
                return await crl.get_reset_credits("tok", None, client=client)
            return await crl.consume_reset_credit(
                "tok", None, credit_id="c", idempotency_key="k", client=client
            )

    with pytest.raises(CodexResetCreditsError) as exc:
        await invoke(lambda r: httpx.Response(401, text="secret upstream body"))
    assert exc.value.kind == "upstream_status" and exc.value.is_unauthorized
    assert "secret" not in str(exc.value)

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(CodexResetCreditsError) as exc:
        await invoke(timeout)
    assert exc.value.kind == "timeout"

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(CodexResetCreditsError) as exc:
        await invoke(refused)
    assert exc.value.kind == "transport"

    with pytest.raises(CodexResetCreditsError) as exc:
        await invoke(lambda r: httpx.Response(200, text="<html>not json</html>"))
    assert exc.value.kind == "invalid_contract"
