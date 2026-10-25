"""Moving sessions that have no Git checkout between two real workers (#639).

A no-project chat and a project whose folder is not a Git repository move
their history, question cards and wakes only. Each runs in the receiving
worker's own folder afterwards: the workspace folder for a chat, the project's
registered folder for a project. Folder contents never travel. Only a folder
proven not to be a repository counts as plain: a failed Git probe never
authorizes a history-only move.
"""
import json
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.core import checkout_transfer
from app.core.config import settings
from app.db import checkouts, database, projects_v2
from app.db.base import get_db
from app.network import control, crypto, engine, handoff, runners
from tests.core.network.harness import Worker, converge
from tests.core.network.test_handoff import pair  # noqa: F401 - fixture
from tests.core.test_checkout_transfer import repositories  # noqa: F401 - fixture

Pair = tuple[Worker, Worker, Path, Path]
Call = Callable[..., Awaitable[dict[str, Any]]]

SID = "folder-move"
ORIGIN = "local:plain-project"
OUTCOMES: list[str] = []  # Hand-off outcomes the tool path reported, newest last.
QUESTIONS = [{"question": "Which option?",
              "options": [{"label": "Keep", "description": "Keep it"}],
              "multiSelect": False, "header": "Choice"}]


def _route(a: Worker, b: Worker, phases: list[str] | None = None) -> Call:
    async def call(device: str, action: str, body: dict[str, Any],
                   **kwargs: Any) -> dict[str, Any]:
        caller = engine.require_context().device_id
        if phases is not None and action == "handoff":
            phases.append(body.get("phase"))
        with (a if device == a.device_id else b).active():
            return await control.handle(action, body, from_device=caller)
    return call


def _chat(a: Worker, b: Worker) -> None:
    with a.active():
        database.create_session(SID, "default", title="Chat", mode="chat")
        database.add_session_message(SID, "user", "Keep this chat")
        engine.scan_all()
    converge([a, b])


def _plain_project(a: Worker, b: Worker, tmp_path: Path, *,
                   on_target: bool = True) -> dict[str, Path]:
    """A ``local:`` project registered in a plain folder on each worker."""
    folders = {}
    for worker in (a, b) if on_target else (a,):
        folder = tmp_path / f"plain-{worker.name}"
        folder.mkdir()
        (folder / "notes.txt").write_text(f"only on {worker.name}")
        with worker.active():
            project = projects_v2.upsert_project(ORIGIN, name="Plain")
            checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(folder))
        folders[worker.name] = folder
    with a.active():
        project = projects_v2.get_project_v2_by_origin(ORIGIN)
        database.create_session(SID, "default", title="Plain", project_v2_id=project["id"])
        database.add_session_message(SID, "user", "Keep this project chat")
        engine.scan_all()
    converge([a, b])
    return folders


def _register(worker: Worker, folder: Path) -> None:
    """Point ``worker``'s registration of the project at ``folder`` instead."""
    with worker.active():
        project = projects_v2.get_project_v2_by_origin(ORIGIN)
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE checkouts SET path=%s WHERE project_id=%s AND runner_id=%s",
                (str(folder), project["id"], settings.effective_instance_id))


async def _move(path: str, src: Worker, dst: Worker, monkeypatch: pytest.MonkeyPatch,
                tmp_path: Path, phases: list[str] | None = None) -> str | None:
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
        listing = {"runners": [{"device_id": dst.device_id, "device_name": "Target",
                                "online": True, "is_self": False}]}
        store = tmp_path / f"pending-{src.name}.json"
        with patch.object(session_handoff, "_workers", return_value=listing), \
                patch.object(session_handoff, "_store_path", lambda: store), \
                patch.object(session_handoff, "_queue_changed", AsyncMock()), \
                patch.object(session_handoff, "schedule", lambda *args: None):
            session_handoff.request(SID, dst.device_id, "continue there")
            outcome = await session_handoff.run_pending(SID)
        OUTCOMES.append(outcome)
        return None if outcome.startswith("Handed off to Target.") else outcome


def _owned_by(worker: Worker, owner: Worker) -> bool:
    with worker.active():
        return database.get_session(SID)["owner_device_id"] == owner.device_id


def _refuse_git_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        pytest.fail("no Git transfer without a checkout")

    for name in ("export_checkout", "import_checkout"):
        monkeypatch.setattr(checkout_transfer, name, refuse)


