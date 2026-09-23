"""E1: cache routing and quota on the ChatGPT Codex backend (design-final 6.1).

Drives fluid's ``OpenAIResponsesClient`` directly, not the app, through a
synthetic conversation of about 40-60k tokens that grows by one tool round per
call, with 2-5 s gaps. Arms A-D are selected per call by setting
``FLUID_RESPONSES_ROUTING_ARM``; the client implements them (fix #1 in
design-final section 3). Arms are interleaved one block (one synthetic user
turn) at a time, in a shuffled order each round.

    A  today's client (no routing identity)
    B  + session-id / thread-id / x-client-request-id = prompt_cache_key
    C  B + x-codex-turn-state replay within a turn
    D  B + a generate:false prewarm at turn start
    E  the real Codex CLI on the same account (positive control, run by hand
       and read back with ``ingest-codex``)

Subcommands:

    run            make the calls (``--dry-run`` uses a fake backend, no network)
    summarize      apply the pre-stated decision rule to one or more JSONL logs
    prepare-codex  write the workspace and turn prompts for arm E
    ingest-codex   turn Codex CLI rollout files into arm-E log rows
    login          device-code login that writes a fluid token file

Before any live run the runner checks, offline against a fake backend, that the
client really implements each arm (identity headers, turn-state replay,
prewarm). A live run refuses to start when an arm is missing, so quota is never
spent measuring four copies of arm A. The client does not implement arm D's
prewarm (it logs and sends nothing), so ``run`` skips D with a one-line note
and the summary marks the "TRANSPORT via D" verdict unreachable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.experiments._common import (  # noqa: E402
    append_jsonl,
    ensure_import_paths,
    read_jsonl,
    responses_sse,
)

ensure_import_paths()

import httpx  # noqa: E402

from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock  # noqa: E402
from fluid.providers import base as _provider_base  # noqa: E402
from fluid.providers.base import ProviderRequest  # noqa: E402
from fluid.providers.openai_responses.client import (  # noqa: E402
    CODEX_RESPONSES_URL,
    OpenAIResponsesClient,
)
from scripts.experiments._stats import ols, proportion_ci  # noqa: E402

ARM_ENV = "FLUID_RESPONSES_ROUTING_ARM"
RUNNER_ARMS = ("A", "B", "C", "D")

#: Whether fluid's client implements arm D's prewarm. It does not: over HTTP
#: SSE there is no documented ``generate: false``, so the client logs the
#: prewarm and sends nothing (``OpenAIResponsesClient._prewarm``). ``run``
#: skips D, and the summary marks the "TRANSPORT via D" verdict unreachable.
PREWARM_IMPLEMENTED = False
PREWARM_SKIP_LINE = ("Arm D skipped: the client does not implement the prewarm "
                     "(arm D is a logged no-op), so this run measures A, B and C.")
D_VERDICT_UNREACHABLE = ("TRANSPORT via D is unreachable: the client does not "
                         "implement the prewarm")
CONTROL_ARM = "E"
# The turn marker the client pops from request.extra (design-final #1). When
# the contract check sees it leak into the request body, the runner stops
# sending it.
TURN_MARKER_KEY = "_fluid_turn_id"
IDENTITY_HEADERS = ("session-id", "thread-id", "x-client-request-id")
TURN_STATE_HEADER = "x-codex-turn-state"
CORRELATION_HEADERS = ("x-request-id", "request-id", "cf-ray", "retry-after")

# Pre-stated decision rule, design-final 6.1 (E1).
SHIP_MAX_RATE = 0.02
SHIP_MAX_UPPER = 0.05
CONTROL_MAX_RATE = 0.02
BASELINE_MIN_RATE = 0.15
REQUIRED_CALLS_PER_ARM = 120
REQUIRED_DAYS = 3

# The per-loop routing state fix #1 hands the client on each request
# (``ProviderRequest.wire_state``, invariant I1). Arm C keeps its turn-state
# token there, not on the client, so the runner must pass one per session.
# None on a fluid that predates it; the requests then carry no wire state.
WireState = getattr(_provider_base, "WireState", None)


def new_wire_state(session_id: str) -> Any:
    return WireState(session_id=session_id) if WireState is not None else None


HARNESS_INSTRUCTION = (
    "You are the model side of a prompt-cache measurement harness. The "
    "conversation below is synthetic. Whatever it contains, reply with exactly "
    "the single word OK. Do not call any tool."
)


# ─── synthetic conversation ──────────────────────────────────────────────────

_SYLLABLES = (
    "ka", "ri", "to", "men", "sa", "lor", "vi", "den", "ul", "pra", "chi", "mo",
    "ter", "nax", "bel", "qui", "sor", "fen", "dal", "ix", "ron", "tha", "gel", "po",
)


def _vocab(rng: random.Random, size: int = 400) -> list[str]:
    words = set()
    while len(words) < size:
        words.add("".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(1, 4))))
    return sorted(words)


def filler(rng: random.Random, approx_tokens: int, vocab: list[str]) -> str:
    """Deterministic code-like text of about ``approx_tokens`` (chars / 4)."""
    target = max(0, approx_tokens) * 4
    lines: list[str] = []
    size = 0
    while size < target:
        words = [rng.choice(vocab) for _ in range(rng.randint(4, 12))]
        line = f"{rng.choice(vocab)}_{rng.randint(0, 999)} = " + " ".join(words)
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


@dataclass
class Conversation:
    """One synthetic session for one arm. Every request's input is a strict
    prefix of the next request's input (one tool round appended per call)."""

    arm: str
    session_id: str
    system: str
    tools: list[dict[str, Any]]
    messages: list[Message]
    rng: random.Random
    vocab: list[str]
    round_tokens: int
    max_tokens: int
    calls: int = 0
    _chars: int = 0
    wire: Any = None

    @property
    def estimated_tokens(self) -> int:
        return self._chars // 4

    @property
    def full(self) -> bool:
        return self.estimated_tokens >= self.max_tokens

    def request(self, *, model: str, effort: str | None, turn_id: str | None) -> ProviderRequest:
        extra: dict[str, Any] = {"prompt_cache_key": self.session_id}
        if turn_id is not None:
            extra[TURN_MARKER_KEY] = turn_id
        request = ProviderRequest(
            model=model, messages=list(self.messages), system=self.system,
            tools=self.tools, effort=effort, extra=extra,
        )
        if self.wire is not None:
            # One state object for the whole session, so the client can tell
            # a new turn from the next call of the same one.
            request.wire_state = self.wire
        return request

    def grow(self) -> None:
        """Append one tool round: an assistant call and its result."""
        n = len(self.messages)
        call_id = f"call_{self.session_id[-8:]}_{n}"
        path = f"src/{self.rng.choice(self.vocab)}/{self.rng.choice(self.vocab)}_{n}.py"
        body = filler(self.rng, self.round_tokens, self.vocab)
        self.messages.append(Message("assistant", [
            ToolUseBlock(id=call_id, name="read_file", input={"path": path})]))
        self.messages.append(Message("user", [
            ToolResultBlock(tool_use_id=call_id, content=[Text(body)])]))
        self._chars += len(body) + len(path) + 40


