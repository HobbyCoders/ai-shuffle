"""App-owned native messaging for Task workers and scoped Swarm peers."""

from __future__ import annotations

import asyncio

from fluid.events.ops import AgentMessage
from fluid.tools.registry import ToolOutcome

from app.core import swarm


def peer_rows(session_id: str | None) -> list[dict]:
    """Use the same authorized identities for discovery and delivery.

    Swarm workers stay inside their workflow. Other sessions can address their
    registered Task workers and any Swarms they own, never arbitrary pool IDs.
    """
    from app.core.task_registry import task_registry

    if not session_id:
        return []
    rows = swarm.peer_rows(session_id)
    if session_id in swarm.workers:
        return rows

    # Nested dispatch records a worker on both the root (UI routing) and the
    # direct caller. The root also knows that caller as a worker; exclude that
    # routing copy rather than sending a nested worker's `main` to the root.
    owners = [
        owner
        for owner in task_registry.sessions_with_task(session_id)
        if (task_registry.get_task(owner, session_id) or {}).get("kind") == "agent"
    ]
    direct_owners = [
        owner
        for owner in owners
        if not any(
            owner != other
            and (task_registry.get_task(owner, other) or {}).get("kind") == "agent"
            for other in owners
        )
    ]
    if len(direct_owners) == 1:
        rows.insert(
            0,
            {
                "name": "main",
                "ref": None,
                "worker_session_id": direct_owners[0],
                "status": "running",
                "kind": "parent",
                "description": "The conversation that started you",
            },
        )

    seen = {row["worker_session_id"] for row in rows}
    # Do not truncate before name resolution: an older duplicate must still
    # make a bare name ambiguous. ListAgents owns the display cap.
    for task in task_registry.get_all(session_id, limit=None):
        target = task["id"]
        if (
            task["kind"] != "agent"
            or not target
            or target == session_id
            or target in seen
        ):
            continue
        rows.append(
            {
                # The roster name fluid derived from the task (round 9), else
                # the agent type, else the subject.
                "name": task.get("name") or task.get("agent") or task["subject"] or target,
                "ref": target,
                "worker_session_id": target,
                "task_id": target,
                "status": task["status"],
                "kind": "subagent",
                "description": task["subject"],
            }
        )
        seen.add(target)
    return rows


def make_roster(supplier):
    def roster(data, ctx):
        rows = peer_rows(supplier())
        return [
            r for r in rows if not data.get("running_only") or r["status"] == "running"
        ]

    return roster


def make_message(supplier):
    async def message(data, ctx):
        from app.core import session_runtime, turn_scheduler
        from app.core.session_pool import get_session_pool
        from app.core.task_registry import task_registry

        session_id = supplier()
        address = str(data.get("to") or "").strip()
        rows = peer_rows(session_id)
        # Exact first: the task id (== worker session id), the name, or
        # "name [ref]". Then case-insensitive on the name — models retype a
        # slug they read as "Fix Flaky Login" (round 9).
        matches = [
            r for r in rows
            if address in {r["name"], r["worker_session_id"], r.get("task_id"),
                           f"{r['name']} [{r['ref']}]"}
        ]
        if not matches and address:
            folded = address.casefold()
            matches = [r for r in rows if str(r["name"]).casefold() == folded]
        if not matches:
            return ToolOutcome(
                text=f"Unknown agent {address!r}; use ListAgents", is_error=True
            )
        if len(matches) != 1:
            options = "; ".join(
                f"{r['name']} [{r['ref']}] (task id {r.get('task_id') or r['worker_session_id']})" for r in matches
            )
            return ToolOutcome(
                text=(
                    f"Ambiguous agent {address!r}; address one by task id or "
                    f"name [ref]: {options}"
                ),
                is_error=True,
            )
        row = matches[0]
        target = row["worker_session_id"]
        resuming = row["status"] != "running"
        if resuming:
            error = await resume_unavailable(target, row)
            if error:
                return ToolOutcome(text=f"Could not resume {row['name']}: {error}", is_error=True)
        if task_registry.is_user_stopped(target):
            return ToolOutcome(
                text=f"Could not deliver to {row['name']}: receiver was stopped",
                is_error=True,
            )
        state = get_session_pool().get(target)
        adapter = getattr(state, "adapter", None)
        engine = getattr(adapter, "engine", None)
        text = str(data.get("message") or "")
        if (not resuming and getattr(adapter, "provider_kind", None) != "delegated"
                and engine is not None and session_runtime.get_live_runtime(target) is not None):
            try:
                # Only the app queue may start a turn. The atomic no-wake op
                # also covers a runtime still starting or an engine just done.
                await engine.submit(AgentMessage(
                    text=text, sender=session_id, wake=False,
                ))
            except RuntimeError:
                # Refusal means not accepted; keep it on the durable queue.
                pass
            else:
                return ToolOutcome(text=f"Delivered to {row['name']}")
        if resuming:
            turn_id = await turn_scheduler.on_agent_message(
                target, session_id, text, reply_to=session_id,
            )
        else:
            turn_id = await turn_scheduler.on_agent_message(target, session_id, text)
        if not turn_id:
            return ToolOutcome(
                text=f"Could not deliver to {row['name']}: receiver was stopped",
                is_error=True,
            )
        if resuming:
            return ToolOutcome(text=f"Resume queued for {row['name']} (turn {turn_id})")
        return ToolOutcome(text=f"Queued for {row['name']}")

    return message


