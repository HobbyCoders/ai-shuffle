"""E3: native compaction acceptance probe (design-final 6.1).

Sends, once each:

* ``openai_public``: a Responses request to ``api.openai.com`` whose input ends
  with ``{"type": "compaction_trigger"}`` (OPENAI_API_KEY).
* ``openai_codex``: the same request to the ChatGPT Codex backend
  (fluid Codex token file).
* ``anthropic``: one Messages request carrying a ``cache_control`` block with
  ``ttl: "1h"``, the context-editing edits ``clear_tool_uses_20250919`` and
  ``clear_thinking_20251015`` under beta ``context-management-2025-06-27``, and
  server compaction ``compact_20260112`` under beta ``compact-2026-01-12``
  (ANTHROPIC_API_KEY). If that combined request is rejected, each identifier
  is retried alone to find which one the API refused, and a rejected 1h TTL is
  retried once with beta ``extended-cache-ttl-2025-04-11``.

Each probe reports ``accepted`` (HTTP 200 and the feature visibly took effect),
``accepted_no_effect`` (HTTP 200, no evidence either way), ``rejected`` (4xx,
with the API's message), ``error`` or ``skipped`` (no credential). Well under
$5: a handful of requests of about 2-6k input tokens.

``--dry-run`` runs every probe against a fake transport and spends nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.experiments._common import ensure_import_paths, responses_sse  # noqa: E402

ensure_import_paths()

import httpx  # noqa: E402

from fluid.providers._sse import sse_events  # noqa: E402
from fluid.providers.openai_responses.client import API_URL, CODEX_RESPONSES_URL  # noqa: E402

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
BETA_CONTEXT_MANAGEMENT = "context-management-2025-06-27"
BETA_COMPACTION = "compact-2026-01-12"
BETA_EXTENDED_TTL = "extended-cache-ttl-2025-04-11"
EDIT_CLEAR_TOOL_USES = "clear_tool_uses_20250919"
EDIT_CLEAR_THINKING = "clear_thinking_20251015"
EDIT_COMPACT = "compact_20260112"
COMPACTION_TRIGGER = {"type": "compaction_trigger"}

# Pre-stated use of the result, design-final 6.1 (E3): decides whether W2's
# native compaction tiers exist.
DECIDES = "Whether W2's native tiers exist"


@dataclass
class ProbeResult:
    probe: str
    outcome: str  # accepted | accepted_no_effect | rejected | error | skipped
    status: int | None = None
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


def _padding(tokens: int) -> str:
    line = "Reference note: the probe pads this block so it can be cached. "
    return (line * (tokens * 4 // len(line) + 1))[: tokens * 4]


def _conversation() -> list[dict[str, Any]]:
    """A short history for the OpenAI probes: two user turns, one reply."""
    return [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "We are testing a compaction request. " + _padding(1500)}]},
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Understood. I will keep the notes short."}]},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Remember that the project codename is HERON."}]},
    ]


def openai_body(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": "You are a concise assistant.",
        "input": _conversation() + [dict(COMPACTION_TRIGGER)],
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
    }


async def _read_sse(resp: httpx.Response) -> tuple[list[dict[str, Any]], dict[str, Any] | None, Any]:
    """Output items, the terminal response object and any stream error."""
    items: list[dict[str, Any]] = []
    terminal = None
    error = None

    async def lines():
        async for line in resp.aiter_lines():
            yield line

    async for name, data in sse_events(lines()):
        data = data or {}
        name = name or data.get("type", "")
        if name == "response.output_item.done":
            items.append(data.get("item") or {})
        elif name in ("response.completed", "response.incomplete"):
            terminal = data.get("response") or {}
        elif name in ("response.failed", "error"):
            error = (data.get("response") or {}).get("error") or data.get("error") or data
    return items, terminal, error


async def probe_openai(name: str, http: httpx.AsyncClient, url: str, headers: dict[str, str],
                       model: str) -> ProbeResult:
    try:
        async with http.stream("POST", url, json=openai_body(model), headers=headers) as resp:
            if resp.status_code != 200:
                text = (await resp.aread()).decode(errors="replace")
                outcome = "rejected" if 400 <= resp.status_code < 500 else "error"
                return ProbeResult(name, outcome, resp.status_code, text[:800])
            items, terminal, error = await _read_sse(resp)
    except httpx.HTTPError as exc:
        return ProbeResult(name, "error", None, f"{type(exc).__name__}: {exc}")
    types = [i.get("type") for i in items]
    usage = (terminal or {}).get("usage") or {}
    evidence = {"output_item_types": types, "usage": usage}
    if error is not None:
        return ProbeResult(name, "rejected", 200, json.dumps(error)[:800], evidence)
    compactions = [i for i in items if "compaction" in str(i.get("type", ""))]
    if compactions:
        evidence["compaction_item_keys"] = sorted(compactions[0].keys())
        return ProbeResult(name, "accepted", 200, f"{len(compactions)} compaction item(s) returned", evidence)
    return ProbeResult(name, "accepted_no_effect", 200,
                       "HTTP 200 but no compaction item; the trigger may have been ignored", evidence)


def codex_headers(token: str, account_id: str | None) -> dict[str, str]:
    """The same headers fluid's client sends to the Codex backend today."""
    headers = {"content-type": "application/json", "accept": "text/event-stream",
               "Authorization": f"Bearer {token}", "OpenAI-Beta": "responses=experimental",
               "originator": "codex_cli_rs"}
    if account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


