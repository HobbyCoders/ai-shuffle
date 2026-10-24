"""Plain-code watch source adapters. No inference occurs during polling."""

from __future__ import annotations
from typing import Any

import asyncio
import hashlib
import json
import math
import re
from datetime import timedelta
from urllib.parse import urlencode

from app.db import database, watches as store
from app.db.async_database import run_sync
from app.core import watch_values as values
from app.db.base import get_db

KINDS = ("connector", "github", "session", "usage", "schedule")


def validate(source: dict[str, Any], filter: dict[str, Any]) -> dict[str, Any]:
    kind = source.get("kind")
    if kind not in KINDS:
        raise ValueError("Unsupported watch source")
    if set(filter) - {"contains", "event_type", "state", "number"}:
        raise ValueError("Unsupported filter; use contains, event_type, state or number")
    if kind == "connector":
        connector = database.get_connector(source.get("connector_id"))
        if not connector:
            raise ValueError("Connector does not exist")
        from app.core.connectors.registry import get_connector_class

        cls = get_connector_class(connector["type"])
        if not cls.TRIGGERS:
            raise ValueError("Unsupported: this connector exposes no events")
        if connector["type"] not in ("gmail", "slack", "discord", "telegram"):
            raise ValueError("Unsupported: this connector exposes no replayable watch source")
        if connector["type"] != "gmail" and not str(source.get("channel_id", "")).strip():
            raise ValueError("Connector watches require an explicit channel_id")
    elif kind == "github":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source.get("repo", "")):
            raise ValueError("GitHub repo must be owner/repo")
        if source.get("resource") not in ("issues", "pulls", "ci"):
            raise ValueError("GitHub resource must be issues, pulls or ci")
    elif kind == "session":
        if source.get("state") not in ("stuck", "waiting_on_owner", "finished"):
            raise ValueError("Session state must be stuck, waiting_on_owner or finished")
        seconds = source.get("stuck_seconds", 900)
        if not isinstance(seconds, int) or not 60 <= seconds <= 86400:
            raise ValueError("stuck_seconds must be 60-86400")
    elif kind == "usage":
        if source.get("metric") not in ("utilization", "credit_remaining"):
            raise ValueError("Usage metric must be utilization or credit_remaining")
        value = source.get("threshold")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("Threshold must be a finite positive number")
        if source.get("metric") == "credit_remaining" and not source.get("provider_id"):
            raise ValueError("Credit threshold requires provider_id")
    elif kind == "schedule":
        interval = source.get("interval_seconds")
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or not 60 <= interval <= 31536000
        ):
            raise ValueError("Schedule interval_seconds must be 60-31536000")
        if source.get("starts_at"):
            values.at(source["starts_at"])
    return source


def matches(filter: dict[str, Any], payload: dict[str, Any]) -> bool:
    for key in ("event_type", "state", "number"):
        if key in filter and payload.get(key) != filter[key]:
            return False
    return str(filter.get("contains", "")).casefold() in values.encode(payload).casefold()


def connector_allowed(
    watch: dict[str, Any], *, allow_stopped: bool = False,
) -> dict[str, Any]:
    from app.core.assistant_service import assistant_id
    from app.db.assistant import connector_grant_available

    source = watch["source"]
    connector = database.get_connector(source["connector_id"])
    with get_db() as conn:
        allowed = (
            connector
            and (connector.get("is_active") or allow_stopped)
            and connector.get("runner_id") == watch["worker_id"]
            and watch["worker_id"] == assistant_id()
            and connector_grant_available(conn, connector, watch["agent_id"])
        )
    if not allowed:
        raise PermissionError("Connector grant was revoked or is not owned on this worker")
    return connector


