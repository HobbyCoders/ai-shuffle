"""Claude reset client: read-only token access, lenient contract, safe errors.

Network calls go through ``httpx.MockTransport``; nothing leaves the process.
"""

import json
import os

import httpx
import pytest

from app.core import claude_rate_limit_resets as crr
from app.core.claude_rate_limit_resets import ClaudeResetsError

ORG = "0b8c2f1e-1111-2222-3333-444455556666"
NOW = 1_800_000_000


def _write_creds(path, **oauth):
    body = {"accessToken": "tok-123", "expiresAt": (NOW + 3600) * 1000,
            "scopes": ["user:inference", "user:profile"], "refreshToken": "never-sent"}
    body.update(oauth)
    path.write_text(json.dumps({"claudeAiOauth": body}), encoding="utf-8")
    return path


def _kind(fn, *a, **kw):
    with pytest.raises(ClaudeResetsError) as exc:
        fn(*a, **kw)
    return exc.value.kind


# ---------------------------------------------------------------------------
# Token reads
# ---------------------------------------------------------------------------


def test_reads_the_cli_token_without_touching_the_file(tmp_path):
    creds = _write_creds(tmp_path / ".credentials.json")
    before = (creds.read_bytes(), os.stat(creds).st_mtime_ns)
    assert crr.read_cli_access_token(str(tmp_path), now=NOW) == "tok-123"
    assert (creds.read_bytes(), os.stat(creds).st_mtime_ns) == before


def test_config_dir_falls_back_to_env_then_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert crr.credentials_path(None) == tmp_path / "cfg" / ".credentials.json"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert crr.credentials_path(None).parent.name == ".claude"
    assert crr.credentials_path(str(tmp_path)) == tmp_path / ".credentials.json"


def test_expired_token_is_reported_not_refreshed(tmp_path):
    _write_creds(tmp_path / ".credentials.json", expiresAt=(NOW + 30) * 1000)
    assert _kind(crr.read_cli_access_token, str(tmp_path), now=NOW) == "token_expired"


def test_token_without_profile_scope(tmp_path):
    _write_creds(tmp_path / ".credentials.json", scopes=["user:inference"])
    assert _kind(crr.read_cli_access_token, str(tmp_path), now=NOW) == "missing_scope"


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"claudeAiOauth": {}}',
                                     '{"claudeAiOauth": {"accessToken": ""}}'])
def test_unreadable_sign_in(tmp_path, content):
    if content is not None:
        (tmp_path / ".credentials.json").write_text(content, encoding="utf-8")
    assert _kind(crr.read_cli_access_token, str(tmp_path), now=NOW) == "token_unavailable"


def test_error_never_carries_the_token(tmp_path):
    _write_creds(tmp_path / ".credentials.json", expiresAt=1)
    with pytest.raises(ClaudeResetsError) as exc:
        crr.read_cli_access_token(str(tmp_path), now=NOW)
    assert "tok-123" not in repr(exc.value) and "tok-123" not in str(exc.value)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def _grant(**over):
    base = {"id": "launch_2026", "label": "Launch bonus", "resets_total": 3, "resets_left": 2,
            "ends_at": "2026-10-30T00:00:00Z", "clears": ["seven_day", "five_hour", "bogus"],
            "paused": False, "usable_now": True, "use_requires_limit": True, "blocking": []}
    base.update(over)
    return base


def test_weekly_block_is_lenient():
    out = crr.normalize_weekly({
        "eligible": True, "at_limit": True, "exhausted": ["seven_day", "x"],
        "grants": [_grant(), _grant(id="BAD ID"), {"id": "nocount"}, "junk"],
        "next_grant_id": "launch_2026", "weekly_resets_at": "2026-10-05T00:00:00Z",
        "ineligible_reason": "brand_new_reason",
    })
    assert [g["id"] for g in out["grants"]] == ["launch_2026"]
    assert out["grants"][0]["clears"] == ["seven_day", "five_hour"]
    assert out["exhausted"] == ["seven_day"]
    assert out["next_grant_id"] == "launch_2026"
    assert out["ineligible_reason"] == "unknown"


def test_weekly_next_grant_must_be_listed():
    out = crr.normalize_weekly({"eligible": True, "grants": [_grant()], "next_grant_id": "other"})
    assert out["next_grant_id"] is None


@pytest.mark.parametrize("block", [None, {}, {"eligible": "yes"}, []])
def test_missing_blocks_read_as_none(block):
    assert crr.normalize_weekly(block) is None
    assert crr.normalize_session(block) is None


def test_session_block():
    out = crr.normalize_session({"eligible": True, "available": True, "resets_per_week": 0,
                                 "next_available_at": None, "ineligible_reason": None})
    assert out == {"eligible": True, "ineligible_reason": None, "available": True,
                   "next_available_at": None, "weekly_resets_at": None, "resets_per_week": 1}


