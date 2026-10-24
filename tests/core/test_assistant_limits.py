"""The owner's spending and job time limits for coordination chats and their jobs."""
import ast
import pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.core import assistant_limits as limits, assistant_service as service, session_orchestration as orch
from app.db import assistant as store, assistant_limits as limits_db, database, turns
from app.db.base import get_db
from tests.db.test_assistant_store import assistant_fixture

ROOT = pathlib.Path(__file__).resolve().parents[2]


def book(session_id, cost, at=None):
    """A settled turn's usage_log row, as turn settlement writes it."""
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO usage_log (session_id, model, tokens_in, tokens_out, cost_usd, duration_ms, created_at) "
            "VALUES (%s,'m',1,1,%s,1,%s)", (session_id, cost, at or datetime.now(timezone.utc)))


def job(parent, title="Background job"):
    """A Task worker a coordination chat (or one of its jobs) started."""
    sid = "job-" + title.lower().replace(" ", "-")
    database.create_session(sid, "assistant-agent", title=title, parent_session_id=parent, is_worker=True,
                            hidden=True, target_instance_id=service.assistant_id())
    return sid


def set_limits(**changes):
    return limits_db.update(service.assistant_id(), changes)


@pytest.fixture
def stops(monkeypatch):
    from app.core import turn_manager
    stop = AsyncMock(side_effect=lambda session_id, mode, turn_id=None, source=None: {"id": turn_id, "state": "interrupting"})
    monkeypatch.setattr(turn_manager, "interrupt_async", stop)
    return stop


def live_turn(session_id, source="agent_message"):
    turn = turns.enqueue_turn(session_id, source, prompt="work", profile_id="assistant-agent")
    return turns.start_turn(turn["id"])


def answer_job_question(session_id, human_input, launch=None):
    """Use the durable question producer, then resume the same parked job."""
    from app.core.session_runtime import SessionRuntime
    from app.db import interactions
    first = turns.enqueue_turn(session_id, "agent_message", prompt="work",
                               profile_id="assistant-agent", payload=launch or {})
    first = turns.start_turn(first["id"])
    assert turns.park_turn(first["id"], ["monitor"])
    question = interactions.create_interaction(session_id, "question", {"request_id": "continue"}, None)
    assert interactions.answer_question_and_enqueue(
        question, {"Continue?": "Yes"}, None, "Continue with Yes", first, human_input=human_input,
    )
    [answer] = turns.get_queued_turns(session_id)
    resumed = turns.start_turn(answer["id"], can_resume=SessionRuntime._can_resume_parked)
    assert resumed["id"] == first["id"]
    return resumed


@pytest.mark.parametrize("human_input", [False, True])
@pytest.mark.parametrize("limit_kind", ["daily_limit", "job_time"])
def test_question_answer_job_limits(assistant_fixture, human_input, limit_kind):
    worker = job(assistant_fixture["id"], "Answering job")
    set_limits(spend_limit_enabled=limit_kind == "daily_limit", daily_spend_limit_usd=1,
               job_limit_enabled=limit_kind == "job_time", max_job_minutes=30)
    book(worker, 2)
    resumed = answer_job_question(worker, human_input)
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET work_ms=%s WHERE id=%s", (45 * 60000, worker))
    refusal = limits.turn_refusal(worker, resumed["source"], payload=resumed["payload"])
    _, planned = limits.plan()
    if human_input:
        assert refusal is None
        assert planned == []
    else:
        assert refusal is not None
        assert [(sid, tid, reason) for sid, tid, reason, _ in planned] == [
            (worker, resumed["id"], limit_kind),
        ]


