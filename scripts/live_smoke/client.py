"""Talk to the worker's own HTTP API the way the local web UI does.

Authentication: none is stored or sent. The runner runs on the worker's
machine and calls it on loopback; the worker treats direct loopback requests
as the local owner (``app.core.owner_auth.local_access``) and requires a
matching ``Origin`` header for anything that changes state
(``docs/content/api-reference/authentication.md``). The HTTP client ignores
proxy environment variables, because a proxied request is never local.

A worker started by the desktop app also requires its per-launch listener
token on loopback, which no outside script has; such a worker answers 401/403
and the runner stops with :class:`AuthRefused` instead of asking for a
password.
"""
from __future__ import annotations

import json
import time
import uuid

import httpx

API = "/api/v1"


class ClientError(RuntimeError):
    pass


class Unreachable(ClientError):
    pass


class AuthRefused(ClientError):
    pass


AUTH_HINT = (
    "the worker refused passwordless local access. Run the smoke on the worker's own "
    "machine against its loopback address (default http://127.0.0.1:8000). A worker "
    "started by the desktop app only admits its own window on loopback, so the runner "
    "cannot drive it; record that worker's rows by hand instead."
)


class HttpWorkerClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 15.0,
                 ack_timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.origin = self.base_url
        self.ack_timeout = ack_timeout
        # Seconds to keep reading after an ack, and to wait for a refusal.
        self.linger_s = 3.0
        self._http = httpx.Client(
            base_url=self.base_url, timeout=timeout, trust_env=False,
            headers={"Origin": self.origin, "Accept": "application/json"}, transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    # -- REST ---------------------------------------------------------------

    def _req(self, method: str, path: str, **kw):
        try:
            resp = self._http.request(method, API + path, **kw)
        except httpx.TransportError as exc:
            raise Unreachable(f"{self.base_url} unreachable: {type(exc).__name__}") from None
        if resp.status_code in (401, 403):
            raise AuthRefused(f"{method} {path}: HTTP {resp.status_code}; {AUTH_HINT}")
        if resp.status_code >= 400:
            detail = resp.text[:300].replace("\n", " ")
            raise ClientError(f"{method} {path}: HTTP {resp.status_code} {detail}")
        return resp.json() if resp.content else {}

    def deploy_status(self) -> dict:
        # Mounted under the settings router (app/api/settings.py, prefix /api/v1/settings).
        return self._req("GET", "/settings/application-updates/deploy-status")

    def installs(self) -> dict:
        return self._req("GET", "/cluster/installs")

    def tool_surface(self) -> dict:
        # Live MCP enumeration may take longer than the default request timeout.
        return self._req("GET", "/tools/surface", timeout=120.0)

    def peer_message(self, sender_id: str, session_id: str, message: str, request_id: str) -> dict:
        return self._req("POST", f"/tools/sessions/{sender_id}/peer-message", json={
            "session_id": session_id, "message": message, "request_id": request_id,
        })

    def models(self) -> dict:
        return self._req("GET", "/models")

    def agents(self) -> list:
        data = self._req("GET", "/agents")
        return data if isinstance(data, list) else []

    def preference(self, key: str):
        """An account preference's value, or None when it was never set."""
        data = self._req("GET", f"/preferences/{key}")
        return data.get("value") if isinstance(data, dict) else None

    def projects(self) -> list:
        data = self._req("GET", "/projects")
        return data if isinstance(data, list) else data.get("projects") or []

    def session(self, session_id: str) -> dict:
        return self._req("GET", f"/sessions/{session_id}", params={"message_limit": 500})

    def state(self, session_id: str) -> dict:
        return self._req("GET", f"/sessions/{session_id}/state")

    def compact(self, session_id: str) -> dict:
        return self._req("POST", f"/sessions/{session_id}/compact")

    def system_notes(self, session_id: str) -> list:
        """The session's ``system.note`` events, read from its event log.

        Notes such as a native ``compact_failed`` live only in the event log
        (the web UI replays it); the message history leaves them out. Reads
        the SSE backfill up to ``caught_up`` and closes the stream.
        """
        notes: list = []
        event = None
        try:
            with self._http.stream("GET", API + f"/sessions/{session_id}/events",
                                   headers={"Accept": "text/event-stream"}) as resp:
                if resp.status_code >= 400:
                    raise ClientError(f"GET /sessions/{session_id}/events: HTTP {resp.status_code}")
                for line in resp.iter_lines():
                    if line.startswith("event: "):
                        event = line[len("event: "):].strip()
                        if event in ("caught_up", "reset"):
                            break
                    elif line.startswith("data: ") and event == "system.note":
                        try:
                            notes.append(json.loads(line[len("data: "):]))
                        except ValueError:
                            pass
        except httpx.TransportError as exc:
            raise Unreachable(f"{self.base_url} unreachable: {type(exc).__name__}") from None
        return notes

    def archive(self, session_id: str) -> None:
        self._req("PATCH", f"/sessions/{session_id}/active", json={"is_active": False, "archived": True})

    # -- chat WebSocket (the same frames the web UI sends) --------------------

    def _ws_url(self) -> str:
        # app/api/websocket.py: router prefix /api/v1, route /ws/chat.
        return "ws" + self.base_url[len("http"):] + API + "/ws/chat"

    def _exchange(self, frame: dict, done, wait_s: float, linger_s: float = 0.0) -> dict | None:
        """Send one frame and read replies until ``done(reply)`` or ``wait_s`` passes.

        After ``done`` matches, keep reading for ``linger_s`` on the same socket
        so the worker finishes the request before the socket closes.
        """
        from websockets.exceptions import InvalidStatus, WebSocketException
        from websockets.sync.client import connect

        try:
            ws = connect(self._ws_url(), origin=self.origin, proxy=None,
                         open_timeout=10, close_timeout=5)
        except InvalidStatus as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise AuthRefused(f"chat socket: HTTP {code}; {AUTH_HINT}") from None
            raise ClientError(f"chat socket: HTTP {code}") from None
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise Unreachable(f"chat socket unreachable: {type(exc).__name__}") from None
        matched = None
        with ws:
            ws.send(json.dumps(frame))
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                try:
                    raw = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                except TimeoutError:
                    break
                except WebSocketException as exc:
                    if matched is not None:
                        break
                    raise ClientError(f"chat socket closed: {exc}") from None
                try:
                    reply = json.loads(raw)
                except ValueError:
                    continue
                kind = reply.get("type")
                if kind == "ping":
                    ws.send(json.dumps({"type": "pong"}))
                    continue
                if kind in ("error", "session_not_found") and matched is None:
                    raise ClientError(f"worker refused: {reply.get('message') or kind}")
                if matched is None and done(reply):
                    matched = reply
                    deadline = time.monotonic() + linger_s
        return matched

    def send(self, prompt: str, *, session_id: str | None = None, model: str | None = None,
             permission_mode: str | None = None, project: str | None = None,
             target_instance_id: str | None = None, profile: str | None = None) -> str:
        """Start a session (no ``session_id``) or send a message; returns the session id."""
        request_id = uuid.uuid4().hex
        frame = {"type": "query", "prompt": prompt, "request_id": request_id}
        if session_id:
            frame["session_id"] = session_id
        if profile:
            frame["profile"] = profile
        if project:
            frame["project"] = project
        if target_instance_id:
            frame["target_instance_id"] = target_instance_id
        overrides = {k: v for k, v in (("model", model), ("permission_mode", permission_mode)) if v}
        if overrides:
            frame["overrides"] = overrides
        if session_id:
            # An existing session acks only when the turn is queued behind a
            # running one; otherwise the turn just starts. Hold the socket a
            # few seconds so a refusal is still seen.
            self._exchange(frame, lambda r: r.get("type") == "query_queued", wait_s=self.linger_s)
            return session_id
        created = self._exchange(
            frame, lambda r: r.get("type") == "session_created" and r.get("request_id") == request_id,
            wait_s=self.ack_timeout, linger_s=self.linger_s)
        if not created:
            raise ClientError("worker did not confirm the new session")
        return created["session_id"]

    def stop(self, session_id: str) -> None:
        self._exchange({"type": "stop", "session_id": session_id}, lambda r: False, wait_s=2.0)
