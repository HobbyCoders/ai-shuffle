"""Dev-child-only activity observer and bounded pause admission gate."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from scripts.devloop.idle import passive, passive_subscription


class ActivityObserver:
    """Awake: no buffering, scope mutation or forwarding. Gate only during pause."""
    def __init__(self, app, receipt: Path):
        from app.core.temp_scratch import process_identity

        self.app, self.receipt = app, receipt
        self.pid, self.identity = os.getpid(), process_identity(os.getpid())
        self.lock = threading.Lock()
        self.active = 0
        self.activity = time.monotonic()
        self.uncertain = False
        self.gate_request = receipt.with_suffix(".gate")
        self.gate_id, self.gate_until = None, 0
        self.gate_state, self.gate_commit_id = "open", None
        self.stopped = threading.Event()
        self.writer = threading.Thread(target=self.publish, daemon=True, name="devloop-activity")
        self.writer.start()

    def publish(self):
        from scripts.devloop.__main__ import write_private_json

        while not self.stopped.is_set():
            try:
                self.control_gate()
                with self.lock:
                    record = {"pid": self.pid, "identity": self.identity,
                              "updated": time.monotonic(), "activity": self.activity,
                              "active": self.active, "uncertain": self.uncertain,
                              "gate_id": self.gate_id, "gate_until": self.gate_until,
                              "gate_state": self.gate_state, "gate_commit_id": self.gate_commit_id}
                write_private_json(self.receipt, record)
            except Exception:
                self.uncertain = True  # Stay awake until TTL, never silently ignore lost evidence.
            self.stopped.wait(0.5)  # At most two tiny writes/s, not one per request/frame.

    def control_gate(self):
        """Closed expires fail-open; committed stays closed until child teardown."""
        try:
            command = json.loads(self.gate_request.read_text())
        except (FileNotFoundError, OSError, ValueError) as exc:
            if not isinstance(exc, FileNotFoundError):
                self.failed()
            with self.lock:
                if self.gate_state != "committed":
                    self.gate_state, self.gate_until = "open", 0
            return
        with self.lock:
            if self.gate_state == "committed":
                return  # Neither expiry nor an unlink/replayed command can reopen.
            now = time.monotonic()
            self.expire_gate(now)
            try:
                identity_matches = command["pid"] == self.pid and command["identity"] == self.identity
                if command.get("action") == "commit":
                    self.gate_commit_id = command["id"]  # Explicit positive or negative ack.
                    if (identity_matches and command["id"] == self.gate_id
                            and self.gate_state == "closed"
                            and self.active == 0 and not self.uncertain):
                        self.gate_state = "committed"
                    return
                if command.get("id") == self.gate_id:
                    return  # Never renew a timed-out close command.
                self.gate_id, self.gate_until, self.gate_state = command["id"], 0, "open"
                if (identity_matches and 0 < command["expires"] - now <= 5
                        and self.active == 0 and not self.uncertain
                        and self.activity <= command["before"]):
                    self.gate_until, self.gate_state = command["expires"], "closed"
            except (KeyError, TypeError):
                self.uncertain = True

    def expire_gate(self, now):
        # Caller holds the admission lock. Committing and admitting a request or
        # client frame therefore cannot straddle the fail-open expiry boundary.
        if self.gate_state == "closed" and now >= self.gate_until:
            self.gate_state, self.gate_until = "open", 0

    def note(self, delta=0, *, activity=True, admit=False):
        with self.lock:
            # Close, commit, expiry and request/frame admission share this lock.
            self.expire_gate(time.monotonic())
            if admit and self.gate_state in ("closed", "committed"):
                return False
            if activity:
                self.activity = time.monotonic()
            self.active += delta
            return True

    def failed(self):
        self.uncertain = True

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        counted, quiet = False, False
        try:
            names = {b"upgrade": "Upgrade", b"sec-websocket-protocol": "Sec-WebSocket-Protocol",
                     b"accept": "Accept"}
            headers = {names[key.lower()]: value.decode("latin1")
                       for key, value in scope.get("headers", ()) if key.lower() in names}
            # ASGI websocket scopes need not contain an Upgrade header.
            if kind == "websocket":
                headers["Upgrade"] = "websocket"
            quiet = passive(scope.get("method", "GET"), scope["path"], headers)
            # This known SSE feed outlives a settled turn. Its keepalives are
            # not work; durable turns/runs protect live work instead. Finite
            # polls and every unknown stream still pin until completion.
            subscription = kind == "http" and passive_subscription(scope.get("method", "GET"), scope["path"])
            counted = not quiet or (kind == "http" and not subscription)
            admitted = self.note(int(counted), activity=not quiet, admit=True)
        except Exception:
            self.failed()
            admitted = self.note(activity=False, admit=True)
        if not admitted:
            if kind == "http":
                await send({"type": "http.response.start", "status": 503,
                            "headers": [(b"retry-after", b"1"), (b"content-length", b"0"),
                                        (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b""})
            else:
                await send({"type": "websocket.close", "code": 1013,
                            "reason": "Private dev instance pausing; retry after wake"})
            return

        async def observe_receive():
            message = await receive()
            if kind == "websocket" and message["type"] == "websocket.receive":
                try:
                    text = message.get("text")
                    # Only known JSON heartbeat types are passive. Large, binary,
                    # malformed and unknown frames count as activity, never stored.
                    heartbeat = False
                    if text is not None and len(text) <= 1024:
                        try:
                            heartbeat = json.loads(text).get("type") in ("ping", "pong")
                        except (ValueError, AttributeError):
                            pass
                    if not heartbeat and not self.note(admit=True):
                        # The chat client renders this synchronous command error,
                        # rather than leaving an optimistic send silently pending.
                        try:
                            await send({"type": "websocket.send", "text": json.dumps({
                                "type": "error", "message": "Private dev instance is pausing. "
                                "Message was NOT sent; reload and retry your message."})})
                            await send({"type": "websocket.close", "code": 1013,
                                        "reason": "Message refused during private dev pause"})
                        except Exception:
                            self.failed()
                        return {"type": "websocket.disconnect", "code": 1013}
                except Exception:
                    self.failed()
            return message  # The very same message, unchanged and without a queue.

        try:
            return await self.app(scope, observe_receive if kind == "websocket" else receive, send)
        finally:
            if counted:
                try:
                    self.note(-1, activity=not quiet)
                except Exception:
                    self.failed()
