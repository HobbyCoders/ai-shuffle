"""E1 cache-routing runner: dry run, arm contract, decision rule, arm E ingest.

Nothing here touches the network: every client talks to a MockTransport.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import httpx
import pytest

from scripts.experiments import e1_cache_routing as e1
from scripts.experiments._common import read_jsonl, responses_sse
from fluid.providers.openai_responses.serialize import build_request_body


# ─── the synthetic conversation ──────────────────────────────────────────────


def test_each_request_input_is_a_strict_prefix_of_the_next():
    conv = e1.build_conversation("A", "t", 0, start_tokens=3_000, round_tokens=200,
                                 max_tokens=10_000, static_tokens=1_000)
    first = build_request_body(conv.request(model="m", effort="low", turn_id=None))
    conv.grow()
    second = build_request_body(conv.request(model="m", effort="low", turn_id=None))
    assert second["input"][: len(first["input"])] == first["input"]
    assert len(second["input"]) == len(first["input"]) + 2
    assert first["instructions"] == second["instructions"]
    assert first["tools"] == second["tools"]
    assert first["prompt_cache_key"] == conv.session_id


def test_sessions_do_not_share_a_prefix_across_arms_or_sessions():
    a0 = e1.build_conversation("A", "t", 0, start_tokens=500, static_tokens=400)
    b0 = e1.build_conversation("B", "t", 0, start_tokens=500, static_tokens=400)
    a1 = e1.build_conversation("A", "t", 1, start_tokens=500, static_tokens=400)
    heads = {c.system.splitlines()[1] for c in (a0, b0, a1)}
    assert len(heads) == 3


def test_blocks_interleave_every_arm_each_round():
    import random

    blocks = e1.plan_blocks("ABCD", 12, 5, random.Random(0))
    counts = {a: sum(n for arm, n in blocks if arm == a) for a in "ABCD"}
    assert counts == {"A": 12, "B": 12, "C": 12, "D": 12}
    first_round = [arm for arm, _ in blocks[:4]]
    assert sorted(first_round) == ["A", "B", "C", "D"]


# ─── a client that implements the arms, for the contract check ───────────────


class SpecClient(e1.OpenAIResponsesClient):
    """Today's client plus arms B-D as design-final #1 describes them, read
    from FLUID_RESPONSES_ROUTING_ARM at call time. Test-only."""

    def __init__(self, *args, honour_arms: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.honour = honour_arms
        self.turn = None
        self.state = None

    async def stream(self, request):
        arm = os.environ.get(e1.ARM_ENV, "A") if self.honour else "A"
        extra = dict(request.extra)
        turn = extra.pop(e1.TURN_MARKER_KEY, None)
        body = build_request_body(dataclasses.replace(request, extra=extra))
        headers = await self._headers()
        key = body.get("prompt_cache_key")
        if arm in ("B", "C", "D") and key:
            for h in e1.IDENTITY_HEADERS:
                headers[h] = key
        new_turn = turn != self.turn
        if new_turn:
            self.turn, self.state = turn, None
        if arm == "D" and new_turn:
            await self._http.post(self._base_url, json={**body, "generate": False}, headers=headers)
        if arm == "C" and self.state:
            headers[e1.TURN_STATE_HEADER] = self.state
        async with self._http.stream("POST", self._base_url, json=body, headers=headers) as resp:
            if arm == "C" and self.state is None:
                self.state = resp.headers.get(e1.TURN_STATE_HEADER)
            from fluid.providers._sse import sse_events
            from fluid.providers.openai_responses.serialize import ResponsesStreamNormalizer

            norm = ResponsesStreamNormalizer()
            async for name, data in sse_events(resp.aiter_lines()):
                for chunk in norm.consume(name, data):
                    yield chunk


def spec_factory(honour_arms=True):
    def make(http):
        return SpecClient(base_url=e1.CODEX_RESPONSES_URL, auth=e1.FakeAuth(), http_client=http,
                          honour_arms=honour_arms)
    return make


async def test_contract_check_accepts_a_client_that_implements_every_arm():
    contract = await e1.check_contract(spec_factory())
    assert contract["ok"], e1.format_contract(contract)
    assert not contract["marker_leak"]
    assert contract["arms"]["C"]["observed"]["turn_state"] is True
    assert contract["arms"]["D"]["observed"]["prewarm"] is True
    assert contract["arms"]["B"]["observed"] == {
        "identity": True, "turn_state": False, "prewarm": False, "errors": []}


async def test_contract_check_flags_arms_the_client_ignores():
    contract = await e1.check_contract(spec_factory(honour_arms=False))
    assert not contract["ok"]
    assert contract["arms"]["A"]["ok"]
    assert [a for a in "BCD" if contract["arms"][a]["ok"]] == []


async def test_a_live_run_refuses_to_start_when_an_arm_is_missing(tmp_path):
    backend = e1.FakeCodexBackend()
    cfg = e1.RunConfig(out=tmp_path / "live.jsonl", calls_per_arm=2, dry_run=False)
    with pytest.raises(e1.ContractError):
        await e1.run_experiment(cfg, spec_factory(honour_arms=False),
                                transport=httpx.MockTransport(backend.handle), log=lambda _: None)
    assert backend.count == 0
    assert not cfg.out.exists()


async def test_the_contract_check_restores_the_arm_variable(monkeypatch):
    monkeypatch.setenv(e1.ARM_ENV, "keep-me")
    await e1.check_contract(spec_factory())
    assert os.environ[e1.ARM_ENV] == "keep-me"


class _Wire:
    """Stand-in for fluid's ``WireState`` (p0/responses-wire): per-session
    turn marker and turn-state token, owned by the caller."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.turn_marker = None
        self.turn_state = None

    def begin(self, marker):
        if marker is None:
            self.turn_marker = self.turn_state = None
            return False
        if marker == self.turn_marker:
            return False
        self.turn_marker, self.turn_state = marker, None
        return True


