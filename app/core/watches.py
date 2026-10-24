"""Owner watch policy and conversational tools; all authorization is host-owned."""

from __future__ import annotations
from typing import Any

import math
import uuid
from datetime import timedelta

from app.db import database, watches as store
from app.db.async_database import run_sync
from app.db.base import get_db
from app.core import watch_sources as sources

READ_TOOLS = {"WebSearch", "WebFetch"}


def owner_session(session_id: str) -> dict[str, Any]:
    from app.core import assistant_service

    session = database.get_session(session_id)
    if (
        not session
        or session.get("api_user_id")
        or session.get("parent_session_id")
        or not assistant_service._owned_here(session)
    ):
        raise PermissionError("Watches require a local owner top-level session")
    return session


def prepare(
    session_id: str | None, data: dict[str, Any], *,
    _session: dict[str, Any] | None = None, _worker: str | None = None,
) -> dict[str, Any]:
    from app.core.assistant_service import assistant_id

    from app.core import watch_placement

    session = _session if _session is not None else owner_session(session_id)
    worker = _worker or watch_placement.resolve(data.get("worker_id"))
    description = str(data.get("description", "")).strip()
    if not description or len(description) > 200 or any(ord(c) < 32 for c in description):
        raise ValueError("description must be 1-200 printable characters")
    source = dict(data.get("source") or {})
    filter = dict(data.get("filter") or {})
    if worker == assistant_id():
        sources.validate(source, filter)
    budget = data.get("daily_budget_usd", 1.0)
    if (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or not math.isfinite(budget)
        or not 0.25 <= budget <= 100
    ):
        raise ValueError("daily_budget_usd must be between 0.25 and 100")
    action = data.get("action", "alert")
    if action not in ("alert", "investigate"):
        raise ValueError("action must be alert or investigate")
    permissions = dict(data.get("permissions") or {})
    if (
        set(permissions) - {"tools"}
        or not isinstance(permissions.get("tools", []), list)
        or not set(permissions.get("tools", [])) <= READ_TOOLS
    ):
        raise ValueError(
            "Background permissions are read-only: tools may only contain WebSearch and WebFetch"
        )
    permissions = dict(tools=sorted(set(permissions.get("tools", []))), read_only=True)
    ends_at = data.get("ends_at")
    if ends_at and store.at(ends_at) <= store.now():
        raise ValueError("ends_at must be in the future")
    row = dict(
        owner="admin",
        creating_session_id=session_id,
        worker_id=worker,
        agent_id=session["profile_id"],
        description=description,
        source=source,
        filter=filter,
        action=action,
        permissions=permissions,
        daily_budget_usd=float(budget),
        ends_at=ends_at,
    )
    if source["kind"] == "connector" and worker == assistant_id():
        sources.connector_allowed(row)
    return row


def confirmation(row: dict[str, Any], worker_name: str | None = None) -> str:
    scope = ", ".join(row["permissions"]["tools"]) or "event context only"
    until = f" until {row['ends_at']}" if row.get("ends_at") else ""
    source = row["source"]
    target = (
        f"{source['repo']} GitHub {source['resource']}"
        if source["kind"] == "github"
        else (
            f"connector {source['connector_id']}"
            if source["kind"] == "connector"
            else (
                f"session {source.get('session_id','states')} {source.get('state','')}"
                if source["kind"] == "session"
                else (
                    f"usage {source.get('metric','')}"
                    if source["kind"] == "usage"
                    else f"a schedule every {source['interval_seconds']} seconds"
                )
            )
        )
    )
    return (
        f"May I watch {row['description']} ({target}) on worker "
        f"{worker_name or row['worker_id']}{until},"
        f" {row['action']} when"
        f" it matches {store.encode(row['filter'])}, with read-only {scope} and a"
        f" ${row['daily_budget_usd']:g} daily cap?"
    )


