"""The owner's "Run work on" preference: stored per account, read with the
other workers, and applied as the Task default for the assistant home and its
jobs only, falling back here with a one-line note."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.core.task_dispatch as td
from app.core import assistant_service as service, assistant_worker
from app.core.provider_agent import WorkerOutcome
from app.db import assistant as store, database, settings as prefs
from app.network import membership, runners
from tests.db.test_assistant_store import assistant_fixture  # noqa: F401 - fixture

SELF = {"device_id": "dev-gaming", "device_name": "DesktopWorker", "online": True, "is_self": True,
        "capabilities": {"os": "windows", "remote_task": 1}}
UNRAID = {"device_id": "dev-serverworker", "device_name": "Unraid", "online": True, "is_self": False,
          "capabilities": {"os": "linux", "remote_task": 1}}
OLD = {"device_id": "dev-old", "device_name": "OldBox", "online": True, "is_self": False,
       "capabilities": {"os": "linux"}}
LAPTOP = {"device_id": "dev-laptop", "device_name": "Laptop", "online": False, "is_self": False,
          "capabilities": {"os": "darwin", "remote_task": 1}}


class _Ctx:
    device_id = "dev-gaming"

    @staticmethod
    def member(device_id):
        return SimpleNamespace(kind=membership.KIND_WORKER, status=membership.STATUS_ACTIVE)


@pytest.fixture()
def network(monkeypatch):
    listing = {"runners": [SELF, UNRAID, OLD, LAPTOP]}
    monkeypatch.setattr(runners.engine, "context", lambda: _Ctx())
    monkeypatch.setattr(runners, "directory", lambda: listing)
    return listing


KEY = assistant_worker.PREFERENCE_KEY + ":dev-gaming"


def stored():
    row = prefs.get_user_preference(*assistant_worker._identity(), KEY)
    return row["value"] if row else None


def save(device_id, key=KEY):
    prefs.set_user_preference(*assistant_worker._identity(), key, device_id)


# ─── read / write ────────────────────────────────────────────────────────────


def test_read_lists_the_other_workers_and_defaults_to_this_one(mock_db, network):
    state = assistant_worker.read()
    assert state == {"device_id": None, "workers": [
        {"device_id": "dev-laptop", "name": "Laptop", "online": False, "remote_task": True},
        {"device_id": "dev-old", "name": "OldBox", "online": True, "remote_task": False},
        {"device_id": "dev-serverworker", "name": "Unraid", "online": True, "remote_task": True},
    ]}


def test_read_without_a_network_has_no_workers(mock_db, monkeypatch):
    monkeypatch.setattr(runners.engine, "context", lambda: None)
    save("dev-serverworker")
    assert assistant_worker.read() == {"device_id": None, "workers": []}


@pytest.mark.parametrize("pick", ["dev-serverworker", "Unraid"])
def test_write_stores_the_device_id(mock_db, network, pick):
    assert assistant_worker.write(pick)["device_id"] == "dev-serverworker"
    assert stored() == "dev-serverworker"


@pytest.mark.parametrize("pick", [None, "", "  ", "dev-gaming", "DesktopWorker"])
def test_empty_or_this_worker_clears_the_choice(mock_db, network, pick):
    save("dev-serverworker")
    assert assistant_worker.write(pick)["device_id"] is None
    assert stored() is None


@pytest.mark.parametrize("pick, error", [
    ("dev-nope", "Unknown worker 'dev-nope'"),
    ("dev-laptop", "Laptop is offline; it was not saved"),
    ("dev-old", "OldBox needs an update before it can run the assistant's work."),
])
def test_write_refuses_workers_that_cannot_take_work(mock_db, network, pick, error):
    save("dev-serverworker")
    with pytest.raises(ValueError, match=error.replace("'", ".").replace("(", ".")):
        assistant_worker.write(pick)
    assert stored() == "dev-serverworker"


@pytest.mark.parametrize("value", ["dev-gone", "dev-gaming", 7, ""])
def test_stale_or_self_id_reads_as_this_worker(mock_db, network, value):
    save(value)
    assert assistant_worker.read()["device_id"] is None


def test_each_worker_reads_its_own_choice(mock_db, network):
    """Unraid's own choice (synced here) never steers this worker's home."""
    save("dev-laptop", key=assistant_worker.PREFERENCE_KEY + ":dev-serverworker")
    assert assistant_worker.read()["device_id"] is None
    assistant_worker.write("dev-serverworker")
    assert prefs.get_user_preference(*assistant_worker._identity(),
                                     assistant_worker.PREFERENCE_KEY + ":dev-serverworker")["value"] == "dev-laptop"


def test_a_revoked_worker_reads_as_this_worker(mock_db, network, monkeypatch):
    save("dev-serverworker")

    class Revoked(_Ctx):
        @staticmethod
        def member(device_id):
            status = membership.STATUS_ACTIVE if device_id != "dev-serverworker" else "revoked"
            return SimpleNamespace(kind=membership.KIND_WORKER, status=status)

    monkeypatch.setattr(runners.engine, "context", lambda: Revoked())
    assert assistant_worker.read()["device_id"] is None


# ─── default_for ─────────────────────────────────────────────────────────────


def test_default_applies_to_the_home_and_its_jobs_only(assistant_fixture, network):
    home = assistant_fixture["id"]
    database.create_session("job-1", "assistant-agent", title="Job")
    store.link_job(service.assistant_id(), "job-1")
    database.create_session("plain", "assistant-agent", title="Ordinary chat")
    assert assistant_worker.default_for(home) is None  # No choice yet.
    save("dev-serverworker")
    assert assistant_worker.default_for(home)["device_id"] == "dev-serverworker"
    assert assistant_worker.default_for("job-1")["device_id"] == "dev-serverworker"
    assert assistant_worker.default_for("plain") is None


def test_default_lookup_failure_runs_here(assistant_fixture, network, monkeypatch):
    save("dev-serverworker")
    monkeypatch.setattr(assistant_worker, "_identity", lambda: (_ for _ in ()).throw(PermissionError("x")))
    assert assistant_worker.default_for(assistant_fixture["id"]) is None


# ─── dispatch ────────────────────────────────────────────────────────────────


@pytest.fixture()
def callers(assistant_fixture):
    database.create_profile("helper", "Helper", None, {"model": "sonnet"})
    database.create_session("job-1", "assistant-agent", title="Job")
    store.link_job(service.assistant_id(), "job-1")
    database.create_session("plain", "assistant-agent", title="Ordinary chat")
    return {"home": assistant_fixture["id"], "job": "job-1", "plain": "plain"}


def dispatch(session_id, **args):
    calls = []

    async def fake_worker(**kwargs):
        calls.append(kwargs)
        return WorkerOutcome(ok=True, session_id="w1", report="done")

    with patch("app.core.provider_agent.dispatch_worker", side_effect=fake_worker):
        payload = asyncio.run(td.dispatch_task_tool(
            {"agent": "helper", "prompt": "go", "mode": "sync", **args}, caller_session_id=session_id))
    return payload, calls


def test_without_a_choice_nothing_changes(callers, network):
    payload, calls = dispatch(callers["home"])
    assert payload["ok"] is True and "worker_note" not in payload and len(calls) == 1


@pytest.mark.parametrize("role", ["home", "job"])
def test_assistant_work_goes_to_the_default_worker(callers, network, monkeypatch, role):
    """The real pick and the real dispatch path hand the work to the default."""
    from app.core import remote_task

    save("dev-serverworker")
    seen = []

    async def remote(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "mode": "background", "task_id": "t1"}

    monkeypatch.setattr(remote_task, "dispatch_remote", remote)
    payload, calls = dispatch(callers[role])
    assert payload == {"ok": True, "mode": "background", "task_id": "t1"}
    assert calls == [] and seen[0]["worker"]["device_id"] == "dev-serverworker"
    assert seen[0]["caller_session_id"] == callers[role]


def test_offline_default_falls_back_with_a_note(callers, network):
    save("dev-serverworker")
    network["runners"] = [SELF, {**UNRAID, "online": False}]
    payload, calls = dispatch(callers["home"])
    assert payload["ok"] is True and len(calls) == 1
    assert payload["worker_note"] == "Ran on this worker: Unraid is offline."


def test_older_default_falls_back_with_a_note(callers, network):
    save("dev-serverworker")
    network["runners"] = [SELF, {**UNRAID, "capabilities": {"os": "linux"}}]
    payload, _ = dispatch(callers["home"])
    assert payload["worker_note"] == ("Ran on this worker: Unraid runs an older release that cannot "
                                      "run remote subagents.")


def test_ordinary_chats_ignore_the_choice(callers, network, monkeypatch):
    save("dev-serverworker")

    def _never(*a, **k):
        raise AssertionError("an ordinary chat never resolves the default")
    monkeypatch.setattr(runners, "resolve_worker", _never)
    payload, calls = dispatch(callers["plain"])
    assert payload["ok"] is True and "worker_note" not in payload and len(calls) == 1


def test_explicit_worker_wins_and_never_falls_back(callers, network):
    save("dev-serverworker")
    payload, calls = dispatch(callers["home"], worker="Laptop")
    assert payload == {"ok": False, "error": "Laptop is offline; nothing was dispatched. Ask the user to "
                                             "bring it online, or omit worker to run it here."}
    assert calls == []
    payload, calls = dispatch(callers["home"], worker="DesktopWorker")
    assert payload["ok"] is True and "worker_note" not in payload and len(calls) == 1


def test_a_reachable_default_is_dispatched_there(callers, network, monkeypatch):
    """Once remote dispatch exists, the default goes through it unchanged."""
    save("dev-serverworker")
    monkeypatch.setattr(td, "_remote_worker_pick", lambda value, sid, row: (UNRAID, None))
    seen = []

    async def remote(args, **kwargs):
        seen.append(args)
        return {"ok": True, "mode": "background", "task_id": "t1", "worker": args.get("worker")}

    real = td._dispatch_task_tool
    monkeypatch.setattr(td, "_dispatch_task_tool",
                        lambda args, **kw: remote(args, **kw) if args.get("worker") else real(args, **kw))
    payload, calls = dispatch(callers["home"])
    assert payload == {"ok": True, "mode": "background", "task_id": "t1", "worker": "dev-serverworker"}
    assert calls == [] and seen[0]["worker"] == "dev-serverworker"


def test_a_late_refusal_that_ran_nothing_still_falls_back(callers, network, monkeypatch):
    save("dev-serverworker")
    monkeypatch.setattr(td, "_remote_worker_pick", lambda value, sid, row: (UNRAID, None))
    real = td._dispatch_task_tool

    async def remote_refuses(args, **kwargs):
        return {"ok": False, "error": "Unraid has no checkout of this project; nothing was dispatched."}

    monkeypatch.setattr(td, "_dispatch_task_tool",
                        lambda args, **kw: remote_refuses(args) if args.get("worker") else real(args, **kw))
    payload, calls = dispatch(callers["home"])
    assert payload["ok"] is True and len(calls) == 1
    assert payload["worker_note"] == "Ran on this worker: Unraid has no checkout of this project."


def test_a_remote_failure_after_dispatch_is_not_rerun_here(callers, network, monkeypatch):
    save("dev-serverworker")
    monkeypatch.setattr(td, "_remote_worker_pick", lambda value, sid, row: (UNRAID, None))
    real = td._dispatch_task_tool

    async def remote_fails(args, **kwargs):
        return {"ok": False, "error": "Worker failed: provider error"}

    monkeypatch.setattr(td, "_dispatch_task_tool",
                        lambda args, **kw: remote_fails(args) if args.get("worker") else real(args, **kw))
    payload, calls = dispatch(callers["home"])
    assert payload == {"ok": False, "error": "Worker failed: provider error"} and calls == []
