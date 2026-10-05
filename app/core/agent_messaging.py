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

    from app.core import remote_task

    # A Task child another worker started here: its `main` is the
    # conversation on that worker (app.core.remote_task.send_to_parent).
    parent = remote_task.remote_parent(session_id)
    if parent is not None:
        rows.insert(0, {
            "name": "main",
            "ref": None,
            "worker_session_id": parent["owner_session_id"],
            "status": "running",
            "kind": "parent",
            "description": f"The conversation that started you, on {parent['worker_name']}",
            "remote_parent": parent["device_id"],
        })

    # Nested dispatch records a worker on both the root (UI routing) and the
    # direct caller. The root also knows that caller as a worker; exclude that
    # routing copy rather than sending a nested worker's `main` to the root.
    owners = [
        owner
        for owner in task_registry.sessions_with_task(session_id)
        if (task_registry.get_task(owner, session_id) or {}).get("kind") == "agent"
    ] if parent is None else []
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
        row = {
            # The roster name fluid derived from the task, else
            # the agent type, else the subject.
            "name": task.get("name") or task.get("agent") or task["subject"] or target,
            "ref": target,
            "worker_session_id": target,
            "task_id": target,
            "status": task["status"],
            "kind": "subagent",
            "description": task["subject"],
        }
        if task.get("worker"):
            # A Task child running on another worker (app.core.remote_task).
            row["worker"] = task["worker"].get("name")
            if task.get("unreachableSince"):
                row["unreachable_since"] = task["unreachableSince"]
        rows.append(row)
        seen.add(target)
    return rows


def may_message(sender: str, target: str) -> bool:
    """Whether ``sender`` can still address ``target`` (the roster's rule).

    A Task child another worker started here also hears from the
    conversation that started it. That sender lives on the other worker, so
    it has no roster here; ``remote_task._message_child`` checked it on
    arrival, and without this its message waited for the child's turn to end
    instead of joining it at the next tool result (#1804).
    """
    if any(row["worker_session_id"] == target for row in peer_rows(sender)):
        return True
    from app.core import remote_task

    parent = remote_task.remote_parent(target)
    return parent is not None and sender in {
        parent.get("owner_session_id"), parent.get("parent_session_id"),
    }


def sender_label(receiver: str | None, sender: str) -> str | None:
    """What ``receiver``'s transcript calls ``sender``: the worker's task
    description (its roster name when the task has none), or ``main`` for the
    parent. None when the receiver's roster does not list it; the chat then
    shows the session id."""
    try:
        rows = peer_rows(receiver)
    except Exception:  # noqa: BLE001 — a label never blocks a delivery note
        return None
    for row in rows:
        if row.get("worker_session_id") != sender:
            continue
        label = row.get("description") if row.get("kind") == "subagent" else None
        label = label or row.get("name")
        if label and label != sender:
            return str(label)
    return None


def make_roster(supplier):
    async def roster(data, ctx):
        from app.core.task_registry import task_registry

        session_id = supplier()
        # After a restart the registry starts empty; the session's finished
        # and interrupted workers come back from the durable rows first.
        await task_registry.ensure_hydrated(session_id)
        rows = peer_rows(session_id)
        return [
            r for r in rows if not data.get("running_only") or r["status"] == "running"
        ]

    return roster


def make_message(supplier):
    async def message(data, ctx):
        from app.core import turn_scheduler
        from app.core.task_registry import task_registry

        session_id = supplier()
        address = str(data.get("to") or "").strip()
        # A worker interrupted by (or finished before) a restart is only in
        # the durable rows until this rebuilds it; without it its task id
        # answered "Unknown agent" although its transcript could be resumed.
        await task_registry.ensure_hydrated(session_id)
        rows = peer_rows(session_id)
        # Exact first: the task id (== worker session id), the name, or
        # "name [ref]". Then case-insensitive on the name — models retype a
        # slug they read as "Fix Flaky Login".
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
        text = str(data.get("message") or "")
        urgent = bool(data.get("urgent"))
        if row.get("remote_parent") or row.get("worker"):
            # That session lives on another worker: the message travels there
            # (app.core.remote_task), and nothing here queues a turn on the
            # read-only mirror.
            from app.core import remote_task

            if row.get("remote_parent"):
                outcome = await remote_task.send_to_parent(session_id, row, text, urgent=urgent)
            else:
                outcome = await remote_task.send_to_child(session_id, row, text, urgent=urgent)
            return ToolOutcome(text=outcome["text"], is_error=outcome["is_error"])
        from app.core import archive_guard

        if await archive_guard.is_archived_async(target):
            archive_guard.log_dropped(target, "agent_message", f"SendMessage from {session_id}")
            return ToolOutcome(
                text=archive_guard.refusal(row["name"], "receive messages or be resumed"),
                is_error=True,
            )
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
        if not resuming:
            status, turn_id = await deliver_message(target, session_id, text, urgent=urgent)
            return ToolOutcome(**delivery_receipt(row["name"], status, turn_id))
        turn_id = await turn_scheduler.on_agent_message(
            target, session_id, text, reply_to=session_id,
        )
        if not turn_id:
            return ToolOutcome(**delivery_receipt(row["name"], STOPPED))
        if turn_id == turn_scheduler.HELD:
            return ToolOutcome(text=held_receipt(row["name"]))
        return ToolOutcome(text=f"Resume queued for {row['name']} (turn {turn_id})")

    return message