async def create_from_conversation(session_id: str, data: dict[str, Any]) -> dict[str, Any]:
    # No model-supplied `confirmed` bit, bypass mode or saved allow rule can
    # authorize recurring work. Yes/no uses the existing durable owner card.
    from app.core.permission_handler import permission_handler
    from app.core.agent_types import PermissionResultAllow

    from app.core import watch_placement

    move_id = data.get("move_watch_id")
    if move_id:
        await run_sync(owner_session, session_id)
        existing = next((w for w in await watch_placement.rows() if w["id"] == move_id), None)
        if not existing or "source" not in existing:
            raise ValueError("Watch is not available for moving")
        if not data.get("worker_id"):
            raise ValueError("Moving a watch requires worker_id")
        data = dict(existing, worker_id=data["worker_id"], permissions={
            "tools": existing["permissions"]["tools"]
        })
    row = await run_sync(prepare, session_id, data)
    target = row["worker_id"]
    if not watch_placement.local(target):
        await watch_placement.call(target, "validate", row=row)
    worker_name = await run_sync(watch_placement.label, target)
    sentence = ("Move this existing watch? " if move_id else "") + confirmation(row, worker_name)

    async def broadcast(event: dict[str, Any]) -> None:
        pass  # permission_handler also broadcasts to the authenticated owner.

    decision = await permission_handler.request_permission(
        request_id="watch-" + str(uuid.uuid4()),
        session_id=session_id,
        profile_id=row["agent_id"],
        tool_name="CreateWatch",
        tool_input=data,
        broadcast_func=broadcast,
        description=sentence,
        skip_saved_rules=True,
        safety_check=True,
        assistant_confirm={"question": sentence, "action": sentence},
    )
    if not isinstance(decision, PermissionResultAllow):
        raise PermissionError("Watch not created: owner confirmation was not granted")
    # Grants and ownership may have changed while the owner was answering.
    row = await run_sync(prepare, session_id, dict(data, worker_id=target))
    if move_id:
        return {"watch": await watch_placement.move(move_id, target)}
    if not watch_placement.local(target):
        result = await watch_placement.call(
            target, "create", row=row, watch_id=str(uuid.uuid4())
        )
        return dict(result, confirmation="Watching " + row["description"] + ".")
    # Connector baselines are captured at approval, not on the first loop.
    cursor = {}
    if row["source"]["kind"] == "connector":
        _, cursor = await sources.poll(dict(row, cursor={}))
    wid = await run_sync(
        store.create, **row, cursor=cursor,
        creating_device_id=await run_sync(watch_placement.canonical, watch_placement.assistant_id()),
    )
    return dict(
        watch=await run_sync(store.get, wid), confirmation="Watching " + row["description"] + "."
    )


def metadata(watch: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    return dict(
        version=1,
        kind="watch",
        watch_id=watch["id"],
        event_id=event["id"],
        read_only=True,
        deadline_at=str(event["deadline_at"]),
        permissions=watch["permissions"],
    )


def assert_allowed(
    meta: dict[str, Any], session_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        not isinstance(meta, dict)
        or meta.get("kind") != "watch"
        or meta.get("read_only") is not True
    ):
        raise ValueError("Invalid watch provenance")
    if meta.get("delivery_device"):
        from app.core.watch_delivery import assert_allowed as remote_allowed

        return remote_allowed(meta, session_id)
    watch = store.get(meta.get("watch_id"))
    if not watch or watch["status"] != "active":
        raise ValueError("Watch is paused or stopped")
    from app.core.assistant_service import assistant_id

    if watch["owner"] != "admin" or watch["worker_id"] != assistant_id():
        raise ValueError("Watch owner or worker changed")
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM watch_events WHERE id=%s AND watch_id=%s",
            (meta.get("event_id"), watch["id"]),
        )
        event = store.parse(c.fetchone())
    if (
        not event
        or event["status"] not in ("admitted", "running")
        or meta != metadata(watch, event)
    ):
        raise ValueError("Watch event is no longer admitted")
    if session_id and event["session_id"] != session_id:
        raise ValueError("Watch session does not match")
    if store.at(event["deadline_at"]) <= store.now():
        raise ValueError("Watch deadline expired")
    if not database.get_profile(watch["agent_id"]):
        raise ValueError("Watch is missing its agent")
    if session_id:
        session = owner_session(session_id)
        from app.core.assistant_service import session_stopped

        if not session.get("is_active") or session_stopped(session_id):
            raise ValueError("Watch delivery session was stopped or settled")
    if watch["source"]["kind"] == "connector":
        sources.connector_allowed(watch)
    return watch, event


