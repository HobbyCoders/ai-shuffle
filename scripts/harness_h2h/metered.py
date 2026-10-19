"""One timing wrapper around fluid's ChatGPT/Codex client, shared by two arms.

The Claude Code arm (through ``anthropic_proxy``) and the bare fluid arm
(``fluid_cli_arm``) both send their model calls through :class:`MeteredClient`,
so per-call latency, time to first token and token counts are measured by
the same code on the same transport, auth, model and effort. A difference
between those arms is then the harness (prompt, tools, loop), not the wire.

Each finished call appends one JSON line to the call log:

    {"t_start": ..., "t_first": ..., "t_end": ..., "model": ..., "effort": ...,
     "cache_key": ..., "input": ..., "cached": ..., "output": ...,
     "reasoning": ..., "stop": ..., "error": ..., "role": ...}

``t_*`` are epoch seconds. ``input`` excludes ``cached`` (fluid's
normalisation), so the prompt size is ``input + cached``.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fluid.providers.base import ProviderRequest, StreamChunk
from fluid.providers.openai_responses.client import (
    CODEX_RESPONSES_URL,
    OpenAIResponsesClient,
)
from fluid.providers.openai_responses.codex_oauth import CodexAuth

# The kinds that mean the model produced something a user would see.
_CONTENT_KINDS = frozenset({"text_delta", "thinking_delta", "tool_args_delta", "block_start"})

_LOG_LOCK = threading.Lock()


def default_token_path() -> Path:
    """The bench's own ChatGPT login, kept apart from the app's stored token."""
    return Path.home() / ".config" / "fluid" / "codex_auth_h2h.json"


def make_codex_client(token_path: Path | None = None) -> OpenAIResponsesClient:
    """fluid's Codex-backend client on the bench login, default timeouts."""
    auth = CodexAuth(token_path=token_path or default_token_path())
    return OpenAIResponsesClient(base_url=CODEX_RESPONSES_URL, auth=auth)


class MeteredClient:
    """A ProviderClient that forwards to ``inner`` and logs every call.

    ``effort`` overrides the request's effort when set, so every arm runs the
    model at the same reasoning depth whatever its harness asks for.
    """

    def __init__(self, inner: Any, log_path: Path, *, effort: str | None = None,
                 role: str = "main") -> None:
        self._inner = inner
        self._log_path = Path(log_path)
        self._effort = effort
        self._role = role
        self.name = inner.name
        self.caps = inner.caps

    def __getattr__(self, item: str) -> Any:
        # estimate_cost, classify_error, routing_headers, ...
        return getattr(self._inner, item)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        if self._effort is not None:
            request.effort = self._effort
        record: dict[str, Any] = {
            "t_start": time.time(), "t_first": None, "t_end": None,
            "model": request.model, "effort": request.effort, "role": self._role,
            "cache_key": request.extra.get("prompt_cache_key"),
            "n_messages": len(request.messages), "n_tools": len(request.tools),
            "input": 0, "cached": 0, "output": 0, "reasoning": 0,
            "stop": None, "error": None, "retries": 0,
        }
        try:
            async for chunk in self._inner.stream(request):
                if record["t_first"] is None and chunk.kind in _CONTENT_KINDS:
                    record["t_first"] = time.time()
                if chunk.kind == "usage":
                    usage = chunk.payload.get("usage") or {}
                    record["input"] = int(usage.get("input_tokens", 0) or 0)
                    record["cached"] = int(usage.get("cache_read_input_tokens", 0) or 0)
                    record["output"] = int(usage.get("output_tokens", 0) or 0)
                    record["reasoning"] = int(usage.get("reasoning_tokens", 0) or 0)
                    record["stop"] = chunk.payload.get("stop_reason")
                elif chunk.kind == "provider_retry":
                    record["retries"] += 1
                elif chunk.kind == "error":
                    record["error"] = str(chunk.payload.get("message") or chunk.payload)[:500]
                yield chunk
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            record["error"] = record["error"] or f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            record["t_end"] = time.time()
            append_jsonl(self._log_path, record)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _LOG_LOCK, path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out
