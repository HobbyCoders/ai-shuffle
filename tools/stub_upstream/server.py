"""The stub upstream HTTP server.

TEST / DEV TOOLING ONLY — never imported from ``app/``. Standard library only
(plus ``cryptography`` for the optional MITM proxy in ``proxy.py``, which is
already a hard dependency of the app).

Loopback-only. Serves the endpoints the translator gateway actually calls:

  ``POST /v1/responses``                     — OpenAI platform Responses API
  ``POST /backend-api/codex/responses``      — ChatGPT/Codex backend
  ``POST /v1/messages``                      — Anthropic passthrough (best effort)
  ``GET  /v1/models``                        — provider catalog fetch
  ``GET  /backend-api/codex/models``         — Codex entitlement preflight

plus a control surface under ``/__control__/`` for scenario selection and the
request log. See ``README.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional, Tuple

from tools.stub_upstream import scenarios as S

DEFAULT_PORT = 8799          # not 8000 (app), 8788 / 8789 (gateways)
DEFAULT_PROXY_PORT = 8798
CONTROL_PREFIX = "/__control__"

# Header names whose values are never written to the request log verbatim.
_REDACT = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "chatgpt-account-id",
    "cookie",
    "set-cookie",
}

_RESPONSES_PATHS = {"/v1/responses", "/backend-api/codex/responses"}
_MESSAGES_PATHS = {"/v1/messages"}
_MODELS_PATHS = {"/v1/models", "/backend-api/codex/models", "/models"}


def _fingerprint(value: str) -> str:
    """A stable, non-reversible 8-hex tag so an operator can tell *whether* a
    credential changed between requests without ever seeing it."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


def redact_headers(items) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in items:
        if key.lower() in _REDACT:
            out[key] = f"<redacted len={len(value)} fp={_fingerprint(value)}>"
        else:
            out[key] = value
    return out


# ─── Shared mutable state (one instance shared by the plain server and proxy) ─

class StubState:
    """Scenario selection + request log. Thread-safe."""

    def __init__(
        self,
        *,
        default: Optional[S.Scenario] = None,
        log_path: Optional[str] = None,
        log_capacity: int = 2000,
        max_hang_seconds: float = 120.0,
    ) -> None:
        self._lock = threading.Lock()
        self._boot_default = default or S.Scenario()
        self._default = self._boot_default
        self._queue: Deque[S.Scenario] = deque()
        self._log: Deque[Dict[str, Any]] = deque(maxlen=log_capacity)
        self._seq = 0
        self.log_path = log_path
        self.max_hang_seconds = float(max_hang_seconds)

    # ── scenario selection ──
    def set_default(self, scn: S.Scenario) -> None:
        with self._lock:
            self._default = scn

    def get_default(self) -> S.Scenario:
        with self._lock:
            return self._default

    def set_queue(self, items: List[S.Scenario]) -> None:
        with self._lock:
            self._queue = deque(items)

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def next_scenario(self, override: Optional[Dict[str, Any]]) -> S.Scenario:
        """Resolve the scenario for one request.

        Precedence: explicit per-request override (``X-Stub-Scenario`` header or
        ``?scenario=``) > one-shot queue > current default.
        """
        with self._lock:
            base = self._default
            if override is None and self._queue:
                return self._queue.popleft()
        if override is None:
            return base
        return base.merged(override)

    # ── request log ──
    def record(self, entry: Dict[str, Any]) -> int:
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._log.append(entry)
            path = self.log_path
        if path:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
            except OSError:
                pass
        return entry["seq"]

    def log_entries(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._log)
        if limit is not None and limit > 0:
            items = items[-limit:]
        return items

    def request_count(self, path_contains: Optional[str] = None) -> int:
        entries = self.log_entries()
        if path_contains:
            entries = [e for e in entries if path_contains in str(e.get("path", ""))]
        return len(entries)

    def reset(self) -> None:
        with self._lock:
            self._default = self._boot_default
            self._queue.clear()
            self._log.clear()
            self._seq = 0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "default_scenario": self._default.to_dict(),
                "queue": [s.to_dict() for s in self._queue],
                "queue_depth": len(self._queue),
                "requests_logged": len(self._log),
                "total_requests": self._seq,
                "log_path": self.log_path,
                "max_hang_seconds": self.max_hang_seconds,
            }


# ─── Request handling ────────────────────────────────────────────────────────