def test_claim_results_fall_back_to_unavailable():
    assert crr.normalize_weekly_claim({"result": "exploded"})["result"] == "unavailable"
    assert crr.normalize_session_claim({"result": "reset"})["result"] == "reset"
    with pytest.raises(ClaudeResetsError):
        crr.normalize_weekly_claim([])


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_status_reads_both_programs():
    seen = []

    def handler(request):
        seen.append(request)
        if "cedar_ember=1" in str(request.url):
            return httpx.Response(200, json={"cedar_ember": {"eligible": True, "grants": [_grant()]}})
        return httpx.Response(200, json={"juniper_tide": {"eligible": True, "available": True}})

    async with _client(handler) as client:
        out = await crr.get_status("tok", "2.1.287", client=client)
    assert out["weekly"]["grants"][0]["id"] == "launch_2026"
    assert out["session"]["available"] is True
    assert {r.url.host for r in seen} == {"api.anthropic.com"}
    for r in seen:
        assert r.headers["authorization"] == "Bearer tok"
        assert r.headers["anthropic-beta"] == crr.OAUTH_BETA
        assert r.headers["user-agent"].startswith("claude-cli/2.1.287")


@pytest.mark.asyncio
async def test_weekly_claim_sends_grant_and_request_id():
    bodies = []

    def handler(request):
        bodies.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"result": "reset", "resets_left": 1, "cleared": ["seven_day"]})

    async with _client(handler) as client:
        out = await crr.claim_weekly("tok", None, ORG, grant_id="launch_2026", request_id="abc-123",
                                     client=client)
    assert bodies == [("POST", f"/api/organizations/{ORG}/reset_rate_limits",
                       {"program": "cedar_ember", "grant_id": "launch_2026", "request_id": "abc-123"})]
    assert out["result"] == "reset" and out["resets_left"] == 1 and out["cleared"] == ["seven_day"]


@pytest.mark.asyncio
async def test_session_claim_body():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"result": "already_used", "next_available_at": "2026-10-09T00:00:00Z"})

    async with _client(handler) as client:
        out = await crr.claim_session("tok", None, ORG, client=client)
    assert bodies == [{"program": "juniper_tide"}]
    assert out == {"result": "already_used", "next_available_at": "2026-10-09T00:00:00Z"}


@pytest.mark.asyncio
@pytest.mark.parametrize("code,kind", [(401, "unauthorized"), (403, "unauthorized"),
                                       (429, "rate_limited"), (500, "upstream_status"),
                                       (302, "upstream_status")])
async def test_http_errors(code, kind):
    def handler(request):
        return httpx.Response(code, headers={"location": "https://evil.example/"}, text="secret body")

    async with _client(handler) as client:
        with pytest.raises(ClaudeResetsError) as exc:
            await crr.claim_session("tok", None, ORG, client=client)
    assert exc.value.kind == kind and "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_in_band_empty_body_is_a_contract_error():
    async with _client(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(ClaudeResetsError) as exc:
            await crr.get_status("tok", None, client=client)
    assert exc.value.kind == "invalid_contract"


@pytest.mark.asyncio
async def test_transport_and_timeout():
    def boom(request):
        raise httpx.ConnectError("down")

    def slow(request):
        raise httpx.ReadTimeout("slow")

    for handler, kind in ((boom, "transport"), (slow, "timeout")):
        async with _client(handler) as client:
            with pytest.raises(ClaudeResetsError) as exc:
                await crr.claim_session("tok", None, ORG, client=client)
        assert exc.value.kind == kind


@pytest.mark.asyncio
async def test_malformed_ids_never_reach_the_network():
    def handler(request):  # pragma: no cover - must not run
        raise AssertionError("sent")

    async with _client(handler) as client:
        with pytest.raises(ValueError):
            await crr.claim_weekly("tok", None, ORG, grant_id="../x", request_id="a", client=client)
        with pytest.raises(ClaudeResetsError) as exc:
            await crr.claim_session("tok", None, "../../admin", client=client)
    assert exc.value.kind == "not_signed_in"


def test_module_never_writes_or_refreshes():
    source = open(crr.__file__, encoding="utf-8").read()
    for forbidden in ("refreshToken", "refresh_token", "write_text", '"w"', "'w'", "oauth/token"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# Meters after a reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_refills_only_windows_on_screen(monkeypatch):
    from app.core.usage_limits import UsageLimitsMonitor

    monitor = UsageLimitsMonitor()
    monitor._windows = [
        {"type": "five_hour", "utilization": 0.0, "resets_at": "2026-10-02T20:00:00Z"},
        {"type": "seven_day", "utilization": 3.0, "resets_at": "2026-10-05T00:00:00Z"},
        {"type": "seven_day_opus", "utilization": 10.0, "resets_at": "2026-10-06T00:00:00Z"},
    ]
    applied = []

    async def _apply(window_type, used, resets_at, status=None):
        applied.append((window_type, used, resets_at, status))

    monkeypatch.setattr(monitor, "apply_live_event_and_broadcast", _apply)
    await monitor.note_reset_and_broadcast(
        ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "five_hour"],
        {"seven_day": "2026-10-12T00:00:00Z"},
    )
    assert applied == [
        ("five_hour", 0.0, None, "allowed"),
        ("seven_day", 0.0, "2026-10-12T00:00:00Z", "allowed"),
        ("seven_day_opus", 0.0, "2026-10-06T00:00:00Z", "allowed"),
    ]
