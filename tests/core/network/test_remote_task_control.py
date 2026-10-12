"""task-start and task-report over the real signed control endpoint between two workers."""
import asyncio

import httpx
import pytest

from app.core import provider_agent, remote_task, turn_scheduler
from app.db import database, remote_tasks, session_events, turns
from app.db import network as store
from app.db.base import get_db
from app.network import control, engine, transport
from tests.core.network.harness import converge, join
from tests.core.network.test_transport import ServedBy

UNRAID_URL = "http://unraid.local:8000"
WINDOWS_URL = "http://windows.local:8000"


class Router:
    """Answers as ServerWorker or Windows by base URL; a worker can be taken down."""

    def __init__(self, a, b):
        self.served = {UNRAID_URL: ServedBy(a, UNRAID_URL), WINDOWS_URL: ServedBy(b, WINDOWS_URL)}
        self.down = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        for base, served in self.served.items():
            if str(request.url).startswith(base):
                if base in self.down:
                    raise httpx.ConnectError("connection refused", request=request)
                return served.handler(request)
        return httpx.Response(502, json={"detail": "unreachable"})

    def requests(self, base):
        return self.served[base].requests


@pytest.fixture
def kicks(monkeypatch):
    seen = []
    monkeypatch.setattr(turn_scheduler, "kick", seen.append)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    return seen