# ─── Anthropic ───────────────────────────────────────────────────────────────


def anthropic_body(model: str, *, ttl: bool = True, edits: list[str] = ()) -> dict[str, Any]:
    """One request with a tool round in history, so context editing has
    something it could clear, and a system block large enough to be cached."""
    system_block: dict[str, Any] = {"type": "text", "text": "You are a concise assistant.\n" + _padding(5000)}
    if ttl:
        system_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 1024,
        "system": [system_block],
        "tools": [{"name": "lookup", "description": "Look up a project fact.",
                   "input_schema": {"type": "object", "properties": {"q": {"type": "string"}},
                                    "required": ["q"]}}],
        "messages": [
            {"role": "user", "content": "What is the project codename? Use the lookup tool."},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_probe_1", "name": "lookup", "input": {"q": "codename"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_probe_1", "content": "HERON"}]},
            # A closed turn and a fresh question: with thinking on, an open
            # tool loop would need a thinking block the probe cannot forge.
            {"role": "assistant", "content": "The codename is HERON."},
            {"role": "user", "content": "Reply with the single word OK."},
        ],
    }
    if EDIT_CLEAR_THINKING in edits:
        # clear_thinking only applies with thinking on.
        body["thinking"] = {"type": "adaptive"}
    if edits:
        body["context_management"] = {"edits": [{"type": e} for e in edits]}
    return body


def _betas_for(edits: list[str], extra: list[str] = ()) -> list[str]:
    betas = list(extra)
    if any(e in (EDIT_CLEAR_TOOL_USES, EDIT_CLEAR_THINKING) for e in edits):
        betas.append(BETA_CONTEXT_MANAGEMENT)
    if EDIT_COMPACT in edits:
        betas.append(BETA_COMPACTION)
    return betas


async def _anthropic_call(http: httpx.AsyncClient, api_key: str, body: dict[str, Any],
                          betas: list[str]) -> tuple[int | None, dict[str, Any] | str]:
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
               "content-type": "application/json"}
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    try:
        resp = await http.post(ANTHROPIC_URL, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:800]


