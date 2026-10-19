"""An Anthropic Messages endpoint that answers through fluid's Codex client.

Claude Code only speaks the Anthropic API. Pointing ``ANTHROPIC_BASE_URL`` at
this proxy lets it run on a ChatGPT subscription through the SAME client the
bare fluid arm uses (``metered.MeteredClient`` around fluid's
``OpenAIResponsesClient``): same auth, model, effort, cache routing and retry
policy. What differs between the arms is then the harness itself.

    python -m scripts.harness_h2h.anthropic_proxy --port 18765 --log calls.jsonl

Translation choices that matter for a fair comparison:

* Every model Claude Code asks for maps to one target model. Its small/fast
  model (anything with "haiku" in the name) runs at ``--small-effort``,
  everything else at ``--effort``.
* The prompt-cache key is Claude Code's own session id (from
  ``metadata.user_id``) plus a hash of the system prompt, so each agent
  thread (the main loop, each subagent type) gets its own cache partition the
  way fluid gives each loop its own key. Side calls get their own partition.
* OpenAI's encrypted reasoning item rides back to Claude Code inside the
  thinking block's ``signature`` and is decoded on the way in, so reasoning
  survives a tool loop the way fluid keeps it.
* Server tools (web search, code execution) are dropped from the tool list:
  the Codex backend cannot run Anthropic's server tools.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fluid.messages import (
    Image,
    Message,
    Reasoning,
    Text,
    ToolResultBlock,
    ToolUseBlock,
)
from fluid.providers.base import ProviderRequest, WireState

SIGNATURE_PREFIX = "fluidrs1:"
_SESSION_RE = re.compile(r"session_([0-9a-fA-F-]{8,})")


# ─── request: Anthropic → fluid ─────────────────────────────────────────────


def encode_signature(item: dict[str, Any]) -> str:
    raw = json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode()
    return SIGNATURE_PREFIX + base64.b64encode(raw).decode()


def decode_signature(signature: str | None) -> dict[str, Any] | None:
    if not signature or not signature.startswith(SIGNATURE_PREFIX):
        return None
    try:
        return json.loads(base64.b64decode(signature[len(SIGNATURE_PREFIX):]))
    except (ValueError, TypeError):
        return None


def _system_text(system: Any) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    parts = [b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"]
    return "\n\n".join(p for p in parts if p)


def _result_content(content: Any) -> list:
    if content is None:
        return []
    if isinstance(content, str):
        return [Text(content)]
    out: list = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out.append(Text(block.get("text", "")))
        elif kind == "image":
            src = block.get("source") or {}
            if src.get("type") == "base64" and src.get("data"):
                out.append(Image(media_type=src.get("media_type", "image/png"), data=src["data"]))
        else:
            out.append(Text(json.dumps(block)[:20000]))
    return out


def convert_messages(messages: list[dict[str, Any]], model: str) -> list[Message]:
    out: list[Message] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        if isinstance(content, str):
            out.append(Message(role, [Text(content)]))
            continue
        blocks: list = []
        for block in content or ():
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                if block.get("text"):
                    blocks.append(Text(block["text"]))
            elif kind == "image":
                src = block.get("source") or {}
                if src.get("type") == "base64" and src.get("data"):
                    blocks.append(Image(media_type=src.get("media_type", "image/png"), data=src["data"]))
            elif kind == "tool_use":
                blocks.append(ToolUseBlock(id=block["id"], name=block["name"],
                                           input=block.get("input") or {}))
            elif kind == "tool_result":
                blocks.append(ToolResultBlock(
                    tool_use_id=block["tool_use_id"],
                    content=_result_content(block.get("content")),
                    is_error=bool(block.get("is_error")),
                ))
            elif kind == "thinking":
                item = decode_signature(block.get("signature"))
                if item is not None:
                    blocks.append(Reasoning(origin="openai_responses", raw=item, model=model))
            elif kind == "document":
                src = block.get("source") or {}
                if src.get("type") == "text":
                    blocks.append(Text(src.get("data", "")))
            # redacted_thinking, server tool blocks: nothing the backend can use
        if blocks:
            out.append(Message(role, blocks))
    return out


def convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for tool in tools or ():
        ttype = tool.get("type")
        if ttype not in (None, "custom"):
            continue  # Anthropic server tool (web_search_*, code_execution_*, ...)
        schema = dict(tool.get("input_schema") or {"type": "object", "properties": {}})
        schema.pop("$schema", None)
        out.append({"name": tool["name"], "description": tool.get("description", ""),
                    "input_schema": schema})
    return out


def convert_tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "any":
        return "required"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "name": choice["name"]}
    if kind == "none":
        return "none"
    return None


def session_of(body: dict[str, Any]) -> str:
    user_id = str((body.get("metadata") or {}).get("user_id") or "")
    m = _SESSION_RE.search(user_id)
    if m:
        return m.group(1)
    # Newer Claude Code builds send JSON in user_id.
    try:
        parsed = json.loads(user_id)
        if isinstance(parsed, dict) and parsed.get("session_id"):
            return str(parsed["session_id"])
    except ValueError:
        pass
    return user_id or "unknown"


def is_small(model: str) -> bool:
    return "haiku" in (model or "").lower()


def build_request(body: dict[str, Any], *, target_model: str, effort: str,
                  small_effort: str, wire_states: dict[str, WireState]) -> ProviderRequest:
    requested = str(body.get("model") or "")
    small = is_small(requested)
    session = session_of(body)
    system = _system_text(body.get("system")) or ""
    thread = hashlib.sha1(system.encode()).hexdigest()[:8]
    key = f"cc:{session}:{thread}:small" if small else f"cc:{session}:{thread}"
    wire = wire_states.setdefault(key, WireState(session_id=key, role="main"))
    extra: dict[str, Any] = {"prompt_cache_key": key}
    choice = convert_tool_choice(body.get("tool_choice"))
    if choice is not None:
        extra["tool_choice"] = choice
    return ProviderRequest(
        model=target_model,
        messages=convert_messages(body.get("messages") or [], target_model),
        system=_system_text(body.get("system")),
        tools=convert_tools(body.get("tools")),
        effort=small_effort if small else effort,
        extra=extra,
        wire_state=wire,
    )


# ─── response: fluid StreamChunks → Anthropic SSE ───────────────────────────


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class AnthropicStreamWriter:
    """Turns fluid StreamChunks into Anthropic Messages SSE events."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        self._index: dict[int, int] = {}
        self._kind: dict[int, str] = {}
        self._next = 0
        self.started = False
        self.usage: dict[str, int] = {}
        self.stop_reason = "end_turn"
        # Non-streaming accumulation.
        self.blocks: list[dict[str, Any]] = []
        self._args: dict[int, list[str]] = {}

    def start(self) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        return [_sse("message_start", {"type": "message_start", "message": {
            "id": self.msg_id, "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }})]

    def _open(self, fluid_index: int, block_type: str, payload: dict[str, Any]) -> list[bytes]:
        if fluid_index in self._index:
            return []
        idx = self._next
        self._next += 1
        self._index[fluid_index] = idx
        if block_type == "tool_use":
            block = {"type": "tool_use", "id": payload.get("tool_use_id") or f"call_{uuid.uuid4().hex[:12]}",
                     "name": payload.get("tool_name") or "", "input": {}}
            self._kind[fluid_index] = "tool_use"
            self._args[idx] = []
        elif block_type == "thinking":
            block = {"type": "thinking", "thinking": "", "signature": ""}
            self._kind[fluid_index] = "thinking"
        else:
            block = {"type": "text", "text": ""}
            self._kind[fluid_index] = "text"
        self.blocks.append(dict(block))
        return [_sse("content_block_start", {"type": "content_block_start", "index": idx,
                                             "content_block": block})]

    def _delta(self, fluid_index: int, delta: dict[str, Any]) -> bytes:
        idx = self._index[fluid_index]
        block = self.blocks[idx]
        if delta["type"] == "text_delta":
            block["text"] = block.get("text", "") + delta["text"]
        elif delta["type"] == "thinking_delta":
            block["thinking"] = block.get("thinking", "") + delta["thinking"]
        elif delta["type"] == "signature_delta":
            block["signature"] = delta["signature"]
        elif delta["type"] == "input_json_delta":
            self._args[idx].append(delta["partial_json"])
        return _sse("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": delta})

    def feed(self, chunk) -> list[bytes]:
        kind, p = chunk.kind, chunk.payload
        out: list[bytes] = []
        if kind in ("message_start", "provider_alive", "provider_retry", "rate_limit",
                    "provider_request", "image_generation_progress", "image_generation"):
            return self.start() if kind == "message_start" else []
        out += self.start()
        if kind == "block_start":
            out += self._open(p["index"], p.get("block_type", "text"), p)
        elif kind == "text_delta":
            out += self._open(p["index"], "text", {})
            if p.get("text"):
                out.append(self._delta(p["index"], {"type": "text_delta", "text": p["text"]}))
        elif kind == "thinking_delta":
            out += self._open(p["index"], "thinking", {})
            if p.get("text"):
                out.append(self._delta(p["index"], {"type": "thinking_delta", "thinking": p["text"]}))
        elif kind == "tool_args_delta":
            out += self._open(p["index"], "tool_use", {})
            out.append(self._delta(p["index"], {"type": "input_json_delta",
                                                "partial_json": p.get("partial_json", "")}))
        elif kind == "block_stop":
            fi = p["index"]
            if fi not in self._index:
                return out
            item = p.get("block")
            if self._kind.get(fi) == "thinking" and isinstance(item, dict) and item.get("type") == "reasoning":
                out.append(self._delta(fi, {"type": "signature_delta",
                                            "signature": encode_signature(item)}))
            if self._kind.get(fi) == "tool_use":
                idx = self._index[fi]
                args = "".join(self._args.get(idx, []))
                if not args and isinstance(item, dict) and item.get("arguments"):
                    # Nothing streamed: deliver the authoritative arguments.
                    args = item["arguments"]
                    out.append(self._delta(fi, {"type": "input_json_delta", "partial_json": args}))
                try:
                    self.blocks[idx]["input"] = json.loads(args) if args else {}
                except ValueError:
                    self.blocks[idx]["input"] = {}
            out.append(_sse("content_block_stop", {"type": "content_block_stop",
                                                   "index": self._index[fi]}))
        elif kind == "usage":
            u = p.get("usage") or {}
            self.usage = {
                "input_tokens": int(u.get("input_tokens", 0) or 0),
                "output_tokens": int(u.get("output_tokens", 0) or 0),
                "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
                "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
            }
            self.stop_reason = p.get("stop_reason") or "end_turn"
        elif kind == "message_stop":
            out.append(_sse("message_delta", {"type": "message_delta",
                                              "delta": {"stop_reason": self.stop_reason,
                                                        "stop_sequence": None},
                                              "usage": self.usage}))
            out.append(_sse("message_stop", {"type": "message_stop"}))
        elif kind == "error":
            out.append(_sse("error", {"type": "error", "error": {
                "type": "api_error", "message": str(p.get("provider_message") or p.get("message"))}}))
        return out

    def message(self) -> dict[str, Any]:
        content = []
        for block in self.blocks:
            if block["type"] == "text" and not block.get("text"):
                continue
            content.append(block)
        return {"id": self.msg_id, "type": "message", "role": "assistant", "model": self.model,
                "content": content, "stop_reason": self.stop_reason, "stop_sequence": None,
                "usage": self.usage or {"input_tokens": 0, "output_tokens": 0}}


def error_status(payload: dict[str, Any]) -> int:
    status = payload.get("status")
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = None
    if status:
        return status
    code = str(payload.get("code") or "")
    if "overloaded" in code:
        return 529
    if "rate_limit" in code:
        return 429
    return 500


# ─── server ─────────────────────────────────────────────────────────────────


def make_app(client, *, target_model: str, effort: str, small_effort: str,
             request_log: Path | None = None):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    wire_states: dict[str, WireState] = {}

    def log_request(body: dict[str, Any]) -> None:
        if request_log is None:
            return
        from scripts.harness_h2h.metered import append_jsonl
        append_jsonl(request_log, {
            "requested_model": body.get("model"), "stream": bool(body.get("stream")),
            "n_messages": len(body.get("messages") or []),
            "n_tools": len(body.get("tools") or []),
            "tool_names": [t.get("name") for t in body.get("tools") or []],
            "system_chars": len(_system_text(body.get("system")) or ""),
            "thinking": body.get("thinking"), "session": session_of(body),
        })

    async def messages(request: Request) -> Response:
        body = await request.json()
        log_request(body)
        req = build_request(body, target_model=target_model, effort=effort,
                            small_effort=small_effort, wire_states=wire_states)
        writer = AnthropicStreamWriter(str(body.get("model") or target_model))
        stream = client.stream(req)

        # Pull until the first content-bearing chunk so a request that fails
        # before any output can answer with a real HTTP status (Claude Code
        # retries 429/5xx/529 itself).
        buffered: list = []
        try:
            async for chunk in stream:
                if chunk.kind == "error":
                    return JSONResponse(
                        {"type": "error", "error": {"type": "api_error", "message": str(
                            chunk.payload.get("provider_message") or chunk.payload.get("message"))}},
                        status_code=error_status(chunk.payload))
                buffered.append(chunk)
                if chunk.kind not in ("message_start", "provider_alive", "provider_retry",
                                      "rate_limit", "provider_request"):
                    break
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"type": "error", "error": {"type": "api_error",
                                                            "message": f"{type(exc).__name__}: {exc}"}},
                                status_code=500)

        if not body.get("stream"):
            for chunk in buffered:
                writer.feed(chunk)
            async for chunk in stream:
                writer.feed(chunk)
            return JSONResponse(writer.message())

        async def gen() -> AsyncIterator[bytes]:
            for chunk in buffered:
                for frame in writer.feed(chunk):
                    yield frame
            try:
                async for chunk in stream:
                    for frame in writer.feed(chunk):
                        yield frame
            except Exception as exc:  # noqa: BLE001
                yield _sse("error", {"type": "error", "error": {
                    "type": "api_error", "message": f"{type(exc).__name__}: {exc}"}})

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})

    async def count_tokens(request: Request) -> Response:
        body = await request.json()
        return JSONResponse({"input_tokens": max(1, len(json.dumps(body)) // 4)})

    async def models(_request: Request) -> Response:
        return JSONResponse({"data": [{"id": target_model, "type": "model",
                                       "display_name": target_model}], "has_more": False})

    async def health(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    return Starlette(routes=[
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/messages/count_tokens", count_tokens, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
    ])


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    from scripts.harness_h2h.metered import MeteredClient, default_token_path, make_codex_client

    parser = argparse.ArgumentParser(prog="harness_h2h.anthropic_proxy")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--small-effort", default="low")
    parser.add_argument("--log", type=Path, required=True, help="per-call metrics JSONL")
    parser.add_argument("--request-log", type=Path, default=None,
                        help="per-request shape JSONL (tools, system size)")
    parser.add_argument("--token", type=Path, default=None)
    args = parser.parse_args(argv)
    client = MeteredClient(make_codex_client(args.token or default_token_path()), args.log)
    app = make_app(client, target_model=args.model, effort=args.effort,
                   small_effort=args.small_effort, request_log=args.request_log)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