@pytest.mark.parametrize("human_input", [False, True])
def test_remote_usage_after_answer_keeps_launch_identity_and_owner_classification(assistant_fixture, human_input):
    worker = job(assistant_fixture["id"], "Remote answer")
    resumed = answer_job_question(worker, human_input, {
        "orchestration_remote_origin": "starter-device", "orchestration_report_device": "starter-device",
    })
    if human_input:
        assert "orchestration_remote_origin" not in resumed["payload"]
    with get_db() as conn:
        cursor = conn.cursor()
        usage = limits._job_usage(cursor, worker, "starter-device", datetime.now(timezone.utc))
        assert limits._job_usage(cursor, worker, "other-device", datetime.now(timezone.utc)) == {"known": False}
    assert usage["known"] is True
    assert usage["turns"] == [{"turn_id": resumed["id"], "person": human_input}]


def test_defaults_without_a_saved_row(assistant_fixture):
    state = limits.status()
    assert state["spend_limit_enabled"] and state["daily_spend_limit_usd"] == 20.0
    assert state["job_limit_enabled"] and state["max_job_minutes"] == 120
    assert state["spent_today_usd"] == 0 and state["timezone"] == "UTC"
    assert not state["limit_reached"] and not state["warning"]


def test_counts_a_coordination_chat_and_everything_it_started_together(assistant_fixture):
    home = assistant_fixture["id"]
    worker = job(home, "Worker")
    chat = job(home, "Second worker")
    nested = job(worker, "Nested")  # Started from a job: counted with the same chat.
    database.create_project("ordinary-project", "Ordinary project", None, "/example/project")
    outsider = database.create_session("outsider", "assistant-agent", title="Independent project work", project_id="ordinary-project")
    for sid, cost in ((home, 1.25), (worker, 2.0), (chat, 0.5), (nested, 0.25), (outsider["id"], 50.0)):
        book(sid, cost)
    assert limits.status()["spent_today_usd"] == pytest.approx(4.0)


def test_running_turns_count_before_they_are_booked(assistant_fixture):
    from app.db.session_events import append_events
    worker = job(assistant_fixture["id"], "Long worker")
    turn = live_turn(worker)
    usage = lambda index, cost: {"type": "turn.usage", "turn_id": turn["id"],
                                 "payload": {"usage": {"model_call_index": index, "cost_usd": cost}}}
    # A retried call repeats its index and is billed once; an unindexed gauge waits for booking.
    append_events(worker, [usage(0, 3.0), usage(1, 2.0), usage(1, 2.0),
                           {"type": "turn.usage", "turn_id": turn["id"], "payload": {"usage": {"cost_usd": 9.0}}}])
    assert limits.status()["spent_today_usd"] == pytest.approx(5.0)


def test_day_rolls_over_in_the_owner_timezone(assistant_fixture):
    home = assistant_fixture["id"]
    now = datetime(2026, 10, 3, 3, 30, tzinfo=timezone.utc)
    book(home, 4.0, datetime(2026, 10, 2, 23, 0, tzinfo=timezone.utc))
    book(home, 1.0, datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc))
    utc = limits.status(now)
    assert utc["day"] == "2026-10-03" and utc["spent_today_usd"] == pytest.approx(1.0)
    assert utc["resets_at"] == "2026-10-04T00:00:00+00:00"
    database.set_user_preference("admin", "admin", "timezone", "America/New_York")
    local = limits.status(now)  # 23:30 on Oct 2 in New York: both bookings are today there.
    assert local["timezone"] == "America/New_York" and local["day"] == "2026-10-02"
    assert local["spent_today_usd"] == pytest.approx(5.0)
    assert local["resets_at"] == "2026-10-03T04:00:00+00:00"


def test_browser_local_timezone_preference_reads_as_utc(assistant_fixture):
    database.set_user_preference("admin", "admin", "timezone", "local")
    assert limits.owner_timezone()[1] == "UTC"


async def test_eighty_percent_is_a_warning_and_stops_nothing(assistant_fixture, stops):
    home = assistant_fixture["id"]
    set_limits(daily_spend_limit_usd=10)
    book(home, 7.0)
    assert not limits.status()["warning"]
    book(home, 1.5)
    state = limits.status()
    assert state["warning"] and not state["limit_reached"]
    await limits.enforce()
    stops.assert_not_awaited()


