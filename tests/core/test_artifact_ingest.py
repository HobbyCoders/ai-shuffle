"""Unit tests for Q5 screenshot artifact ingest.

Covers the pure decode/hash helpers (app/core/artifact_ingest.py) and the
TurnRecorder ingest seam (app/core/turn_recorder.py): the ingest predicate
hit/miss, content-address stamping onto tool.result, artifact.created emission,
runner_id holder attribution, content-address dedup, and failure isolation (a
store error never breaks the pump).

The recorder is driven DIRECTLY with pump-shaped dicts (same convention as
tests/core/test_turn_recorder.py).
"""

import base64

import pytest

from app.core import artifact_ingest, artifact_store
from app.core.turn_recorder import TurnRecorder
from app.db import artifacts as artifacts_db
from app.db import session_events as se
from app.db import sessions as sessions_db

SID = "sess-q5"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _mk_session(session_id: str = SID) -> None:
    sessions_db.create_session(session_id, "default")


async def _recorder(session_id: str = SID) -> TurnRecorder:
    rec = await TurnRecorder.maybe_create(session_id)
    assert rec is not None
    return rec


def _rows(session_id: str = SID):
    return se.get_events(session_id)


def _types(session_id: str = SID):
    return [r["event_type"] for r in _rows(session_id)]