#: What :func:`deliver_message` did with a message.
DELIVERED = "delivered"
QUEUED = "queued"
HELD = "held"
STOPPED = "stopped"


async def deliver_message(target: str, sender: str, text: str, *,
                          urgent: bool = False) -> tuple[str, str | None]:
    """Hand ``text`` from ``sender`` to ``target``, a session on this worker.

    ``(DELIVERED, None)`` when its live engine took it (read at the next tool
    result), ``(QUEUED, turn id)`` when it waits on the session's queue (a
    running receiver still takes it at its next tool boundary), ``(HELD, None)``
    when the receiver is parked on background work and the message waits for
    its next wake, ``(STOPPED, None)`` when the receiver was stopped. Used for
    local receivers and for messages another worker sent here
    (``app.core.remote_task``).
    """
    from app.core import session_runtime, turn_scheduler
    from app.core.session_pool import get_session_pool

    state = get_session_pool().get(target)
    adapter = getattr(state, "adapter", None)
    engine = getattr(adapter, "engine", None)
    # An engine that says it is idle is not offered the message: the
    # scheduler below decides between a wake and a hold on the app's own
    # park state, which the engine's can lag. Engines that do not report
    # (None) are offered it as before and refuse when idle.
    if (getattr(adapter, "provider_kind", None) != "delegated"
            and engine is not None and session_runtime.get_live_runtime(target) is not None
            and getattr(engine, "turn_running", None) is not False):
        try:
            # Only the app queue may start a turn. The atomic no-wake op
            # also covers a runtime still starting or an engine just done.
            await engine.submit(AgentMessage(
                text=text, sender=sender, wake=False, urgent=urgent,
            ))
        except RuntimeError:
            # Refusal means not accepted; keep it on the durable queue.
            pass
        else:
            return DELIVERED, None
    turn_id = await turn_scheduler.on_agent_message(
        target, sender, text, **({"urgent": True} if urgent else {}),
    )
    if not turn_id:
        return STOPPED, None
    if turn_id == turn_scheduler.HELD:
        return HELD, None
    # A receiver that is running (a delegated CLI turn, or a native one the
    # engine path above could not reach) takes the queued message at its
    # next tool boundary; the row's receipt retires it exactly once.
    await session_runtime.try_steer_pending(target)
    return QUEUED, turn_id


def delivery_receipt(name: str, status: str, turn_id: str | None = None) -> dict:
    """The sender's tool result for a :func:`deliver_message` status."""
    if status == DELIVERED:
        return {"text": f"Delivered to {name}"}
    if status == QUEUED:
        return {"text": f"Queued for {name}"}
    if status == HELD:
        return {"text": held_receipt(name)}
    return {"text": f"Could not deliver to {name}: receiver was stopped", "is_error": True}


def held_receipt(name: str) -> str:
    """What the sender reads when its message was held, not delivered."""
    return (
        f"Held for {name} until it wakes: it is parked waiting on background "
        "work and reads your message with its next wake. Do not wait for a "
        "reply. Send again with urgent=true only if you are blocked on its answer."
    )


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
    # An untouched dedicated checkout is reclaimed when its worker finishes;
    # the resumed turn reuses that saved cwd, so bring it back first.
    from app.core.worker_checkout import restore_reclaimed_worker_checkout
    if not await asyncio.to_thread(restore_reclaimed_worker_checkout, target):
        return "its dedicated checkout could not be restored; dispatch a new Task instead"
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

    async with turn_scheduler.message_resume_lock(session_id):
        record = supervisor.get_worker(owner, session_id)
        if (record and record.torn_down) or task_registry.is_user_stopped(session_id):
            return
        # Read only this turn's report: an interrupted continuation must never
        # report the previous successful turn as its new result.
        events = await asyncio.to_thread(provider_agent._load_session_events, session_id)
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
        # Read park BEFORE busy: unpark starts queued/running before clearing
        # the parked row. The single busy query covers queued->running too.
        # Admission holds this lock until both queue and ownership are written.
        parked = await asyncio.to_thread(turn_scheduler._session_has_parked_turn, session_id)
        busy = await asyncio.to_thread(turns_db.session_has_unsettled_turns, session_id)
        latest = await asyncio.to_thread(turn_scheduler._latest_context_turn, session_id)
        if (record and record.torn_down) or task_registry.is_user_stopped(session_id):
            return
        settled_now = False
        if (not busy and not parked and (not latest or latest["id"] == settled["id"])
                and not task_registry.is_woken(owner, session_id, completion_id=settled["id"])):
            settled_now = True
            task_registry.complete_task(owner, session_id, status, summary=summary)
            if (record and record.continuation_turn_id
                    and supervisor.get_worker(owner, session_id) is record):
                supervisor.unregister_worker(owner, session_id)
    # Completion dedupe is per turn, independent of the shared worker's status.
    # Deliver outside the admission lock: a parent's wake may itself enqueue.
    await turn_scheduler.on_task_terminal(owner, {
        "task_id": session_id, "tool_use_id": session_id,
        "status": status, "summary": summary,
        "agent_message_turn_id": settled["id"],
    })
    if settled_now:
        # Same rule as a first run: an untouched dedicated checkout is reclaimed.
        from app.core.worker_checkout import reclaim_untouched_worker_checkout
        await asyncio.to_thread(reclaim_untouched_worker_checkout, session_id)