def _anthropic_result(name: str, status: int | None, payload: Any, *, want_ttl: bool) -> ProbeResult:
    if status is None:
        return ProbeResult(name, "error", None, str(payload))
    if status != 200:
        message = payload.get("error", {}).get("message") if isinstance(payload, dict) else str(payload)
        return ProbeResult(name, "rejected" if 400 <= status < 500 else "error", status, str(message)[:800])
    usage = payload.get("usage") or {}
    creation = usage.get("cache_creation") or {}
    evidence = {"usage": usage, "context_management": payload.get("context_management"),
                "content_types": [b.get("type") for b in payload.get("content") or []]}
    if not want_ttl:
        return ProbeResult(name, "accepted", 200, "", evidence)
    # Acceptance rule for the 1h TTL: only a reported 1h cache write proves the
    # TTL took effect. A cache read proves a cache was hit, not which TTL wrote
    # it (a re-run inside the 5m window reads a 5m entry just as well), and a
    # 5m write where 1h was asked for means the TTL was ignored.
    if creation.get("ephemeral_1h_input_tokens"):
        return ProbeResult(name, "accepted", 200, "1h cache write reported", evidence)
    if creation.get("ephemeral_5m_input_tokens"):
        return ProbeResult(name, "accepted_no_effect", 200,
                           "HTTP 200 but the write was reported at 5m, not 1h: the TTL was ignored", evidence)
    if usage.get("cache_read_input_tokens"):
        return ProbeResult(name, "accepted_no_effect", 200,
                           "HTTP 200 with a cache read but no write: re-run after 5 minutes so the "
                           "write is visible", evidence)
    return ProbeResult(name, "accepted_no_effect", 200, "HTTP 200 but no 1h cache write was reported", evidence)


# clear_thinking goes first: the API applies edits in order and wants the
# thinking edit ahead of the tool-use edit when both are present.
ALL_EDITS = [EDIT_CLEAR_THINKING, EDIT_CLEAR_TOOL_USES, EDIT_COMPACT]


async def probe_anthropic(http: httpx.AsyncClient, api_key: str, model: str) -> list[ProbeResult]:
    """The combined request first; isolate identifiers only if it is refused."""
    status, payload = await _anthropic_call(http, api_key, anthropic_body(model, edits=ALL_EDITS),
                                            _betas_for(ALL_EDITS))
    combined = _anthropic_result("anthropic_combined", status, payload, want_ttl=True)
    results = [combined]
    if combined.outcome in ("accepted", "accepted_no_effect"):
        return results
    status, payload = await _anthropic_call(http, api_key, anthropic_body(model), [])
    ttl = _anthropic_result("anthropic_ttl_1h_no_beta", status, payload, want_ttl=True)
    results.append(ttl)
    if ttl.outcome == "rejected":
        status, payload = await _anthropic_call(http, api_key, anthropic_body(model), [BETA_EXTENDED_TTL])
        results.append(_anthropic_result("anthropic_ttl_1h_with_beta", status, payload, want_ttl=True))
    for edit in ALL_EDITS:
        status, payload = await _anthropic_call(
            http, api_key, anthropic_body(model, ttl=False, edits=[edit]), _betas_for([edit]))
        results.append(_anthropic_result(f"anthropic_{edit}", status, payload, want_ttl=False))
    return results


# ─── orchestration ───────────────────────────────────────────────────────────


@dataclass
class ProbeConfig:
    public_model: str = "gpt-6-astra"
    codex_model: str = "gpt-6-astra"
    anthropic_model: str = "claude-opus-5"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    codex_token: str | None = None
    codex_account_id: str | None = None
    only: tuple[str, ...] = ("openai_public", "openai_codex", "anthropic")


