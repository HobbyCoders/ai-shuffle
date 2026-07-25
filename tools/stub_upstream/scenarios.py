"""Scenario definitions + wire serialization for the stub upstream.

TEST / DEV TOOLING ONLY — never imported from ``app/``.

The canned event payloads are NOT invented here: they are reused verbatim from
``tests/core/translator_gateway/_fixtures.py``, which was built from the live
SSE dumps recorded in ``docs/CODEX_SPIKE_FINDINGS.md``. This module only decides
*which* canned stream to serve and turns ``(event_name, payload)`` tuples back
into OpenAI-Responses SSE wire bytes.

Wire-shape notes (all grounded in docs/CODEX_SPIKE_FINDINGS.md, "Observed SSE
event inventory"):

  * Every frame is ``event: <name>`` + ``data: <json>`` + blank line.
  * The real backend repeats the event name inside the payload as ``type`` and
    carries a monotonic ``sequence_number``; both are re-added here because the
    live dumps have them (``upstream.iter_sse_events`` prefers the ``event:``
    line, so this is fidelity, not a requirement).
  * The real Codex backend does NOT emit a ``data: [DONE]`` sentinel — the
    stream simply ends after ``response.completed``. That is the default here;
    ``emit_done_sentinel`` opts into the api.openai.com-style sentinel.

Fidelity limits are documented in README.md ("Where the stub is NOT faithful").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple

Event = Tuple[str, Dict[str, Any]]


# ─── Canned payload source (tests/core/translator_gateway/_fixtures.py) ──────

def _fixtures():
    """Import the shared gateway fixtures lazily with an actionable error."""
    try:
        from tests.core.translator_gateway import _fixtures  # type: ignore
    except ImportError as exc:  # pragma: no cover - operator misconfiguration
        raise RuntimeError(
            "stub_upstream reuses the canned Responses payloads from "
            "tests/core/translator_gateway/_fixtures.py. Run it from the repo "
            "root (or with the repo root on PYTHONPATH) so 'tests' is "
            f"importable. Original error: {exc}"
        ) from exc
    return _fixtures


# ─── Scenario ────────────────────────────────────────────────────────────────

SCENARIO_NAMES = (
    "text",          # normal streaming assistant reply
    "tool_call",     # streaming function-call turn
    "reasoning",     # reasoning summary + encrypted_content + text
    "slow",          # 'text', but with configurable delays (timeout paths)
    "stream_error",  # partial text, then response.failed + error, clean close
    "truncate",      # partial text, then the connection drops mid-stream
    "rate_limit",    # HTTP 429 + Retry-After (circuit-breaker verification)
    "auth_error",    # HTTP 401 (codex refresh-once-then-retry path)
    "http_status",   # arbitrary HTTP status, configurable
)


@dataclass
class Scenario:
    """One canned upstream behavior. Every field is settable over the control
    API, so scenarios are chosen without editing code."""

    name: str = "text"

    # ── streaming content ──
    text: str = "PONG from the stub upstream."
    model: str = "gpt-5.6-sol"
    response_id: Optional[str] = None
    n_deltas: int = 3
    emit_done_sentinel: bool = False

    # ── tool_call ──
    tool_name: str = "get_weather"
    tool_arguments: str = '{"city":"Paris","unit":"celsius","include_forecast":true}'
    tool_call_id: str = "call_stub_0001"

    # ── reasoning ──
    reasoning_summary: str = "**Thinking about the stubbed request**"
    reasoning_answer: str = "42"

    # ── HTTP error scenarios ──
    status: int = 500
    retry_after: Optional[str] = "30"
    error_type: str = "rate_limit_exceeded"
    error_code: Optional[str] = None
    error_message: str = (
        "Rate limit reached for this account. Please try again later."
    )
    # Extra response headers merged onto EVERY response (e.g. the x-codex-*
    # quota headers that provider_usage.capture_codex_headers consumes).
    headers: Dict[str, str] = field(default_factory=dict)

    # ── timing (seconds) ──
    delay_before_headers: float = 0.0
    delay_before_first_event: float = 0.0
    delay_between_events: float = 0.0
    # 'slow' only: stall this long after the last event before closing. Bounded
    # by the server's --max-hang-seconds so a stub never wedges forever.
    hang_seconds: float = 0.0

    # ── stream_error only ──
    fail_after_events: int = 4

    def merged(self, overrides: Dict[str, Any]) -> "Scenario":
        """Return a copy with ``overrides`` applied (unknown keys rejected)."""
        known = {f.name for f in fields(self)}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise ValueError(f"unknown scenario field(s): {', '.join(unknown)}")
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(overrides)
        if data["name"] not in SCENARIO_NAMES:
            raise ValueError(
                f"unknown scenario {data['name']!r}; "
                f"expected one of {', '.join(SCENARIO_NAMES)}"
            )
        return Scenario(**data)

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def scenario_from_dict(data: Dict[str, Any]) -> Scenario:
    return Scenario().merged(data or {})


# ─── HTTP-level outcome ──────────────────────────────────────────────────────

@dataclass
class HttpOutcome:
    """What the server should send for one request."""

    status: int
    headers: Dict[str, str]
    # Either a complete body (error responses) ...
    body: Optional[bytes] = None
    # ... or a streamed SSE event list (2xx responses).
    events: Optional[List[Event]] = None
    # Emit the trailing ``data: [DONE]`` sentinel after the events.
    done_sentinel: bool = False


def _error_body(scn: Scenario) -> bytes:
    """An OpenAI-shaped error body.

    Modeled on OpenAI's documented platform error envelope. NOTE: the Codex
    backend's own 429 body was never captured live in
    docs/CODEX_SPIKE_FINDINGS.md, so the *body* is a plausible reconstruction.
    The parts the gateway actually keys on — the status code and the
    ``Retry-After`` header — are exact.
    """
    return json.dumps(
        {
            "error": {
                "message": scn.error_message,
                "type": scn.error_type,
                "param": None,
                "code": scn.error_code or scn.error_type,
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")


def build_outcome(scn: Scenario) -> HttpOutcome:
    """Turn a :class:`Scenario` into the HTTP outcome to serve."""
    fx = _fixtures()
    extra = dict(scn.headers or {})

    if scn.name == "rate_limit":
        headers = {"Content-Type": "application/json"}
        if scn.retry_after is not None:
            headers["Retry-After"] = str(scn.retry_after)
        headers.update(extra)
        return HttpOutcome(status=429, headers=headers, body=_error_body(scn))

    if scn.name == "auth_error":
        headers = {"Content-Type": "application/json"}
        headers.update(extra)
        body_scn = scn
        if scn.error_type == Scenario.error_type:  # caller left the default
            body_scn = scn.merged(
                {
                    "error_type": "invalid_request_error",
                    "error_code": "invalid_api_key",
                    "error_message": "Incorrect API key provided.",
                }
            )
        return HttpOutcome(status=401, headers=headers, body=_error_body(body_scn))

    if scn.name == "http_status":
        headers = {"Content-Type": "application/json"}
        if scn.retry_after is not None and scn.status == 429:
            headers["Retry-After"] = str(scn.retry_after)
        headers.update(extra)
        return HttpOutcome(status=int(scn.status), headers=headers, body=_error_body(scn))

    # ── streaming scenarios ──
    if scn.name == "tool_call":
        events = fx.tool_call_stream(
            arguments=scn.tool_arguments,
            call_id=scn.tool_call_id,
            name=scn.tool_name,
            response_id=scn.response_id or "resp_stub_tool",
            model=scn.model,
        )
    elif scn.name == "reasoning":
        events = fx.reasoning_stream(
            summary_text=scn.reasoning_summary,
            answer=scn.reasoning_answer,
            response_id=scn.response_id or "resp_stub_reason",
            model=scn.model,
        )
    else:  # text | slow | stream_error | truncate
        events = fx.plain_text_stream(
            text=scn.text,
            response_id=scn.response_id or "resp_stub_text",
            model=scn.model,
            n_deltas=max(1, int(scn.n_deltas)),
        )

    if scn.name == "stream_error":
        events = _truncate_with_error(events, scn)
    elif scn.name == "truncate":
        events = events[: max(1, int(scn.fail_after_events))]

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-store",
    }
    headers.update(extra)
    return HttpOutcome(
        status=200,
        headers=headers,
        events=events,
        done_sentinel=bool(scn.emit_done_sentinel),
    )


def _truncate_with_error(events: List[Event], scn: Scenario) -> List[Event]:
    """Cut a healthy stream short and append a mid-stream failure.

    IMPORTANT (fidelity): ``response.failed`` and ``error`` were listed in
    docs/CODEX_SPIKE_FINDINGS.md as *not observed* on the live backend for the
    spike prompts. The frame shapes below follow the public OpenAI Responses
    event schema, so treat this scenario as "a plausible mid-stream failure",
    not "the exact bytes ChatGPT sends".
    """
    keep = max(1, int(scn.fail_after_events))
    head = events[:keep]
    response_id = scn.response_id or "resp_stub_text"
    err = {
        "message": scn.error_message,
        "type": scn.error_type,
        "code": scn.error_code or scn.error_type,
        "param": None,
    }
    head.append(
        (
            "response.failed",
            {
                "response": {
                    "id": response_id,
                    "model": scn.model,
                    "status": "failed",
                    "output": [],
                    "error": err,
                }
            },
        )
    )
    head.append(("error", dict(err)))
    return head


# ─── SSE serialization ───────────────────────────────────────────────────────

def sse_frame(name: str, payload: Dict[str, Any], sequence_number: int) -> bytes:
    """Encode one Responses SSE frame exactly as the live dumps show it."""
    body = dict(payload)
    body.setdefault("type", name)
    body.setdefault("sequence_number", sequence_number)
    return (
        f"event: {name}\n"
        f"data: {json.dumps(body, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def sse_bytes(events: List[Event], *, done_sentinel: bool = False) -> bytes:
    """Serialize a whole event list (used by tests and by --dump)."""
    out = b"".join(sse_frame(n, p, i) for i, (n, p) in enumerate(events))
    if done_sentinel:
        out += b"data: [DONE]\n\n"
    return out


# ─── Anthropic passthrough (best-effort; see README fidelity notes) ──────────

def anthropic_messages_sse(scn: Scenario) -> bytes:
    """A well-formed Anthropic Messages SSE stream for the passthrough mode.

    The gateway forwards passthrough bytes verbatim without parsing them, so
    this is included only so an ``anthropic`` provider row pointed at the stub
    does not dead-end. It has NOT been validated against the bundled Claude Code
    CLI's parser — see README ("Where the stub is NOT faithful").
    """
    def frame(kind: str, data: Dict[str, Any]) -> bytes:
        return (
            f"event: {kind}\n"
            f"data: {json.dumps(dict(data, type=kind), separators=(',', ':'))}\n\n"
        ).encode("utf-8")

    fx = _fixtures()
    frags = fx.split_into(scn.text, max(1, int(scn.n_deltas)))
    out = [
        frame(
            "message_start",
            {
                "message": {
                    "id": "msg_stub_0001",
                    "type": "message",
                    "role": "assistant",
                    "model": scn.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                }
            },
        ),
        frame(
            "content_block_start",
            {"index": 0, "content_block": {"type": "text", "text": ""}},
        ),
    ]
    for frag in frags:
        out.append(
            frame(
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": frag}},
            )
        )
    out += [
        frame("content_block_stop", {"index": 0}),
        frame(
            "message_delta",
            {
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": len(scn.text)},
            },
        ),
        frame("message_stop", {}),
    ]
    return b"".join(out)