async def resume_unavailable(target, row):
    """A message is never authority to undo Stop or retry a policy rejection."""
    from app.core.task_registry import task_registry
    from app.core import provider_agent, turn_scheduler
    from app.db import sessions

    if task_registry.is_user_stopped(target):
        return "receiver was stopped by the user; ask them to continue it in its chat"
    if row["status"] not in {"completed", "error", "failed", "interrupted"}:
        return f"state {row['status']} is not resumable; open the worker chat"
    session = await asyncio.to_thread(sessions.get_session, target)
    context = await asyncio.to_thread(turn_scheduler._latest_context_turn, target)
    if not session or not context or not context.get("profile_id"):
        return "saved session/turn context is unavailable; open the worker chat"
    outcome = await asyncio.to_thread(provider_agent.final_turn_outcome, target)
    terminal = await asyncio.to_thread(turn_scheduler._turn_terminal_payload, target, context["id"])
    error = (str(outcome.get("error") or "") + " " + str(terminal.get("errors") or "")).casefold()
    if any(marker in error for marker in ("cyber_policy", "content_policy", "policy rejection", "policy violation", "safety policy")):
        return "the provider rejected this work on policy grounds; ask the user, do not replay it"
    return None


async def finish_message_resume(session_id, settled, outcome):
    """Complete a resumed worker through the same parent wake gate as Task."""
    from app.core import provider_agent, turn_scheduler
    from app.core.task_registry import task_registry

    owner = (settled.get("payload") or {}).get("agent_message_reply_to")
    if not owner or outcome == "parked":
        return
    from app.core import provider_agent_supervisor as supervisor
    from app.db import turns as turns_db

    record = supervisor.get_worker(owner, session_id)
    try:
        if (record and record.torn_down) or task_registry.is_user_stopped(session_id):
            return
        # Read only this turn's report: an interrupted continuation must never
        # report the previous successful turn as its new result.
        events = await asyncio.to_thread(provider_agent._load_session_events, session_id)
        if (record and record.torn_down) or task_registry.is_user_stopped(session_id):
            return
        events = [event for event in events if event.get("turn_id") == settled["id"]]
        text = ""
        for event in events:
            payload = event.get("payload") or {}
            if event.get("event_type") == "tool.result":
                text = ""
            elif event.get("event_type") == "assistant.text_delta":
                text += payload.get("text") or payload.get("delta") or ""
            elif event.get("event_type") == "message.assistant" and payload.get("kind") in (None, "text"):
                text += payload.get("content") or ""
            elif event.get("event_type") == "turn.aborted":
                text = payload.get("message") or "Worker continuation interrupted"
            elif event.get("event_type") == "turn.completed" and payload.get("is_error"):
                outcome = "error"
                text = str(payload.get("errors") or "Worker continuation failed")
        status = "completed" if outcome == "completed" else "error"
        summary, _ = provider_agent.bound_report(text or "Worker continuation ended without a report")
        task_registry.complete_task(owner, session_id, status, summary=summary)
        await turn_scheduler.on_task_terminal(owner, {
            "task_id": session_id, "tool_use_id": session_id,
            "status": status, "summary": summary,
            "agent_message_turn_id": settled["id"],
            "origin": turn_scheduler.PROVIDER_AGENT_ORIGIN,
        })
    finally:
        if record and record.continuation_turn_id:
            queued = await asyncio.to_thread(turns_db.get_queued_turns, session_id)
            if (not any(t["id"] == record.continuation_turn_id for t in queued)
                    and supervisor.get_worker(owner, session_id) is record):
                supervisor.unregister_worker(owner, session_id)