class WireStateClient(e1.OpenAIResponsesClient):
    """Arms as p0/responses-wire implements them: the arm is read once at
    construction, and arm C keeps its token in ``request.wire_state`` (I1),
    never on the client. ``prewarm=False`` models its HTTP no-op arm D."""

    def __init__(self, *args, prewarm: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.arm = os.environ.get(e1.ARM_ENV, "A")
        self.prewarm = prewarm

    async def stream(self, request):
        extra = dict(request.extra)
        marker = extra.pop(e1.TURN_MARKER_KEY, None)
        body = build_request_body(dataclasses.replace(request, extra=extra))
        headers = await self._headers()
        wire = getattr(request, "wire_state", None)
        if self.arm in ("B", "C", "D"):
            for h in e1.IDENTITY_HEADERS:
                headers[h] = body["prompt_cache_key"]
        new_turn = wire.begin(marker) if wire is not None and self.arm in ("C", "D") else False
        if self.arm == "D" and new_turn and self.prewarm:
            await self._http.post(self._base_url, json={**body, "generate": False}, headers=headers)
        if self.arm == "C" and wire is not None and wire.turn_state:
            headers[e1.TURN_STATE_HEADER] = wire.turn_state
        async with self._http.stream("POST", self._base_url, json=body, headers=headers) as resp:
            if self.arm == "C" and wire is not None and wire.turn_marker and wire.turn_state is None:
                wire.turn_state = resp.headers.get(e1.TURN_STATE_HEADER)
            from fluid.providers._sse import sse_events
            from fluid.providers.openai_responses.serialize import ResponsesStreamNormalizer

            norm = ResponsesStreamNormalizer()
            async for name, data in sse_events(resp.aiter_lines()):
                for chunk in norm.consume(name, data):
                    yield chunk


def wire_factory(prewarm=True):
    def make(http):
        return WireStateClient(base_url=e1.CODEX_RESPONSES_URL, auth=e1.FakeAuth(), http_client=http,
                               prewarm=prewarm)
    return make


@pytest.fixture
def wire_state(monkeypatch):
    monkeypatch.setattr(e1, "WireState", _Wire)


async def test_contract_check_passes_turn_state_through_the_request_wire_state(wire_state):
    contract = await e1.check_contract(wire_factory())
    assert contract["ok"], e1.format_contract(contract)
    assert contract["arms"]["C"]["observed"]["turn_state"] is True


def test_each_session_hands_the_client_one_wire_state(wire_state):
    conv = e1.build_conversation("C", "t", 0, start_tokens=500, static_tokens=400)
    first = conv.request(model="m", effort=None, turn_id="t1")
    conv.grow()
    second = conv.request(model="m", effort=None, turn_id="t1")
    assert first.wire_state is second.wire_state is conv.wire
    assert conv.wire.session_id == conv.session_id
    other = e1.build_conversation("C", "t", 1, start_tokens=500, static_tokens=400)
    assert other.wire is not conv.wire


async def test_a_run_without_d_is_not_refused_when_only_d_is_missing(tmp_path, wire_state):
    contract = await e1.check_contract(wire_factory(prewarm=False), arms=("A", "B", "C"))
    assert contract["ok"] and set(contract["arms"]) == {"A", "B", "C"}
    backend = e1.FakeCodexBackend()
    cfg = e1.RunConfig(out=tmp_path / "abc.jsonl", calls_per_arm=2, dry_run=False, arms=("A", "B", "C"),
                       start_tokens=500, max_tokens=5_000, round_tokens=50, seed=1)

    async def no_sleep(_):
        return None
    await e1.run_experiment(cfg, wire_factory(prewarm=False), transport=httpx.MockTransport(backend.handle),
                            sleep=no_sleep, log=lambda _: None)
    assert {r["arm"] for r in read_jsonl([cfg.out])} == {"A", "B", "C"}
    with pytest.raises(e1.ContractError):
        await e1.run_experiment(e1.RunConfig(out=tmp_path / "abcd.jsonl", calls_per_arm=2, dry_run=False),
                                wire_factory(prewarm=False),
                                transport=httpx.MockTransport(backend.handle), log=lambda _: None)


# ─── the dry run ─────────────────────────────────────────────────────────────


def test_dry_run_cli_runs_the_whole_path_and_logs_every_field(tmp_path, capsys):
    out = tmp_path / "dry.jsonl"
    code = e1.main(["run", "--dry-run", "--calls-per-arm", "6", "--block-size", "3",
                    "--start-tokens", "3000", "--max-tokens", "4000", "--round-tokens", "300",
                    "--seed", "7", "--out", str(out)])
    assert code == 0
    rows = list(read_jsonl([out]))
    assert len(rows) == 24
    assert {r["arm"] for r in rows} == {"A", "B", "C", "D"}
    assert all(sum(1 for r in rows if r["arm"] == a) == 6 for a in "ABCD")
    row = rows[-1]
    for key in ("arm", "prompt_tokens", "cached_tokens", "zero_cache", "latency_s", "ttft_s",
                "quota", "session", "cluster", "call_index", "block", "block_edge", "dry_run"):
        assert key in row, key
    assert row["dry_run"] is True
    assert row["quota"]["x-codex-primary-used-percent"]
    assert "x-codex-turn-state" not in row["quota"]
    assert all(r["prompt_tokens"] > 0 and r["error"] is None for r in rows)
    # 3,000 -> 4,000 tokens at 300 per round rolls over to a new session.
    assert max(r["call_index"] for r in rows) < 6
    assert len({r["session"] for r in rows if r["arm"] == "A"}) >= 2
    printed = capsys.readouterr().out
    assert "Arm contract check" in printed and "Verdict:" in printed


async def test_each_call_runs_with_its_own_arm_selected(tmp_path):
    seen = []

    class Recording(SpecClient):
        async def stream(self, request):
            seen.append((os.environ.get(e1.ARM_ENV), request.extra["prompt_cache_key"]))
            async for chunk in super().stream(request):
                yield chunk

    def factory(http):
        return Recording(base_url=e1.CODEX_RESPONSES_URL, auth=e1.FakeAuth(), http_client=http)

    backend = e1.FakeCodexBackend()
    cfg = e1.RunConfig(out=tmp_path / "d.jsonl", calls_per_arm=4, block_size=2, start_tokens=500,
                       max_tokens=5_000, round_tokens=50, dry_run=True, seed=1)

    async def no_sleep(_):
        return None
    await e1.run_experiment(cfg, factory, transport=httpx.MockTransport(backend.handle),
                            sleep=no_sleep, log=lambda _: None)
    # The contract check makes three calls per arm first, then the run itself.
    assert sum(1 for _, key in seen if "-contract-" in key) == 12
    run = [(arm, key) for arm, key in seen if "-contract-" not in key]
    assert len(run) == 16
    assert all(f"-{arm}-" in key for arm, key in run)
    rows = list(read_jsonl([cfg.out]))
    d_rows = [r for r in rows if r["arm"] == "D"]
    assert d_rows[0]["evidence"]["prewarm_requests"] == 1
    assert all(r["evidence"]["identity_headers"] for r in rows if r["arm"] != "A")
    assert not any(r["evidence"]["identity_headers"] for r in rows if r["arm"] == "A")


# ─── decision rule ───────────────────────────────────────────────────────────


def _rows(day, arm, n, misses, *, start=0.0):
    """n measured calls in clusters of five, ``misses`` of them zero-cache."""
    ts = datetime_ts(day) + start
    out = []
    for i in range(n):
        out.append({
            "row_id": f"{day}:{arm}:{i}", "arm": arm, "day": day, "ts": ts + i,
            "session": f"{day}-{arm}", "cluster": f"{day}-{arm}:{i // 5}", "call_index": i + 1,
            "prompt_tokens": 50_000, "cached_tokens": 0 if i < misses else 49_000,
            "zero_cache": i < misses, "error": None, "status": 200, "latency_s": 1.0,
        })
    return out


def datetime_ts(day):
    from datetime import datetime

    return datetime.fromisoformat(day).timestamp() + 3600


def _day(day, *, a=12, b=0, c=0, d=0, e=0, n=40):
    rows = []
    for arm, misses in (("A", a), ("B", b), ("C", c), ("D", d), ("E", e)):
        rows += _rows(day, arm, n, misses)
    return rows


def test_rule_ships_headers_when_b_is_clean_on_three_qualifying_days():
    rows = _day("2026-10-01") + _day("2026-10-02") + _day("2026-10-03")
    s = e1.summarize(rows)
    assert s["qualifying_days"] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert s["enough_data"]
    assert s["pooled"]["B"]["n"] == 120 and s["pooled"]["B"]["upper"] < 0.05
    assert s["verdict"] == "SHIP HEADERS: arms B and C meet the rule"


def test_rule_excludes_drift_days_and_days_without_the_gap():
    rows = (_day("2026-10-01") + _day("2026-10-02") + _day("2026-10-03")
            + _day("2026-10-04", e=8, b=8, c=8)      # control bad too: drift
            + _day("2026-10-05", a=2, b=8, c=8))     # A under 15%: gap absent
    s = e1.summarize(rows)
    assert s["qualifying_days"] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert s["days"]["2026-10-04"]["status"].startswith("excluded: backend drift")
    assert s["days"]["2026-10-05"]["status"].startswith("excluded: gap not reproduced")
    assert s["verdict"].startswith("SHIP HEADERS")


def test_rule_needs_the_control_on_the_same_day():
    rows = [r for r in _day("2026-10-01") if r["arm"] != "E"]
    s = e1.summarize(rows)
    assert s["qualifying_days"] == []
    assert s["verdict"] == "NO DECISION: no qualifying day yet"


def test_rule_names_the_single_arm_that_ships():
    rows = []
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        rows += _day(day, c=6)
    assert e1.summarize(rows)["verdict"] == "SHIP HEADERS: arm B meets the rule"


def test_rule_points_to_transport_when_only_prewarm_closes_the_gap():
    rows = []
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        rows += _day(day, b=6, c=6, d=0)
    s = e1.summarize(rows)
    assert s["verdict"].startswith("TRANSPORT: only the prewarm arm (D)")


def test_rule_points_to_transport_when_only_the_codex_control_is_clean():
    rows = []
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        rows += _day(day, b=6, c=6, d=6)
    assert e1.summarize(rows)["verdict"].startswith("TRANSPORT: only the Codex CLI control")


def test_rule_reaches_a_final_verdict_without_the_prewarm_arm():
    rows = []
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        rows += [r for r in _day(day) if r["arm"] != "D"]
    s = e1.summarize(rows)
    assert s["enough_data"]
    assert s["verdict"] == "SHIP HEADERS: arms B and C meet the rule"


def test_rule_is_provisional_below_three_days_or_120_calls():
    s = e1.summarize(_day("2026-10-01"))
    assert s["verdict"].startswith("PROVISIONAL")


def test_a_low_rate_with_a_wide_bound_does_not_ship():
    # 1 miss in 40 on one day: 2.5% with few clusters; bound far above 5%.
    rows = _day("2026-10-01", b=1, c=1)
    s = e1.summarize(rows)
    assert s["pooled"]["B"]["upper"] >= 0.05
    assert "SHIP HEADERS" not in s["verdict"]


def test_first_calls_errors_and_dry_runs_are_not_measured():
    rows = _day("2026-10-01") + _day("2026-10-02") + _day("2026-10-03")
    rows.append({**rows[0], "row_id": "first", "call_index": 0, "zero_cache": True, "arm": "B"})
    rows.append({**rows[0], "row_id": "err", "error": {"status": 500}, "zero_cache": True, "arm": "B"})
    rows.append({**rows[0], "row_id": "dry", "dry_run": True, "zero_cache": True, "arm": "B"})
    assert e1.summarize(rows)["pooled"]["B"]["k"] == 0


def test_quota_regression_recovers_the_per_token_slope():
    rows = []
    pct = 0.0
    for i in range(30):
        uncached = 5_000 + 1_000 * (i % 7)
        cached = 40_000 + 2_000 * (i % 5)
        pct += uncached / 1e6 * 10 + cached / 1e6 * 1
        rows.append({"row_id": str(i), "arm": "A", "ts": float(i), "prompt_tokens": uncached + cached,
                     "cached_tokens": cached, "quota": {"x-codex-primary-used-percent": f"{pct:.6f}"}})
    q = e1.quota_regression(rows)
    assert q["pct_per_million_uncached"] == pytest.approx(10, abs=0.01)
    assert q["pct_per_million_cached"] == pytest.approx(1, abs=0.01)


# ─── arm E: Codex CLI rollouts ───────────────────────────────────────────────


def _rollout(path: Path, cwd: str, session: str, usages):
    lines = [
        {"timestamp": "2026-10-01T15:00:00Z", "type": "session_meta", "payload": {"id": session, "cwd": cwd}},
        {"timestamp": "2026-10-01T15:00:01Z", "type": "turn_context", "payload": {"model": "gpt-6-astra",
                                                                               "turn_id": "t1"}},
    ]
    for i, (inp, cached) in enumerate(usages):
        lines.append({"timestamp": f"2026-10-01T15:00:{10 + i:02d}Z", "type": "token_usage_record",
                      "payload": {"session_id": session, "turn_id": "t1", "response_id": f"resp_{session}_{i}",
                                  "usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                            "output_tokens": 10}}})
        lines.append({"timestamp": f"2026-10-01T15:00:{10 + i:02d}Z", "type": "event_msg",
                      "payload": {"type": "token_count", "info": {},
                                  "rate_limits": {"primary": {"used_percent": 10.0 + i, "window_minutes": 300}}}})
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def test_ingest_reads_codex_rollouts_for_the_workspace_only(tmp_path):
    work = tmp_path / "e1-codex"
    work.mkdir()
    _rollout(tmp_path / "rollout-a.jsonl", str(work), "s-a", [(25_000, 19_200), (41_000, 40_000), (42_000, 0)])
    _rollout(tmp_path / "rollout-b.jsonl", str(tmp_path / "elsewhere"), "s-b", [(1, 0)])
    rows = e1.ingest_codex_rollouts(sorted(tmp_path.glob("rollout-*.jsonl")), cwd=str(work))
    assert [r["session"] for r in rows] == ["s-a"] * 3
    assert [r["call_index"] for r in rows] == [0, 1, 2]
    assert [r["zero_cache"] for r in rows] == [False, False, True]
    assert rows[0]["arm"] == "E" and rows[0]["model"] == "gpt-6-astra"
    assert rows[1]["quota"]["x-codex-primary-used-percent"] == "11.0"
    assert rows[0]["cluster"] == "s-a:t1"


def test_ingest_counts_first_calls_per_thread_not_per_root_session(tmp_path):
    work = tmp_path / "ws"
    work.mkdir()
    lines = [{"timestamp": "2026-10-01T15:00:00Z", "type": "session_meta", "payload": {"cwd": str(work)}}]
    for i, thread in enumerate(("root", "root", "sub", "sub")):
        lines.append({"timestamp": f"2026-10-01T15:00:{10 + i:02d}Z", "type": "token_usage_record",
                      "payload": {"session_id": "root", "thread_id": thread, "turn_id": "t1",
                                  "response_id": f"r{i}",
                                  "usage": {"input_tokens": 30_000, "cached_input_tokens": 0 if i in (0, 2)
                                            else 29_000, "output_tokens": 5}}})
    path = tmp_path / "rollout-t.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    rows = e1.ingest_codex_rollouts([path], cwd=str(work))
    # The subagent thread's first call is cold by construction, not a miss.
    assert [r["call_index"] for r in rows] == [0, 1, 0, 1]


def test_ingest_cli_writes_arm_e_rows_the_summary_reads(tmp_path, capsys):
    work = tmp_path / "ws"
    work.mkdir()
    sessions = tmp_path / "sessions" / "2026" / "10" / "01"
    sessions.mkdir(parents=True)
    _rollout(sessions / "rollout-x.jsonl", str(work), "s-x", [(30_000, 0), (31_000, 30_000)])
    out = tmp_path / "e.jsonl"
    assert e1.main(["ingest-codex", "--sessions", str(tmp_path / "sessions"), "--cwd", str(work),
                    "--out", str(out)]) == 0
    rows = list(read_jsonl([out]))
    assert len(rows) == 2 and rows[1]["arm"] == "E"


def test_prepare_codex_writes_the_files_and_turn_prompts(tmp_path):
    prompts = e1.prepare_codex_workspace(tmp_path / "ws")
    assert len(prompts) == e1.CODEX_TURNS
    assert (tmp_path / "ws" / "part_01.txt").stat().st_size > 3_000
    assert "part_12.txt" in prompts[1].read_text(encoding="utf-8")


# ─── login ───────────────────────────────────────────────────────────────────


async def test_device_login_saves_a_fluid_token_file(tmp_path):
    import base64

    claims = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_1"}}).encode()).decode().rstrip("=")
    id_token = f"h.{claims}.s"
    polls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/usercode"):
            return httpx.Response(200, json={"device_auth_id": "dev1", "user_code": "ABCD", "interval": 1})
        polls.append(1)
        if len(polls) == 1:
            return httpx.Response(403, json={"error": {"code": "deviceauth_authorization_pending"}})
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "id_token": id_token})

    async def no_sleep(_):
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        path = await e1.device_login(tmp_path / "tok.json", client=http, sleep=no_sleep, log=lambda _: None)
    saved = json.loads(Path(path).read_text())
    assert saved["access_token"] == "at" and saved["refresh_token"] == "rt"
    assert len(polls) == 2


def test_fake_backend_sse_is_a_valid_responses_stream():
    body = responses_sse(response_id="r", model="m", usage={"input_tokens": 3})
    assert b"response.completed" in body and b"response.created" in body
