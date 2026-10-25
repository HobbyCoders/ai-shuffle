"""Moving sessions that have no Git checkout between two real workers (#639).

A no-project chat and a project whose folder is not a Git repository move
their history, question cards and wakes only. Each runs in the receiving
worker's own folder afterwards: the workspace folder for a chat, the project's
registered folder for a project. Folder contents never travel.
"""
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.core import checkout_transfer
from app.core.config import settings
from app.db import checkouts, database, projects_v2
from app.db.base import get_db
from app.network import control, crypto, engine, handoff, runners
from tests.core.network.harness import converge
from tests.core.network.test_handoff import pair  # noqa: F401 - fixture
from tests.core.test_checkout_transfer import repositories  # noqa: F401 - fixture

SID = "folder-move"
OUTCOMES = []  # Hand-off outcomes the tool path reported, newest last.
QUESTIONS = [{"question": "Which option?", "options": [{"label": "Keep", "description": "Keep it"}],
              "multiSelect": False, "header": "Choice"}]


def _route(a, b, phases=None):
    async def call(device, action, body, **kwargs):
        caller = engine.require_context().device_id
        if phases is not None and action == "handoff":
            phases.append(body.get("phase"))
        with (a if device == a.device_id else b).active():
            return await control.handle(action, body, from_device=caller)
    return call


def _chat(a, b):
    with a.active():
        database.create_session(SID, "default", title="Chat", mode="chat")
        database.add_session_message(SID, "user", "Keep this chat")
        engine.scan_all()
    converge([a, b])


def _plain_project(a, b, tmp_path, *, on_target=True):
    """A ``local:`` project registered in a plain folder on each worker."""
    folders = {}
    for worker in (a, b) if on_target else (a,):
        folder = tmp_path / f"plain-{worker.name}"
        folder.mkdir()
        (folder / "notes.txt").write_text(f"only on {worker.name}")
        with worker.active():
            project = projects_v2.upsert_project("local:plain-project", name="Plain")
            checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(folder))
        folders[worker.name] = folder
    with a.active():
        project = projects_v2.get_project_v2_by_origin("local:plain-project")
        database.create_session(SID, "default", title="Plain", project_v2_id=project["id"])
        database.add_session_message(SID, "user", "Keep this project chat")
        engine.scan_all()
    converge([a, b])
    return folders


async def _move(path, src, dst, monkeypatch, tmp_path, phases=None):
    """Run a producing path on ``src``: the Move endpoint or a HandOffSession hand-off.

    Returns the refusal text, or None when the move reported success.
    """
    from fastapi import HTTPException
    from app.api.sessions import move_session_to
    from app.core import session_handoff

    # In a network the device id names a worker; legacy instance ids are aliases.
    target = dst.device_id
    monkeypatch.setattr(control, "call", _route(src, dst, phases))
    monkeypatch.setattr("app.core.placement.capable_runners", lambda *args: [target])
    with src.active():
        if path == "api":
            try:
                result = await move_session_to(SID, database.get_session(SID), target)
            except HTTPException as exc:
                return exc.detail
            assert result.moved is True
            return None
        listing = {"runners": [{"device_id": dst.device_id, "device_name": "Target", "online": True,
                                "is_self": False}]}
        with patch.object(session_handoff, "_workers", return_value=listing), \
                patch.object(session_handoff, "_store_path", lambda: tmp_path / f"pending-{src.name}.json"), \
                patch.object(session_handoff, "_queue_changed", AsyncMock()), \
                patch.object(session_handoff, "schedule", lambda *args: None):
            session_handoff.request(SID, dst.device_id, "continue there")
            outcome = await session_handoff.run_pending(SID)
        OUTCOMES.append(outcome)
        return None if outcome.startswith("Handed off to Target.") else outcome


def _owned_by(worker, owner):
    with worker.active():
        return database.get_session(SID)["owner_device_id"] == owner.device_id


def _refuse_git_transfer(monkeypatch):
    for name in ("export_checkout", "import_checkout"):
        monkeypatch.setattr(checkout_transfer, name,
                            lambda *args, **kwargs: pytest.fail("no Git transfer without a checkout"))


def _record_exports(monkeypatch):
    exported = []
    real_begin = handoff._begin

    def begin(sid, target, *args, **kwargs):
        reply = real_begin(sid, target, *args, **kwargs)
        ctx = engine.require_context()
        exported.append(json.loads(crypto.decrypt_payload(ctx.network_key, reply["payload"],
                                                          handoff._aad(sid, target))))
        return reply

    monkeypatch.setattr(handoff, "_begin", begin)
    return exported


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["api", "tool"])
async def test_no_project_chat_moves_and_moves_back(pair, monkeypatch, tmp_path, path):  # noqa: F811
    a, b, _, _ = pair
    _chat(a, b)
    _refuse_git_transfer(monkeypatch)
    exported = _record_exports(monkeypatch)
    phases = []
    assert await _move(path, a, b, monkeypatch, tmp_path, phases) is None
    assert phases[:3] == ["receive", "begin", "commit"]
    assert exported[-1]["checkout"] is None and "plain_folder" not in exported[-1]
    assert _owned_by(a, b) and _owned_by(b, b)
    with b.active():
        row = database.get_session(SID)
        assert row["mode"] == "chat" and not row["project_v2_id"] and not row["branch"]
        assert database.get_session_messages(SID)[0]["content"] == "Keep this chat"
    if path == "tool":
        assert OUTCOMES[-1].startswith("Handed off to Target. ")
        assert "same branch" not in OUTCOMES[-1]
    converge([a, b])
    assert await _move(path, b, a, monkeypatch, tmp_path) is None
    assert _owned_by(a, a) and _owned_by(b, a)
    with a.active():
        assert database.get_session_messages(SID)[0]["content"] == "Keep this chat"