class StubHandlerMixin:
    """The stub's HTTP behavior, shared by the direct server and the MITM proxy.

    Hosting classes must provide ``self.state`` (a :class:`StubState`) and may
    provide ``self.upstream_host`` (the CONNECT host, when tunneled).
    """

    upstream_host: Optional[str] = None
    server_version = "stub-upstream/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> StubState:
        return self.server.state  # type: ignore[attr-defined]

    # ── plumbing ──
    def log_message(self, fmt, *args):  # noqa: A003 - BaseHTTPRequestHandler API
        if os.environ.get("STUB_UPSTREAM_VERBOSE"):
            super().log_message(fmt, *args)  # type: ignore[misc]

    def _split_path(self) -> Tuple[str, Dict[str, str]]:
        from urllib.parse import parse_qsl, urlsplit

        parts = urlsplit(self.path)  # type: ignore[attr-defined]
        return parts.path, dict(parse_qsl(parts.query))

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")  # type: ignore[attr-defined]
        if length:
            try:
                return self.rfile.read(int(length))  # type: ignore[attr-defined]
            except (ValueError, OSError):
                return b""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":  # type: ignore[attr-defined]
            chunks = []
            while True:
                line = self.rfile.readline()  # type: ignore[attr-defined]
                if not line:
                    break
                try:
                    size = int(line.strip().split(b";")[0] or b"0", 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()  # type: ignore[attr-defined]
                    break
                chunks.append(self.rfile.read(size))  # type: ignore[attr-defined]
                self.rfile.readline()  # type: ignore[attr-defined]
            return b"".join(chunks)
        return b""

    def _send_json(self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/json")  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(body)))  # type: ignore[attr-defined]
        for key, value in (headers or {}).items():
            self.send_header(key, value)  # type: ignore[attr-defined]
        self.end_headers()  # type: ignore[attr-defined]
        self.wfile.write(body)  # type: ignore[attr-defined]

    def _override_from_request(self, query: Dict[str, str]) -> Optional[Dict[str, Any]]:
        raw = self.headers.get("X-Stub-Scenario")  # type: ignore[attr-defined]
        if not raw:
            raw = query.get("scenario")
        if not raw:
            return None
        raw = raw.strip()
        if raw.startswith("{"):
            return json.loads(raw)
        return {"name": raw}

    # ── dispatch ──
    def do_GET(self):  # noqa: N802
        path, query = self._split_path()
        if path.startswith(CONTROL_PREFIX):
            return self._handle_control(path, query, b"")
        entry = self._begin_log("GET", path, b"")
        if path in _MODELS_PATHS:
            models = _model_catalog(self.state.get_default())
            entry["response_status"] = 200
            entry["scenario"] = "models"
            self.state.record(entry)
            return self._send_json(200, models)
        entry["response_status"] = 404
        self.state.record(entry)
        return self._send_json(404, {"error": {"message": f"stub: no route for {path}"}})

    def do_POST(self):  # noqa: N802
        path, query = self._split_path()
        body = self._read_body()
        if path.startswith(CONTROL_PREFIX):
            return self._handle_control(path, query, body)

        entry = self._begin_log("POST", path, body)
        try:
            scn = self.state.next_scenario(self._override_from_request(query))
        except (ValueError, json.JSONDecodeError) as exc:
            entry["response_status"] = 400
            entry["error"] = str(exc)
            self.state.record(entry)
            return self._send_json(400, {"error": {"message": f"stub: {exc}"}})

        entry["scenario"] = scn.name

        if path in _MESSAGES_PATHS:
            return self._serve_anthropic(entry, scn)
        if path in _RESPONSES_PATHS:
            return self._serve_responses(entry, scn)

        entry["response_status"] = 404
        self.state.record(entry)
        return self._send_json(404, {"error": {"message": f"stub: no route for {path}"}})

    # ── logging ──
    def _begin_log(self, method: str, path: str, body: bytes) -> Dict[str, Any]:
        try:
            parsed: Any = json.loads(body) if body else None
        except (json.JSONDecodeError, ValueError):
            parsed = None
        entry: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "path": path,
            "host": self.upstream_host or self.headers.get("Host"),  # type: ignore[attr-defined]
            "via": "proxy" if self.upstream_host else "direct",
            "headers": redact_headers(self.headers.items()),  # type: ignore[attr-defined]
            "body_bytes": len(body),
        }
        if parsed is not None:
            entry["body"] = parsed
        elif body:
            entry["body_text"] = body.decode("utf-8", "replace")[:20000]
        return entry

    # ── responses (SSE) ──
    def _serve_responses(self, entry: Dict[str, Any], scn: S.Scenario):
        outcome = S.build_outcome(scn)
        if scn.delay_before_headers:
            time.sleep(min(scn.delay_before_headers, self.state.max_hang_seconds))

        if outcome.body is not None:
            entry["response_status"] = outcome.status
            entry["response_headers"] = dict(outcome.headers)
            self.state.record(entry)
            self.send_response(outcome.status)  # type: ignore[attr-defined]
            for key, value in outcome.headers.items():
                self.send_header(key, value)  # type: ignore[attr-defined]
            self.send_header("Content-Length", str(len(outcome.body)))  # type: ignore[attr-defined]
            self.end_headers()  # type: ignore[attr-defined]
            self.wfile.write(outcome.body)  # type: ignore[attr-defined]
            return

        entry["response_status"] = outcome.status
        entry["response_headers"] = dict(outcome.headers)
        entry["events"] = [name for name, _ in (outcome.events or [])]
        self.state.record(entry)

        self.send_response(outcome.status)  # type: ignore[attr-defined]
        for key, value in outcome.headers.items():
            self.send_header(key, value)  # type: ignore[attr-defined]
        self.send_header("Transfer-Encoding", "chunked")  # type: ignore[attr-defined]
        self.end_headers()  # type: ignore[attr-defined]

        try:
            if scn.delay_before_first_event:
                time.sleep(min(scn.delay_before_first_event, self.state.max_hang_seconds))
            for i, (name, payload) in enumerate(outcome.events or []):
                if i and scn.delay_between_events:
                    time.sleep(min(scn.delay_between_events, self.state.max_hang_seconds))
                self._write_chunk(S.sse_frame(name, payload, i))
            if outcome.done_sentinel:
                self._write_chunk(b"data: [DONE]\n\n")
            if scn.hang_seconds:
                # Bounded stall so a hung-upstream test cannot wedge the stub.
                time.sleep(min(scn.hang_seconds, self.state.max_hang_seconds))
            if scn.name == "truncate":
                # Drop the connection without the terminating 0-length chunk so
                # the client sees an aborted stream, like a dead upstream.
                self.close_connection = True  # type: ignore[attr-defined]
                return
            self.wfile.write(b"0\r\n\r\n")  # type: ignore[attr-defined]
            self.wfile.flush()  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True  # type: ignore[attr-defined]

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")  # type: ignore[attr-defined]
        self.wfile.flush()  # type: ignore[attr-defined]

    # ── anthropic passthrough ──
    def _serve_anthropic(self, entry: Dict[str, Any], scn: S.Scenario):
        outcome = S.build_outcome(scn)
        if outcome.body is not None:  # error scenarios share the same shape
            return self._serve_responses(entry, scn)
        body = S.anthropic_messages_sse(scn)
        entry["response_status"] = 200
        entry["response_headers"] = {"Content-Type": "text/event-stream"}
        self.state.record(entry)
        self.send_response(200)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(body)))  # type: ignore[attr-defined]
        self.end_headers()  # type: ignore[attr-defined]
        self.wfile.write(body)  # type: ignore[attr-defined]

    # ── control plane ──
    def _handle_control(self, path: str, query: Dict[str, str], body: bytes):
        route = path[len(CONTROL_PREFIX):].strip("/")
        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError) as exc:
            return self._send_json(400, {"error": f"invalid JSON: {exc}"})

        try:
            if route in ("", "state"):
                return self._send_json(200, self.state.snapshot())
            if route == "scenario":
                scn = S.scenario_from_dict(payload)
                self.state.set_default(scn)
                return self._send_json(200, {"ok": True, "default_scenario": scn.to_dict()})
            if route == "queue":
                items = payload.get("queue") or payload.get("scenarios") or []
                if not isinstance(items, list):
                    raise ValueError("'queue' must be a list of scenario objects")
                built: List[S.Scenario] = []
                for item in items:
                    if isinstance(item, str):
                        item = {"name": item}
                    repeat = int(item.pop("repeat", 1)) if isinstance(item, dict) else 1
                    scn = S.scenario_from_dict(item)
                    built.extend([scn] * max(1, repeat))
                self.state.set_queue(built)
                return self._send_json(200, {"ok": True, "queue_depth": len(built)})
            if route == "log":
                limit = int(query.get("limit", "0") or 0)
                return self._send_json(
                    200, {"entries": self.state.log_entries(limit or None)}
                )
            if route == "count":
                return self._send_json(
                    200,
                    {"count": self.state.request_count(query.get("path") or None)},
                )
            if route == "reset":
                self.state.reset()
                return self._send_json(200, {"ok": True})
        except (ValueError, TypeError) as exc:
            return self._send_json(400, {"error": str(exc)})
        return self._send_json(404, {"error": f"unknown control route {route!r}"})


