"""Cross-install upload delivery — lift at send, materialize at spawn (D8).

The invariants that matter:

* **Containment.** A prompt can name any absolute path; lifting one that is not
  under the project directory would turn "send a message" into "read an
  arbitrary file off my disk". That is refused BEFORE the file is opened.
* **No silent loss.** Every failure raises so the caller fails the send / fails
  the turn — never a prompt referencing an attachment that isn't there.
* **Round-trip.** What was lifted lands in ``<cwd>/uploads`` on the other side
  and the prompt's uploader-absolute paths become the local ones by exact-string
  replacement.
"""

import pytest

from app.core import upload_transfer
from app.db import upload_blobs as upload_blobs_db


@pytest.fixture
def project_dir(tmp_path):
    d = tmp_path / "proj"
    (d / "uploads").mkdir(parents=True)
    return d


@pytest.fixture
def project(project_dir):
    return {"id": "proj-1", "path": str(project_dir)}


def _meta(blob_id: str):
    """A blob's metadata row, asserted present — a None here is a test bug."""
    row = upload_blobs_db.get_blob(blob_id)
    assert row is not None, f"blob {blob_id} vanished"
    return row


def _stage(project_dir, name="notes.txt", content=b"hello"):
    p = project_dir / "uploads" / name
    p.write_bytes(content)
    return str(p)


# ═══ lift ════════════════════════════════════════════════════════════════════


def test_lift_stores_bytes_and_metadata(mock_db, project, project_dir):
    path = _stage(project_dir, content=b"payload-bytes")

    ids = upload_transfer.lift_paths_to_blobs([path], project, "sess-1", "pv2-1")

    assert len(ids) == 1
    meta = _meta(ids[0])
    assert meta["source_path"] == path
    assert meta["filename"] == "notes.txt"
    assert meta["size"] == len(b"payload-bytes")
    assert meta["session_id"] == "sess-1"
    assert meta["materialized_at"] is None
    assert upload_blobs_db.get_blob_data(ids[0]) == b"payload-bytes"


def test_lift_preserves_order(mock_db, project, project_dir):
    a = _stage(project_dir, "a.txt", b"a")
    b = _stage(project_dir, "b.txt", b"b")

    ids = upload_transfer.lift_paths_to_blobs([b, a], project, "sess-1")

    assert [_meta(i)["filename"] for i in ids] == ["b.txt", "a.txt"]


def test_lift_refuses_a_path_outside_the_project(mock_db, project, tmp_path):
    outside = tmp_path / "secrets.env"
    outside.write_bytes(b"API_KEY=hunter2")

    with pytest.raises(upload_transfer.UploadLiftError) as exc:
        upload_transfer.lift_paths_to_blobs([str(outside)], project, "sess-1")

    assert "outside the project" in str(exc.value)
    # Nothing was read, nothing was stored.
    assert upload_blobs_db.list_blobs_for_session("sess-1") == []


def test_lift_refuses_a_missing_file(mock_db, project, project_dir):
    with pytest.raises(upload_transfer.UploadLiftError) as exc:
        upload_transfer.lift_paths_to_blobs(
            [str(project_dir / "uploads" / "ghost.txt")], project, "sess-1"
        )
    assert "not found" in str(exc.value).lower()


def test_lift_refuses_an_oversized_file(mock_db, project, project_dir, monkeypatch):
    path = _stage(project_dir, content=b"x" * 200)
    monkeypatch.setattr(upload_transfer, "_max_upload_size", lambda: 100)

    with pytest.raises(upload_transfer.UploadLiftError) as exc:
        upload_transfer.lift_paths_to_blobs([path], project, "sess-1")
    assert "too large" in str(exc.value)


def test_lift_of_nothing_is_a_no_op(mock_db, project):
    assert upload_transfer.lift_paths_to_blobs([], project, "sess-1") == []


# ═══ materialize + rewrite (the spawn side) ══════════════════════════════════


def test_materialize_writes_files_clears_bytes_and_rewrites_prompt(
    mock_db, project, project_dir, tmp_path
):
    src = _stage(project_dir, "report.pdf", b"%PDF-1.4 fake")
    ids = upload_transfer.lift_paths_to_blobs([src], project, "sess-1")
    prompt = f"Summarize @{src} please"

    working_dir = tmp_path / "remote-checkout"
    working_dir.mkdir()
    mapping = upload_transfer.materialize_blobs_for_turn(ids, str(working_dir))

    landed = working_dir / "uploads" / "report.pdf"
    assert landed.read_bytes() == b"%PDF-1.4 fake"
    assert mapping == {src: str(landed)}

    # Bytes are dropped once delivered; the metadata row survives as the record.
    assert upload_blobs_db.get_blob_data(ids[0]) is None
    assert _meta(ids[0])["materialized_at"] is not None

    rewritten = upload_transfer.rewrite_prompt(prompt, mapping)
    assert rewritten == f"Summarize @{landed} please"
    assert src not in rewritten