async def test_reaching_the_limit_blocks_new_work_and_stops_running_work(assistant_fixture, stops):
    home = assistant_fixture["id"]
    worker = job(assistant_fixture["id"], "Fix tests")
    turn = live_turn(worker)
    set_limits(daily_spend_limit_usd=5)
    book(worker, 5.5)
    reason = limits.start_refusal(home)
    assert reason and "$5.00" in reason and "only the owner can change it in Settings > Usage" in reason
    assert limits.start_refusal(worker) == reason  # Nested starts from a job are refused too.
    assert limits.turn_refusal(home, "agent_message")
    assert limits.turn_refusal(home, "user") is None  # The owner's own turn in their chat runs.
    assert limits.turn_refusal(worker, "agent_message")
    assert limits.turn_refusal(worker, "user") is None  # The owner's own turn in a job runs.
    assert limits.turn_refusal("unrelated", "wakeup") is None
    await limits.enforce()
    stops.assert_awaited_once_with(worker, "stop", turn_id=turn["id"], source="assistant")
    await limits.enforce()  # Stopped once.
    assert stops.await_count == 1


def test_start_session_and_task_are_refused_at_the_limit(assistant_fixture, monkeypatch):
    home = assistant_fixture["id"]
    database.set_session_active(home, True)
    turn = turns.enqueue_turn(home, "user", prompt="Coordinate", profile_id="assistant-agent")
    turns.settle_turn(turn["id"], "completed")
    set_limits(daily_spend_limit_usd=1)
    book(home, 2.0)
    with pytest.raises(ValueError, match="spending limit is reached"):
        orch._queue(home, {"title": "More work", "prompt": "Go", "request_id": "over"}, "start")
    from app.db.watches import list_watches
    assert list_watches() == []


async def test_watch_is_not_admitted_at_the_limit(assistant_fixture, monkeypatch):
    from app.core import watch_manager, watches
    from app.db import watches as watches_db
    row = watches.prepare(assistant_fixture['id'], {'description':'At cap',
                          'source':{'kind':'schedule','interval_seconds':60}})
    wid = watches_db.create(**row)
    set_limits(daily_spend_limit_usd=1)
    book(assistant_fixture['id'],2.0)
    await watch_manager.WatchManager().run_once()
    assert watches_db.events(wid,('pending',))
    assert watches_db.events(wid,('running',))==[]


async def test_a_job_past_its_time_limit_is_stopped(assistant_fixture, stops):
    worker = job(assistant_fixture["id"], "Nightly sweep")
    turn = live_turn(worker)
    set_limits(max_job_minutes=30)
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET work_ms=%s WHERE id=%s", (31 * 60000, worker))
    assert limits.turn_refusal(worker, "agent_message")
    await limits.enforce()
    stops.assert_awaited_once_with(worker, "stop", turn_id=turn["id"], source="assistant")


async def test_limits_switched_off_block_nothing(assistant_fixture, stops):
    home = assistant_fixture["id"]
    worker = job(assistant_fixture["id"], "Free run")
    live_turn(worker)
    set_limits(spend_limit_enabled=False, job_limit_enabled=False, max_job_minutes=5)
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET work_ms=%s WHERE id=%s", (600 * 60000, worker))
    book(home, 500.0)
    state = limits.status()
    # With both limits off, coordination chats are not even measured.
    assert not state["limit_reached"] and not state["warning"]
    assert limits.start_refusal(home) is None
    assert limits.turn_refusal(home, "user") is None and limits.turn_refusal(worker, "agent_message") is None
    await limits.enforce()
    stops.assert_not_awaited()