def gmail_poll(
    instance: Any, cursor: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Gmail history is a replayable cursor, unlike an unread snapshot.

    Expired history (HTTP 404) fails/pause rather than silently rebasing and
    losing mail. One API page per pass; the page token is durable too.
    """
    sdk = instance.build_thread_client()
    if sdk is None:
        raise ValueError("Gmail is not connected")
    users = sdk.users()
    if "legacy_seen_ids" in cursor:
        history_id = cursor.get("history_id") or str(
            users.getProfile(userId="me").execute()["historyId"]
        )
        args = dict(
            userId="me",
            q=(
                ("after:" + str(cursor["legacy_after"]))
                if cursor.get("legacy_after")
                else "is:unread"
            ),
            maxResults=100,
            labelIds=[
                s.strip()
                for s in str(instance.config.get("labels", "INBOX")).split(",")
                if s.strip()
            ],
        )
        if cursor.get("legacy_page_token"):
            args["pageToken"] = cursor["legacy_page_token"]
        page = users.messages().list(**args).execute()
        events = []
        for item in page.get("messages", []):
            mid = str(item["id"])
            if mid in cursor["legacy_seen_ids"]:
                continue
            message = (
                users.messages()
                .get(userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject"])
                .execute()
            )
            headers = {
                h["name"].lower(): h.get("value", "")
                for h in message.get("payload", {}).get("headers", [])
            }
            events.append(
                (
                    "email_received:" + mid,
                    dict(
                        event_type="email_received",
                        message_id=mid,
                        sender=headers.get("from", ""),
                        subject=headers.get("subject", ""),
                        snippet=str(message.get("snippet", ""))[:4000],
                    ),
                )
            )
        checkpoint = {"history_id": history_id}
        if page.get("nextPageToken"):
            checkpoint.update(
                legacy_seen_ids=cursor["legacy_seen_ids"], legacy_page_token=page["nextPageToken"]
            )
            if cursor.get("legacy_after"):
                checkpoint["legacy_after"] = cursor["legacy_after"]
        return events, checkpoint
    if not cursor.get("history_id"):
        profile = users.getProfile(userId="me").execute()
        return [], {"history_id": str(profile["historyId"])}
    args = dict(
        userId="me",
        startHistoryId=cursor["history_id"],
        historyTypes=["messageAdded"],
        maxResults=100,
    )
    if cursor.get("page_token"):
        args["pageToken"] = cursor["page_token"]
    page = users.history().list(**args).execute()
    labels = {
        s.strip() for s in str(instance.config.get("labels", "INBOX")).split(",") if s.strip()
    }
    messages = {}
    for history in page.get("history", []):
        for added in history.get("messagesAdded", []):
            msg = added["message"]
            if not labels or labels.intersection(msg.get("labelIds", [])):
                messages[str(msg["id"])] = msg
    events = []
    for mid in messages:
        msg = (
            users.messages()
            .get(userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject"])
            .execute()
        )
        headers = {
            h["name"].lower(): h.get("value", "") for h in msg.get("payload", {}).get("headers", [])
        }
        events.append(
            (
                "email_received:" + mid,
                dict(
                    event_type="email_received",
                    message_id=mid,
                    subject=headers.get("subject", ""),
                    sender=headers.get("from", ""),
                    snippet=str(msg.get("snippet", ""))[:4000],
                ),
            )
        )
    next_cursor = {"history_id": str(page.get("historyId", cursor["history_id"]))}
    if page.get("nextPageToken"):
        next_cursor = {"history_id": cursor["history_id"], "page_token": page["nextPageToken"]}
    return events, next_cursor


async def gh_api(endpoint: str) -> Any:
    # No shell, no model, no source-supplied executable/flags, and no credentials in logs.
    from fluid.tools.child_env import scrub_child_env
    from app.core.platform import get_gh_executable

    executable = get_gh_executable()
    if not executable:
        raise ValueError(
            "GitHub watches require the GitHub CLI; install or reconnect GitHub on this worker"
        )
    process = await asyncio.create_subprocess_exec(
        executable,
        "api",
        endpoint,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=scrub_child_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError("GitHub API request failed; check gh authentication and repository access")
    return json.loads(stdout)


async def github_poll(
    watch: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    source, cursor = watch["source"], watch["cursor"]
    resource = source["resource"]
    # Issues endpoint includes PRs and supports updated-since. Filter the kind
    # locally. CI uses created>= plus overlapping updated snapshots.
    endpoint = f"repos/{source['repo']}/" + ("actions/runs" if resource == "ci" else "issues")
    since = values.at(cursor.get("since", watch["created_at"])).isoformat()
    page = int(cursor.get("page", 1))
    query = {"per_page": 100, "page": page}
    if resource != "ci":
        query.update(state="all", sort="updated", direction="asc", since=str(since))
    else:
        query["created"] = ">=" + str(since)
    response = await gh_api(endpoint + "?" + urlencode(query))
    rows = response.get("workflow_runs", []) if resource == "ci" else response
    events = []
    for row in rows:
        if resource == "pulls" and not row.get("pull_request"):
            continue
        if resource == "issues" and row.get("pull_request"):
            continue
        stamp = row.get("updated_at", row.get("created_at", ""))
        key = f"{row['id']}:{stamp}:{row.get('status','')}:{row.get('conclusion','')}"
        events.append(
            (
                key,
                dict(
                    event_type="github_" + resource,
                    id=row["id"],
                    number=row.get("number"),
                    title=row.get("title", row.get("name", "")),
                    state=row.get("state", row.get("status")),
                    conclusion=row.get("conclusion"),
                    url=row.get("html_url"),
                    updated_at=stamp,
                ),
            )
        )
    # Keep the scan lower bound stable through all pages. Never jump to now:
    # CI can change an older run; retain the creation bound until it is terminal.
    if len(rows) == 100:
        return events, {
            "since": str(since),
            "page": page + 1,
            "high": cursor.get("high", str(values.now())),
        }
    if resource == "ci":
        return events, {"since": str(since)}
    high = cursor.get("high", str(values.now()))
    return events, {"since": (values.at(high) - timedelta(seconds=60)).isoformat()}


def own_delivery(turn: dict | None, watch_id: str) -> bool:
    if not turn or turn.get("source") != "watch":
        return False
    payload = turn.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    meta = payload.get("_assistant_proactive")
    return isinstance(meta, dict) and meta.get("watch_id") == watch_id


def session_poll(watch: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    source = watch["source"]
    cursor = dict(watch["cursor"])
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM sessions WHERE api_user_id IS NULL AND parent_session_id IS NULL AND"
            " instance_id=%s",
            (watch["worker_id"],),
        )
        rows = [dict(r) for r in c.fetchall()]
        events = {}
        for session in rows:
            sid, state = session["id"], source["state"]
            if source.get("session_id") and sid != source["session_id"]:
                continue

            def add(identity: str | None) -> None:
                key = f"{sid}:{identity}:{state}"
                events[key] = dict(
                    event_type="session_state",
                    state=state,
                    session_id=sid,
                    title=session.get("title"),
                )

            # Replay durable transitions, even if the chat has already moved
            # on to another turn while this worker was down.
            c.execute(
                "SELECT * FROM session_events WHERE session_id=%s AND seq>%s AND created_at>=%s AND"
                " event_type IN ('turn.completed','turn.aborted','interaction.requested') ORDER BY"
                " seq LIMIT 100",
                (sid, int(cursor.get(sid, 0)), watch["created_at"]),
            )
            for event in c.fetchall():
                cursor[sid] = event["seq"]
                payload = event["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if state == "finished" and event["event_type"] in (
                    "turn.completed",
                    "turn.aborted",
                ):
                    c.execute(
                        "SELECT source,payload FROM turns WHERE id=%s AND session_id=%s",
                        (event["turn_id"], sid),
                    )
                    completed = c.fetchone()
                    if not own_delivery(dict(completed) if completed else None, watch["id"]):
                        add(event["turn_id"])
                elif state == "waiting_on_owner" and event["event_type"] == "interaction.requested":
                    add(payload.get("interaction_id", event["id"]))
            c.execute(
                "SELECT * FROM turns WHERE session_id=%s ORDER BY position DESC LIMIT 1", (sid,)
            )
            turn = c.fetchone()
            if not turn:
                continue
            c.execute(
                "SELECT id FROM pending_interactions WHERE session_id=%s AND state='pending'",
                (sid,),
            )
            waiting = c.fetchall()
            if state == "waiting_on_owner":
                for interaction in waiting:
                    add(interaction["id"])
            elif (
                state == "finished"
                and turn["state"] in ("completed", "aborted")
                and not own_delivery(dict(turn), watch["id"])
            ):
                add(turn["id"])
            elif (
                state == "stuck"
                and turn["state"] in ("running", "draining", "interrupting")
                and values.at(session["updated_at"])
                < values.now() - timedelta(seconds=source.get("stuck_seconds", 900))
            ):
                add(turn["id"])
    return list(events.items()), cursor


async def usage_poll(
    watch: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    from app.api.usage import get_cached_usage_payload

    snapshot = await get_cached_usage_payload()
    source = watch["source"]
    rows = [snapshot]
    if source.get("provider_id"):
        rows = [
            r
            for r in snapshot.get("providers") or []
            if r["provider_id"] == source["provider_id"] and r.get("status") == "fresh"
        ]
    values = []
    for row in rows:
        if source["metric"] == "utilization":
            values.extend(
                float(w["utilization"])
                for w in row.get("windows", [])
                if not source.get("window") or w["type"] == source["window"]
            )
        else:
            credits = row.get("credits") or {}
            value = credits.get(source.get("credit_field", "balance_remaining"))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(value)
    if not values:
        return [], watch["cursor"]  # Unknown/stale is not a threshold crossing.
    value = max(values) if source["metric"] == "utilization" else min(values)
    hit = (
        value >= source["threshold"]
        if source["metric"] == "utilization"
        else value <= source["threshold"]
    )
    cursor = dict(watch["cursor"])
    generation = int(cursor.get("generation", 0))
    events = []
    if hit and not cursor.get("hit"):
        generation += 1
        events = [
            (
                str(generation),
                dict(
                    event_type="usage_threshold",
                    metric=source["metric"],
                    value=value,
                    threshold=source["threshold"],
                ),
            )
        ]
    return events, dict(hit=hit, generation=generation)


async def poll(watch: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    source, cursor = watch["source"], watch["cursor"]
    kind = source["kind"]
    if kind == "connector":
        connector = await run_sync(connector_allowed, watch)
        from app.core.connectors.manager import connector_manager

        instance = await connector_manager.get_running_connector(connector["id"])
        if instance is None:
            raise ValueError("Connector is offline")
        if connector["type"] == "gmail":
            return await run_sync(gmail_poll, instance, cursor)
        from app.core.watch_connector_sources import poll as connector_poll

        return await connector_poll(connector["type"], instance, source, cursor)
    if kind == "github":
        return await github_poll(watch)
    if kind == "session":
        return await run_sync(session_poll, watch)
    if kind == "usage":
        return await usage_poll(watch)
    due = values.at(cursor.get("next_at") or source.get("starts_at") or watch["created_at"])
    events = []
    for _ in range(100):
        if due > values.now():
            break
        events.append((due.isoformat(), dict(event_type="schedule", scheduled_at=due.isoformat())))
        due += timedelta(seconds=source["interval_seconds"])
    return events, dict(next_at=due.isoformat())


def record_connector(connector_id: str, event_type: str, payload: dict[str, Any]) -> None:
    from app.core.assistant_service import assistant_id

    connector = database.get_connector(connector_id)
    if not connector:
        return
    # Gmail connector startup emits old unread messages, so callbacks only wake
    # the replayable history poll. Never admit those pre-opt-in messages.
    if connector["type"] in ("gmail", "slack", "discord"):
        return
    from app.core.connectors.registry import get_connector_class

    if event_type not in {t.id for t in get_connector_class(connector["type"]).TRIGGERS}:
        return
    external_id = payload.get("message_id") or payload.get("event_id") or payload.get("id")
    key = (
        event_type
        + ":"
        + str(external_id or hashlib.sha256(values.encode(payload).encode()).hexdigest())
    )
    if connector["type"] == "telegram":
        store.record_connector_event(connector_id, key, dict(payload, event_type=event_type))
        return
    event = dict(payload, event_type=event_type)
    # Limit untrusted input without breaking a JSON envelope.
    if len(values.encode(event)) > 16000:
        event = dict(event_type=event_type, data=values.encode(payload)[:12000], truncated=True)
    for watch in store.list_watches(worker_id=assistant_id()):
        if watch["status"] != "active" or watch["source"] != dict(
            kind="connector", connector_id=connector_id
        ):
            continue
        connector_allowed(watch)
        if matches(watch["filter"], event):
            store.append_events(watch, [(key, event)])