def tool_surface(meta: dict[str, Any]) -> list[str] | None:
    if isinstance(meta, dict) and meta.get("kind") == "watch":
        return sorted(READ_TOOLS.intersection((meta.get("permissions") or {}).get("tools", [])))
    return None


def panel_rows(owner: str = "admin") -> list[dict[str, Any]]:
    return [
        dict(
            row,
            kind="watch",
            what=row["description"],
            can_pause=row["status"] == "active",
            can_stop=row["status"] != "stopped",
        )
        for row in store.list_watches(owner)
        if row.get("reason") != "Transferred"
    ]


def tools(session_id: str) -> list[Any]:
    from app.mcp.inproc import tool
    from fluid.tools.inproc import InprocTool

    def wrap(value: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": store.encode(value)}]}

    @tool(
        "CreateWatch",
        "Create a durable watch, defaulting to an available always-on worker. Override worker_id."
        " To move an existing watch, provide move_watch_id; the host confirms placement."
        " It asks one"
        " plain confirmation sentence; the owner must answer yes, even in bypass mode. Never invent"
        " a confirmed flag. No inference while polling. Source kinds:"
        " connector {connector_id,channel_id?};"
        " github {repo,resource:issues|pulls|ci}; session"
        " {state:stuck|waiting_on_owner|finished,session_id?,stuck_seconds?}; usage"
        " {metric:utilization|credit_remaining,threshold,provider_id?,window?,credit_field?};"
        " schedule {interval_seconds,starts_at?}. Filter: contains/event_type/state/number."
        " Permissions tools are WebSearch/WebFetch only; omitted means event context only. Requests"
        " beyond scope require the owner in the resulting chat. Daily budgets include in-flight"
        " reservations.",
        {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "worker_id": {"type": "string"},
                "move_watch_id": {"type": "string"},
                "source": {"type": "object"},
                "filter": {"type": "object"},
                "action": {"type": "string", "enum": ["alert", "investigate"]},
                "permissions": {"type": "object"},
                "daily_budget_usd": {"type": "number"},
                "ends_at": {"type": "string"},
            },
            "required": ["description", "source"],
            "additionalProperties": False,
        },
    )
    async def create(args: Any) -> dict[str, Any]:
        try:
            return wrap(await create_from_conversation(session_id, args))
        except (ValueError, PermissionError) as exc:
            return dict(wrap(str(exc)), is_error=True)

    @tool(
        "ListWatches",
        "List the owner watches, including paused/stopped rows, costs and reasons.",
        {},
    )
    async def listing(args: Any) -> dict[str, Any]:
        await run_sync(owner_session, session_id)
        from app.core.watch_placement import rows

        return wrap(await rows())

    result = [create, listing]
    for name, status in [("PauseWatch", "paused"), ("StopWatch", "stopped")]:

        def build(name: str, status: str) -> InprocTool:
            @tool(
                name,
                "Pause or permanently stop an owner watch. Use the exact id from ListWatches.",
                {"watch_id": str},
            )
            async def change(args: dict[str, Any]) -> dict[str, Any]:
                await run_sync(owner_session, session_id)
                from app.core.watch_placement import change

                return wrap(await change(args["watch_id"], status))

            return change

        result.append(build(name, status))
    return result