def test_chat_hand_off_request_does_not_promise_a_git_push(pair, tmp_path):  # noqa: F811
    from app.core import session_handoff

    a, b, _, _ = pair
    _chat(a, b)
    listing = {"runners": [{"device_id": b.device_id, "device_name": "Target", "online": True,
                            "is_self": False}]}
    with a.active(), patch.object(session_handoff, "_workers", return_value=listing), \
            patch.object(session_handoff, "_store_path", lambda: tmp_path / "pending.json"):
        message = session_handoff.request(SID, b.device_id)["message"]
        session_handoff.cancel(SID)
        assert "moves this session and its history there" in message
        assert "push" not in message
        # A Git project session keeps its wording.
        message = session_handoff.request("handoff-test", b.device_id)["message"]
        session_handoff.cancel("handoff-test")
        assert "commits and pushes the branch" in message


@pytest.mark.asyncio
async def test_no_project_chat_carries_questions_and_wakes(pair, monkeypatch, tmp_path):  # noqa: F811
    from app.core import wakeup_service
    from app.core.user_question_handler import user_question_handler
    from app.db import wakeups

    a, b, _, _ = pair
    _chat(a, b)
    real_find = runners.find
    monkeypatch.setattr(runners, "find", lambda target: {
        **(real_find(target) or {}), "capabilities": {"move_questions": 1, "move_wakes": 3}})
    armed = []
    monkeypatch.setattr(wakeup_service, "arm_persisted", lambda row: armed.append(row["id"]))
    fire_at = time.time() + 3600
    with a.active():
        wakeups.create_wakeup(id="chat-wake", session_id=SID, prompt="check back", fire_at=fire_at,
                              reason="later", instance_id=settings.effective_instance_id)
        await user_question_handler.post_question(
            request_id="chat-question", tool_use_id="ask", session_id=SID,
            questions=QUESTIONS, broadcast_func=AsyncMock())
    assert await _move("api", a, b, monkeypatch, tmp_path) is None
    with b.active():
        [card] = user_question_handler.get_pending_questions(SID)
        assert card["questions"] == QUESTIONS
        assert [row["id"] for row in wakeups.get_session_wakeups(SID)] == ["chat-wake"]
    assert armed == ["chat-wake"]
    with a.active():
        assert wakeups.get_session_wakeups(SID) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["api", "tool"])
async def test_plain_project_folder_moves_history_only_and_back(pair, monkeypatch, tmp_path, path):  # noqa: F811
    from app.core import project_resolver

    a, b, _, _ = pair
    folders = _plain_project(a, b, tmp_path)
    _refuse_git_transfer(monkeypatch)
    exported = _record_exports(monkeypatch)
    assert await _move(path, a, b, monkeypatch, tmp_path) is None
    assert exported[-1]["checkout"] is None and exported[-1]["plain_folder"] is True
    assert _owned_by(a, b) and _owned_by(b, b)
    with b.active():
        row = database.get_session(SID)
        assert not row["branch"]
        assert project_resolver.resolve_existing_cwd(row) == str(folders["b"])
        assert database.get_session_messages(SID)[0]["content"] == "Keep this project chat"
    # Folder contents never travel: each worker keeps only its own files.
    assert [p.name for p in folders["b"].iterdir()] == ["notes.txt"]
    assert (folders["b"] / "notes.txt").read_text() == "only on b"
    converge([a, b])
    assert await _move(path, b, a, monkeypatch, tmp_path) is None
    assert _owned_by(a, a) and _owned_by(b, a)
    assert (folders["a"] / "notes.txt").read_text() == "only on a"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["api", "tool"])
async def test_plain_project_missing_on_target_is_refused(pair, monkeypatch, tmp_path, path):  # noqa: F811
    a, b, _, _ = pair
    _plain_project(a, b, tmp_path, on_target=False)
    refusal = await _move(path, a, b, monkeypatch, tmp_path)
    assert "isn't checked out on" in refusal and "Open the project there once" in refusal
    assert _owned_by(a, a)


