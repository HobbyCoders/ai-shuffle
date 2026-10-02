"""API coverage for Claude weekly and 5-hour resets.

The CLI status, the token read and the upstream calls are all faked; no test
reaches the network or a real Claude sign-in.
"""

import pytest

from app.core import claude_cli_status
from app.core import claude_rate_limit_resets as resets
from app.core.claude_cli_status import ClaudeCliStatus
from app.core.claude_rate_limit_resets import ClaudeResetsError
from app.core.usage_limits import usage_limits_monitor

ROOT = "/api/v1/usage/claude/resets"
ORG = "0b8c2f1e-1111-2222-3333-444455556666"


@pytest.fixture
def cli(monkeypatch):
    status = ClaudeCliStatus(signed_in=True, cli_version="2.1.287", org_id=ORG, config_dir="/cfg")
    calls = {"invalidated": 0, "token_dirs": [], "noted": []}

    async def _status(force=False):
        return status

    def _invalidate():
        calls["invalidated"] += 1

    def _token(config_dir, **_):
        calls["token_dirs"].append(config_dir)
        return "tok"

    async def _note(window_types, resets_at=None):
        calls["noted"].append((list(window_types), resets_at))

    monkeypatch.setattr(claude_cli_status, "get_status_async", _status)
    monkeypatch.setattr(claude_cli_status, "invalidate", _invalidate)
    monkeypatch.setattr(resets, "read_cli_access_token", _token)
    monkeypatch.setattr(usage_limits_monitor, "note_reset_and_broadcast", _note)
    return status, calls


def _weekly_block():
    return {"eligible": True, "ineligible_reason": None, "at_limit": True, "exhausted": ["seven_day"],
            "grants": [{"id": "launch", "label": "Launch", "resets_total": 2, "resets_left": 1,
                        "starts_at": None, "ends_at": "2026-10-30T00:00:00Z", "clears": ["seven_day"],
                        "paused": False, "usable_now": True, "use_requires_limit": True, "blocking": []}],
            "next_grant_id": "launch", "weekly_resets_at": "2026-10-05T00:00:00Z", "cooldown_until": None}


def test_status(authenticated_client, cli, monkeypatch):
    _, calls = cli
    seen = {}

    async def _get(token, cli_version, **_):
        seen.update(token=token, version=cli_version)
        return {"weekly": _weekly_block(), "session": None}

    monkeypatch.setattr(resets, "get_status", _get)
    res = authenticated_client.get(ROOT)
    assert res.status_code == 200
    body = res.json()
    assert body["weekly"]["grants"][0]["id"] == "launch"
    assert body["session"] is None
    assert seen == {"token": "tok", "version": "2.1.287"}
    assert calls["token_dirs"] == ["/cfg"]
    assert "tok" not in res.text


def test_signed_out_cli_never_reads_the_token(authenticated_client, cli):
    status, calls = cli
    status.signed_in = False
    res = authenticated_client.get(ROOT)
    assert res.status_code == 409
    assert "not signed in" in res.json()["detail"]
    assert calls["token_dirs"] == []
    assert calls["invalidated"] == 1


@pytest.mark.parametrize("kind,code,needle", [
    ("token_expired", 409, "Run a Claude turn"),
    ("unauthorized", 409, "Run a Claude turn"),
    ("token_unavailable", 409, "claude.ai"),
    ("missing_scope", 409, "Sign in to Claude Code again"),
    ("rate_limited", 429, "Too many"),
    ("upstream_status", 502, "unavailable"),
    ("invalid_contract", 502, "unavailable"),
])
def test_error_messages(authenticated_client, cli, monkeypatch, kind, code, needle):
    async def _fail(*_a, **_kw):
        raise ClaudeResetsError(kind, status_code=500)

    monkeypatch.setattr(resets, "claim_session", _fail)
    res = authenticated_client.post(f"{ROOT}/session")
    assert res.status_code == code
    assert needle in res.json()["detail"]


def test_weekly_claim_passes_the_key_and_refreshes_meters(authenticated_client, cli, monkeypatch):
    _, calls = cli
    seen = {}

    async def _claim(token, cli_version, org_id, *, grant_id, request_id, **_):
        seen.update(org=org_id, grant=grant_id, request=request_id)
        return {"result": "reset", "reason": None, "resets_left": 0, "cleared": ["seven_day", "five_hour"],
                "weekly_resets_at": "2026-10-12T00:00:00Z", "cooldown_until": None}

    monkeypatch.setattr(resets, "claim_weekly", _claim)
    res = authenticated_client.post(f"{ROOT}/weekly", json={"grant_id": "launch", "idempotency_key": "k-1"})
    assert res.status_code == 200
    assert res.json()["result"] == "reset"
    assert seen == {"org": ORG, "grant": "launch", "request": "k-1"}
    assert calls["noted"] == [(["seven_day", "five_hour"], {"seven_day": "2026-10-12T00:00:00Z"})]


def test_weekly_claim_without_reset_leaves_meters(authenticated_client, cli, monkeypatch):
    _, calls = cli

    async def _claim(*_a, **_kw):
        return {"result": "already_used", "reason": "already_used", "resets_left": 0, "cleared": [],
                "weekly_resets_at": None, "cooldown_until": None}

    monkeypatch.setattr(resets, "claim_weekly", _claim)
    res = authenticated_client.post(f"{ROOT}/weekly", json={"grant_id": "launch", "idempotency_key": "k-1"})
    assert res.status_code == 200 and res.json()["result"] == "already_used"
    assert calls["noted"] == []


@pytest.mark.parametrize("body", [
    {"grant_id": "../x", "idempotency_key": "k"},
    {"grant_id": "launch", "idempotency_key": "has space"},
    {"grant_id": "launch"},
    {"grant_id": "launch", "idempotency_key": "k", "extra": 1},
])
def test_weekly_claim_validates_input(authenticated_client, cli, monkeypatch, body):
    async def _claim(*_a, **_kw):  # pragma: no cover - must not run
        raise AssertionError("claimed")

    monkeypatch.setattr(resets, "claim_weekly", _claim)
    assert authenticated_client.post(f"{ROOT}/weekly", json=body).status_code == 422


def test_session_claim(authenticated_client, cli, monkeypatch):
    _, calls = cli

    async def _claim(token, cli_version, org_id, **_):
        assert org_id == ORG
        return {"result": "reset", "next_available_at": "2026-10-09T00:00:00Z"}

    monkeypatch.setattr(resets, "claim_session", _claim)
    res = authenticated_client.post(f"{ROOT}/session")
    assert res.status_code == 200
    assert res.json() == {"result": "reset", "next_available_at": "2026-10-09T00:00:00Z"}
    assert calls["noted"] == [(["five_hour"], None)]


def test_routes_need_auth(client):
    assert client.get(ROOT).status_code in (401, 403)
    assert client.post(f"{ROOT}/session").status_code in (401, 403)
