"""Drive a real AI Shuffle session on this worker over loopback.

Builds on the live-smoke client (``scripts/live_smoke/client.py``): loopback
requests are the local owner, and state changes carry a matching Origin. Adds
what the bench needs: a project at a fixed path, per-turn overrides for
model, effort and fast mode, an idle wait, and the full event log.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx

from scripts.live_smoke.client import API, ClientError, HttpWorkerClient

IDLE_GRACE_S = 4.0
POLL_S = 1.0


class AppClient(HttpWorkerClient):
    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        super().__init__(base_url, timeout=30.0, ack_timeout=60.0)

    def ensure_project(self, project_id: str, path: str) -> dict:
        try:
            project = self._req("GET", f"/projects/{project_id}")
        except ClientError:
            project = self._req("POST", "/projects", json={
                "id": project_id, "name": "Harness H2H bench",
                "description": "Workspace for scripts/harness_h2h runs. Safe to delete."})
        got = project.get("path") or ""
        if not got or Path(got).expanduser().resolve() != Path(path).expanduser().resolve():
            raise ClientError(f"project {project_id} points at {got!r}, expected {path!r}")
        return project

    def resolve_model(self, model: str) -> str:
        """``prov:<id>:<model>`` on the ChatGPT-subscription provider row.

        A bare model id binds no provider (the session then has no
        credentials), so the bench always names the row the way the composer
        does.
        """
        if model.startswith("prov:"):
            return model
        data = self._req("GET", "/models")
        providers = data.get("providers") or []
        if isinstance(providers, dict):
            providers = list(providers.values())
        for row in providers:
            if row.get("auth_mode") != "chatgpt" and row.get("provider_type") != "chatgpt":
                continue
            if any(m.get("id") == model for m in row.get("models") or []):
                return f"prov:{row['id']}:{model}"
        raise ClientError(f"no ChatGPT provider row on this worker serves {model}")

    def provider_usable(self, model: str) -> bool:
        """Whether the ChatGPT row is signed in (``usable``) right now."""
        pid = self.resolve_model(model).split(":")[1]
        rows = self._req("GET", "/models").get("providers") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        return any(r.get("id") == pid and r.get("usable", True) for r in rows)

    def start(self, prompt: str, *, project: str, profile: str, model: str, effort: str) -> str:
        model = self.resolve_model(model)
        request_id = uuid.uuid4().hex
        frame = {
            "type": "query", "prompt": prompt, "request_id": request_id,
            "project": project, "profile": profile, "mode": "code",
            "overrides": {"model": model, "effort": effort, "fast": False,
                          "permission_mode": "bypassPermissions"},
        }
        created = self._exchange(
            frame, lambda r: r.get("type") == "session_created" and r.get("request_id") == request_id,
            wait_s=self.ack_timeout, linger_s=self.linger_s)
        if not created:
            raise ClientError("worker did not confirm the new session")
        return created["session_id"]

    def _busy(self, sid: str, s: dict) -> bool:
        if s.get("has_running_tasks") or s.get("parked_task_kind"):
            return True
        return bool(self.state(sid).get("is_streaming"))

    def wait_idle(self, sid: str, *, deadline: float) -> tuple[float | None, bool]:
        """Poll until the turn is over. Returns (time it went idle, timed_out)."""
        idle_since: float | None = None
        saw_busy = False
        while time.time() < deadline:
            s = self._req("GET", f"/sessions/{sid}", params={"message_limit": 5})
            busy = self._busy(sid, s)
            saw_busy = saw_busy or busy
            if not busy and saw_busy:
                idle_since = idle_since or time.time()
                if time.time() - idle_since >= IDLE_GRACE_S:
                    return idle_since, False
            elif not busy and not saw_busy:
                # Not started yet (queued) or finished before the first poll.
                idle_since = idle_since or time.time()
                if time.time() - idle_since >= 30:
                    return idle_since, False
            else:
                idle_since = None
            time.sleep(POLL_S)
        return None, True

    def events(self, sid: str) -> list[dict]:
        """The whole event log: SSE backfill up to ``caught_up``."""
        out: list[dict] = []
        event = None
        with self._http.stream("GET", API + f"/sessions/{sid}/events",
                               headers={"Accept": "text/event-stream"},
                               timeout=httpx.Timeout(120.0)) as resp:
            if resp.status_code >= 400:
                raise ClientError(f"events: HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    event = line[len("event: "):].strip()
                    if event in ("caught_up", "reset"):
                        break
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[len("data: "):])
                    except ValueError:
                        continue
                    out.append({"event_type": event, "data": data})
        return out
