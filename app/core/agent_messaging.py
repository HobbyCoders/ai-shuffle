"""App-owned native messaging for Task workers and scoped Swarm peers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fluid.events.ops import AgentMessage
from fluid.tools.registry import ToolOutcome

from app.core import swarm

logger = logging.getLogger(__name__)


def _persisted_parents(child: str) -> list[str]:
    """Host-written, install-scoped Task edges; read off the event loop."""
    try:
        from app.db.base import get_db
        with get_db() as conn:
            cursor = conn.cursor()
            from app.core.config import settings
            cursor.execute("SELECT DISTINCT session_id FROM background_tasks "
                           "WHERE task_id=%s AND kind='agent' AND assignment_owner=TRUE AND instance_id=%s",
                           (child, settings.effective_instance_id))
            return [row["session_id"] for row in cursor.fetchall()]
    except Exception:  # noqa: BLE001 - missing/old store never establishes trust
        logger.debug("Could not read persisted Task parents", exc_info=True)
        return []


def _live_parents(child: str) -> set[str]:
    """Read/prune the loop-owned registry only on its owning loop."""
    from app.core.task_registry import task_registry
    return {
        owner for owner in task_registry.sessions_with_task(child)
        if (row := task_registry.get_task(owner, child) or {}).get("kind") == "agent"
        and row.get("assignmentOwner", True)
    } - {child}


def _parent_graph(child: str) -> dict[str, set[str]]:
    """Live-only synchronous view; async callers supply the durable graph."""
    parents: dict[str, set[str]] = {}
    pending = [child]
    while pending:
        node = pending.pop()
        if node not in parents:
            parents[node] = _live_parents(node)
            pending.extend(parents[node] - parents.keys())
    return parents


async def parent_graph(child: str | None) -> dict[str, set[str]]:
    """Only SQL runs in a thread; registry, swarm and cache reads stay on-loop."""
    parents: dict[str, set[str]] = {}
    pending = [child] if child else []
    while pending:
        node = pending.pop()
        if node not in parents:
            durable = await asyncio.to_thread(_persisted_parents, node)
            parents[node] = (set(durable) | _live_parents(node)) - {node}
            pending.extend(parents[node] - parents.keys())
    return parents


async def peer_rows_async(session_id: str | None) -> list[dict]:
    return peer_rows(session_id, parents=await parent_graph(session_id))


async def message_provenance_async(receiver: str, sender: str) -> str:
    return message_provenance(receiver, sender, parents=await parent_graph(receiver))


async def may_message_async(sender: str, target: str) -> bool:
    return may_message(sender, target, parents=await parent_graph(sender))


async def sender_label_async(receiver: str | None, sender: str) -> str | None:
    return sender_label(receiver, sender, parents=await parent_graph(receiver))


def _ancestors(child: str, parents: dict[str, set[str]]) -> set[str]:
    seen = {child}
    pending = list(parents.get(child, ()))
    while pending:
        node = pending.pop()
        if node not in seen:
            seen.add(node)
            pending.extend(parents.get(node, ()))
    return seen - {child}


def peer_rows(session_id: str | None, *, parents: dict | None = None) -> list[dict]:
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
    parents = (parents if parents is not None else _parent_graph(session_id)) if parent is None else {}
    owners = parents.get(session_id, set())
    direct_owners = [owner for owner in owners if not any(
        owner != other and owner in _ancestors(other, parents) for other in owners
    )]
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


def may_message(sender: str, target: str, *, parents: dict | None = None) -> bool:
    """Whether ``sender`` can still address ``target`` (the roster's rule).

    A Task child another worker started here also hears from the
    conversation that started it. That sender lives on the other worker, so
    it has no roster here; ``remote_task._message_child`` checked it on
    arrival, and without this its message waited for the child's turn to end
    instead of joining it at the next tool result (#1804).
    """
    if any(row["worker_session_id"] == target for row in peer_rows(sender, parents=parents)):
        return True
    from app.core import remote_task

    parent = remote_task.remote_parent(target)
    return parent is not None and sender in {
        parent.get("owner_session_id"), parent.get("parent_session_id"),
    }


def message_provenance(receiver: str, sender: str, *, parents: dict | None = None) -> str:
    """Host-known Task ancestors update assignments; never infer from the body."""
    try:
        from app.core import remote_task
        parent = remote_task.remote_parent(receiver)
        if parent is not None and sender in {
            parent.get("owner_session_id"), parent.get("parent_session_id"),
        }:
            return "parent"
        if receiver in swarm.workers:
            if any(row.get("kind") == "parent" and row.get("worker_session_id") == sender
                   for row in swarm.peer_rows(receiver)):
                return "parent"
        elif sender in _ancestors(receiver, parents if parents is not None else _parent_graph(receiver)):
            return "parent"
    except Exception:  # noqa: BLE001 - missing registry/remote identity is not proof
        logger.debug("Could not verify message parent", exc_info=True)
    return "peer"


def sender_label(receiver: str | None, sender: str, *, parents: dict | None = None) -> str | None:
    """What ``receiver``'s transcript calls ``sender``: the worker's task
    description (its roster name when the task has none), or ``main`` for the
    parent. None when the receiver's roster does not list it; the chat then
    shows the session id."""
    try:
        rows = peer_rows(receiver, parents=parents)
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
        rows = await peer_rows_async(session_id)
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
        rows = await peer_rows_async(session_id)
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
        from app.db import turns
        failure = await asyncio.to_thread(turns.get_cwd_failure, target)
        if failure:
            return ToolOutcome(text=f"Could not resume {row['name']}: working directory missing: {failure['path']}; ask the owner to resume or restore it", is_error=True)
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
    running receiver still takes it at its next tool boundary, and a parked
    receiver wakes immediately), ``(STOPPED, None)`` when stopped. Used for
    local receivers and for messages another worker sent here
    (``app.core.remote_task``).
    """
    from app.core import session_runtime, turn_scheduler
    from app.core.session_pool import get_session_pool
    from app.db import turns

    if await asyncio.to_thread(turns.get_cwd_failure, target):
        return STOPPED, None
    state = get_session_pool().get(target)
    adapter = getattr(state, "adapter", None)
    engine = getattr(adapter, "engine", None)
    # An idle engine is not offered the message: the scheduler owns its wake,
    # including while parked on background work. Engines that do not report
    # (None) are offered it as before and refuse when idle.
    if (getattr(adapter, "provider_kind", None) != "delegated"
            and engine is not None and session_runtime.get_live_runtime(target) is not None
            and getattr(engine, "turn_running", None) is not False):
        try:
            # Only the app queue may start a turn. The atomic no-wake op
            # also covers a runtime still starting or an engine just done.
            await engine.submit(AgentMessage(
                text=text, sender=sender, wake=False, urgent=urgent,
                provenance=await message_provenance_async(target, sender),
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
    """Compatibility receipt for a legacy worker reporting held delivery."""
    return f"Queued for {name}; delivery is not yet confirmed. Do not resend."


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
    from app.core.worker_checkout import restore_worker_checkout
    restored, note = await asyncio.to_thread(restore_worker_checkout, target)
    if not restored:
        return note or "its dedicated checkout could not be restored; dispatch a new Task instead"
    return None


def _persist_continuation_report(owner, session_id, report):
    """Write a continuation's report where the first round's went."""
    from app.core import provider_agent
    from app.db import database

    try:
        owner_row = database.get_session(owner)
    except Exception:  # noqa: BLE001 — the global project root still applies
        logger.debug("agent_messaging: owner row read failed", exc_info=True)
        owner_row = None
    return provider_agent._persist_worker_report(
        dict(owner_row) if owner_row else None, session_id, report,
    )


def live_continuation_owner(session_id):
    """The parent whose SendMessage continuation of ``session_id`` is live.

    A continuation registers a supervisor record carrying
    ``continuation_turn_id``; a first Task dispatch does not, so its own
    dispatch tail stays the only completer.
    """
    from app.core import provider_agent_supervisor as supervisor
    from app.core.task_registry import task_registry

    for owner in task_registry.sessions_with_task(session_id):
        record = supervisor.get_worker(owner, session_id)
        if record and record.continuation_turn_id:
            return owner
    return None


def _inherited_continuation(session_id, settled):
    """How a reply_to-less turn relates to the live continuation, if at all.

    Returns ``(owner, carrier, delivered)`` or ``None``. ``carrier`` is the
    latest turn before ``settled`` that carries the owner's reply_to (the
    continuation's own turn); ``delivered`` says its report already reached
    the parent. Only a turn queued AFTER the continuation qualifies (higher
    ``position``): one already running when SendMessage queued it (a first
    dispatch, a user's turn) ended before the continuation's work began. The
    parent's task row must still be running, and the carrier must be at rest.
    Blocking (indexed reads).
    """
    from app.core import provider_agent_supervisor as supervisor
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    owner = live_continuation_owner(session_id)
    if not owner:
        return None
    if (task_registry.get_task(owner, session_id) or {}).get("status") != "running":
        return None
    record = supervisor.get_worker(owner, session_id)
    continuation = turns_db.get_turn(record.continuation_turn_id) if record else None
    try:
        if (continuation is None or settled.get("position") is None
                or float(settled["position"]) <= float(continuation["position"])):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    carrier = turns_db.latest_reply_to_turn(session_id, owner, before_position=settled["position"])
    if carrier is None or carrier["id"] == settled["id"]:
        return None
    delivered = task_registry.is_woken(owner, session_id, completion_id=carrier["id"])
    if delivered and carrier.get("state") in ("completed", "aborted"):
        return owner, carrier, True  # at most the quiet close of the task row
    # Stop and archive settle parked rows 'parked_settled' too, and the user's
    # next message clears the latch. A Stop or archive since this admission
    # ended the continuation: what the user says afterwards is not its result.
    if (record.stop_generation is None
            or task_registry.stop_generation(session_id) != record.stop_generation
            or task_registry.is_user_stopped(session_id)):
        return None
    if carrier.get("stop_reason") == "parked_settled":
        return owner, carrier, False
    # Parked or live (it reports itself), or ended without a delivery this
    # turn may not stand in for: never a second report.
    return None


def _turn_report(session_id, turn, outcome, events):
    """``(status, report, summary)`` for ``turn``'s continuation report."""
    from app.core import provider_agent

    events = [event for event in events if event.get("turn_id") == turn["id"]]
    pieces: list[tuple[bool, str]] = []
    # Every text of the turn: the report when nothing follows the last
    # input (a message delivered after the worker's closing text).
    turn_text: list[tuple[bool, str]] = []
    for event in events:
        payload = event.get("payload") or {}
        piece = provider_agent.assistant_text_piece(event)
        if piece is not None:
            pieces.append(piece)
            turn_text.append(piece)
        elif event.get("event_type") in ("tool.result", "message.user"):
            # The report is what follows the last tool result or input (a
            # parked worker's wake reuses this turn id).
            pieces = []
        elif event.get("event_type") == "turn.aborted":
            pieces = [(False, payload.get("message") or "Worker continuation interrupted")]
        elif event.get("event_type") == "turn.completed" and payload.get("is_error"):
            outcome = "error"
            pieces = [(False, str(payload.get("errors") or "Worker continuation failed"))]
    text = (provider_agent.join_assistant_text(pieces).strip()
            or provider_agent.join_assistant_text(turn_text).strip())
    status = "completed" if outcome == "completed" else "error"
    report = text or "Worker continuation ended without a report"
    summary, _ = provider_agent.bound_report(report)
    # A worker a usage limit paused: its parent was not woken at the pause,
    # so this report is where it hears, once, that it was paused (#1831).
    from app.core.limit_resume import report_note
    note = report_note(turn.get("payload"))
    if note:
        summary = f"{note}\n\n{summary}"
        report = f"{note}\n\n{report}"
    return status, report, summary


async def _settle_delivered_continuation(session_id, owner, settled):
    """Close the task row of a continuation whose report already went out.

    Its own completion found the worker busy (a turn queued behind it), so the
    row stayed running. Once that later turn is at rest, record the report the
    parent was sent as the task's result, quietly: the parent already has it,
    so this never wakes the parent again and never reports the later turn's
    text.
    """
    from app.core import turn_scheduler
    from app.core import provider_agent_supervisor as supervisor
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db

    settled_now = False
    async with turn_scheduler.message_resume_lock(session_id):
        record = supervisor.get_worker(owner, session_id)
        if (record and record.torn_down) or task_registry.is_user_stopped(session_id):
            return
        parked = await asyncio.to_thread(turn_scheduler._session_has_parked_turn, session_id)
        busy = await asyncio.to_thread(turns_db.session_has_unsettled_turns, session_id)
        latest = await asyncio.to_thread(turn_scheduler._latest_context_turn, session_id)
        if busy or parked or (latest and latest["id"] != settled["id"]):
            return
        if record is None or record.delivered_report is None:
            # Kept in memory only: after a restart there is nothing exact to
            # record, and a guess could store the wrong text. Leave the row.
            return
        status, summary, report_path, report_chars = record.delivered_report
        task_registry.complete_task(
            owner, session_id, status, summary=summary,
            report_path=report_path, report_chars=report_chars,
        )
        from app.core.turn_recorder import record_task_settle_frame

        await record_task_settle_frame(owner, session_id, status, summary, tool_use_id=session_id)
        if record and record.continuation_turn_id and supervisor.get_worker(owner, session_id) is record:
            supervisor.unregister_worker(owner, session_id)
        settled_now = True
    if settled_now:
        from app.core.worker_checkout import reclaim_untouched_worker_checkout
        await asyncio.to_thread(reclaim_untouched_worker_checkout, session_id)


async def _finish_displaced_continuations(session_id, settled):
    """Fail self-healed continuations whose parent cannot inherit this turn.

    The durable hand-off survives another park/self-heal. Report only the
    routing failure, never the live turn's text belonging to a different parent.
    """
    from app.core import provider_agent_supervisor as supervisor
    from app.core import turn_scheduler
    from app.core.task_registry import task_registry
    from app.core.turn_recorder import record_task_settle_frame
    from app.db import turns as turns_db

    for item in (settled.get("payload") or {}).get("agent_message_displaced", []):
        try:
            owner, completion_id = item["owner"], item["turn_id"]
            summary = "Worker continuation interrupted: the continuing turn reports to a different parent."
            async with turn_scheduler.message_resume_lock(session_id):
                if task_registry.is_woken(owner, session_id, completion_id=completion_id):
                    continue
                record = supervisor.get_worker(owner, session_id)
                if task_registry.is_user_stopped(session_id) or (record and record.torn_down):
                    continue  # Stop is never authority to wake a displaced parent either.
                if record and record.continuation_turn_id:
                    current = await asyncio.to_thread(turns_db.get_turn, record.continuation_turn_id)
                    if current and float(current["position"]) > item["position"]:
                        continue  # A newer admitted continuation owns the shared task row.
                task = task_registry.get_task(owner, session_id)
                if not task or task.get("status") != "running":
                    continue
                task_registry.complete_task(owner, session_id, "error", summary=summary)
                await record_task_settle_frame(owner, session_id, "error", summary, tool_use_id=session_id)
                if record and record.continuation_turn_id:
                    supervisor.unregister_worker(owner, session_id)
            if task_registry.is_user_stopped(session_id) or (record and record.torn_down):
                continue
            await turn_scheduler.on_task_terminal(owner, {
                "task_id": session_id, "tool_use_id": session_id,
                "status": "error", "summary": summary,
                "agent_message_turn_id": completion_id,
            })
        except Exception:
            logger.warning(
                "agent_messaging: displaced continuation of %s could not be closed: %s",
                session_id, item, exc_info=True,
            )


def _continuation_has_followup(session_id: str, settled: dict[str, Any]) -> bool:
    """A task-result wake continues this round, unlike a new user/peer turn.

    Probe before reading/persisting the report, as for remote children. Include
    the latest executed turn: a wake can finish during the probe, and the old
    callback must still not deliver its interim text after that final report.
    """
    from app.core import provider_agent
    from app.db import turns as turns_db

    if provider_agent._active_worker_background_work(session_id):
        return True
    candidates = [
        *turns_db.get_queued_turns(session_id),
        turns_db.get_live_turn(session_id),
        turns_db.get_latest_run_turn(session_id),
    ]
    for turn in candidates:
        while turn and turn.get("source") == "unpark":
            parent_id = turn.get("parent_turn_id")
            if parent_id == settled["id"]:
                return True
            turn = turns_db.get_turn(parent_id) if parent_id else None
    return False


async def finish_message_resume(session_id, settled, outcome):
    """Complete a resumed worker through the same parent wake gate as Task."""
    from app.core import provider_agent, turn_scheduler
    from app.core.task_registry import task_registry

    # Displaced parents are owed a terminal even if the kept continuation parks.
    await _finish_displaced_continuations(session_id, settled)
    if outcome == "parked":
        return
    owner = (settled.get("payload") or {}).get("agent_message_reply_to")
    # Delivery dedupe key: this turn, or for a stand-in turn the continuation
    # it reports, so one continuation never wakes its parent twice.
    completion_id = settled["id"]
    if not owner:
        # The turn that ran the continuation's last step need not be the one
        # SendMessage queued: a wakeup turn (idle watchdog, ScheduleWakeup)
        # that parked beside it is what the unpark wake resumes, and the
        # continuation's own row was settled without reporting (#123).
        found = await asyncio.to_thread(_inherited_continuation, session_id, settled)
        if not found:
            return
        owner, carrier, delivered = found
        if delivered:
            await _settle_delivered_continuation(session_id, owner, settled)
            return
        logger.info(
            "agent_messaging: turn %s of %s carries no reply_to; it reports the "
            "continuation %s settled unreported, for %s",
            settled.get("id"), session_id, carrier["id"], owner,
        )
        completion_id = carrier["id"]
    from app.core import provider_agent_supervisor as supervisor
    from app.db import turns as turns_db

    async with turn_scheduler.message_resume_lock(session_id):
        record = supervisor.get_worker(owner, session_id)
        if (not ((record and record.torn_down) or task_registry.is_user_stopped(session_id))
                and await asyncio.to_thread(_continuation_has_followup, session_id, settled)):
            return  # The result wake, not this interim note, owes the report.
        # Read only this turn's report: an interrupted continuation must never
        # report the previous successful turn as its new result.
        events = await asyncio.to_thread(provider_agent._load_session_events, session_id)
        status, report, summary = _turn_report(session_id, settled, outcome, events)
        from app.core.provider_agent_feed import record_continuation_report

        stopped = (record and record.torn_down) or task_registry.is_user_stopped(session_id)
        await record_continuation_report(
            owner, session_id, completion_id, "killed" if stopped else status, report,
        )
        if stopped:
            return  # Preserve Stop: a card update is not authority to wake anyone.
        # Same report file as a first completion (#104): the fixed path now
        # holds this round, and the previous round moves to a round file.
        report_path, report_chars = await asyncio.to_thread(
            _persist_continuation_report, owner, session_id, summary,
        )
        # Read park BEFORE busy: unpark starts queued/running before clearing
        # the parked row. The single busy query covers queued->running too.
        # Admission holds this lock until both queue and ownership are written.
        parked = await asyncio.to_thread(turn_scheduler._session_has_parked_turn, session_id)
        busy = await asyncio.to_thread(turns_db.session_has_unsettled_turns, session_id)
        if await asyncio.to_thread(turns_db.get_cwd_failure, session_id):
            # Held peers cannot run and must not keep the failed continuation's
            # task marked running forever. Never ignore a genuinely live turn.
            busy = bool(await asyncio.to_thread(turns_db.get_live_turn, session_id))
        latest = await asyncio.to_thread(turn_scheduler._latest_context_turn, session_id)
        if (record and record.torn_down) or task_registry.is_user_stopped(session_id):
            return
        settled_now = False
        if (not busy and not parked and (not latest or latest["id"] == settled["id"])
                and not task_registry.is_woken(owner, session_id, completion_id=completion_id)):
            settled_now = True
            task_registry.complete_task(
                owner, session_id, status, summary=summary,
                report_path=report_path, report_chars=report_chars,
            )
            # Only the last continuation may settle the shared task row. Emit
            # under the admission lock so a new resume cannot race this frame.
            from app.core.turn_recorder import record_task_settle_frame

            await record_task_settle_frame(
                owner, session_id, status, summary, tool_use_id=session_id,
            )
            if (record and record.continuation_turn_id
                    and supervisor.get_worker(owner, session_id) is record):
                supervisor.unregister_worker(owner, session_id)
        elif record is not None and record.continuation_turn_id:
            # The row stays running for the turn behind it; remember what the
            # parent was told so closing the row later records exactly this.
            record.delivered_report = (status, summary, report_path, report_chars)
    # Completion dedupe is per turn, independent of the shared worker's status.
    # Deliver outside the admission lock: a parent's wake may itself enqueue.
    await turn_scheduler.on_task_terminal(owner, {
        "task_id": session_id, "tool_use_id": session_id,
        "status": status, "summary": summary,
        "agent_message_turn_id": completion_id,
        **({"report_path": report_path} if report_path else {}),
    })
    if settled_now:
        # Same rule as a first run: an untouched dedicated checkout is reclaimed.
        from app.core.worker_checkout import reclaim_untouched_worker_checkout
        await asyncio.to_thread(reclaim_untouched_worker_checkout, session_id)