def _model_catalog(scn: S.Scenario) -> Dict[str, Any]:
    """An OpenAI-shaped ``/v1/models`` body (``_parse_models_response`` accepts
    both ``{"data":[...]}`` and a bare list; the ``data`` form is used here)."""
    ids = [scn.model, "gpt-5.6-sol", "gpt-5-codex", "gpt-5.4-mini"]
    seen: List[str] = []
    for mid in ids:
        if mid and mid not in seen:
            seen.append(mid)
    return {
        "object": "list",
        "data": [{"id": mid, "object": "model", "owned_by": "stub"} for mid in seen],
        # Codex entitlement preflight uses "models"; include both keys so either
        # caller shape works.
        "models": [{"id": mid} for mid in seen],
    }


class StubHandler(StubHandlerMixin, BaseHTTPRequestHandler):
    """Direct (non-proxied) stub handler."""


class StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, state: StubState):
        self.state = state
        super().__init__(addr, handler_cls)


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    state: Optional[StubState] = None,
) -> Tuple[StubServer, threading.Thread, StubState]:
    """Start the stub on a background thread. Loopback binding is enforced."""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError(
            f"refusing to bind {host!r}: the stub upstream is loopback-only"
        )
    st = state or StubState()
    httpd = StubServer((host, port), StubHandler, st)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, st


