"""Owner-scoped sidebar management; never starts execution.

SettleSession, UnsettleSession and ArchiveSession take one ``session_id`` (the
original receipt), a ``session_ids`` list, or for ArchiveSession
``all_settled=true``. Bulk forms check every chat with the same per-session
rules, process each independently and return a bounded summary.

Settling never stops anything. Archiving does: an archived chat is read-only
until the owner restores it (#1822), so its running and parked work is stopped
on the execution owner as the archive commits (``session_active``).
"""
import logging

from fluid.tools.builtins.session_orchestration import MAX_SESSIONS_PER_CALL

from app.core import session_orchestration as orchestration
from app.core.config import settings
from app.db import sessions

log = logging.getLogger(__name__)
# Only refusals and failures are echoed back, and at most this many of them.
MAX_LISTED_ISSUES = 50
MAX_REASON = 200
INACCESSIBLE = "Session is not an accessible top-level chat; use ListSessions."
ARCHIVED = "This session is archived. Restore it in the app before settling or unsettling it."
ASSISTANT_HOME = "Your personal assistant is a persistent conversation and cannot be settled or archived"
SELF_ARCHIVE = ("A chat cannot archive itself: archiving stops its work, including this turn. "
                "The owner can archive it from the app.")
DONE = {"settle": "settled", "unsettle": "unsettled", "archive": "archived"}


def visible_session(row):
    from app.network import engine, membership
    from app.network.runners import self_device_id
    if not row:
        return False
    owner = row.get("owner_device_id")
    local = (row.get("instance_id") == settings.effective_instance_id
             and owner in (None, self_device_id()))
    network = engine.context()
    member = network.member(owner) if network and owner else None
    remote = bool(member and member.status == membership.STATUS_ACTIVE)
    return bool((local or remote) and not row.get("hidden") and not row.get("is_worker")
                and not row.get("parent_session_id") and row.get("mode") != "voice")


def check(caller_id, caller_owner, target_id, row):
    """Every per-session access rule, shared by the single and bulk forms."""
    from app.core import mobile_tool_identity
    from app.core.assistant_service import is_primary_session
    # Let the shared transaction enforce the persistent assistant-home guard.
    visible = visible_session(row) or (row and target_id == caller_id and is_primary_session(target_id))
    if not visible or orchestration._owner(row) != caller_owner:
        raise ValueError(INACCESSIBLE)
    mobile_tool_identity.validate(orchestration._principal(target_id))
    return row


def authorize(caller_id, data):
    caller = orchestration._caller(caller_id, allow_settled=True)
    target_id = orchestration._text(data, "session_id", 200)
    return check(caller_id, orchestration._owner(caller), target_id, sessions.get_session(target_id))


def selector(data, action):
    """Validate the target selector: exactly one of session_id, session_ids, all_settled.

    Returns ``(key, value, duplicates_ignored)``.
    """
    keys = ("session_id", "session_ids") + (("all_settled",) if action == "archive" else ())
    allowed = set(keys) | ({"scope"} if action == "archive" else set())
    unknown = sorted(str(key) for key in set(data) - allowed)
    if unknown:
        raise ValueError(f"Unknown parameter(s): {', '.join(unknown)[:200]}. Use {' / '.join(keys)}.")
    given = [key for key in keys if data.get(key) is not None]
    if len(given) != 1:
        raise ValueError(f"Pass exactly one of {', '.join(keys)}.")
    key = given[0]
    if data.get("scope") is not None and key != "all_settled":
        raise ValueError("scope applies only with all_settled=true.")
    if key == "session_id":
        return key, None, 0
    if key == "all_settled":
        if data["all_settled"] is not True:
            raise ValueError("all_settled must be true when given.")
        scope = data.get("scope") or "project"
        if scope not in ("project", "all"):
            raise ValueError("scope must be project or all")
        return key, scope, 0
    raw = data["session_ids"]
    if not isinstance(raw, list) or not raw or len(raw) > MAX_SESSIONS_PER_CALL:
        raise ValueError(f"session_ids must be a list of 1 to {MAX_SESSIONS_PER_CALL} session IDs; "
                         "split larger groups into several calls.")
    ids = []
    for value in raw:
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError("Each session_ids entry must be a non-empty string of at most 200 characters.")
        ids.append(value.strip())
    unique = list(dict.fromkeys(ids))
    return key, unique, len(ids) - len(unique)


def _issue(session_id, outcome, reason):
    return {"session_id": session_id[:200], "outcome": outcome, "reason": str(reason)[:MAX_REASON]}


def _settled_matches(caller, caller_id, caller_owner, scope):
    """The settled chats ListSessions(state='settled', scope) would show, capped.

    The current chat is never selected. Returns ``(ids, rows, remaining)``.
    """
    project = caller.get("project_v2_id") or caller.get("project_id")
    matches, offset = {}, 0
    while len(matches) <= MAX_SESSIONS_PER_CALL:
        page = sessions.get_settled_sessions(caller.get("api_user_id") or "", MAX_SESSIONS_PER_CALL, offset)
        for row in page:
            if (row["id"] == caller_id or not visible_session(row)
                    or orchestration._owner(row) != caller_owner
                    or (scope == "project" and (row.get("project_v2_id") or row.get("project_id")) != project)):
                continue
            matches[row["id"]] = row
            if len(matches) > MAX_SESSIONS_PER_CALL:
                break
        if len(page) < MAX_SESSIONS_PER_CALL:
            break
        offset += MAX_SESSIONS_PER_CALL
    return list(matches)[:MAX_SESSIONS_PER_CALL], matches, len(matches) > MAX_SESSIONS_PER_CALL


