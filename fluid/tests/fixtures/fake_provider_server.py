"""A scripted fake model-provider HTTP server for native-leg end-to-end tests.

``fake_claude_script.py`` stands in for the ``claude`` CLI on the delegated
leg; this stands in for the HTTP endpoint on the native leg. It is a real
loopback server (stdlib ``http.server`` on a free port, in a daemon thread),
so the real fluid provider clients run unmodified: httpx, SSE parsing, retry,
the stream watchdogs and the normalizers all see real bytes on a real socket.

Each request pops the next scripted :class:`Reply` (FIFO). A test states the
exact frames it needs next to its assertions; the builders below speak the
three wire formats the native engine uses:

* :class:`Chat` — OpenAI chat/completions (OpenRouter, local, custom rows):
  ``choices[].delta.content`` / ``delta.tool_calls[]`` argument fragments,
  ``finish_reason`` and the ``include_usage`` terminal chunk.
* :class:`Messages` — Anthropic Messages: ``message_start``,
  ``content_block_start`` (``text`` / ``tool_use``), ``input_json_delta``,
  ``message_delta`` (stop reason + usage), ``message_stop``, ``error``.
* :class:`Responses` — OpenAI Responses (Codex / OpenAI rows):
  ``response.created``, ``output_item.added``/``done``,
  ``output_text.delta``, ``function_call_arguments.delta``,
  ``response.completed``.

Every request is recorded (:attr:`FakeProviderServer.requests`: path, headers,
parsed JSON body), so a test also asserts on what the engine SENT: the tool
results it fed back, the order of messages, the tools it offered.

Frame items in :attr:`Reply.frames`:

  ``bytes`` / ``str``     written verbatim (use :func:`sse` for SSE frames)
  ``Sleep(s)``            pause mid-stream (a stall); aborts early on close
  ``CUT``                 drop the connection here without a terminal frame

``Reply.framing`` is ``"close"`` (HTTP/1.0 style: the body ends when the
socket closes, so a CUT looks like a clean EOF) or ``"chunked"`` (HTTP/1.1
chunked: a CUT leaves an incomplete chunked body, which httpx reports as a
transport error). Both are how real proxies end a stream early.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# ─── script vocabulary ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sleep:
    seconds: float


class _Cut:
    def __repr__(self) -> str:
        return "CUT"


CUT = _Cut()


@dataclass
class Reply:
    status: int = 200
    frames: list[Any] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    framing: str = "close"  # "close" | "chunked"
    content_type: str = "text/event-stream"


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: Any


def sse(data: Any, event: str | None = None) -> bytes:
    """One SSE frame. ``data`` may be a dict (JSON-encoded) or a raw string."""
    payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {payload}\n\n".encode()


def error_reply(status: int, message: str = "error", *, retry_after: str | None = None,
                body: Any = None) -> Reply:
    """A non-200 JSON error response."""
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    payload = body if body is not None else {"error": {"message": message, "type": "error"}}
    return Reply(status=status, frames=[json.dumps(payload)], headers=headers,
                 content_type="application/json")


def _split(text: str, parts: int) -> list[str]:
    """``text`` in ``parts`` near-equal pieces (for fragmenting deltas)."""
    if parts <= 1 or len(text) <= 1:
        return [text]
    size = max(1, -(-len(text) // parts))
    return [text[i:i + size] for i in range(0, len(text), size)]


# ─── OpenAI chat/completions ────────────────────────────────────────────────


class Chat:
    """chat/completions SSE builders."""

    MODEL = "fake-model"

    @staticmethod
    def chunk(delta: dict | None = None, finish: str | None = None, **extra: Any) -> bytes:
        body: dict[str, Any] = {
            "id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": Chat.MODEL,
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        }
        body.update(extra)
        return sse(body)

    @staticmethod
    def usage(prompt: int = 10, completion: int = 5, cached: int = 0, **extra: Any) -> bytes:
        usage: dict[str, Any] = {
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }
        if cached:
            usage["prompt_tokens_details"] = {"cached_tokens": cached}
        usage.update(extra)
        return sse({"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                    "model": Chat.MODEL, "choices": [], "usage": usage})

    DONE = sse("[DONE]")

    @classmethod
    def text(cls, text: str, *, pieces: int = 2, finish: str | None = "stop",
             usage: bool = True, prompt: int = 10, completion: int = 5,
             done: bool = True) -> list[Any]:
        frames: list[Any] = [cls.chunk({"role": "assistant", "content": ""})]
        frames += [cls.chunk({"content": piece}) for piece in _split(text, pieces)]
        if finish is not None:
            frames.append(cls.chunk({}, finish))
        if usage:
            frames.append(cls.usage(prompt, completion))
        if done:
            frames.append(cls.DONE)
        return frames

    @classmethod
    def tool_calls(cls, calls: list[dict], *, pieces: int = 3, text: str = "",
                   finish: str | None = "tool_calls", usage: bool = True,
                   prompt: int = 10, completion: int = 5, with_ids: bool = True,
                   done: bool = True) -> list[Any]:
        """``calls``: [{"id", "name", "args": dict | raw str}]. Each call's
        argument JSON is split into ``pieces`` fragments, interleaved call by
        call as parallel calls stream in practice."""
        frames: list[Any] = [cls.chunk({"role": "assistant", "content": None})]
        if text:
            frames.append(cls.chunk({"content": text}))
        for i, call in enumerate(calls):
            head: dict[str, Any] = {
                "index": i, "type": "function",
                "function": {"name": call["name"], "arguments": ""},
            }
            if with_ids:
                head["id"] = call["id"]
            frames.append(cls.chunk({"tool_calls": [head]}))
        fragments = [
            _split(c["args"] if isinstance(c["args"], str) else json.dumps(c["args"]), pieces)
            for c in calls
        ]
        for step in range(max(len(f) for f in fragments)):
            for i, frags in enumerate(fragments):
                if step < len(frags):
                    frames.append(cls.chunk({"tool_calls": [
                        {"index": i, "function": {"arguments": frags[step]}}
                    ]}))
        if finish is not None:
            frames.append(cls.chunk({}, finish))
        if usage:
            frames.append(cls.usage(prompt, completion))
        if done:
            frames.append(cls.DONE)
        return frames


# ─── Anthropic Messages ─────────────────────────────────────────────────────


class Messages:
    """Anthropic Messages SSE builders."""

    MODEL = "claude-sonnet-4-5"

    @staticmethod
    def ev(kind: str, **body: Any) -> bytes:
        return sse({"type": kind, **body}, event=kind)

    @classmethod
    def start(cls, input_tokens: int = 10, cache_read: int = 0) -> bytes:
        return cls.ev("message_start", message={
            "id": "msg_fake", "type": "message", "role": "assistant", "model": cls.MODEL,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 1,
                      "cache_read_input_tokens": cache_read,
                      "cache_creation_input_tokens": 0},
        })

    @classmethod
    def text_block(cls, index: int, text: str, pieces: int = 2) -> list[bytes]:
        frames = [cls.ev("content_block_start", index=index,
                         content_block={"type": "text", "text": ""})]
        frames += [cls.ev("content_block_delta", index=index,
                          delta={"type": "text_delta", "text": piece})
                   for piece in _split(text, pieces)]
        frames.append(cls.ev("content_block_stop", index=index))
        return frames

    @classmethod
    def tool_block(cls, index: int, id: str, name: str, args: dict | str,
                   pieces: int = 3, stop: bool = True) -> list[bytes]:
        raw = args if isinstance(args, str) else json.dumps(args)
        frames = [cls.ev("content_block_start", index=index, content_block={
            "type": "tool_use", "id": id, "name": name, "input": {}})]
        frames += [cls.ev("content_block_delta", index=index,
                          delta={"type": "input_json_delta", "partial_json": piece})
                   for piece in _split(raw, pieces)]
        if stop:
            frames.append(cls.ev("content_block_stop", index=index))
        return frames

    @classmethod
    def end(cls, stop_reason: str = "end_turn", output_tokens: int = 5) -> list[bytes]:
        return [
            cls.ev("message_delta", delta={"stop_reason": stop_reason, "stop_sequence": None},
                   usage={"output_tokens": output_tokens}),
            cls.ev("message_stop"),
        ]

    @classmethod
    def text(cls, text: str, *, input_tokens: int = 10, output_tokens: int = 5) -> list[Any]:
        return [cls.start(input_tokens), *cls.text_block(0, text),
                *cls.end("end_turn", output_tokens)]

    @classmethod
    def tool_calls(cls, calls: list[dict], *, text: str = "", input_tokens: int = 10,
                   output_tokens: int = 5, pieces: int = 3) -> list[Any]:
        frames: list[Any] = [cls.start(input_tokens)]
        index = 0
        if text:
            frames += cls.text_block(0, text)
            index = 1
        for call in calls:
            frames += cls.tool_block(index, call["id"], call["name"], call["args"], pieces)
            index += 1
        return frames + cls.end("tool_use", output_tokens)

    @classmethod
    def error(cls, kind: str = "overloaded_error", message: str = "Overloaded") -> bytes:
        return cls.ev("error", error={"type": kind, "message": message})


# ─── OpenAI Responses ───────────────────────────────────────────────────────


class Responses:
    """OpenAI Responses SSE builders."""

    @staticmethod
    def ev(kind: str, **body: Any) -> bytes:
        return sse({"type": kind, **body}, event=kind)

    @classmethod
    def created(cls) -> bytes:
        return cls.ev("response.created", response={"id": "resp_fake", "status": "in_progress"})

    @classmethod
    def completed(cls, input_tokens: int = 10, output_tokens: int = 5,
                  status: str = "completed", **response: Any) -> bytes:
        return cls.ev("response.completed", response={
            "id": "resp_fake", "status": status, "output": [],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "total_tokens": input_tokens + output_tokens,
                      "input_tokens_details": {"cached_tokens": 0}},
            **response,
        })

    @classmethod
    def message(cls, item_id: str, text: str, output_index: int = 0,
                pieces: int = 2) -> list[bytes]:
        item = {"type": "message", "id": item_id, "role": "assistant", "status": "in_progress",
                "content": []}
        frames = [cls.ev("response.output_item.added", output_index=output_index, item=item)]
        frames += [cls.ev("response.output_text.delta", item_id=item_id,
                          output_index=output_index, content_index=0, delta=piece)
                   for piece in _split(text, pieces)]
        done = {**item, "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
        frames.append(cls.ev("response.output_item.done", output_index=output_index, item=done))
        return frames

    @classmethod
    def function_call(cls, item_id: str, call_id: str, name: str, args: dict | str,
                      output_index: int = 0, pieces: int = 3,
                      done: bool = True) -> list[bytes]:
        raw = args if isinstance(args, str) else json.dumps(args)
        item = {"type": "function_call", "id": item_id, "call_id": call_id, "name": name,
                "arguments": "", "status": "in_progress"}
        frames = [cls.ev("response.output_item.added", output_index=output_index, item=item)]
        frames += [cls.ev("response.function_call_arguments.delta", item_id=item_id,
                          output_index=output_index, delta=piece)
                   for piece in _split(raw, pieces)]
        if done:
            frames.append(cls.ev("response.output_item.done", output_index=output_index,
                                 item={**item, "arguments": raw, "status": "completed"}))
        return frames

    @classmethod
    def text(cls, text: str, **usage: Any) -> list[Any]:
        return [cls.created(), *cls.message("msg_1", text), cls.completed(**usage)]

    @classmethod
    def tool_calls(cls, calls: list[dict], *, pieces: int = 3, **usage: Any) -> list[Any]:
        frames: list[Any] = [cls.created()]
        for i, call in enumerate(calls):
            frames += cls.function_call(f"fc_{i}", call["id"], call["name"], call["args"],
                                        output_index=i, pieces=pieces)
        return frames + [cls.completed(**usage)]


# ─── the server ─────────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib name
        return

    def do_GET(self) -> None:  # noqa: N802 — stdlib name
        self._serve()

    def do_POST(self) -> None:  # noqa: N802 — stdlib name
        self._serve()

    def _serve(self) -> None:
        owner = self.server.owner
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        owner._record(Recorded(self.command, self.path, dict(self.headers.items()), body))
        reply = owner._next_reply()
        if reply.framing == "chunked":
            self.protocol_version = "HTTP/1.1"
        self.send_response(reply.status)
        self.send_header("content-type", reply.content_type)
        for key, value in reply.headers.items():
            self.send_header(key, value)
        if reply.framing == "chunked":
            self.send_header("transfer-encoding", "chunked")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for item in reply.frames:
                if isinstance(item, Sleep):
                    if owner._stopping.wait(item.seconds):
                        return
                    continue
                if item is CUT:
                    self._abort()
                    return
                data = item.encode() if isinstance(item, str) else bytes(item)
                if reply.framing == "chunked":
                    data = f"{len(data):x}\r\n".encode() + data + b"\r\n"
                self.wfile.write(data)
                self.wfile.flush()
            if reply.framing == "chunked":
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # the client went away (Stop, a watchdog): nothing to do

    def _abort(self) -> None:
        """Drop the socket mid-body."""
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    owner: "FakeProviderServer"


class FakeProviderServer:
    """Loopback provider endpoint that plays scripted replies in order.

    Use as a context manager (or call :meth:`close`). ``url`` is the origin
    (``http://127.0.0.1:<port>``); every path is served the same way, so point
    a client at ``url + "/v1/messages"`` or ``url + "/v1/chat/completions"``.
    A request that arrives after the script ran out gets ``fallback`` (a 500
    by default, so an unexpected extra model call fails loudly).
    """

    def __init__(self, *replies: Reply | list[Any], fallback: Reply | None = None) -> None:
        self._lock = threading.Lock()
        self._replies: deque[Reply] = deque()
        self.requests: list[Recorded] = []
        self.fallback = fallback or error_reply(500, "fake provider: script exhausted")
        self.exhausted = 0
        self._stopping = threading.Event()
        self._arrived = threading.Condition(self._lock)
        self.script(*replies)
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05},
            name="fake-provider", daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def script(self, *replies: Reply | list[Any]) -> None:
        """Queue replies; a bare frame list is a 200 SSE reply."""
        with self._lock:
            for reply in replies:
                self._replies.append(reply if isinstance(reply, Reply) else Reply(frames=reply))

    def _next_reply(self) -> Reply:
        with self._lock:
            if self._replies:
                return self._replies.popleft()
            self.exhausted += 1
            return self.fallback

    def _record(self, recorded: Recorded) -> None:
        with self._arrived:
            self.requests.append(recorded)
            self._arrived.notify_all()

    def wait_requests(self, count: int, timeout: float = 5.0) -> bool:
        """Block (thread-side) until ``count`` requests arrived."""
        deadline = time.monotonic() + timeout
        with self._arrived:
            while len(self.requests) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._arrived.wait(remaining)
        return True

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._replies)

    def close(self) -> None:
        self._stopping.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeProviderServer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
