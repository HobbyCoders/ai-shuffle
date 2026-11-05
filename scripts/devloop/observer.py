"""Dev-child-only, pure observational ASGI wrapper (never used by production)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from scripts.devloop.idle import passive


class ActivityObserver:
    """No buffering, scope mutation or network forwarding; bounded receipts only."""
    def __init__(self, app, receipt: Path):
        from app.core.temp_scratch import process_identity

        self.app, self.receipt = app, receipt
        self.pid, self.identity = os.getpid(), process_identity(os.getpid())
        self.lock = threading.Lock()
        self.active = 0
        self.activity = time.monotonic()
        self.uncertain = False
        self.stopped = threading.Event()
        self.writer = threading.Thread(target=self.publish, daemon=True, name="devloop-activity")
        self.writer.start()

    def publish(self):
        from scripts.devloop.__main__ import write_private_json

        while not self.stopped.is_set():
            try:
                with self.lock:
                    record = {"pid": self.pid, "identity": self.identity,
                              "updated": time.monotonic(), "activity": self.activity,
                              "active": self.active, "uncertain": self.uncertain}
                write_private_json(self.receipt, record)
            except Exception:
                self.uncertain = True  # Stay awake until TTL, never silently ignore lost evidence.
            self.stopped.wait(0.5)  # At most two tiny writes/s, not one per request/frame.

    def note(self, delta=0, *, activity=True):
        with self.lock:
            if activity:
                self.activity = time.monotonic()
            self.active += delta

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
            if kind == "http" or not quiet:
                self.note(1, activity=not quiet)
                counted = True
        except Exception:
            self.failed()

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
                    if not heartbeat:
                        self.note()
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
