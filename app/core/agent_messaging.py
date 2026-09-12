"""App-owned native messaging for Task workers and scoped Swarm peers."""

from __future__ import annotations

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
        if row["status"] != "running":
            return ToolOutcome(
                text=f"Agent {row['name']} is not running; this host cannot resume finished workers",
                is_error=True,
            )
        target = row["worker_session_id"]
        if task_registry.is_user_stopped(target):
            return ToolOutcome(
                text=f"Could not deliver to {row['name']}: receiver was stopped",
                is_error=True,
            )
        state = get_session_pool().get(target)
        adapter = getattr(state, "adapter", None)
        engine = getattr(adapter, "engine", None)
        if getattr(adapter, "provider_kind", None) == "delegated":
            return ToolOutcome(
                text="Peer has no live native messaging engine", is_error=True
            )
        text = str(data.get("message") or "")
        if engine is not None and session_runtime.get_live_runtime(target) is not None:
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
        turn_id = await turn_scheduler.on_agent_message(target, session_id, text)
        if not turn_id:
            return ToolOutcome(
                text=f"Could not deliver to {row['name']}: receiver was stopped",
                is_error=True,
            )
        return ToolOutcome(text=f"Queued for {row['name']}")

    return message
