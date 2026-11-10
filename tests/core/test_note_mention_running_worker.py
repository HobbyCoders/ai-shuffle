"""#1364: a note picked from @ resolves on the worker that runs the chat.

A note's ``file_path`` is per-worker (``local_fields`` in the sync policy), so
the absolute path the viewing worker's ``/notes/autocomplete`` reports means
nothing on another worker with a different data folder. In a chat on another
worker the @ menu inserts ``@Notes/<id>``, and the running worker expands that
at turn setup: to its own copy of the note file, or to the note's text while it
has no file yet.

Two workers are modelled the way they differ for notes: one shared database
and two data folders. Worker A writes the note through the real notes API;
worker B sees the row as sync delivers it (no ``file_path``), and later as its
startup materialization leaves it (its own file). Both turns on B run through
the real engine entry (``_stream_to_websocket_impl``). A message queued while a
turn runs is never steered into it (steering has no turn setup): it waits and
runs as its own turn, as the principal that sent it.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.notes import router
from app.core import note_files, session_runtime
from app.db import database
from app.db import turns as turns_db
from app.network.policy import COLLECTIONS
from tests.core.test_query_engine_note_references import _sent_prompt
from tests.core.test_steering_delivery_flows import (
    _Adapter,
    _live_runtime,
    _pooled,
    _start_live,
)

CONTENT = "Secret word: pelican"


@pytest.fixture(autouse=True)
def _clean_runtimes():
    session_runtime._live_runtimes.clear()
    yield
    session_runtime._live_runtimes.clear()


def _on_worker(data_dir: Path):
    """Run as the worker whose data folder is ``data_dir``."""
    return patch.object(note_files, "settings", SimpleNamespace(effective_data_dir=data_dir))


@pytest.fixture
def workers(tmp_path, mock_db, monkeypatch):
    """A note written on worker A, as worker B's copy of the row sees it."""
    dir_a, dir_b = tmp_path / "worker-a" / "data", tmp_path / "worker-b" / "data"
    monkeypatch.setattr("app.api.notes.database", database)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[require_auth] = lambda: "owner"
    with _on_worker(dir_a), TestClient(application) as client:
        created = client.post("/api/v1/notes", json={"title": "Repro 1364", "content": CONTENT})
        assert created.status_code == 201
        note_id = created.json()["id"]
        [listed] = client.get("/api/v1/notes/autocomplete", params={"search": "Repro"}).json()
    viewer_path = listed["absolute_path"]
    assert Path(viewer_path).is_relative_to(dir_a) and Path(viewer_path).read_text()

    # Sync never carries file_path: B's row arrives without one.
    notes = COLLECTIONS["notes"]
    assert "file_path" in notes.local_fields and "file_path" not in notes.fields
    mock_db.execute("UPDATE notes SET file_path = NULL WHERE id = ?", (note_id,))
    mock_db.commit()
    return SimpleNamespace(dir_a=dir_a, dir_b=dir_b, note_id=note_id, viewer_path=viewer_path)


async def test_worker_b_inlines_a_note_it_has_no_file_for(workers, tmp_path):
    with _on_worker(workers.dir_b):
        sent = await _sent_prompt(f"read @Notes/{workers.note_id}", None, tmp_path)
    assert f"[Note: Repro 1364]\n{CONTENT}" in sent
    assert str(workers.dir_a) not in sent and "@Notes/" not in sent


async def test_worker_b_points_at_its_own_copy_once_materialized(workers, tmp_path):
    with _on_worker(workers.dir_b):
        # What B's startup does for notes that reached it without a file.
        database.bulk_update_note_file_paths(
            note_files.materialize_all_notes(database.get_notes_without_file_path()))
        sent = await _sent_prompt(f"read @Notes/{workers.note_id}", None, tmp_path)
    assert sent.startswith("read @") and "@Notes/" not in sent
    resolved = Path(sent.removeprefix("read @"))
    assert resolved.is_relative_to(workers.dir_b) and CONTENT in resolved.read_text()
    # The path the viewing worker's menu reported is not where B keeps it.
    assert resolved != Path(workers.viewer_path)
    assert not Path(workers.viewer_path).is_relative_to(workers.dir_b)


async def test_a_queued_note_reference_is_not_steered_and_holds_its_place(workers, tmp_path):
    sid = "note-steer-1364"
    runtime = _live_runtime(sid, _start_live(sid))
    row = turns_db.enqueue_turn(sid, "user", prompt=f"also @Notes/{workers.note_id}")
    after = turns_db.enqueue_turn(sid, "user", prompt="and one more thing")
    adapter = _Adapter()
    with _on_worker(workers.dir_b), _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 0
        # x and edit on the waiting chip: nothing was handed to the engine.
        assert await session_runtime.try_cancel_steer(sid, row["id"]) == "not_submitted"
    assert adapter.calls == [] and adapter.cancels == []
    assert runtime._steer_refusal(row) == "a note reference needs turn setup"
    # The message behind it does not overtake it.
    assert [r["id"] for r in turns_db.get_queued_turns(sid)] == [row["id"], after["id"]]

    # Its own turn on B, as the principal that sent it, expands the reference.
    queued = session_runtime.SessionRuntime(turns_db.get_turn(row["id"]))
    with _on_worker(workers.dir_b):
        sent = await _sent_prompt(row["prompt"], queued.api_user_id, tmp_path)
    assert sent == f"also [Note: Repro 1364]\n{CONTENT}"


async def test_a_queued_message_without_a_note_reference_still_steers(workers):
    sid = "note-steer-1364-plain"
    _live_runtime(sid, _start_live(sid))
    row = turns_db.enqueue_turn(sid, "user", prompt="Notes/ is just a folder name here")
    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 1
    assert adapter.calls == [("Notes/ is just a folder name here", row["id"])]


async def test_a_key_queuing_into_an_owner_turn_gets_only_its_own_notes(workers, tmp_path):
    """The owner's live turn never expands a reference an API key queued."""
    sid = "note-steer-1364-key"
    runtime = _live_runtime(sid, _start_live(sid))
    assert runtime.api_user_id is None
    row = turns_db.enqueue_turn(
        sid, "user", prompt=f"also @Notes/{workers.note_id}", payload={"api_user_id": "key-1"},
    )
    adapter = _Adapter()
    with _on_worker(workers.dir_b), _pooled(adapter):
        assert await session_runtime.try_steer_pending(sid) == 0
    assert adapter.calls == []
    queued = session_runtime.SessionRuntime(turns_db.get_turn(row["id"]))
    assert queued.api_user_id == "key-1"
    with _on_worker(workers.dir_b):
        sent = await _sent_prompt(row["prompt"], queued.api_user_id, tmp_path)
    assert sent == f"also @Notes/{workers.note_id}"