def test_get_usage_reports_todays_spend_to_a_coordination_chat(assistant_fixture):
    home = assistant_fixture["id"]
    book(home, 3.0)
    block = limits.session_usage(home)
    assert block["spent_today_usd"] == 3.0 and block["daily_limit_usd"] == 20.0
    assert block["max_job_minutes"] == 120 and "Only the owner" in block["note"]
    assert limits.session_usage("someone-else") is None
    assert "GetUsage" in limits.role_context(home) and limits.role_context("someone-else") == ""


async def test_get_usage_payload_carries_the_assistant_block(assistant_fixture, monkeypatch):
    from app.api import usage
    monkeypatch.setattr(usage, "get_cached_usage_payload", AsyncMock(return_value={"available": False, "windows": []}))
    payload = await usage.get_session_usage_payload(assistant_fixture["id"])
    assert payload["session_usage"]["scope"] == "assistant_today"
    plain = await usage.get_session_usage_payload(database.create_session("plain", "assistant-agent")["id"])
    assert "session_usage" not in plain


def test_role_context_is_static_for_prompt_caching(assistant_fixture):
    home = assistant_fixture["id"]
    first = limits.role_context(home)
    book(home, 12.34)
    assert limits.role_context(home) == first and "12.34" not in first


def _tool_sources():
    """Every module that defines or serves a model-facing tool."""
    roots = [ROOT / "app" / "mcp", ROOT / "fluid" / "fluid" / "tools"]
    files = [path for root in roots for path in root.rglob("*.py")]
    files += [ROOT / "app" / "core" / "session_orchestration.py", ROOT / "app" / "core" / "provider_agent.py"]
    return files


def test_no_model_facing_tool_can_change_the_limits():
    writers = {"update", "record_stop"}
    for path in _tool_sources():
        source = path.read_text(encoding="utf-8")
        assert "assistant_limits (" not in source and "UPDATE assistant_limits" not in source, path
        tree = ast.parse(source)
        imported = {alias.asname or alias.name for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names if "assistant_limits" in (getattr(node, "module", "") or "") + alias.name}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in writers and isinstance(node.value, ast.Name):
                assert node.value.id not in imported, f"{path} writes the assistant limits"
    # The only writer is the owner-only API.
    api = (ROOT / "app" / "api" / "assistant.py").read_text(encoding="utf-8")
    assert "dependencies=[Depends(require_admin)]" in api and "limits_db.update" in api


def test_stops_are_recorded_once(assistant_fixture):
    aid = service.assistant_id()
    assert limits_db.record_stop(aid, "s", "t1", "job_time", "Job")
    assert not limits_db.record_stop(aid, "s", "t1", "job_time", "Job")
    assert limits_db.stopped("t1") and not limits_db.stopped("t2")


def test_schema_is_idempotent():
    with get_db() as conn:
        limits_db.create_schema(conn.cursor())
        limits_db.create_schema(conn.cursor())
    with pytest.raises(ValueError):
        limits_db.update("x", {"daily_spend_limit_usd": 0.01})
    with pytest.raises(ValueError):
        limits_db.update("x", {"max_job_minutes": 2})
    with pytest.raises(ValueError):
        limits_db.update("x", {"unknown": 1})


async def test_the_owners_own_turn_in_a_coordination_chat_is_never_stopped(assistant_fixture, stops):
    home = assistant_fixture["id"]
    live_turn(home, "user")
    set_limits(daily_spend_limit_usd=2)
    book(home, 2.5)
    await limits.enforce()
    stops.assert_not_awaited()


# --- jobs on another worker ----------------------------------------------------------

def remote_job(session_id="remote-job", kind="task"):
    return store.link_remote_job(service.assistant_id(), session_id, "unraid-device", kind, "Remote job",
                                 starter_session_id="starter-chat")