def network(workers, monkeypatch):
    """A ("ServerWorker") runs the child; B ("Windows") runs the parent chat."""
    a, b = workers("unraid"), workers("windows")
    router = Router(a, b)
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(router.handler)
        return real_async(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(control, "CONNECT_RETRY_DELAY", 0)
    with a.active():
        engine.create_network("Home", device_name="ServerWorker")
    join(a, b, name="Windows")
    with b.active():
        database.create_session(session_id="caller", profile_id="default", project_id=None,
                                title="Windows chat", is_active=True, permission_mode="acceptEdits")
        engine.scan_all()
    converge([a, b])
    with b.active():
        store.touch_member(a.device_id, endpoints=[UNRAID_URL])
    with a.active():
        store.touch_member(b.device_id, endpoints=[WINDOWS_URL])
    # Dial only these test addresses (each worker also announces this
    # machine's real LAN address on every token exchange), and rebuild the
    # process-wide link cache.
    from app.network import endpoints
    urls = {a.device_id: UNRAID_URL, b.device_id: WINDOWS_URL}
    monkeypatch.setattr(endpoints, "peer_endpoints",
                        lambda member, presence: [urls[member["device_id"]]])
    transport._links.pop(a.device_id, None)
    transport._links.pop(b.device_id, None)
    transport.note_contact(a.device_id)
    transport.note_contact(b.device_id)
    return a, b, router


def dispatch(a, b, **changes):
    fields = dict(
        worker={"device_id": a.device_id, "device_name": "ServerWorker"},
        caller_row=database.get_session("caller"), caller_session_id="caller", root_session_id="caller",
        depth=1, profile_id="default", agent_name="Default", result_agent="default", provider_id=None,
        model_id="sonnet", effort=None, prompt="Check the disk.", label="Task · Default",
        roster_name=None, mode="background", launch_tool_use_id="toolu_remote",
    )
    fields.update(changes)
    return asyncio.run(remote_task.dispatch_remote(**fields))


def finish_child(child, text="Disk is 40% full.", outcome="completed", deferred=None):
    """Run the child's first turn to an end on its worker (no provider)."""
    [turn] = [t for t in all_turns(child) if t["source"] == "provider_agent"]
    assert turns.start_turn(turn["id"])
    session_events.append_events(child, [{"type": "message.user", "turn_id": turn["id"],
                                          "payload": {"content": "Check the disk."}}])
    if deferred:
        from app.core import away_mode
        assert asyncio.run(away_mode.defer_if_away(child, deferred)) == away_mode.DEFERRED_MESSAGE
    if outcome == "completed":
        session_events.append_events(child, [{"type": "assistant.text_delta", "turn_id": turn["id"],
                                              "payload": {"text": text}}])
    else:
        session_events.append_events(child, [{"type": "turn.aborted", "turn_id": turn["id"],
                                              "payload": {"message": text}}])
    assert turns.settle_turn(turn["id"], outcome)
    return turn


def all_turns(session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        # The shim timestamps have second resolution. Equal timestamps must
        # follow FIFO position, not whichever index the planner happens to use.
        cursor.execute("SELECT * FROM turns WHERE session_id=%s ORDER BY created_at, position", (session_id,))
        return [turns._row(r) for r in cursor.fetchall()]


def wakes():
    return [t for t in all_turns("caller") if t["source"] == "unpark"]


def started_child(a, b):
    with b.active():
        result = dispatch(a, b)
    assert result["ok"] is True, result
    return result["worker_session_id"]


# --- task-start ---------------------------------------------------------------

def test_parent_starts_a_child_on_the_other_worker(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    with b.active():
        result = dispatch(a, b)
    assert result["ok"] is True, result
    assert result["worker"] == {"device_id": a.device_id, "name": "ServerWorker"}
    assert result["checkout"] == {"mode": "none", "branch": None}
    assert result["permission_mode"] == "acceptEdits"
    assert "you will be woken with its report" in result["note"]
    assert ("POST", "/api/v1/network/peer/control/task-start") in router.requests(UNRAID_URL)
    child = result["worker_session_id"]
    with a.active():
        row = database.get_session(child)
        assert row["owner_device_id"] == a.device_id and row["is_worker"] and row["hidden"]
        assert row["parent_session_id"] == "caller" and row["away_mode"]
        owed = remote_task.report_owed(child)
        assert owed["device_id"] == b.device_id and owed["owner_session_id"] == "caller"
        assert remote_tasks.get_child(child)["state"] == remote_tasks.RUNNING
    with b.active():
        record = remote_tasks.get_dispatch(child)
        assert record["target_device_id"] == a.device_id and record["owner_session_id"] == "caller"
        assert record["tool_use_id"] == "toolu_remote" and record["completion_id"] is None
        assert all_turns(child) == []
    assert kicks == [child]


def test_a_target_refusal_comes_back_by_name(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    with b.active():
        result = dispatch(a, b, profile_id="nobody")
        with get_db() as conn:
            cursor = conn.cursor()
            remote_tasks.init_schema(conn)
            cursor.execute("SELECT COUNT(*) AS n FROM remote_tasks")
            assert cursor.fetchone()["n"] == 0  # never started, so nothing is tracked
    assert result == {"ok": False, "error": (
        "ServerWorker refused the subagent: The agent 'nobody' does not exist on this worker; nothing was "
        "started; nothing was dispatched.")}
    assert kicks == []


def test_an_older_target_is_refused_as_older(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    monkeypatch.setattr(control, "known_action", lambda action: action != remote_task.TASK_START_ACTION)
    with b.active():
        result = dispatch(a, b)
    assert result["ok"] is False and "it runs an older release" in result["error"]
    with a.active():
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM turns WHERE source='provider_agent'")
            assert cursor.fetchone()["n"] == 0


# --- task-report ----------------------------------------------------------------

def test_the_report_comes_back_and_wakes_the_caller_once(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        turn = finish_child(child, deferred="permission for Bash")
        asyncio.run(remote_task.on_child_settled(child))
        asyncio.run(remote_task.deliver_due())  # the hook's own send may still be in flight
        assert remote_tasks.get_child(child)["state"] == remote_tasks.DELIVERED
    assert ("POST", "/api/v1/network/peer/control/task-report") in router.requests(WINDOWS_URL)
    with b.active():
        [wake] = wakes()
        prompt = wake["prompt"]
        assert "<task-notification>" in prompt and f"<task-id>{child}</task-id>" in prompt
        assert "<status>completed</status>" in prompt and "<tool-use-id>toolu_remote</tool-use-id>" in prompt
        assert "Ran on ServerWorker. Disk is 40% full." in prompt
        assert "held back for the owner" in prompt and "permission for Bash" in prompt
        record = remote_tasks.get_dispatch(child)
        assert record["completion_id"] == turn["id"] and record["status"] == "completed"
        # Stored where local background reports go, and named in the wake.
        from pathlib import Path
        assert Path(record["report_path"]).read_text(encoding="utf-8") == "Disk is 40% full."
        assert f"[The same report is at {record['report_path']}.]" in prompt
        # A retried delivery (the target never saw the answer) wakes nobody again.
        again = asyncio.run(control.handle(remote_task.TASK_REPORT_ACTION, remote_tasks_report(a, child),
                                           from_device=a.device_id))
        assert again == {"delivered": True, "duplicate": True}
        assert len(wakes()) == 1


def remote_tasks_report(a, child):
    with a.active():
        return remote_tasks.get_child(child)["report"]


def test_an_unreachable_parent_gets_the_report_when_it_is_back(workers, monkeypatch, kicks):
    a, b, router = network(workers, monkeypatch)
    child = started_child(a, b)
    router.down.add(WINDOWS_URL)
    with a.active():
        finish_child(child, text="Done while you were away.")
        assert asyncio.run(remote_task.sweep()) == 0
        row = remote_tasks.get_child(child)
        assert row["state"] == remote_tasks.OWED and row["attempts"] == 1
        assert row["next_attempt_at"] > row["updated_at"] or row["next_attempt_at"]
        assert "Couldn't reach" in row["last_error"]
        # Not due yet: the next sweep does not send.
        assert asyncio.run(remote_task.sweep()) == 0
        assert remote_tasks.get_child(child)["attempts"] == 1
    with b.active():
        assert wakes() == []
    router.down.clear()
    with a.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE remote_task_children SET next_attempt_at=%s",
                                  ("2000-01-01T00:00:00+00:00",))
        assert asyncio.run(remote_task.sweep()) == 1
        assert remote_tasks.get_child(child)["state"] == remote_tasks.DELIVERED
    with b.active():
        [wake] = wakes()
        assert "Done while you were away." in wake["prompt"]


def test_a_target_restart_still_delivers_the_report(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        finish_child(child, text="Restarted mid-delivery.")
        # The process restarted before the settle hook ran: nothing in memory.
        provider_agent.reset_worker_sessions()
        asyncio.run(remote_task.on_child_settled(child))  # skipped: not known to this process
        assert remote_tasks.get_child(child)["state"] == remote_tasks.RUNNING
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        [wake] = wakes()
        assert "Restarted mid-delivery." in wake["prompt"]


def test_an_interrupted_child_reports_a_failure(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        finish_child(child, text="The worker restarted before the turn finished.", outcome="aborted")
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        [wake] = wakes()
        assert "<status>error</status>" in wake["prompt"]
        assert "The worker restarted before the turn finished." in wake["prompt"]
        assert remote_tasks.get_dispatch(child)["status"] == "failed"


def test_a_long_report_wakes_with_its_head_and_the_file(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    long_text = "Findings. " + "x" * 2990
    with a.active():
        finish_child(child, text=long_text)
        assert asyncio.run(remote_task.sweep()) == 1
    with b.active():
        [wake] = wakes()
        record = remote_tasks.get_dispatch(child)
        assert f"first 2000 of {len(long_text)} characters" in wake["prompt"]
        assert record["report_path"] in wake["prompt"] and long_text not in wake["prompt"]


def test_a_running_child_owes_nothing_yet(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with a.active():
        assert asyncio.run(remote_task.sweep()) == 0
        assert remote_tasks.get_child(child)["state"] == remote_tasks.RUNNING


def test_reports_are_taken_only_from_the_worker_the_task_went_to(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    body = {"version": 1, "worker_session_id": child, "completion_id": "t1", "status": "completed",
            "report": "done", "truncated": False, "needs_owner": []}
    with b.active():
        with pytest.raises(PermissionError, match="Only active workers"):
            remote_task.accept_task_report(body, "dev-stranger")
        with pytest.raises(PermissionError, match="No subagent with this id"):
            remote_task.accept_task_report({**body, "worker_session_id": "someone-else"}, a.device_id)
        remote_tasks.record_dispatch(worker_session_id="sent-elsewhere", target_device_id="dev-laptop",
                                     owner_session_id="caller", root_session_id="caller", tool_use_id=None,
                                     mode="background", label="x", agent="default")
        with pytest.raises(PermissionError, match="No subagent with this id"):
            remote_task.accept_task_report({**body, "worker_session_id": "sent-elsewhere"}, a.device_id)
        with pytest.raises(ValueError, match="status"):
            remote_task.accept_task_report({**body, "status": "approved"}, a.device_id)
        assert wakes() == []


def test_a_refused_report_is_not_retried(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    child = started_child(a, b)
    with b.active():
        with get_db() as conn:
            conn.cursor().execute("DELETE FROM remote_tasks")
    with a.active():
        finish_child(child)
        assert asyncio.run(remote_task.sweep()) == 0
        row = remote_tasks.get_child(child)
        assert row["state"] == remote_tasks.REFUSED and "No subagent with this id" in row["last_error"]
        assert remote_tasks.due_reports() == []