@pytest.mark.asyncio
@pytest.mark.parametrize("git_on", ["source", "target"])
async def test_git_on_only_one_worker_is_refused_clearly(pair, monkeypatch, tmp_path, git_on):  # noqa: F811
    a, b, source, target = pair
    _plain_project(a, b, tmp_path)
    worker, repo = (a, source) if git_on == "source" else (b, target)
    before = checkout_transfer.fingerprint(repo)
    with worker.active():
        # This worker registered the project in a Git checkout instead.
        project = projects_v2.get_project_v2_by_origin("local:plain-project")
        with get_db() as conn:
            conn.cursor().execute("UPDATE checkouts SET path=%s WHERE project_id=%s AND runner_id=%s",
                                  (str(repo), project["id"], settings.effective_instance_id))
    assert await _move("api", a, b, monkeypatch, tmp_path) == handoff.PLAIN_MISMATCH
    assert _owned_by(a, a) and _owned_by(b, a)
    assert checkout_transfer.fingerprint(repo) == before


@pytest.mark.asyncio
async def test_old_owner_gets_a_clear_update_refusal(pair, monkeypatch, tmp_path):  # noqa: F811
    a, b, _, _ = pair
    _plain_project(a, b, tmp_path)
    real_find = runners.find
    # The owner reports capabilities from before history-only folder moves.
    monkeypatch.setattr(runners, "find", lambda target: {
        **(real_find(target) or {}), "capabilities": {"move_wakes": 3, "handoff_poll": 1}})
    assert await _move("api", a, b, monkeypatch, tmp_path) == handoff.PLAIN_OWNER_UPDATE
    assert _owned_by(a, a) and _owned_by(b, a)


@pytest.mark.asyncio
async def test_old_owner_without_capability_report_still_refuses_cleanly(pair, monkeypatch, tmp_path):  # noqa: F811
    """An older owner ignores ``plain_folder`` and refuses a begin without a remote."""
    a, b, _, _ = pair
    _plain_project(a, b, tmp_path)
    real_begin = handoff.begin

    def old_begin(sid, target, remote=None, note=None, wake_barrier=False, plain=False):
        return real_begin(sid, target, remote, note, wake_barrier)

    monkeypatch.setattr(handoff, "begin", old_begin)
    assert await _move("api", a, b, monkeypatch, tmp_path) == "Update both workers to use Git-based resume."
    assert _owned_by(a, a) and _owned_by(b, a)


@pytest.mark.asyncio
async def test_old_receiver_answer_without_plain_flag_is_refused(pair, monkeypatch, tmp_path):  # noqa: F811
    """A receiver must not install history when the owner did not confirm a plain folder."""
    a, b, _, _ = pair
    _plain_project(a, b, tmp_path)
    real_begin = handoff._begin

    def begin_without_flag(sid, target, remote, note, wake_barrier, timing, plain=False):
        reply = real_begin(sid, target, remote, note, wake_barrier, timing, plain)
        ctx = engine.require_context()
        data = json.loads(crypto.decrypt_payload(ctx.network_key, reply["payload"], handoff._aad(sid, target)))
        data.pop("plain_folder")
        payload = crypto.encrypt_payload(ctx.network_key, json.dumps(data).encode(), handoff._aad(sid, target))
        return {**reply, "payload": payload}

    monkeypatch.setattr(handoff, "_begin", begin_without_flag)
    assert await _move("api", a, b, monkeypatch, tmp_path) == handoff.PLAIN_OWNER_UPDATE
    assert _owned_by(a, a) and _owned_by(b, a)


@pytest.mark.asyncio
async def test_receiver_cannot_skip_git_publication_for_a_git_project(pair):  # noqa: F811
    """A forged ``plain_folder`` begin for a Git checkout is refused by the owner."""
    a, b, source, _ = pair
    before = checkout_transfer.fingerprint(source)
    with a.active():
        with pytest.raises(ValueError, match="Git repository on one worker"):
            await control.handle("handoff", {"session_id": "handoff-test", "phase": "begin",
                                             "remote": None, "wake_barrier": True,
                                             "plain_folder": True}, from_device=b.device_id)
        assert database.get_session("handoff-test")["owner_device_id"] == a.device_id
    assert checkout_transfer.fingerprint(source) == before


@pytest.mark.asyncio
async def test_git_project_protocol_is_unchanged(pair, monkeypatch):  # noqa: F811
    """Git projects keep the Git path: same phases and fields, no ``plain_folder``."""
    a, b, _, _ = pair
    bodies = []
    route = _route(a, b)

    async def call(device, action, body, **kwargs):
        bodies.append(dict(body))
        return await route(device, action, body, **kwargs)

    monkeypatch.setattr(control, "call", call)
    exported = _record_exports(monkeypatch)
    with b.active():
        await handoff.receive(database.get_session("handoff-test"))
        assert database.get_session("handoff-test")["owner_device_id"] == b.device_id
    assert [body["phase"] for body in bodies] == ["begin", "commit"]
    assert set(bodies[0]) == {"session_id", "phase", "remote", "wake_barrier"}
    assert set(bodies[1]) == {"session_id", "phase", "token", "fingerprint", "branch",
                              "session_branch", "wake_ids"}
    assert bodies[1]["fingerprint"] and bodies[1]["branch"] == "feature/only-on-worker"
    assert "plain_folder" not in exported[0] and exported[0]["checkout"]["branch"] == "feature/only-on-worker"