def _data_uri(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _tool_result_event(screenshot=None):
    ev = {"type": "tool_result", "name": "browser", "tool_use_id": "tu_1",
          "output": "", "is_error": False}
    if screenshot is not None:
        ev["screenshot"] = screenshot
    return ev


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_parse_data_uri_roundtrip():
    payload = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    parsed = artifact_ingest.parse_screenshot_data_uri(_data_uri(payload, "image/jpeg"))
    assert parsed is not None
    data, mime = parsed
    assert data == payload
    assert mime == "image/jpeg"


@pytest.mark.parametrize("bad", [
    None,
    123,
    "",
    "not a data uri",
    "https://example.com/x.png",
    "data:image/png;base64,",          # empty payload
    "data:image/png,rawtext",           # not base64-flagged
])
def test_parse_data_uri_misses(bad):
    assert artifact_ingest.parse_screenshot_data_uri(bad) is None


def test_content_hash_matches_store():
    import hashlib
    data = b"abc123"
    assert artifact_ingest.content_hash(data) == hashlib.sha256(data).hexdigest()


def test_filename_for_known_and_unknown_mime():
    digest = "a" * 64
    assert artifact_ingest.filename_for(digest, "image/png") == f"{digest}.png"
    assert artifact_ingest.filename_for(digest, "image/jpeg") == f"{digest}.jpg"
    assert artifact_ingest.filename_for(digest, "application/x-thing") == digest


# ---------------------------------------------------------------------------
# recorder ingest — predicate HIT
# ---------------------------------------------------------------------------


async def test_ingest_hit_stamps_hash_and_emits_artifact_created(mock_db, artifacts_dir):
    _mk_session()
    rec = await _recorder()
    payload = b"screenshot-bytes-hit"
    digest = artifact_ingest.content_hash(payload)

    await rec.on_event(_tool_result_event(_data_uri(payload, "image/png")))

    rows = _rows()
    # artifact.created is appended in the SAME batch, just before tool.result.
    assert rows[-2]["event_type"] == "artifact.created"
    assert rows[-1]["event_type"] == "tool.result"

    created = rows[-2]["payload"]
    assert created["hash"] == digest
    assert created["mime"] == "image/png"
    assert created["filename"] == f"{digest}.png"
    assert created["size"] == len(payload)

    assert rows[-1]["payload"]["artifact_hashes"] == [digest]

    await rec.close()

    # The fire-and-forget store is drained on close: bytes on disk + DB row with
    # holder attribution == the ingesting install.
    on_disk = artifacts_dir / digest[:2] / digest
    assert on_disk.exists()
    assert on_disk.read_bytes() == payload
    db_row = artifacts_db.get_artifact(digest)
    assert db_row is not None
    assert db_row["runner_id"] == rec.runner_id
    assert db_row["size"] == len(payload)
    assert db_row["mime"] == "image/png"


# ---------------------------------------------------------------------------
# recorder ingest — predicate MISS
# ---------------------------------------------------------------------------


async def test_ingest_miss_no_screenshot(mock_db, artifacts_dir):
    _mk_session()
    rec = await _recorder()
    await rec.on_event(_tool_result_event(screenshot=None))
    rows = _rows()
    assert _types() == ["tool.result"]
    assert rows[-1]["payload"]["artifact_hashes"] == []
    await rec.close()
    assert not artifacts_dir.exists() or not any(artifacts_dir.rglob("*"))


async def test_ingest_miss_non_data_uri_screenshot(mock_db, artifacts_dir):
    _mk_session()
    rec = await _recorder()
    # A non-data-URI screenshot value (e.g. a plain URL) is not ingestable.
    await rec.on_event(_tool_result_event("https://example.com/shot.png"))
    assert _types() == ["tool.result"]
    assert _rows()[-1]["payload"]["artifact_hashes"] == []
    await rec.close()
    assert artifacts_db.get_artifact("x") is None


# ---------------------------------------------------------------------------
# content-address dedup
# ---------------------------------------------------------------------------


async def test_ingest_dedup_same_bytes_one_blob(mock_db, artifacts_dir):
    _mk_session()
    rec = await _recorder()
    payload = b"identical-screenshot"
    digest = artifact_ingest.content_hash(payload)
    uri = _data_uri(payload, "image/png")

    await rec.on_event(_tool_result_event(uri))
    ev2 = _tool_result_event(uri)
    ev2["tool_use_id"] = "tu_2"
    await rec.on_event(ev2)
    await rec.close()

    # Both tool.results reference the same content address.
    tool_results = [r for r in _rows() if r["event_type"] == "tool.result"]
    assert len(tool_results) == 2
    assert all(r["payload"]["artifact_hashes"] == [digest] for r in tool_results)

    # ...and the store deduped to a single on-disk blob (no temp leftovers).
    shard = artifacts_dir / digest[:2]
    assert [p.name for p in shard.iterdir()] == [digest]


# ---------------------------------------------------------------------------
# failure isolation — a store error never breaks the pump
# ---------------------------------------------------------------------------


async def test_ingest_store_error_is_isolated(mock_db, artifacts_dir, monkeypatch):
    _mk_session()
    rec = await _recorder()

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(artifact_store, "store_bytes", _boom)

    payload = b"doomed-store"
    digest = artifact_ingest.content_hash(payload)

    # on_event must not raise despite the store blowing up in the background.
    await rec.on_event(_tool_result_event(_data_uri(payload)))
    # The tool.result is still recorded (streaming unaffected) and the hash is
    # still stamped/announced — degraded (no bytes) but never a pump break.
    rows = _rows()
    assert rows[-1]["event_type"] == "tool.result"
    assert rows[-1]["payload"]["artifact_hashes"] == [digest]

    # close() drains the failing task without propagating.
    await rec.close()
    assert artifacts_db.get_artifact(digest) is None


# ---------------------------------------------------------------------------
# subagent lane — screenshot ingested, raw data URI stripped from the log
# ---------------------------------------------------------------------------


def _subagent_tool_result_event(screenshot=None):
    ev = {"type": "subagent_tool_result", "agent_id": "parent-1",
          "name": "mcp__browser__browser_screenshot", "tool_use_id": "tu_1",
          "output": "1280x720", "is_error": False}
    if screenshot is not None:
        ev["screenshot"] = screenshot
    return ev


async def test_subagent_ingest_stamps_hash_and_strips_raw_uri(mock_db, artifacts_dir):
    _mk_session()
    rec = await _recorder()
    payload = b"subagent-screenshot-bytes"
    digest = artifact_ingest.content_hash(payload)

    await rec.on_event(_subagent_tool_result_event(_data_uri(payload, "image/jpeg")))

    rows = _rows()
    assert rows[-2]["event_type"] == "artifact.created"
    assert rows[-1]["event_type"] == "subagent.progress"

    progress = rows[-1]["payload"]
    assert progress["task_id"] == "parent-1"
    assert progress["kind"] == "tool_result"
    inner = progress["payload"]
    # The content address is carried; the raw base64 URI never hits the log.
    assert inner["artifact_hashes"] == [digest]
    assert "screenshot" not in inner
    assert inner["output"] == "1280x720"

    # close() drains the fire-and-forget store: bytes durable on disk.
    await rec.close()
    on_disk = artifacts_dir / digest[:2] / digest
    assert on_disk.exists()
    assert on_disk.read_bytes() == payload


async def test_subagent_without_screenshot_payload_unchanged(mock_db, artifacts_dir):
    _mk_session()
    rec = await _recorder()
    await rec.on_event(_subagent_tool_result_event(screenshot=None))
    assert _types() == ["subagent.progress"]
    inner = _rows()[-1]["payload"]["payload"]
    assert "screenshot" not in inner
    assert "artifact_hashes" not in inner
    await rec.close()


# ---------------------------------------------------------------------------
# unfenced path — between-turns drained events ingest too
# ---------------------------------------------------------------------------


async def test_drained_subagent_tool_result_ingests(mock_db, artifacts_dir):
    from app.core import turn_recorder as tr

    _mk_session()
    payload = b"drained-subagent-screenshot"
    digest = artifact_ingest.content_hash(payload)

    await tr.record_drained_event(
        SID, _subagent_tool_result_event(_data_uri(payload, "image/png"))
    )

    rows = _rows()
    assert [r["event_type"] for r in rows] == ["artifact.created", "subagent.progress"]
    inner = rows[-1]["payload"]["payload"]
    assert inner["artifact_hashes"] == [digest]
    assert "screenshot" not in inner

    # The unfenced store is awaited inline — bytes are durable immediately.
    on_disk = artifacts_dir / digest[:2] / digest
    assert on_disk.read_bytes() == payload
    assert artifacts_db.get_artifact(digest) is not None
