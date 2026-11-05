"""Bounded session intent, not an executor or independent verification."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

MAX_TASKS = 50
MAX_PAYLOAD = 32768
MAX_RECEIPTS = 64
STATUSES = ("pending", "in_progress", "blocked", "completed", "cancelled")
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
GUIDANCE = (
    "For complex (~5+ real steps or multi-turn) work, use Plan for the goal, "
    "done_when acceptance and stable tasks; skip it for simple requests. Batch "
    "updates at meaningful transitions, keep blockers visible, and never treat "
    "model-reported completion as independent verification or cancellation as success. "
    "Plan records intent only: it never starts workers, polls or schedules wakes. "
    "Do not copy the checklist across stores."
)
def guidance(tools=None):
    text = GUIDANCE
    names = set(tools or ("Ledger", "Remember", "WorkingRecord")) if tools is None else set(tools)
    for name, purpose in (("WorkingRecord", "owner-wide commitments/results"), ("Ledger", "constraints/decisions"), ("Remember", "lasting facts")):
        if any(n == name or n.endswith("__" + name) for n in names):
            text += f" Keep {purpose} in {name}."
    return text


TASK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "pattern": ID_PATTERN},
        "title": {"type": "string", "maxLength": 200},
        "status": {"type": "string", "enum": list(STATUSES)},
        "depends_on": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
        "blocked_reason": {"type": "string", "maxLength": 500},
        "evidence": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 500}},
        "references": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 100}},
    }, "required": ["id", "title", "status"],
}
SPEC = ToolSpec(
    name="Plan", description=(
        "Session-owned plan. get returns current state; update atomically upserts "
        "tasks by stable id (omitted tasks survive), optionally revising goal/done_when. "
        "Start from revision 0; update requires expected_revision and a unique request_id. "
        "Retry the same request verbatim (last 64 receipts retained); on conflict get "
        "the latest state before editing. Initial update requires goal, done_when and tasks. "
        "50 active-generation tasks, 32KiB payload; blocked needs blocked_reason. "
        "rollover=true starts a new generation only when every old task is terminal; "
        "supply goal, done_when and tasks. IDs are stable within a generation; history "
        "is retained by the host. Dependencies must exist "
        "and be acyclic; completed tasks require completed dependencies. Evidence and "
        "existing execution references are optional, not proof. Reported statuses "
        "are intent, not independent verification; cancelled is not successful "
        "completion. This tool never executes work."
    ),
    input_schema={"type": "object", "additionalProperties": False, "properties": {
        "action": {"type": "string", "enum": ["get", "update"]},
        "rollover": {"type": "boolean", "description": "Explicit new generation after all old tasks completed/cancelled; retains history."},
        "expected_revision": {"type": "integer", "minimum": 0},
        "request_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "goal": {"type": "string", "maxLength": 600},
        "done_when": {"type": "string", "maxLength": 1000},
        "tasks": {"type": "array", "maxItems": MAX_TASKS, "items": TASK_SCHEMA},
    }, "required": ["action"]}, risk="safe",
)


def empty_plan() -> dict:
    return {"version": 1, "generation": 1, "revision": 0, "goal": "", "done_when": "", "tasks": []}


def _text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{name} must be {'a' if empty else 'a non-empty'} string of at most {maximum} characters")
    if any(ord(c) == 0 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f"{name} contains unsupported characters")
    return value.strip()


def validate_input(args: dict) -> None:
    if not isinstance(args, dict) or len(json.dumps(args, ensure_ascii=True).encode()) > MAX_PAYLOAD:
        raise ValueError("Plan payload must be an object of at most 32KiB")
    allowed = set(SPEC.input_schema["properties"])
    if set(args) - allowed or args.get("action") not in ("get", "update"):
        raise ValueError("Unknown Plan action or field")
    if args["action"] == "get":
        if set(args) != {"action"}:
            raise ValueError("get takes only action")
        return
    if type(args.get("expected_revision")) is not int or args["expected_revision"] < 0:
        raise ValueError("update requires a non-negative expected_revision")
    _text(args.get("request_id"), "request_id", 80)


def apply_update(document: dict, receipts: list, args: dict) -> tuple[dict, list, dict, bool]:
    """Pure transition shared by PostgreSQL and standalone SQLite transactions."""
    validate_input(args)
    digest = hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    for receipt in receipts:
        if receipt["request_id"] == args["request_id"]:
            if receipt["digest"] != digest:
                raise ValueError("request_id was already used with different input")
            return document, receipts, {**receipt["result"], "replayed": True}, False
    if args["expected_revision"] != document["revision"]:
        raise ValueError(f"Revision conflict: current revision is {document['revision']}; get before updating")
    if "rollover" in args and type(args["rollover"]) is not bool:
        raise ValueError("rollover must be boolean")
    updated = deepcopy(document)
    if args.get("rollover"):
        if not document["revision"] or any(t["status"] not in ("completed", "cancelled") for t in document["tasks"]):
            raise ValueError("Rollover requires an existing plan with all tasks completed or cancelled")
        if not all(args.get(k) for k in ("goal", "done_when", "tasks")):
            raise ValueError("Rollover requires goal, done_when and new tasks")
        updated.update(tasks=[], generation=document["generation"] + 1)
    for key, bound in (("goal", 600), ("done_when", 1000)):
        if key in args:
            updated[key] = _text(args[key], key, bound)
    tasks = {t["id"]: deepcopy(t) for t in updated["tasks"]}
    incoming = args.get("tasks", [])
    if not isinstance(incoming, list) or len(incoming) > MAX_TASKS:
        raise ValueError("tasks must be an array of at most 50 upserts")
    seen = set()
    for task in incoming:
        if not isinstance(task, dict) or set(task) - set(TASK_SCHEMA["properties"]):
            raise ValueError("Unknown task field")
        tid = task.get("id")
        if not isinstance(tid, str) or not re.fullmatch(ID_PATTERN, tid) or tid in seen:
            raise ValueError("Task IDs must be unique stable alphanumeric/_/- identifiers (1..64 characters)")
        seen.add(tid)
        title = _text(task.get("title"), "title", 200)
        if task.get("status") not in STATUSES:
            raise ValueError("Invalid task status")
        current = {**tasks.get(tid, {}), **deepcopy(task), "title": title}
        for key, count, bound in (("depends_on", 10, 64), ("evidence", 3, 500), ("references", 5, 100)):
            values = current.get(key, [])
            if not isinstance(values, list) or len(values) > count:
                raise ValueError(f"{key} must have at most {count} entries")
            current[key] = list(dict.fromkeys(_text(v, key, bound) for v in values))
        if current["status"] == "blocked":
            current["blocked_reason"] = _text(current.get("blocked_reason"), "blocked_reason", 500)
        else:
            current.pop("blocked_reason", None)
        tasks[tid] = current
    if not updated["goal"] or not updated["done_when"] or not tasks:
        raise ValueError("A plan needs goal, done_when and at least one task")
    if len(tasks) > MAX_TASKS:
        raise ValueError("A plan may contain at most 50 tasks (including terminal tasks)")
    visiting, visited = set(), set()
    def visit(tid):
        if tid in visiting:
            raise ValueError("Task dependency cycle")
        if tid in visited:
            return
        visiting.add(tid)
        for dependency in tasks[tid].get("depends_on", []):
            if dependency not in tasks:
                raise ValueError("Unknown task dependency")
            if tasks[tid]["status"] == "completed" and tasks[dependency]["status"] != "completed":
                raise ValueError("Completed tasks require completed dependencies; cancellation is not success")
            visit(dependency)
        visiting.remove(tid)
        visited.add(tid)
    for tid in tasks:
        visit(tid)
    updated.update(revision=document["revision"] + 1, tasks=list(tasks.values()))
    if len(json.dumps(updated, ensure_ascii=True).encode()) > MAX_PAYLOAD:
        raise ValueError("Stored plan exceeds 32KiB")
    result = {"revision": updated["revision"], "generation": updated["generation"], "changed_ids": list(t["id"] for t in incoming),
              "counts": {s: sum(t["status"] == s for t in updated["tasks"]) for s in STATUSES},
              "status_source": "model_reported"}
    receipt = {"request_id": args["request_id"], "digest": digest, "result": result}
    return updated, [*receipts, receipt][-MAX_RECEIPTS:], result, True


def _local(args: dict, ctx: ToolCtx) -> dict:
    from fluid.state import project_state_root
    if not ctx.session_id:
        raise ValueError("Plan requires an active session")
    path = project_state_root(ctx) / "plans.sqlite3"
    if args["action"] == "get" and not path.exists():
        return {"plan": empty_plan(), "status_source": "model_reported"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=5) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS plans (session_id TEXT PRIMARY KEY, document TEXT, receipts TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS history (session_id TEXT, revision INTEGER, document TEXT, PRIMARY KEY(session_id,revision))")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT document, receipts FROM plans WHERE session_id=?", (ctx.session_id,)).fetchone()
        document, receipts = (json.loads(row[0]), json.loads(row[1])) if row else (empty_plan(), [])
        if args["action"] == "get":
            return {"plan": document, "status_source": "model_reported"}
        document, receipts, result, changed = apply_update(document, receipts, args)
        if changed:
            conn.execute("INSERT INTO history VALUES (?,?,?)", (ctx.session_id, document["revision"], json.dumps(document)))
            conn.execute("INSERT INTO plans VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET document=excluded.document, receipts=excluded.receipts",
                         (ctx.session_id, json.dumps(document), json.dumps(receipts)))
        return result


def execute(args: dict, ctx: ToolCtx) -> dict:
    validate_input(args)
    backend = ctx.extra.get("session_plan")
    # A host backend never falls back to local storage on failure.
    return backend(args) if backend else _local(args, ctx)


async def handler(args: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    try:
        # Capture host fencing before yielding to a thread (pooled engines update
        # their per-turn proof). No model-controlled session id or ownership input.
        backend = ctx.extra.get("session_plan")
        call = backend.capture() if hasattr(backend, "capture") else lambda a: execute(a, ctx)
        result = await asyncio.to_thread(call, args)
        return ToolOutcome(text=json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        return ToolOutcome(text=str(exc), is_error=True)


def render_reminder(document: dict) -> str | None:
    if not document.get("revision"):
        return None
    tasks = document["tasks"]
    lines = [f"# Session plan (revision {document['revision']}; model-reported, not independent verification)",
             "Goal: " + document["goal"][:300], "Done when: " + document["done_when"][:400]]
    active = sorted((t for t in tasks if t["status"] not in ("completed", "cancelled")),
                    key=lambda t: (t["status"] != "blocked", t["status"] != "in_progress"))
    lines.append("Counts: " + ", ".join(f"{s}={sum(t['status'] == s for t in tasks)}" for s in STATUSES))
    footer = "Plan get reads all tasks/blockers; batch meaningful changes with expected_revision. Cancelled is not success."
    for task in active[:8]:
        line = f"{task['id']} [{task['status']}]: {task['title'][:90]}" + (
            " — " + task["blocked_reason"][:96] if task["status"] == "blocked" else "")
        if len("\n".join([*lines, line, footer])) > 2400:
            break
        lines.append(line)
    return "<system-reminder>\n" + "\n".join([*lines, footer]) + "\n</system-reminder>"


def reminder(ctx: ToolCtx) -> str | None:
    try:
        return render_reminder(execute({"action": "get"}, ctx)["plan"])
    except Exception:
        # Loss of a reminder must not lose a turn; tool reads still report errors.
        return None