def _record_exports(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    real_begin = handoff._begin

    def begin(sid: str, target: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        reply = real_begin(sid, target, *args, **kwargs)
        ctx = engine.require_context()
        raw = crypto.decrypt_payload(ctx.network_key, reply["payload"], handoff._aad(sid, target))
        exported.append(json.loads(raw))
        return reply

    monkeypatch.setattr(handoff, "_begin", begin)
    return exported


def _corrupt_checkout(repo: Path) -> None:
    """A real checkout whose Git metadata no longer reads: every probe fails."""
    (repo / ".git" / "HEAD").write_text("not a ref\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["api", "tool"])
async def test_no_project_chat_moves_and_moves_back(
        pair: Pair, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,  # noqa: F811
        path: str) -> None:
    a, b, _, _ = pair
    _chat(a, b)
    _refuse_git_transfer(monkeypatch)
    exported = _record_exports(monkeypatch)
    phases: list[str] = []
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


def test_chat_hand_off_request_does_not_promise_a_git_push(pair: Pair,  # noqa: F811
                                                           tmp_path: Path) -> None:
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
async def test_no_project_chat_carries_questions_and_wakes(pair: Pair,  # noqa: F811
                                                           monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path) -> None:
    from app.core import wakeup_service
    from app.core.user_question_handler import user_question_handler
    from app.db import wakeups

    a, b, _, _ = pair
    _chat(a, b)
    real_find = runners.find
    monkeypatch.setattr(runners, "find", lambda target: {
        **(real_find(target) or {}), "capabilities": {"move_questions": 1, "move_wakes": 3}})
    armed: list[str] = []
    monkeypatch.setattr(wakeup_service, "arm_persisted", lambda row: armed.append(row["id"]))
    fire_at = time.time() + 3600
    with a.active():
        wakeups.create_wakeup(id="chat-wake", session_id=SID, prompt="check back",
                              fire_at=fire_at, reason="later",
                              instance_id=settings.effective_instance_id)
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
async def test_plain_project_folder_moves_history_only_and_back(
        pair: Pair, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,  # noqa: F811
        path: str) -> None:
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
async def test_plain_project_missing_on_target_is_refused(
        pair: Pair, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,  # noqa: F811
        path: str) -> None:
    a, b, _, _ = pair
    _plain_project(a, b, tmp_path, on_target=False)
    refusal = await _move(path, a, b, monkeypatch, tmp_path)
    assert "isn't checked out on" in refusal and "Open the project there once" in refusal
    assert _owned_by(a, a)


@pytest.mark.asyncio
@pytest.mark.parametrize("git_on", ["source", "target"])
async def test_git_on_only_one_worker_is_refused_clearly(
        pair: Pair, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,  # noqa: F811
        git_on: str) -> None:
    a, b, source, target = pair
    _plain_project(a, b, tmp_path)
    worker, repo = (a, source) if git_on == "source" else (b, target)
    before = checkout_transfer.fingerprint(repo)
    _register(worker, repo)  # This worker registered the project in a Git checkout.
    assert await _move("api", a, b, monkeypatch, tmp_path) == handoff.PLAIN_MISMATCH
    assert _owned_by(a, a) and _owned_by(b, a)
    assert checkout_transfer.fingerprint(repo) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_on", ["source", "target"])
async def test_a_checkout_whose_git_probe_fails_never_moves_history_only(
        pair: Pair, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,  # noqa: F811
        broken_on: str) -> None:
    """Unreadable Git metadata is not a plain folder, on either worker."""
    a, b, source, target = pair
    _plain_project(a, b, tmp_path)
    worker, repo = (a, source) if broken_on == "source" else (b, target)
    _register(worker, repo)
    _corrupt_checkout(repo)
    _refuse_git_transfer(monkeypatch)
    exported = _record_exports(monkeypatch)
    refusal = await _move("api", a, b, monkeypatch, tmp_path)
    assert refusal and "plain_folder" not in json.dumps(exported)
    if broken_on == "source":
        # The receiver asked for history only; the owner refuses to grant it.
        assert refusal == handoff.PLAIN_MISMATCH
    else:
        # The receiver keeps the Git error instead of asking for history only.
        assert "Git rev-parse failed" in refusal and not exported
    assert _owned_by(a, a) and _owned_by(b, a)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_on", ["source", "target"])
async def test_a_failed_plain_folder_probe_keeps_ownership(
        pair: Pair, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,  # noqa: F811
        failing_on: str) -> None:
    """Dubious ownership (or Git missing) on the classifier probe fails closed."""
    from app.core.git_service import git_service

    a, b, _, _ = pair
    _plain_project(a, b, tmp_path)
    exported = _record_exports(monkeypatch)
    failing = a if failing_on == "source" else b
    real_run = git_service._run_git

    def run_git(path: str, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if engine.require_context().device_id == failing.device_id:
            return subprocess.CompletedProcess(
                ["git", *args], 128, "",
                "fatal: detected dubious ownership in repository at '" + path + "'\n")
        return real_run(path, args, **kwargs)

    monkeypatch.setattr(git_service, "_run_git", run_git)
    refusal = await _move("api", a, b, monkeypatch, tmp_path)
    if failing_on == "source":
        # The owner cannot prove its folder is plain, so it grants nothing.
        assert refusal == handoff.PLAIN_UNKNOWN
    else:
        # The receiver keeps the Git error instead of asking for history only.
        assert "Git rev-parse failed" in refusal
    assert not exported
    assert _owned_by(a, a) and _owned_by(b, a)


@pytest.mark.asyncio
async def test_old_owner_gets_a_clear_update_refusal(pair: Pair,  # noqa: F811
                                                     monkeypatch: pytest.MonkeyPatch,
                                                     tmp_path: Path) -> None:
    a, b, _, _ = pair
    _plain_project(a, b, tmp_path)
    real_find = runners.find
    # The owner reports capabilities from before history-only folder moves.
    monkeypatch.setattr(runners, "find", lambda target: {
        **(real_find(target) or {}), "capabilities": {"move_wakes": 3, "handoff_poll": 1}})
    assert await _move("api", a, b, monkeypatch, tmp_path) == handoff.PLAIN_OWNER_UPDATE
    assert _owned_by(a, a) and _owned_by(b, a)


@pytest.mark.asyncio
async def test_old_owner_without_capability_report_still_refuses_cleanly(
        pair: Pair, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: F811
    """An older owner ignores ``plain_folder`` and refuses a begin without a remote."""
    a, b, _, _ = pair
    _plain_project(a, b, tmp_path)
    real_begin = handoff.begin

    def old_begin(sid: str, target: str, remote: str | None = None, note: Any = None,
                  wake_barrier: bool = False, plain: bool = False) -> dict[str, Any]:
        return real_begin(sid, target, remote, note, wake_barrier)

    monkeypatch.setattr(handoff, "begin", old_begin)
    refusal = await _move("api", a, b, monkeypatch, tmp_path)
    assert refusal == "Update both workers to use Git-based resume."
    assert _owned_by(a, a) and _owned_by(b, a)


@pytest.mark.asyncio
async def test_old_receiver_answer_without_plain_flag_is_refused(
        pair: Pair, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: F811
    """A receiver must not install history when the owner did not confirm a plain folder."""
    a, b, _, _ = pair
    _plain_project(a, b, tmp_path)
    real_begin = handoff._begin

    def begin_without_flag(sid: str, target: str, remote: str | None, note: Any,
                           wake_barrier: bool, timing: Any,
                           plain: bool = False) -> dict[str, Any]:
        reply = real_begin(sid, target, remote, note, wake_barrier, timing, plain)
        ctx = engine.require_context()
        aad = handoff._aad(sid, target)
        data = json.loads(crypto.decrypt_payload(ctx.network_key, reply["payload"], aad))
        data.pop("plain_folder")
        payload = crypto.encrypt_payload(ctx.network_key, json.dumps(data).encode(), aad)
        return {**reply, "payload": payload}

    monkeypatch.setattr(handoff, "_begin", begin_without_flag)
    assert await _move("api", a, b, monkeypatch, tmp_path) == handoff.PLAIN_OWNER_UPDATE
    assert _owned_by(a, a) and _owned_by(b, a)


@pytest.mark.asyncio
async def test_receiver_cannot_skip_git_publication_for_a_git_project(
        pair: Pair) -> None:  # noqa: F811
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
async def test_git_project_protocol_is_unchanged(pair: Pair,  # noqa: F811
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Git projects keep the Git path: same phases and fields, no ``plain_folder``."""
    a, b, _, _ = pair
    bodies: list[dict[str, Any]] = []
    route = _route(a, b)

    async def call(device: str, action: str, body: dict[str, Any],
                   **kwargs: Any) -> dict[str, Any]:
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
    assert "plain_folder" not in exported[0]
    assert exported[0]["checkout"]["branch"] == "feature/only-on-worker"