def plan(caller_id, action, key, value):
    """Run every per-session check; nothing is written here."""
    from app.db.assistant import home_session_ids
    caller = orchestration._caller(caller_id, allow_settled=True)
    caller_owner = orchestration._owner(caller)
    remaining = False
    if key == "session_ids":
        ids, rows = value, sessions.get_sessions_by_ids(value)
    else:
        ids, rows, remaining = _settled_matches(caller, caller_id, caller_owner, value)
    homes = home_session_ids(ids) if action != "unsettle" else set()
    targets, issues, already = [], [], 0
    for session_id in ids:
        row = rows.get(session_id)
        try:
            check(caller_id, caller_owner, session_id, row)
        except (ValueError, PermissionError, LookupError) as exc:
            issues.append(_issue(session_id, "refused", exc))
            continue
        except Exception as exc:  # one broken item must not abort the rest
            log.exception("Session check failed for %s", session_id)
            issues.append(_issue(session_id, "failed", exc))
            continue
        archived = row.get("status") == "archived"
        if session_id in homes:
            issues.append(_issue(session_id, "refused", ASSISTANT_HOME))
        elif action == "archive" and session_id == caller_id and not archived:
            issues.append(_issue(session_id, "refused", SELF_ARCHIVE))
        elif (archived if action == "archive"
              else not archived and bool(row.get("is_active")) is (action == "unsettle")):
            already += 1
        elif archived:
            issues.append(_issue(session_id, "refused", ARCHIVED))
        else:
            targets.append(row)
    return {"ids": ids, "targets": targets, "issues": issues, "already": already, "remaining": remaining}


async def apply(action, targets):
    """Write planned changes; returns ``(changed, failure_issues)``.

    A worker that is not networked archives/settles with one UPDATE, and a
    networked settle/unsettle commits the shared records in one transaction.
    A networked archive goes chat by chat because it may need the execution
    owner. If a batch fails, every item is retried on its own.
    """
    from app.db.async_database import run_sync
    from app.network import engine, session_active
    active, archived = action == "unsettle", (True if action == "archive" else None)
    ids = [row["id"] for row in targets]
    if not ids:
        return 0, []
    networked = engine.context() is not None
    if not networked or archived is None:
        try:
            if networked:
                rows = await run_sync(session_active.write_shared_many, ids, active)
            else:
                rows = await run_sync(sessions.set_sessions_active, ids, active, archived)
        except Exception:
            log.warning("Batched %s failed; retrying each session", action, exc_info=True)
        else:
            for row in rows:
                await session_active._apply_settle_change(row, active, archived)
                await session_active._broadcast(row, row["id"])
                await session_active._apply_archive_change(row["id"], archived)
            done = {row["id"] for row in rows}
            return len(done), [_issue(sid, "failed", "The session changed or disappeared before it could be updated.")
                               for sid in ids if sid not in done]
    changed, failures = 0, []
    for row in targets:
        try:
            await session_active.update_membership(row, active, archived)
            changed += 1
        except Exception as exc:  # noqa: BLE001 - independent items: report and continue
            log.info("Could not %s session %s: %s", action, row["id"], exc)
            failures.append(_issue(row["id"], "failed", exc))
    return changed, failures


async def manage(caller_id, data, action):
    from app.db.async_database import run_sync
    key, value, duplicates = selector(data, action)
    if key == "session_id":
        return await manage_one(caller_id, data, action)
    planned = await run_sync(plan, caller_id, action, key, value)
    changed, failures = await apply(action, planned["targets"])
    issues = planned["issues"] + failures
    result = {"action": action, "selected_by": key,
              ("requested" if key == "session_ids" else "matched"): len(planned["ids"]),
              DONE[action]: changed, "already_in_state": planned["already"],
              "refused": sum(issue["outcome"] == "refused" for issue in issues),
              "failed": sum(issue["outcome"] == "failed" for issue in issues),
              "issues": issues[:MAX_LISTED_ISSUES]}
    if len(issues) > MAX_LISTED_ISSUES:
        result["issues_omitted"] = len(issues) - MAX_LISTED_ISSUES
    if duplicates:
        result["duplicates_ignored"] = duplicates
    note = (f"Sidebar state updated. {_execution_note(action)} No conversation was deleted. "
            "issues lists only refused and failed sessions.")
    if key == "all_settled":
        result.update(scope=value, remaining=planned["remaining"])
        if planned["remaining"]:
            note += " More settled chats match: repeat the same call to continue."
    result["note"] = note
    return result


def _execution_note(action):
    if action == "archive":
        return ("Archived chats are read-only until the owner restores them; any running or "
                "parked work in them was stopped.")
    if action == "settle":
        return ("Pending owner prompts were dismissed on the owning worker (on reconnect if offline). "
                "Other background work was not stopped.")
    return "Execution was not started or stopped."


async def manage_one(caller_id, data, action):
    from app.db.async_database import run_sync
    from app.network.session_active import update_membership
    row = await run_sync(authorize, caller_id, data)
    archived = row.get("status") == "archived"
    if action != "archive" and archived:
        raise ValueError(ARCHIVED)
    if action == "archive" and row["id"] == caller_id and not archived:
        from app.core.assistant_service import is_primary_session
        # The home's own refusal (ASSISTANT_HOME, from the write) says more.
        if not is_primary_session(caller_id):
            raise ValueError(SELF_ARCHIVE)
    updated = await update_membership(row, action == "unsettle", True if action == "archive" else None)
    return {"session_id": updated["id"], "is_active": bool(updated.get("is_active")),
            "status": updated.get("status"), "action": action,
            "note": f"Sidebar state updated. {_execution_note(action)} No conversation was deleted."}