def test_a_remote_jobs_rise_is_booked_once_on_the_day_it_is_learned(assistant_fixture):
    aid = service.assistant_id()
    remote_job()
    now = datetime.now(timezone.utc)
    yesterday = limits.day_window(now)[1] - timedelta(hours=1)
    assert store.record_remote_usage(aid, "remote-job", 2.0, active=True, at=yesterday) == 2.0
    assert limits.status(now)["spent_today_usd"] == 0  # Learned yesterday: yesterday's spend.
    assert store.record_remote_usage(aid, "remote-job", 2.0, active=True) == 0  # Seen twice.
    assert store.record_remote_usage(aid, "remote-job", 1.5, active=True) == 0  # Settled lower.
    assert store.record_remote_usage(aid, "remote-job", 3.25, active=True) == pytest.approx(1.25)
    assert limits.status(now)["spent_today_usd"] == pytest.approx(1.25)
    assert store.record_remote_usage(aid, "not-linked", 9.0, active=True) == 0.0
    # A job that spent something is never forgotten as never started.
    store.forget_remote_job(aid, "remote-job")
    assert store.get_remote_job(aid, "remote-job") is not None
    remote_job("never-started")
    store.forget_remote_job(aid, "never-started")
    assert store.get_remote_job(aid, "never-started") is None


def test_remote_jobs_age_out_only_once_their_worker_says_they_are_idle(assistant_fixture):
    aid = service.assistant_id()
    remote_job("idle")
    remote_job("unknown")
    remote_job("quiet")  # Its worker never answers.
    start = datetime.now(timezone.utc)
    store.record_remote_usage(aid, "idle", 1.0, active=False, at=start)  # Spent: not idle yet.
    store.record_remote_usage(aid, "idle", 1.0, active=False, at=start)
    store.note_remote_unknown(aid, "unknown", at=start)
    later = start + limits.REMOTE_IDLE_FOR + timedelta(minutes=1)
    assert [j["session_id"] for j in store.remote_jobs_to_check(aid, limits.REMOTE_IDLE_FOR, later)] == ["quiet"]
    store.record_remote_usage(aid, "idle", 1.5, active=False, at=later)  # Spent again: back in.
    assert {j["session_id"] for j in store.remote_jobs_to_check(aid, limits.REMOTE_IDLE_FOR, later)} == {"idle", "quiet"}


def test_a_remote_usage_answer_that_does_not_add_up_is_ignored():
    assert limits._entry({"known": True, "cost_usd": -1}) is None
    assert limits._entry({"known": True, "cost_usd": float("nan")}) is None
    assert limits._entry({"known": True, "cost_usd": True}) is None
    assert limits._entry({"cost_usd": 1.0}) is None
    assert limits._entry({"known": False, "cost_usd": 5}) == {"known": False}
    entry = limits._entry({"known": True, "cost_usd": 1e12, "turns": [{"turn_id": "t1"}, {"turn_id": 3}, "x"]})
    assert entry["cost_usd"] == limits.MAX_REPORTED_USD
    # A turn not said to be unattended is treated as the owner's and never stopped.
    assert entry["turns"] == [{"turn_id": "t1", "person": True}] and entry["running"] is True


def test_remote_stops_follow_the_limits(assistant_fixture):
    job = dict(session_id="r", device_id="d", kind="chat", label="R", job_minutes=10.0,
               turns=[{"turn_id": "t1", "person": False}, {"turn_id": "t2", "person": True}])
    state = dict(limit_reached=False, job_limit_enabled=True, max_job_minutes=30)
    assert limits.plan_remote(state, [job]) == []
    [stop] = limits.plan_remote({**state, "limit_reached": True}, [job])
    assert stop["reason"] == "daily_limit" and stop["turn_ids"] == ["t1"]
    [stop] = limits.plan_remote(state, [{**job, "job_minutes": 30.0}])
    assert stop["reason"] == "job_time"
    assert limits.plan_remote({**state, "job_limit_enabled": False}, [{**job, "job_minutes": 300.0}]) == []
    limits_db.record_stop(service.assistant_id(), "r", "t1", "job_time", "R")
    assert limits.plan_remote({**state, "limit_reached": True}, [job]) == []