def test_materialize_dedupes_colliding_filenames(mock_db, project, project_dir, tmp_path):
    src = _stage(project_dir, "dup.txt", b"one")
    ids = upload_transfer.lift_paths_to_blobs([src], project, "sess-1")

    working_dir = tmp_path / "wd"
    (working_dir / "uploads").mkdir(parents=True)
    (working_dir / "uploads" / "dup.txt").write_bytes(b"pre-existing")

    mapping = upload_transfer.materialize_blobs_for_turn(ids, str(working_dir))

    assert mapping[src].endswith("dup_1.txt")
    assert (working_dir / "uploads" / "dup.txt").read_bytes() == b"pre-existing"
    assert (working_dir / "uploads" / "dup_1.txt").read_bytes() == b"one"


def test_materialize_is_idempotent_when_the_file_is_still_there(
    mock_db, project, project_dir, tmp_path
):
    src = _stage(project_dir, "again.txt", b"again")
    ids = upload_transfer.lift_paths_to_blobs([src], project, "sess-1")
    working_dir = tmp_path / "wd"
    working_dir.mkdir()

    first = upload_transfer.materialize_blobs_for_turn(ids, str(working_dir))
    second = upload_transfer.materialize_blobs_for_turn(ids, str(working_dir))

    assert first == second


def test_materialize_raises_when_bytes_are_gone_and_file_is_missing(
    mock_db, project, project_dir, tmp_path
):
    src = _stage(project_dir, "vanished.txt", b"v")
    ids = upload_transfer.lift_paths_to_blobs([src], project, "sess-1")
    working_dir = tmp_path / "wd"
    working_dir.mkdir()
    upload_transfer.materialize_blobs_for_turn(ids, str(working_dir))
    (working_dir / "uploads" / "vanished.txt").unlink()

    with pytest.raises(upload_transfer.UploadLiftError) as exc:
        upload_transfer.materialize_blobs_for_turn(ids, str(working_dir))
    assert "no longer on disk" in str(exc.value)


def test_materialize_raises_for_a_purged_blob(mock_db, tmp_path):
    working_dir = tmp_path / "wd"
    working_dir.mkdir()
    with pytest.raises(upload_transfer.UploadLiftError):
        upload_transfer.materialize_blobs_for_turn(["ub-gone"], str(working_dir))


# ═══ rewrite_prompt ══════════════════════════════════════════════════════════


def test_rewrite_prefers_the_longest_source_path():
    """A path that is a prefix of another must never be partially rewritten."""
    mapping = {"/p/a.txt": "/w/a.txt", "/p/a.txt.bak": "/w/a.txt.bak"}
    out = upload_transfer.rewrite_prompt("see /p/a.txt.bak and /p/a.txt", mapping)
    assert out == "see /w/a.txt.bak and /w/a.txt"


def test_rewrite_without_a_mapping_is_a_passthrough():
    assert upload_transfer.rewrite_prompt("unchanged", {}) == "unchanged"


# ═══ sweeper ═════════════════════════════════════════════════════════════════


def test_purge_old_drops_stale_blob_rows(mock_db, project, project_dir):
    from datetime import datetime, timedelta, timezone

    from app.db.base import get_db

    stale = upload_transfer.lift_paths_to_blobs(
        [_stage(project_dir, "old.txt", b"o")], project, "sess-1"
    )[0]
    fresh = upload_transfer.lift_paths_to_blobs(
        [_stage(project_dir, "new.txt", b"n")], project, "sess-1"
    )[0]
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE upload_blobs SET created_at = %s WHERE id = %s", (old_ts, stale)
        )

    removed = upload_blobs_db.purge_old(older_than_days=7)

    assert removed == 1
    assert upload_blobs_db.get_blob(stale) is None
    assert upload_blobs_db.get_blob(fresh) is not None

def test_materialization_does_not_write_through_existing_hardlink(mock_db, project, project_dir, tmp_path):
    import os
    ids = upload_transfer.lift_paths_to_blobs([_stage(project_dir)], project, "sess-1")
    receiver = tmp_path / "receiver"
    (receiver / "uploads").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside marker")
    os.link(outside, receiver / "uploads" / "notes.txt")
    mapping = upload_transfer.materialize_blobs_for_turn(ids, str(receiver))
    assert outside.read_bytes() == b"outside marker"
    from pathlib import Path
    destination = Path(next(iter(mapping.values())))
    assert destination.name == "notes_1.txt"
    assert destination.read_bytes() == b"hello"