def build_conversation(
    arm: str, run_id: str, index: int, *,
    start_tokens: int = 40_000, round_tokens: int = 600, max_tokens: int = 60_000,
    static_tokens: int = 24_000,
) -> Conversation:
    """A fresh session. The nonce in the first instruction line keeps each
    session's prefix unique, so no call can hit another session's or arm's
    cache: every hit measured is within-session routing."""
    rng = random.Random(f"{run_id}:{arm}:{index}")
    vocab = _vocab(rng)
    session_id = f"e1-{run_id}-{arm}-{index}-{uuid.UUID(int=rng.getrandbits(128)).hex[:8]}"
    nonce = f"Session nonce: {session_id}."
    manual = filler(rng, static_tokens // 2, vocab)
    system = f"{HARNESS_INSTRUCTION}\n{nonce}\n\nReference manual:\n{manual}"
    tools = []
    per_tool = max(50, (static_tokens // 2) // 24)
    for i in range(24):
        tools.append({
            "name": f"tool_{i:02d}_{vocab[i]}",
            "description": filler(rng, per_tool, vocab),
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "arg": {"type": "string"}},
                "required": ["path"],
            },
        })
    tools.append({
        "name": "read_file", "description": "Read a file from the workspace.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                         "required": ["path"]},
    })
    task = f"Task for {session_id}: review the files below.\n" + filler(rng, 800, vocab)
    conv = Conversation(
        arm=arm, session_id=session_id, system=system, tools=tools,
        messages=[Message("user", [Text(task)])], rng=rng, vocab=vocab,
        round_tokens=round_tokens, max_tokens=max_tokens, wire=new_wire_state(session_id),
    )
    conv._chars = len(system) + len(json.dumps(tools)) + len(task)
    while conv.estimated_tokens < start_tokens:
        conv.grow()
    return conv


# ─── wire observation ────────────────────────────────────────────────────────


@dataclass
class WireObservation:
    headers: dict[str, str]
    body: dict[str, Any]
    at: float


@dataclass
class Recorder:
    """httpx event hooks that keep what one call sent and received."""

    requests: list[WireObservation] = field(default_factory=list)
    responses: list[tuple[int, dict[str, str], float]] = field(default_factory=list)

    def reset(self) -> None:
        self.requests.clear()
        self.responses.clear()

    async def on_request(self, request: httpx.Request) -> None:
        try:
            body = json.loads(request.content or b"{}")
        except (ValueError, httpx.RequestNotRead):
            body = {}
        self.requests.append(WireObservation(
            {k.lower(): v for k, v in request.headers.items()}, body, time.monotonic()))

    async def on_response(self, response: httpx.Response) -> None:
        self.responses.append((response.status_code,
                               {k.lower(): v for k, v in response.headers.items()},
                               time.monotonic()))

    def hooks(self) -> dict[str, list]:
        return {"request": [self.on_request], "response": [self.on_response]}


def is_prewarm(body: dict[str, Any]) -> bool:
    return body.get("generate") is False


def quota_headers(headers: dict[str, str]) -> dict[str, str]:
    """Usage-limit, rate-limit and correlation headers; never auth or cookies."""
    keep = {}
    for key, value in headers.items():
        if key == TURN_STATE_HEADER:
            continue
        if key.startswith("x-codex-") or key.startswith("x-ratelimit-") or key in CORRELATION_HEADERS:
            keep[key] = value[:200]
    return keep


# ─── fake backend (dry run and contract check) ───────────────────────────────


class FakeCodexBackend:
    """An offline stand-in for the Codex responses endpoint.

    Cache behaviour is invented: a request without ``session-id`` misses with
    probability ``miss_without_identity``, one with it with
    ``miss_with_identity``. Every 200 carries a fresh ``x-codex-turn-state``
    and quota headers that rise with the tokens billed.
    """

    def __init__(self, seed: int = 0, *, miss_without_identity: float = 0.25,
                 miss_with_identity: float = 0.01, model: str = "gpt-fake") -> None:
        self.rng = random.Random(seed)
        self.miss_without = miss_without_identity
        self.miss_with = miss_with_identity
        self.model = model
        self.prev_prompt: dict[str, int] = {}
        self.used_percent = 0.0
        self.count = 0
        self.turn_states: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        body = json.loads(request.content or b"{}")
        headers = {"content-type": "text/event-stream",
                   "x-request-id": f"req_fake_{self.count}",
                   "x-codex-primary-window-minutes": "300"}
        if is_prewarm(body):
            headers["x-codex-primary-used-percent"] = f"{self.used_percent:.1f}"
            return httpx.Response(200, headers=headers, content=responses_sse(
                response_id=f"resp_warm_{self.count}", model=self.model,
                usage={"input_tokens": 0, "output_tokens": 0}, text=None))
        prompt = (len(json.dumps(body.get("input", []))) + len(body.get("instructions", ""))
                  + len(json.dumps(body.get("tools", [])))) // 4
        key = body.get("prompt_cache_key") or "_none"
        prev = self.prev_prompt.get(key)
        routed = bool(request.headers.get("session-id"))
        miss_p = self.miss_with if routed else self.miss_without
        cached = 0 if prev is None or self.rng.random() < miss_p else (min(prev, prompt) // 128) * 128
        self.prev_prompt[key] = prompt
        self.used_percent += ((prompt - cached) + cached * 0.1) / 200_000
        state = f"ts_fake_{self.count}"
        self.turn_states.append(state)
        headers.update({
            TURN_STATE_HEADER: state,
            "x-codex-primary-used-percent": f"{self.used_percent:.1f}",
            "x-codex-secondary-used-percent": f"{self.used_percent / 7:.1f}",
        })
        usage = {"input_tokens": prompt, "input_tokens_details": {"cached_tokens": cached},
                 "output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 0},
                 "total_tokens": prompt + 2}
        return httpx.Response(200, headers=headers, content=responses_sse(
            response_id=f"resp_fake_{self.count}", model=self.model, usage=usage))


class FakeAuth:
    account_id = "acct_dry_run"

    async def get_access_token(self) -> str:
        return "dry-run-token"

    async def on_401(self) -> None:
        return None


ClientFactory = Callable[[httpx.AsyncClient], Any]


def default_client_factory(*, base_url: str, auth: Any = None, api_key: str | None = None) -> ClientFactory:
    def make(http_client: httpx.AsyncClient) -> OpenAIResponsesClient:
        return OpenAIResponsesClient(api_key=api_key, base_url=base_url, auth=auth,
                                     http_client=http_client)
    return make


class _ArmEnv:
    """Sets ``FLUID_RESPONSES_ROUTING_ARM`` and restores the old value."""

    def __init__(self) -> None:
        self._old = os.environ.get(ARM_ENV)

    @staticmethod
    def set(arm: str) -> None:
        os.environ[ARM_ENV] = arm

    def restore(self) -> None:
        if self._old is None:
            os.environ.pop(ARM_ENV, None)
        else:
            os.environ[ARM_ENV] = self._old


# ─── one call ────────────────────────────────────────────────────────────────


async def one_call(client: Any, request: ProviderRequest, recorder: Recorder) -> dict[str, Any]:
    """Stream one request; return usage, timings, headers and arm evidence."""
    recorder.reset()
    started = time.monotonic()
    ttft = None
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    try:
        async for chunk in client.stream(request):
            if chunk.kind in ("text_delta", "thinking_delta", "tool_args_delta", "block_start") and ttft is None:
                ttft = time.monotonic() - started
            elif chunk.kind == "usage":
                usage = chunk.payload.get("usage") or {}
            elif chunk.kind == "error":
                error = {k: v for k, v in chunk.payload.items() if k in ("status", "message", "retriable")}
                if isinstance(error.get("message"), str):
                    error["message"] = error["message"][:500]
    except Exception as exc:  # noqa: BLE001 - one failed call must not end the run
        error = {"message": f"{type(exc).__name__}: {exc}"[:500]}
    latency = time.monotonic() - started
    main = [r for r in recorder.requests if not is_prewarm(r.body)]
    prewarms = [r for r in recorder.requests if is_prewarm(r.body)]
    main_req = main[-1] if main else None
    responses = recorder.responses
    main_resp = responses[-1] if responses else None
    usage = usage or {}
    cached = int(usage.get("cache_read_input_tokens", 0) or 0)
    prompt = (int(usage.get("input_tokens", 0) or 0) + cached
              + int(usage.get("cache_creation_input_tokens", 0) or 0))
    sent = main_req.headers if main_req else {}
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "reasoning_tokens": int(usage.get("reasoning_tokens", 0) or 0),
        "latency_s": round(latency, 3),
        "ttft_s": round(ttft, 3) if ttft is not None else None,
        "ttfb_s": round(main_resp[2] - started, 3) if main_resp else None,
        "status": main_resp[0] if main_resp else None,
        "attempts": len(main),
        "error": error,
        "quota": quota_headers(main_resp[1]) if main_resp else {},
        "evidence": {
            "identity_headers": all(sent.get(h) for h in IDENTITY_HEADERS),
            "session_id_header": sent.get("session-id"),
            "turn_state_sent": bool(sent.get(TURN_STATE_HEADER)),
            "prewarm_requests": len(prewarms),
            "marker_in_body": bool(main_req and TURN_MARKER_KEY in main_req.body),
        },
        "_turn_state_received": (main_resp[1].get(TURN_STATE_HEADER) if main_resp else None),
    }


# ─── contract check ──────────────────────────────────────────────────────────

EXPECTED = {
    "A": {"identity": False, "turn_state": False, "prewarm": False},
    "B": {"identity": True, "turn_state": False, "prewarm": False},
    "C": {"identity": True, "turn_state": True, "prewarm": False},
    # D = B + prewarm; whether it also replays turn state is not part of its definition.
    "D": {"identity": True, "prewarm": True},
}


async def check_contract(factory: ClientFactory, *, model: str = "gpt-fake",
                         arms: Iterable[str] = RUNNER_ARMS) -> dict[str, Any]:
    """Run each of ``arms`` three times against the fake backend (two calls
    in one turn, one in the next) and compare what went over the wire with the
    arm's definition. Costs nothing: no network. Only the arms a run will
    measure are checked, so ``--arms A,B,C`` can run while D is missing."""
    env = _ArmEnv()
    result: dict[str, Any] = {"arms": {}, "marker_leak": False}
    try:
        for arm in arms:
            env.set(arm)
            backend = FakeCodexBackend(seed=1)
            recorder = Recorder()
            async with httpx.AsyncClient(transport=httpx.MockTransport(backend.handle),
                                         event_hooks=recorder.hooks()) as http:
                client = factory(http)
                conv = build_conversation(arm, "contract", 0, start_tokens=200,
                                          round_tokens=50, max_tokens=10_000, static_tokens=400)
                calls = []
                for turn in ("t1", "t1", "t2"):
                    env.set(arm)
                    req = conv.request(model=model, effort=None, turn_id=f"contract-{arm}-{turn}")
                    calls.append(await one_call(client, req, recorder))
                    calls[-1]["_sent"] = [r.headers for r in recorder.requests if not is_prewarm(r.body)]
                    conv.grow()
            first_state = calls[0]["_turn_state_received"]
            second_sent = (calls[1]["_sent"] or [{}])[-1].get(TURN_STATE_HEADER)
            third_sent = (calls[2]["_sent"] or [{}])[-1].get(TURN_STATE_HEADER)
            observed = {
                "identity": all(c["evidence"]["identity_headers"] and
                                c["evidence"]["session_id_header"] == conv.session_id for c in calls),
                # Codex keeps the first token of a turn for the rest of it and
                # starts the next turn without one.
                "turn_state": bool(first_state) and second_sent == first_state and not third_sent,
                "prewarm": any(c["evidence"]["prewarm_requests"] for c in calls),
                "errors": [c["error"] for c in calls if c["error"]],
            }
            leak = any(c["evidence"]["marker_in_body"] for c in calls)
            result["marker_leak"] = result["marker_leak"] or leak
            expected = EXPECTED[arm]
            ok = not observed["errors"] and all(observed[k] == v for k, v in expected.items())
            result["arms"][arm] = {"observed": observed, "expected": expected, "ok": ok}
    finally:
        env.restore()
    result["ok"] = all(a["ok"] for a in result["arms"].values())
    return result


def format_contract(contract: dict[str, Any]) -> str:
    lines = ["Arm contract check (offline, fake backend):"]
    for arm, info in contract["arms"].items():
        obs = info["observed"]
        lines.append(
            f"  {arm}: {'ok' if info['ok'] else 'NOT IMPLEMENTED AS SPECIFIED'}"
            f"  identity={obs['identity']} turn_state={obs['turn_state']} prewarm={obs['prewarm']}"
            + (f" errors={obs['errors']}" if obs["errors"] else "")
        )
    if contract["marker_leak"]:
        lines.append(f"  turn marker {TURN_MARKER_KEY!r} reaches the request body; "
                     "the runner will not send it")
    return "\n".join(lines)


# ─── the run ─────────────────────────────────────────────────────────────────


@dataclass
class RunConfig:
    out: Path
    model: str = "gpt-6-astra"
    effort: str | None = "low"
    calls_per_arm: int = 40
    block_size: int = 5
    gap_min: float = 2.0
    gap_max: float = 5.0
    start_tokens: int = 40_000
    round_tokens: int = 600
    max_tokens: int = 60_000
    seed: int | None = None
    dry_run: bool = False
    skip_contract_check: bool = False
    arms: tuple[str, ...] = RUNNER_ARMS


class ContractError(RuntimeError):
    pass


def plan_blocks(arms: Iterable[str], calls_per_arm: int, block_size: int,
                rng: random.Random) -> list[tuple[str, int]]:
    """``(arm, calls)`` blocks: each round runs every arm once, in a fresh
    shuffled order, until every arm has ``calls_per_arm`` calls."""
    arms = list(arms)
    left = {a: calls_per_arm for a in arms}
    blocks = []
    while any(left.values()):
        order = arms[:]
        rng.shuffle(order)
        for arm in order:
            n = min(block_size, left[arm])
            if n:
                blocks.append((arm, n))
                left[arm] -= n
    return blocks


async def run_experiment(
    cfg: RunConfig,
    factory: ClientFactory,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    contract_factory: ClientFactory | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the interleaved arms and append one JSONL row per call to ``cfg.out``.

    ``transport`` is the fake backend's transport in a dry run and None live.
    The contract check always runs against its own fake backend.
    """
    contract = await check_contract(contract_factory or factory, model=cfg.model, arms=cfg.arms)
    log(format_contract(contract))
    if not contract["ok"] and not cfg.dry_run and not cfg.skip_contract_check:
        raise ContractError(
            "the client does not implement every arm as specified; refusing to spend "
            "quota (pass --skip-contract-check to run anyway)")
    use_marker = not contract["marker_leak"]
    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:4]
    rng = random.Random(cfg.seed if cfg.seed is not None else run_id)
    blocks = plan_blocks(cfg.arms, cfg.calls_per_arm, cfg.block_size, rng)
    env = _ArmEnv()
    https: dict[str, httpx.AsyncClient] = {}
    clients: dict[str, Any] = {}
    recorders: dict[str, Recorder] = {}
    conversations: dict[str, Conversation] = {}
    session_counts: dict[str, int] = defaultdict(int)
    seq = 0
    rows = 0
    try:
        for arm in cfg.arms:
            env.set(arm)
            recorders[arm] = Recorder()
            https[arm] = httpx.AsyncClient(timeout=600.0, transport=transport,
                                           event_hooks=recorders[arm].hooks())
            clients[arm] = factory(https[arm])
        for block_index, (arm, n_calls) in enumerate(blocks):
            turn_id = f"turn-{run_id}-{block_index}"
            for pos in range(n_calls):
                conv = conversations.get(arm)
                if conv is None or conv.full:
                    conv = build_conversation(
                        arm, run_id, session_counts[arm], start_tokens=cfg.start_tokens,
                        round_tokens=cfg.round_tokens, max_tokens=cfg.max_tokens)
                    session_counts[arm] += 1
                    conversations[arm] = conv
                gap = rng.uniform(cfg.gap_min, cfg.gap_max) if seq else 0.0
                if gap:
                    await sleep(gap)
                env.set(arm)
                request = conv.request(model=cfg.model, effort=cfg.effort,
                                       turn_id=turn_id if use_marker else None)
                result = await one_call(clients[arm], request, recorders[arm])
                result.pop("_turn_state_received", None)
                now = time.time()
                row = {
                    "row_id": f"{run_id}:{seq}", "source": "runner", "dry_run": cfg.dry_run,
                    "run_id": run_id, "ts": round(now, 3), "day": _day(now), "arm": arm,
                    "model": cfg.model, "block": block_index, "block_pos": pos,
                    "block_edge": "first" if pos == 0 else ("last" if pos == n_calls - 1 else ""),
                    "session": conv.session_id, "turn": turn_id,
                    "cluster": f"{conv.session_id}:{turn_id}", "call_index": conv.calls,
                    "gap_s": round(gap, 3), "estimated_prompt_tokens": conv.estimated_tokens,
                    **result,
                }
                row["zero_cache"] = row["cached_tokens"] == 0
                append_jsonl(cfg.out, row)
                conv.calls += 1
                conv.grow()
                seq += 1
                rows += 1
                log(f"[{rows}/{len(cfg.arms) * cfg.calls_per_arm}] arm={arm} call={row['call_index']} "
                    f"prompt={row['prompt_tokens']} cached={row['cached_tokens']} "
                    f"latency={row['latency_s']}s" + (f" error={row['error']}" if row["error"] else ""))
    finally:
        env.restore()
        for http in https.values():
            await http.aclose()
    return {"run_id": run_id, "rows": rows, "contract": contract, "log": str(cfg.out)}


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


# ─── Codex CLI control (arm E) ───────────────────────────────────────────────

CODEX_TURNS = 8


CODEX_FIRST_TURN_FILES = 11
CODEX_FILES_PER_TURN = 5


def prepare_codex_workspace(directory: Path, *, file_tokens: int = 1_000) -> list[Path]:
    """Write the arm-E workspace: numbered files to read and one prompt per turn.

    Codex's own instructions and tools are about 20k tokens. The first turn
    reads eleven files of about 1k tokens; each later turn reads five more, one
    shell command per file, which gives about five model calls per turn (the
    shape of a runner block) and a thread of roughly 30-65k tokens.
    """
    files = CODEX_FIRST_TURN_FILES + CODEX_FILES_PER_TURN * (CODEX_TURNS - 1)
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"codex:{directory.name}")
    vocab = _vocab(rng)
    for i in range(1, files + 1):
        (directory / f"part_{i:02d}.txt").write_text(filler(rng, file_tokens, vocab), encoding="utf-8")
    prompts = []
    first = list(range(1, CODEX_FIRST_TURN_FILES + 1))
    rest = list(range(CODEX_FIRST_TURN_FILES + 1, files + 1))
    turns = [first] + [rest[i:i + CODEX_FILES_PER_TURN] for i in range(0, len(rest), CODEX_FILES_PER_TURN)]
    for t, numbers in enumerate(turns, start=1):
        names = ", ".join(f"part_{n:02d}.txt" for n in numbers)
        text = (
            f"Read these files one at a time, one shell command per file, in this order: {names}. "
            "Do not summarise them and do not edit anything. When you have read the last one, "
            "reply with the single word DONE."
        )
        path = directory / f"turn_{t:02d}.txt"
        path.write_text(text, encoding="utf-8")
        prompts.append(path)
    return prompts


def _iso_ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _same_dir(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.abspath(p)))  # noqa: E731
    return norm(a) == norm(b)


def ingest_codex_rollouts(files: Iterable[Path], *, cwd: str | None = None,
                          since: float | None = None) -> list[dict[str, Any]]:
    """Arm-E rows from Codex CLI rollout JSONL files.

    One row per model response, from ``token_usage_record`` lines; files from
    CLI builds without them fall back to ``event_msg``/``token_count``
    ``last_token_usage``. Codex reports ``input_tokens`` including the cached
    part, like the Responses API. The newest ``rate_limits`` seen before a
    row's successor is attached to it as quota headers.
    """
    rows: list[dict[str, Any]] = []
    for path in files:
        meta_cwd = None
        model = None
        file_rows: list[dict[str, Any]] = []
        saw_records = False
        last_total = None
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("type")
            payload = rec.get("payload") or {}
            if kind == "session_meta":
                meta_cwd = payload.get("cwd")
            elif kind == "turn_context":
                model = payload.get("model") or model
            elif kind == "token_usage_record":
                saw_records = True
                usage = payload.get("usage") or {}
                # thread_id is the cache-affinity unit: a subagent thread shares
                # its root's session_id but starts its own cold cache.
                file_rows.append(_codex_row(path, rec, payload.get("thread_id") or payload.get("session_id"),
                                            payload.get("turn_id"), usage, model,
                                            payload.get("response_id")))
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                limits = payload.get("rate_limits") or {}
                if file_rows and limits:
                    file_rows[-1]["quota"] = _codex_quota(limits)
                if not saw_records and info.get("last_token_usage"):
                    total = (info.get("total_token_usage") or {}).get("total_tokens")
                    if total is not None and total == last_total:
                        continue
                    last_total = total
                    file_rows.append(_codex_row(path, rec, None, None, info["last_token_usage"], model, None))
                    if limits:
                        file_rows[-1]["quota"] = _codex_quota(limits)
        if cwd and not _same_dir(meta_cwd, cwd):
            continue
        per_session: dict[str, int] = defaultdict(int)
        for row in file_rows:
            if since is not None and (row["ts"] or 0) < since:
                continue
            row["call_index"] = per_session[row["session"]]
            per_session[row["session"]] += 1
            rows.append(row)
    return rows


def _codex_row(path: Path, rec: dict[str, Any], session: str | None, turn: str | None,
               usage: dict[str, Any], model: str | None, response_id: str | None) -> dict[str, Any]:
    ts = _iso_ts(rec.get("timestamp")) or 0.0
    session = session or Path(path).stem
    turn = turn or "unknown"
    prompt = int(usage.get("input_tokens", 0) or 0)
    cached = int(usage.get("cached_input_tokens", 0) or 0)
    return {
        "row_id": f"codex:{response_id or f'{session}:{ts}'}", "source": "codex_cli",
        "arm": CONTROL_ARM, "ts": ts, "day": _day(ts) if ts else None, "model": model,
        "session": session, "turn": turn, "cluster": f"{session}:{turn}",
        "prompt_tokens": prompt, "cached_tokens": cached, "zero_cache": cached == 0,
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "error": None, "status": 200, "quota": {},
    }


def _codex_quota(limits: dict[str, Any]) -> dict[str, str]:
    out = {}
    for window in ("primary", "secondary"):
        w = limits.get(window) or {}
        if w.get("used_percent") is not None:
            out[f"x-codex-{window}-used-percent"] = str(w["used_percent"])
        if w.get("window_minutes") is not None:
            out[f"x-codex-{window}-window-minutes"] = str(w["window_minutes"])
    return out


# ─── summary and decision rule ───────────────────────────────────────────────


def _dedupe(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        seen[str(row.get("row_id") or id(row))] = row
    return sorted(seen.values(), key=lambda r: r.get("ts") or 0)


def _measured(row: dict[str, Any]) -> bool:
    """A call counted toward the zero-cache rate: a completed call after the
    session's first."""
    return (not row.get("error") and (row.get("status") in (None, 200))
            and int(row.get("call_index") or 0) >= 1 and int(row.get("prompt_tokens") or 0) > 0)


def _rate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    clusters: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        c = clusters[str(row.get("cluster") or row.get("session"))]
        c[0] += 1 if row.get("zero_cache") else 0
        c[1] += 1
    return proportion_ci([tuple(v) for v in clusters.values()]).as_dict()


def _passes(stats: dict[str, Any] | None) -> bool:
    return bool(stats and stats["n"] and stats["rate"] <= SHIP_MAX_RATE and stats["upper"] < SHIP_MAX_UPPER)


def summarize(rows: Iterable[dict[str, Any]], *, include_dry_run: bool = False) -> dict[str, Any]:
    """Per-day and pooled zero-cache rates with the pre-stated verdict."""
    rows = [r for r in _dedupe(rows) if include_dry_run or not r.get("dry_run")]
    for row in rows:
        if not row.get("day") and row.get("ts"):
            row["day"] = _day(row["ts"])
    measured = [r for r in rows if _measured(r)]
    arms = sorted({r["arm"] for r in rows})
    days = sorted({r["day"] for r in measured if r.get("day")})
    per_day: dict[str, Any] = {}
    qualifying: list[str] = []
    for day in days:
        day_rows = [r for r in measured if r.get("day") == day]
        stats = {arm: _rate([r for r in day_rows if r["arm"] == arm]) for arm in arms}
        e, a = stats.get(CONTROL_ARM), stats.get("A")
        if not e or not e["n"]:
            status = "excluded: no Codex CLI control (arm E) data this day"
        elif e["rate"] > CONTROL_MAX_RATE:
            status = f"excluded: backend drift (E zero-cache {e['rate']:.1%} > {CONTROL_MAX_RATE:.0%})"
        elif not a or not a["n"]:
            status = "excluded: no arm A data this day"
        elif a["rate"] < BASELINE_MIN_RATE:
            status = f"excluded: gap not reproduced (A zero-cache {a['rate']:.1%} < {BASELINE_MIN_RATE:.0%})"
        else:
            status = "qualifying"
            qualifying.append(day)
        per_day[day] = {"status": status, "arms": stats}
    pooled_rows = [r for r in measured if r.get("day") in qualifying]
    pooled = {arm: _rate([r for r in pooled_rows if r["arm"] == arm]) for arm in arms}
    counts = {arm: sum(1 for r in pooled_rows if r["arm"] == arm) for arm in arms}
    # Every arm that was run needs the calls, and A and E always: a run
    # without D (``--arms A,B,C``) can still reach a final verdict.
    required = {"A", CONTROL_ARM} | set(arms)
    enough = (len(qualifying) >= REQUIRED_DAYS
              and all(counts.get(a, 0) >= REQUIRED_CALLS_PER_ARM for a in required))
    winners = [a for a in ("B", "C") if _passes(pooled.get(a))]
    if not qualifying:
        verdict = "NO DECISION: no qualifying day yet"
    elif winners:
        verdict = ("SHIP HEADERS: " + (f"arm {winners[0]} meets" if len(winners) == 1
                                       else "arms B and C meet") + " the rule")
    elif PREWARM_IMPLEMENTED and _passes(pooled.get("D")):
        verdict = "TRANSPORT: only the prewarm arm (D) closes the gap; WebSocket moves into W1"
    else:
        verdict = "TRANSPORT: only the Codex CLI control closes the gap; WebSocket moves into W1"
    if qualifying and not enough:
        verdict = "PROVISIONAL (needs >=3 qualifying days and >=120 measured calls per arm): " + verdict
    latency = {}
    for arm in RUNNER_ARMS:
        vals = [r["latency_s"] for r in rows
                if r["arm"] == arm and not r.get("error") and r.get("latency_s") is not None]
        ttft = [r["ttft_s"] for r in rows if r["arm"] == arm and r.get("ttft_s") is not None]
        if vals:
            latency[arm] = {"n": len(vals), "median_s": round(statistics.median(vals), 3),
                            "mean_s": round(statistics.fmean(vals), 3),
                            "median_ttft_s": round(statistics.median(ttft), 3) if ttft else None}
    return {
        "rule": (f"ship headers if B or C is <= {SHIP_MAX_RATE:.0%} zero-cache with an upper 95% bound "
                 f"< {SHIP_MAX_UPPER:.0%} on days when E is <= {CONTROL_MAX_RATE:.0%} and A is >= "
                 f"{BASELINE_MIN_RATE:.0%}; days where E is also bad are excluded as backend drift"),
        "days": per_day, "qualifying_days": qualifying, "pooled": pooled,
        "measured_calls_on_qualifying_days": counts, "enough_data": enough,
        "verdict": verdict, "latency": latency, "quota": quota_regression(rows),
        "unreachable_verdicts": [] if PREWARM_IMPLEMENTED else [D_VERDICT_UNREACHABLE],
        "total_rows": len(rows), "measured_rows": len(measured),
    }


def _used_percent(row: dict[str, Any]) -> float | None:
    raw = (row.get("quota") or {}).get("x-codex-primary-used-percent")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def quota_regression(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Regress the change in primary used-percent between consecutive
    observations (all arms, time order) on the uncached and cached tokens of
    the later call. The intercept absorbs other use of the account in the gap.
    Coarse: the header moves in whole or tenth percents."""
    obs = [(r, _used_percent(r)) for r in sorted(rows, key=lambda r: r.get("ts") or 0)
           if not r.get("error")]
    obs = [(r, p) for r, p in obs if p is not None]
    x, y = [], []
    for (prev, p0), (cur, p1) in zip(obs, obs[1:]):
        delta = p1 - p0
        if delta < 0:  # the window rolled over
            continue
        cached = int(cur.get("cached_tokens") or 0)
        uncached = max(0, int(cur.get("prompt_tokens") or 0) - cached)
        x.append([1.0, uncached / 1e6, cached / 1e6])
        y.append(delta)
    coef = ols(x, y) if len(x) >= 5 else None
    if coef is None:
        return {"n": len(x), "note": "not enough quota observations for a fit"}
    return {"n": len(x), "intercept_pct": round(coef[0], 4),
            "pct_per_million_uncached": round(coef[1], 4),
            "pct_per_million_cached": round(coef[2], 4)}


def format_summary(summary: dict[str, Any]) -> str:
    def fmt(s: dict[str, Any] | None) -> str:
        if not s or not s["n"]:
            return "n=0"
        return (f"{s['rate']:6.1%} zero-cache  [{s['lower']:.1%}, {s['upper']:.1%}]  "
                f"n={s['n']} clusters={s['clusters']}")
    lines = ["E1 cache routing: summary", f"Rule: {summary['rule']}", ""]
    for day, info in summary["days"].items():
        lines.append(f"{day}: {info['status']}")
        for arm, s in info["arms"].items():
            lines.append(f"  {arm}: {fmt(s)}")
    lines += ["", f"Pooled over qualifying days ({', '.join(summary['qualifying_days']) or 'none'}):"]
    for arm, s in summary["pooled"].items():
        lines.append(f"  {arm}: {fmt(s)}")
    lines += ["", f"Verdict: {summary['verdict']}"]
    lines += [f"Unreachable: {text}" for text in summary.get("unreachable_verdicts") or []]
    lines += [""]
    if summary["latency"]:
        lines.append("Latency (runner arms, all days):")
        for arm, lat in summary["latency"].items():
            lines.append(f"  {arm}: median {lat['median_s']}s, mean {lat['mean_s']}s, "
                         f"median TTFT {lat['median_ttft_s']}s, n={lat['n']}")
    q = summary["quota"]
    if "note" in q:
        lines.append(f"Quota: {q['note']} (n={q['n']})")
    else:
        lines.append(f"Quota: primary used-percent per 1M tokens: uncached {q['pct_per_million_uncached']}, "
                     f"cached {q['pct_per_million_cached']}, intercept {q['intercept_pct']} (n={q['n']})")
    return "\n".join(lines)


# ─── login ───────────────────────────────────────────────────────────────────


async def device_login(token_path: Path, *, client: httpx.AsyncClient | None = None,
                       sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                       log: Callable[[str], None] = print) -> Path:
    """Device-code login that saves a fluid token bundle. A separate login
    from the Codex CLI's, so the two never rotate each other's refresh token."""
    from fluid.providers.openai_responses import codex_oauth as oauth

    start = await oauth.start_device_code(client=client)
    log(f"Open {start.verification_uri} and enter the code {start.user_code}")
    deadline = time.monotonic() + start.expires_in
    interval = start.interval
    while time.monotonic() < deadline:
        await sleep(interval)
        try:
            bundle = await oauth.poll_device_code(start.device_code, start.code_verifier,
                                                  start.user_code, client=client)
        except oauth.CodexAuthPending as pending:
            if pending.slow_down:
                interval += 5
            continue
        path = oauth.save_bundle(oauth.StoredBundle.from_bundle(bundle), token_path)
        log(f"Saved the token to {path}")
        return path
    raise SystemExit("device code expired before it was approved")


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _live_factory(args: argparse.Namespace) -> ClientFactory:
    if "chatgpt.com" in args.base_url:
        from fluid.providers.openai_responses.codex_oauth import DEFAULT_TOKEN_PATH, CodexAuth

        auth = CodexAuth(token_path=Path(args.token_path or DEFAULT_TOKEN_PATH))
        return default_client_factory(base_url=args.base_url, auth=auth)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY is not set")
    return default_client_factory(base_url=args.base_url, api_key=key)


def _cmd_run(args: argparse.Namespace) -> int:
    arms = tuple(a.strip().upper() for a in args.arms.split(",") if a.strip())
    bad = [a for a in arms if a not in RUNNER_ARMS]
    if bad:
        raise SystemExit(f"unknown arm(s) {bad}; the runner drives {RUNNER_ARMS}")
    if "D" in arms and not PREWARM_IMPLEMENTED:
        arms = tuple(a for a in arms if a != "D")
        print(PREWARM_SKIP_LINE)
        if not arms:
            raise SystemExit("no arm left to run")
    out = Path(args.out or f"e1-{'dry-' if args.dry_run else ''}{datetime.now():%Y%m%d-%H%M%S}.jsonl")
    cfg = RunConfig(
        out=out, model=args.model, effort=None if args.effort == "none-sent" else args.effort,
        calls_per_arm=args.calls_per_arm, block_size=args.block_size,
        gap_min=args.gap_min, gap_max=args.gap_max, start_tokens=args.start_tokens,
        round_tokens=args.round_tokens, max_tokens=args.max_tokens, seed=args.seed,
        dry_run=args.dry_run, skip_contract_check=args.skip_contract_check, arms=arms,
    )
    total = len(arms) * cfg.calls_per_arm
    mean_prompt = (cfg.start_tokens + cfg.max_tokens) / 2
    print(f"Plan: {total} calls ({cfg.calls_per_arm} per arm, arms {','.join(arms)}), "
          f"model {cfg.model}, prompts {cfg.start_tokens}-{cfg.max_tokens} tokens, "
          f"about {total * mean_prompt / 1e6:.1f}M input tokens, gaps {cfg.gap_min}-{cfg.gap_max}s. "
          f"Log: {out}")
    if args.dry_run:
        backend = FakeCodexBackend(seed=args.seed or 0, model=cfg.model)
        transport = httpx.MockTransport(backend.handle)
        factory = default_client_factory(base_url=CODEX_RESPONSES_URL, auth=FakeAuth())

        async def no_sleep(_: float) -> None:
            return None
        asyncio.run(run_experiment(cfg, factory, transport=transport,
                                   sleep=no_sleep if not args.real_gaps else asyncio.sleep))
    else:
        if not args.yes:
            answer = input("This spends real quota. Type 'yes' to start: ").strip().lower()
            if answer != "yes":
                print("Not started.")
                return 1
        contract_factory = default_client_factory(base_url=CODEX_RESPONSES_URL, auth=FakeAuth())
        try:
            asyncio.run(run_experiment(cfg, _live_factory(args), contract_factory=contract_factory))
        except ContractError as exc:
            print(f"Refused: {exc}")
            return 2
    summary = summarize(read_jsonl([out]), include_dry_run=args.dry_run)
    print()
    print(format_summary(summary))
    return 0


def _cmd_summarize(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.logs]
    summary = summarize(read_jsonl(paths), include_dry_run=args.include_dry_run)
    print(format_summary(summary))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


def _cmd_prepare_codex(args: argparse.Namespace) -> int:
    prompts = prepare_codex_workspace(Path(args.dir))
    print(f"Wrote {len(prompts)} turn prompts and the files to read under {args.dir}")
    return 0


def _cmd_ingest_codex(args: argparse.Namespace) -> int:
    root = Path(args.sessions).expanduser()
    files = sorted(root.rglob("rollout-*.jsonl")) if root.is_dir() else [root]
    since = datetime.fromisoformat(args.since).timestamp() if args.since else None
    rows = ingest_codex_rollouts(files, cwd=args.cwd, since=since)
    out = Path(args.out)
    if out.exists() and not args.append:
        out.unlink()
    for row in rows:
        append_jsonl(out, row)
    print(f"Wrote {len(rows)} arm-E rows from {len(files)} rollout files to {out}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    from fluid.providers.openai_responses.codex_oauth import DEFAULT_TOKEN_PATH

    asyncio.run(device_login(Path(args.token_path or DEFAULT_TOKEN_PATH)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="make the calls")
    run.add_argument("--dry-run", action="store_true", help="fake backend, no network, no quota")
    run.add_argument("--real-gaps", action="store_true", help="sleep the gaps in a dry run too")
    run.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    run.add_argument("--skip-contract-check", action="store_true")
    run.add_argument("--out", help="JSONL log path")
    run.add_argument("--model", default="gpt-6-astra")
    run.add_argument("--effort", default="low", help="reasoning effort, or 'none-sent' to omit it")
    run.add_argument("--arms", default=",".join(RUNNER_ARMS))
    run.add_argument("--calls-per-arm", type=int, default=40,
                     help="per run; the rule needs >=120 per arm over >=3 days")
    run.add_argument("--block-size", type=int, default=5, help="calls per arm per block (one turn)")
    run.add_argument("--gap-min", type=float, default=2.0)
    run.add_argument("--gap-max", type=float, default=5.0)
    run.add_argument("--start-tokens", type=int, default=40_000)
    run.add_argument("--round-tokens", type=int, default=600)
    run.add_argument("--max-tokens", type=int, default=60_000)
    run.add_argument("--seed", type=int)
    run.add_argument("--base-url", default=CODEX_RESPONSES_URL)
    run.add_argument("--token-path", help="fluid Codex token file (default ~/.config/fluid/codex_auth.json)")
    run.set_defaults(func=_cmd_run)

    summ = sub.add_parser("summarize", help="apply the decision rule to logs")
    summ.add_argument("logs", nargs="+")
    summ.add_argument("--json", help="also write the summary as JSON here")
    summ.add_argument("--include-dry-run", action="store_true")
    summ.set_defaults(func=_cmd_summarize)

    prep = sub.add_parser("prepare-codex", help="write the arm-E workspace")
    prep.add_argument("--dir", required=True)
    prep.set_defaults(func=_cmd_prepare_codex)

    ing = sub.add_parser("ingest-codex", help="read arm-E rows from Codex CLI rollouts")
    ing.add_argument("--sessions", default="~/.codex/sessions")
    ing.add_argument("--cwd", required=True, help="only rollouts started in this workspace")
    ing.add_argument("--since", help="ISO date or datetime, local time")
    ing.add_argument("--out", required=True)
    ing.add_argument("--append", action="store_true")
    ing.set_defaults(func=_cmd_ingest_codex)

    login = sub.add_parser("login", help="device-code login for a fluid token file")
    login.add_argument("--token-path")
    login.set_defaults(func=_cmd_login)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