# ─── CLI ─────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.stub_upstream",
        description="Local stub upstream for zero-spend provider E2E (loopback only).",
    )
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("STUB_UPSTREAM_PORT", DEFAULT_PORT)))
    p.add_argument("--scenario",
                   default=os.environ.get("STUB_UPSTREAM_SCENARIO", "text"),
                   choices=list(S.SCENARIO_NAMES))
    p.add_argument("--scenario-json",
                   default=os.environ.get("STUB_UPSTREAM_SCENARIO_JSON"),
                   help="JSON object of scenario field overrides applied at boot")
    p.add_argument("--log-file",
                   default=os.environ.get("STUB_UPSTREAM_LOG"),
                   help="append each request as one JSON line to this file")
    p.add_argument("--max-hang-seconds", type=float,
                   default=float(os.environ.get("STUB_UPSTREAM_MAX_HANG", "120")))
    p.add_argument("--proxy", action="store_true",
                   help="also run the HTTPS MITM proxy so the app's hardcoded "
                        "upstream URLs land here (see README)")
    p.add_argument("--proxy-port", type=int,
                   default=int(os.environ.get("STUB_UPSTREAM_PROXY_PORT",
                                              DEFAULT_PROXY_PORT)))
    p.add_argument("--ca-dir", default=os.environ.get("STUB_UPSTREAM_CA_DIR"),
                   help="directory holding the generated MITM CA (default: "
                        "./.stub-upstream-ca)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    overrides: Dict[str, Any] = {"name": args.scenario}
    if args.scenario_json:
        overrides.update(json.loads(args.scenario_json))
    state = StubState(
        default=S.scenario_from_dict(overrides),
        log_path=args.log_file,
        max_hang_seconds=args.max_hang_seconds,
    )

    httpd, _thread, _ = serve(port=args.port, state=state)
    print(f"[stub-upstream] direct  http://127.0.0.1:{args.port}")
    print(f"[stub-upstream] control http://127.0.0.1:{args.port}{CONTROL_PREFIX}/state")
    print(f"[stub-upstream] scenario={state.get_default().name}")
    if args.log_file:
        print(f"[stub-upstream] request log -> {args.log_file}")

    proxy_httpd = None
    if args.proxy:
        from tools.stub_upstream import proxy as proxy_mod

        ca_dir = args.ca_dir or os.path.join(os.getcwd(), ".stub-upstream-ca")
        proxy_httpd, _pt, ca_pem = proxy_mod.serve_proxy(
            port=args.proxy_port, state=state, ca_dir=ca_dir
        )
        print(f"[stub-upstream] proxy   http://127.0.0.1:{args.proxy_port}")
        print(f"[stub-upstream] CA cert {ca_pem}")
        print("[stub-upstream] point the app at it with:")
        print(f"    HTTPS_PROXY=http://127.0.0.1:{args.proxy_port}")
        print(f"    SSL_CERT_FILE={ca_pem}")
        print("    NO_PROXY=127.0.0.1,localhost")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        if proxy_httpd is not None:
            proxy_httpd.shutdown()
    return 0