async def run_probes(cfg: ProbeConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    async with httpx.AsyncClient(timeout=300.0, transport=transport) as http:
        if "openai_public" in cfg.only:
            if not cfg.openai_api_key:
                results.append(ProbeResult("openai_public", "skipped", detail="OPENAI_API_KEY not set"))
            else:
                headers = {"content-type": "application/json", "accept": "text/event-stream",
                           "Authorization": f"Bearer {cfg.openai_api_key}"}
                results.append(await probe_openai("openai_public", http, API_URL, headers, cfg.public_model))
        if "openai_codex" in cfg.only:
            if not cfg.codex_token:
                results.append(ProbeResult("openai_codex", "skipped", detail="no Codex token file"))
            else:
                results.append(await probe_openai(
                    "openai_codex", http, CODEX_RESPONSES_URL,
                    codex_headers(cfg.codex_token, cfg.codex_account_id), cfg.codex_model))
        if "anthropic" in cfg.only:
            if not cfg.anthropic_api_key:
                results.append(ProbeResult("anthropic", "skipped", detail="ANTHROPIC_API_KEY not set"))
            else:
                results.extend(await probe_anthropic(http, cfg.anthropic_api_key, cfg.anthropic_model))
    return results


class FakeProviders:
    """Offline stand-ins. The public API returns a compaction item, the Codex
    backend refuses the trigger, and Anthropic refuses the combined request
    because of the compaction beta, so every branch of the probe runs."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        body = json.loads(request.content or b"{}")
        if host == "api.openai.com":
            item = {"id": "cmp_1", "type": "compaction", "encrypted_content": "OPAQUE"}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=responses_sse(response_id="resp_pub", model=body.get("model", ""),
                                                        usage={"input_tokens": 1600, "output_tokens": 300},
                                                        text=None, extra_items=[item]))
        if host == "chatgpt.com":
            return httpx.Response(400, json={"detail": "Unsupported input item type: compaction_trigger"})
        if host == "api.anthropic.com":
            betas = request.headers.get("anthropic-beta", "")
            edits = [e["type"] for e in (body.get("context_management") or {}).get("edits", [])]
            if edits and BETA_CONTEXT_MANAGEMENT not in betas and EDIT_COMPACT not in edits:
                return httpx.Response(400, json={"type": "error", "error": {
                    "type": "invalid_request_error", "message": "context_management requires a beta"}})
            if EDIT_COMPACT in edits:
                return httpx.Response(400, json={"type": "error", "error": {
                    "type": "invalid_request_error", "message": f"unknown edit type {EDIT_COMPACT}"}})
            ttl = any((b.get("cache_control") or {}).get("ttl") == "1h" for b in body.get("system", []))
            usage = {"input_tokens": 60, "output_tokens": 12,
                     "cache_creation": {"ephemeral_1h_input_tokens": 5000 if ttl else 0,
                                        "ephemeral_5m_input_tokens": 0}}
            payload = {"id": "msg_fake", "type": "message", "role": "assistant",
                       "content": [{"type": "text", "text": "HERON"}], "usage": usage}
            if edits:
                payload["context_management"] = {"applied_edits": []}
            return httpx.Response(200, json=payload)
        return httpx.Response(404, text="unknown host")


def format_results(results: list[ProbeResult]) -> str:
    lines = [f"E3 native compaction acceptance. Decides: {DECIDES}."]
    for r in results:
        status = f" HTTP {r.status}" if r.status is not None else ""
        lines.append(f"  {r.probe:36s} {r.outcome}{status}" + (f"  {r.detail}" if r.detail else ""))
    return "\n".join(lines)


def _load_codex_token(token_path: str | None) -> tuple[str | None, str | None]:
    from fluid.providers.openai_responses.codex_oauth import DEFAULT_TOKEN_PATH, CodexAuth

    auth = CodexAuth(token_path=Path(token_path or DEFAULT_TOKEN_PATH))
    try:
        token = asyncio.run(auth.get_access_token())
    except Exception as exc:  # noqa: BLE001 - reported as a skipped probe
        print(f"Codex token unavailable: {exc}")
        return None, None
    return token, auth.account_id


def main(argv: list[str] | None = None, *, emit: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="fake transport, no network, no cost")
    parser.add_argument("--only", default="openai_public,openai_codex,anthropic")
    parser.add_argument("--public-model", default="gpt-6-astra")
    parser.add_argument("--codex-model", default="gpt-6-astra")
    parser.add_argument("--anthropic-model", default="claude-opus-5")
    parser.add_argument("--token-path", help="fluid Codex token file")
    parser.add_argument("--out", help="write the results as JSON here")
    args = parser.parse_args(argv)
    cfg = ProbeConfig(public_model=args.public_model, codex_model=args.codex_model,
                      anthropic_model=args.anthropic_model,
                      only=tuple(p.strip() for p in args.only.split(",") if p.strip()))
    transport = None
    if args.dry_run:
        transport = httpx.MockTransport(FakeProviders().handle)
        cfg.openai_api_key = cfg.anthropic_api_key = "dry-run-key"
        cfg.codex_token, cfg.codex_account_id = "dry-run-token", "acct_dry_run"
    else:
        cfg.openai_api_key = os.environ.get("OPENAI_API_KEY")
        cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        if "openai_codex" in cfg.only:
            cfg.codex_token, cfg.codex_account_id = _load_codex_token(args.token_path)
    results = asyncio.run(run_probes(cfg, transport=transport))
    emit(format_results(results))
    if args.out:
        Path(args.out).write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
