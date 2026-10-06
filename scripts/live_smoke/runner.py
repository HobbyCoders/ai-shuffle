"""Run the script rows of the smoke scenarios against one worker.

The client is injected (``client.HttpWorkerClient`` for real runs, a fake in
tests). Every session the run creates is archived in ``finally``, so a
failure, the token cap or Ctrl+C still cleans up, and the result file is
written in every case.
"""
from __future__ import annotations

import re
import secrets
import struct
import time
import zlib
from collections.abc import Callable
from pathlib import Path

from scripts.live_smoke import results as results_mod
from scripts.live_smoke import scenarios as scen
from scripts.live_smoke.client import ClientError

CHEAP_HINTS = ("haiku", "nano", "flash-lite", "mini", "lite", "flash", "small")
BUSY_REMOTE = {"working", "parked", "permission", "question"}
IDLE_GRACE_S = 45.0


class StepFailed(Exception):
    pass


class CapExceeded(Exception):
    pass


class SetupError(Exception):
    """The run cannot start (worker unreachable, refused, no revision)."""


def tiny_png(path: Path) -> Path:
    """A 4x4 grey PNG for the image-edit row (stdlib only)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * 4 for _ in range(4))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


def pick_model(models: dict) -> tuple[str | None, str]:
    """Cheapest-looking usable model, by name; ``(None, why)`` when none looks cheap."""
    candidates = []
    if (models.get("claude") or {}).get("enabled", True):
        candidates += [(m.get("value"), m.get("label") or "") for m in models.get("models") or []]
    for prov in models.get("providers") or []:
        if prov.get("usable"):
            candidates += [(f"prov:{prov['id']}:{m['id']}", m.get("label") or "") for m in prov.get("models") or []]
    for hint in CHEAP_HINTS:
        for value, label in candidates:
            if value and (hint in value.lower() or hint in label.lower()):
                return value, f"cheapest by name ('{hint}')"
    return None, "worker default (no model name looked cheap)"


def _tokens(session: dict) -> dict:
    get = lambda *keys: int(next((session[k] for k in keys if session.get(k) is not None), 0) or 0)
    return {
        "input": get("total_tokens_in"),
        "output": get("total_tokens_out"),
        "cache_write": get("total_cache_creation_tokens", "cache_creation_tokens"),
        "cache_read": get("total_cache_read_tokens", "cache_read_tokens"),
    }


def counted(usage: dict) -> int:
    """Tokens that count against the cap: fresh input, output and cache writes.

    Cache reads are recorded but not counted (they bill at a tenth of input).
    """
    return usage["input"] + usage["output"] + usage["cache_write"]


def _texts(messages: list) -> list:
    return [(i, m) for i, m in enumerate(messages)
            if m.get("role") == "assistant" and m.get("type") in ("text", None) and (m.get("content") or "").strip()]


def _user_count(messages: list) -> int:
    return sum(1 for m in messages if m.get("role") == "user" and m.get("type") in (None, "text"))


def _pending_user(messages: list) -> bool:
    """True while the newest user message has no assistant reply after it."""
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    replies = [i for i, _m in _texts(messages)]
    return bool(users) and (not replies or users[-1] > replies[-1])


def _tool_names(messages: list) -> set:
    names = set()
    for m in messages:
        for key in ("toolName", "tool_name"):
            if m.get(key):
                names.add(m[key])
        for child in m.get("agentChildren") or []:
            if child.get("toolName"):
                names.add(child["toolName"])
    return names


def _has_tool(names: set, wanted: str) -> bool:
    return any(n == wanted or n.endswith("__" + wanted) for n in names)


class Runner:
    def __init__(self, client, data: dict, *, results_base: Path, model: str | None = None,
                 token_cap: int | None = None, peer: str | None = None,
                 only: list | None = None, project: str | None = None, agent: str | None = None,
                 poll_s: float = 3.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, run_id: str | None = None,
                 log: Callable[[str], None] = lambda _m: None):
        self.client = client
        self.data = data
        self.defaults = data.get("defaults") or {}
        self.base = Path(results_base)
        self.model_flag = model or self.defaults.get("model")
        self.cap = int(token_cap if token_cap is not None else self.defaults.get("token_cap") or 600000)
        self.peer_flag = peer
        self.only = only
        self.project_flag = project or self.defaults.get("project")
        self.agent = agent
        self.poll_s, self.sleep, self.clock, self.log = poll_s, sleep, clock, log
        self.run_id = run_id or secrets.token_hex(3)
        self.created: list = []          # [{"session_id", "row", "alias", "on"}]
        self.usage: dict = {}            # session id -> token dict
        self.peer: dict | None = None
        self.self_worker: dict = {}
        self.cap_exceeded = False
        self.model: str | None = None

    # -- setup --------------------------------------------------------------

    def preflight(self) -> None:
        try:
            status = self.client.deploy_status()
            installs = self.client.installs()
        except ClientError as exc:
            raise SetupError(str(exc)) from None
        workers = status.get("workers") or []
        me = next((w for w in workers if w.get("is_self")), None)
        if not me or not me.get("revision"):
            raise SetupError("the worker did not report its own revision (deploy-status)")
        self.self_worker = me
        rows = installs.get("installs") or []
        others = [w for w in workers if not w.get("is_self")]
        if self.peer_flag:
            others = [w for w in others if (w.get("label") or "").lower() == self.peer_flag.lower()
                      or w.get("device_id") == self.peer_flag]
            if not others:
                self.peer = {"label": self.peer_flag, "online": False, "why": f"worker offline: {self.peer_flag} is not paired"}
                return
        if len(others) != 1:
            why = "no other worker is paired" if not others else "several other workers; pick one with --peer"
            self.peer = {"label": None, "online": False, "why": f"worker offline: {why}"}
            return
        peer = others[0]
        install = next((i for i in rows if not i.get("is_self") and (
            i.get("id") == peer.get("device_id") or i.get("device_name") == peer.get("label"))), None)
        online = bool(peer.get("online") and install and install.get("online") and install.get("approved", True))
        self.peer = {
            "label": peer.get("label"), "revision": peer.get("revision"), "online": online,
            "instance_id": (install or {}).get("id"),
            "why": None if online else f"worker offline: {peer.get('label')}",
        }

    def choose_model(self) -> dict:
        if self.model_flag:
            return {"model": self.model_flag, "why": "given"}
        try:
            value, why = pick_model(self.client.models())
        except ClientError as exc:
            value, why = None, f"worker default (models list failed: {exc})"
        return {"model": value, "why": why}

    def resolve_project(self) -> str | None:
        if not self.project_flag:
            return None
        try:
            projects = self.client.projects()
        except ClientError:
            return None
        want = self.project_flag.lower()
        for p in projects:
            if want in {str(p.get("id", "")).lower(), str(p.get("name", "")).lower()}:
                return p.get("id")
        return None

    # -- helpers ------------------------------------------------------------

    def _fmt(self, text: str, sessions: dict) -> str:
        def sub(m: re.Match) -> str:
            key = m.group(1)
            if key == "run":
                return self.run_id
            if key == "peer":
                return (self.peer or {}).get("label") or ""
            if key == "self":
                return self.self_worker.get("label") or ""
            if key == "image":
                return str(self.image_path)
            if key.startswith("session:"):
                return sessions.get(key.split(":", 1)[1], "")
            return m.group(0)
        return re.sub(r"\{(run|peer|self|image|session:[a-z0-9_]+)\}", sub, text)

    @property
    def image_path(self) -> Path:
        return self.base / self.revision / "assets" / f"live-smoke-{self.run_id}.png"

    @property
    def revision(self) -> str:
        return (self.self_worker.get("revision") or "").lower()

    def total_tokens(self) -> int:
        return sum(counted(u) for u in self.usage.values())

    def _snapshot(self, sid: str) -> dict:
        s = self.client.session(sid)
        self.usage[sid] = _tokens(s)
        if self.total_tokens() > self.cap:
            self.cap_exceeded = True
            raise CapExceeded(f"token cap exceeded: {self.total_tokens()} > {self.cap} tokens")
        return s

    def _busy(self, sid: str, s: dict) -> bool:
        if s.get("has_running_tasks") or s.get("parked_task_kind"):
            return True
        if s.get("is_remote"):
            return s.get("remote_runtime_state") in BUSY_REMOTE
        return bool(self.client.state(sid).get("is_streaming"))

    def _wait(self, sid: str, within_s: float, until: str = "idle", users_at_least: int = 0) -> dict:
        """Poll a session until its last user message has a reply and the turn is over.

        ``until="reply"`` returns as soon as any reply exists (the turn may
        still be running). ``users_at_least`` also waits for a just-sent
        message to reach the transcript.
        """
        start = self.clock()
        deadline = start + within_s
        idle_since = None
        while True:
            s = self._snapshot(sid)
            messages = s.get("messages") or []
            users = _user_count(messages)
            if until == "reply" and _texts(messages):
                return s
            busy = self._busy(sid, s)
            pending = _pending_user(messages) or users < users_at_least
            if until == "idle" and not busy and not pending:
                return s
            if not busy and pending and users >= max(1, users_at_least):
                idle_since = idle_since if idle_since is not None else self.clock()
                if self.clock() - idle_since > IDLE_GRACE_S:
                    raise StepFailed("the turn ended without a reply")
            else:
                idle_since = None
            if self.clock() >= deadline:
                raise StepFailed(f"no finished reply within {int(within_s)}s")
            self.sleep(self.poll_s)

    def _check(self, step: dict, s: dict, sessions: dict, evidence: dict) -> None:
        messages = s.get("messages") or []
        replies = _texts(messages)
        if replies:
            idx, last = replies[-1]
            evidence.update(message_index=idx, message_id=last.get("id"))
        last_text = (replies[-1][1].get("content") or "").strip() if replies else ""
        pattern = step.get("expect_reply")
        if pattern and not re.search(pattern, last_text):
            raise StepFailed(f"reply {last_text[:120]!r} does not match {pattern!r}")
        names = _tool_names(messages)
        for tool in step.get("expect_tool") or []:
            if not _has_tool(names, tool):
                raise StepFailed(f"tool {tool} was not used (saw {sorted(names)[:8]})")
        if step.get("expect_owner") == "peer":
            peer = self.peer or {}
            if not s.get("is_remote") or (s.get("device_name") != peer.get("label")
                                          and s.get("instance_id") != peer.get("instance_id")):
                raise StepFailed(f"session is owned by {s.get('device_name') or 'this worker'}, not {peer.get('label')}")
        for raw in step.get("expect_files") or []:
            path = self._fmt(raw, sessions)
            if not Path(path).is_file():
                raise StepFailed(f"expected file {path} does not exist")
        if "user_messages" in step:
            got = _user_count(messages)
            if got != step["user_messages"]:
                raise StepFailed(f"{got} user messages, expected {step['user_messages']}")
        for pattern, want in (step.get("replies") or {}).items():
            got = sum(1 for _i, m in replies if re.search(pattern, m.get("content") or ""))
            if got != want:
                raise StepFailed(f"{got} replies match {pattern!r}, expected {want}")

    # -- steps --------------------------------------------------------------

    def _run_row(self, row: dict, ctx: dict) -> dict:
        sessions: dict = {}
        evidence: dict = {"sessions": sessions}
        ctx["evidence"] = evidence
        within_default = self.defaults.get("within_s", 180)
        for n, step in enumerate(row["steps"], 1):
            evidence["step"] = n
            op = step["op"]
            if op == "workers":
                self._check_workers(evidence)
                continue
            within = step.get("within_s", within_default)
            users_at_least = 1
            if op == "start":
                alias = step.get("as", "a")
                on_peer = step.get("on") == "peer"
                sid = self.client.send(
                    self._fmt(step["prompt"], sessions), model=self.model, project=ctx.get("project"),
                    permission_mode=self.defaults.get("permission_mode"), profile=self.agent,
                    target_instance_id=self.peer["instance_id"] if on_peer else None)
                sessions[alias] = sid
                self.created.append({"session_id": sid, "row": row["id"], "alias": alias,
                                     "on": "peer" if on_peer else "self"})
            else:
                sid = sessions[step.get("session", "a")]
                if op == "compact":
                    self._compact(sid, within)
                    evidence["compact"] = "boundary recorded"
                    continue
                if op == "message":
                    if step.get("when") == "reply_seen":
                        self._wait(sid, within, until="reply")
                    users_at_least = _user_count(self._snapshot(sid).get("messages") or []) + 1
                    self.client.send(self._fmt(step["prompt"], sessions), session_id=sid, model=self.model)
            if step.get("wait", True) is False:
                continue
            s = self._wait(sid, within, users_at_least=users_at_least)
            self._check(step, s, sessions, evidence)
        return evidence

    def _compact(self, sid: str, within: float) -> None:
        try:
            self.client.compact(sid)
        except ClientError as exc:
            raise StepFailed(f"compact refused: {exc}") from None
        deadline = self.clock() + within
        while True:
            s = self._snapshot(sid)
            subtypes = [m.get("systemSubtype") or m.get("subtype") for m in s.get("messages") or []]
            if "compact_failed" in subtypes:
                raise StepFailed("compact_failed recorded")
            if "compact_boundary" in subtypes and not self._busy(sid, s):
                return
            if self.clock() >= deadline:
                raise StepFailed(f"no compact boundary within {int(within)}s")
            self.sleep(self.poll_s)

    def _check_workers(self, evidence: dict) -> None:
        status = self.client.deploy_status()
        workers = status.get("workers") or []
        evidence["workers"] = [{k: w.get(k) for k in ("label", "short", "online")} for w in workers]
        if len(workers) < 2:
            raise StepFailed("deploy-status lists fewer than two workers")
        for w in workers:
            label = w.get("label") or ""
            if re.fullmatch(r"(dev_)?[0-9a-f]{8,}", label) or not label:
                raise StepFailed(f"worker shown as {label!r}, not by name")
            if w.get("online") and not w.get("revision"):
                raise StepFailed(f"{label} shows no revision")

    # -- the run ------------------------------------------------------------

    def run(self) -> tuple[dict, Path]:
        self.preflight()
        started = results_mod.now_iso()
        choice = self.choose_model()
        self.model = choice["model"]
        all_rows = scen.rows(self.data)
        selected = [r for r in all_rows if not self.only or r["id"] in self.only]
        if self.only and len(selected) != len(set(self.only)):
            unknown = sorted(set(self.only) - {r["id"] for r in selected})
            raise SetupError(f"unknown row ids: {', '.join(unknown)}")
        out_rows = [{"id": r["id"], "kind": r["kind"], "check": r["check"], "status": "not_run",
                     "reason": "not reached", "evidence": {}} for r in selected]
        project = None
        if any(r.get("needs_project") for r in selected if r["kind"] == "script"):
            project = self.resolve_project()
        if any("{image}" in s.get("prompt", "") for r in selected for s in r.get("steps") or []):
            tiny_png(self.image_path)
        outcome = None
        current = None
        try:
            for row, out in zip(selected, out_rows):
                current = out
                if row["kind"] != "script":
                    out["reason"] = "needs browser" if row["kind"] == "browser" else "owner step"
                    continue
                if self.cap_exceeded:
                    out["reason"] = "token cap reached"
                    continue
                if scen.is_two_worker(row) and not (self.peer or {}).get("online"):
                    out["reason"] = (self.peer or {}).get("why") or "worker offline"
                    continue
                if row.get("needs_project") and not project:
                    out["reason"] = f"project {self.project_flag!r} not found on this worker (--project)"
                    continue
                self.log(f"{row['id']} {row['check']} ...")
                ctx = {"project": project}
                try:
                    out["evidence"] = self._run_row(row, ctx)
                    out.update(status="pass", reason=None)
                except CapExceeded as exc:
                    out.update(status="fail", reason=str(exc), evidence=ctx.get("evidence", {}))
                    self._stop_row_sessions(row["id"])
                except (StepFailed, ClientError) as exc:
                    out.update(status="fail", reason=str(exc), evidence=ctx.get("evidence", {}))
                self.log(f"  {out['status']}{': ' + out['reason'] if out['reason'] else ''}")
            current = None
        except KeyboardInterrupt:
            outcome = "interrupted"
            for out in out_rows:
                if out["status"] == "not_run" and (out is current or out["reason"] == "not reached"):
                    out["reason"] = "interrupted"
        finally:
            cleanup = self.cleanup()
            result = self._result(started, choice, out_rows, outcome, cleanup)
            path = results_mod.result_path(self.base, self.revision, self.self_worker.get("label") or "worker",
                                           partial=self.run_id if self.only else None)
            results_mod.write(path, result)
        return result, path

    def _stop_row_sessions(self, row_id: str) -> None:
        for c in self.created:
            if c["row"] == row_id:
                try:
                    self.client.stop(c["session_id"])
                except ClientError:
                    pass

    def cleanup(self) -> list:
        """Stop anything still running, then archive every session this run created."""
        report = []
        for c in self.created:
            sid = c["session_id"]
            entry = {"session_id": sid, "row": c["row"], "archived": False}
            try:
                try:
                    if self._busy(sid, self.client.session(sid)):
                        self.client.stop(sid)
                except ClientError:
                    pass
                self.client.archive(sid)
                entry["archived"] = True
            except ClientError as exc:
                entry["error"] = str(exc)
            report.append(entry)
        return report

    def _result(self, started: str, choice: dict, rows: list, outcome: str | None, cleanup: list) -> dict:
        if outcome is None:
            if self.cap_exceeded or any(r["status"] == "fail" for r in rows):
                outcome = "fail"
            elif any(r["status"] != "pass" and r["kind"] == "script" for r in rows):
                outcome = "incomplete"
            else:
                outcome = "pass"
        usage = {k: sum(u[k] for u in self.usage.values()) for k in ("input", "output", "cache_write", "cache_read")}
        return {
            "format": 1,
            "revision": self.revision,
            "worker": self.self_worker.get("label"),
            "app_version": self.self_worker.get("app_version"),
            "peer": {k: (self.peer or {}).get(k) for k in ("label", "revision", "online")},
            "run_id": self.run_id,
            "partial": bool(self.only),
            "started_at": started,
            "finished_at": results_mod.now_iso(),
            "model": choice,
            "token_cap": self.cap,
            "tokens": {**usage, "counted": self.total_tokens()},
            "cap_exceeded": self.cap_exceeded,
            "outcome": outcome,
            "rows": rows,
            "sessions": cleanup,
        }
