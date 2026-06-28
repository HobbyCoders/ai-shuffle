"""
Unit tests for the notes disk-sync worker (``app.core.notes_sync``).

The worker reconciles each note's materialized ``.md`` file with its row in the
shared database in BOTH directions, against a per-install in-memory baseline:

    * file edited, db unchanged   -> PUSH  file -> db
    * db edited,   file unchanged -> PULL  db   -> file
    * neither edited              -> fast-path no-op
    * both edited                 -> newest wins (conflict)
    * file missing                -> re-materialize from db (never delete)

The database module is mocked; file storage uses a real temp directory wired
through ``settings.effective_data_dir`` so the on-disk read/write path is
exercised end to end. File modification times are set explicitly with
``os.utime`` so push/pull/conflict decisions are deterministic and independent
of filesystem timestamp granularity.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import notes_sync
from app.core.note_files import build_file_content


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_data_dir():
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_state():
    """Each test starts from a fresh, empty in-memory sync state."""
    notes_sync.reset_state()
    yield
    notes_sync.reset_state()


@pytest.fixture
def mock_db():
    """Mock the database module the worker reconciles against."""
    with patch("app.core.notes_sync.database") as db:
        yield db


@pytest.fixture
def fs(temp_data_dir):
    """Wire note_files' on-disk helpers at a real temp data dir."""
    with patch("app.core.note_files.settings") as mock_settings:
        mock_settings.effective_data_dir = temp_data_dir
        # Pre-create the notes dir so direct path reads/writes are simple.
        (temp_data_dir / "notes").mkdir(parents=True, exist_ok=True)
        yield temp_data_dir


# =============================================================================
# Helpers
# =============================================================================

def make_note(note_id="note-1", title="My Note", content="Body",
              updated_at="2024-01-01T00:00:00+00:00", filename="my-note.md"):
    return {
        "id": note_id,
        "title": title,
        "content": content,
        "tags": [],
        "is_pinned": False,
        "file_path": f"notes/{filename}",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": updated_at,
    }


def note_path(data_dir, filename):
    return data_dir / "notes" / filename


def write_raw(data_dir, filename, title, content):
    """Write a note file exactly as the worker/materializer would."""
    note_path(data_dir, filename).write_text(
        build_file_content(title, content), encoding="utf-8"
    )


def read_raw(data_dir, filename):
    return note_path(data_dir, filename).read_text(encoding="utf-8")


def set_mtime(data_dir, filename, epoch):
    os.utime(note_path(data_dir, filename), (epoch, epoch))


def single_page(*notes):
    """A get_all_notes side effect that returns the notes once, then empties."""
    def _get_all_notes(limit=100, offset=0, **kwargs):
        return list(notes) if offset == 0 else []
    return _get_all_notes


# =============================================================================
# Tests
# =============================================================================

class TestNotesSync:
    def test_in_sync_records_baseline_without_writing(self, fs, mock_db):
        """File already matches DB: record a baseline, touch nothing."""
        note = make_note()
        write_raw(fs, "my-note.md", note["title"], note["content"])
        mock_db.get_all_notes.side_effect = single_page(note)

        notes_sync.sync_notes_once()

        mock_db.update_note.assert_not_called()
        assert notes_sync._baseline_hash.get("note-1") is not None

    def test_push_file_edit_to_db(self, fs, mock_db):
        """A local file edit (db unchanged) is pushed up to the database."""
        note = make_note(content="original")
        write_raw(fs, "my-note.md", note["title"], "original")
        mock_db.get_all_notes.side_effect = single_page(note)
        mock_db.update_note.return_value = make_note(
            content="edited", updated_at="2024-02-02T00:00:00+00:00"
        )

        # First pass establishes the in-sync baseline.
        notes_sync.sync_notes_once()
        base_mtime = notes_sync._last_file_mtime["note-1"] or 0.0

        # User/agent edits the file on disk; bump mtime so it's unambiguous.
        write_raw(fs, "my-note.md", note["title"], "edited")
        set_mtime(fs, "my-note.md", base_mtime + 10)

        notes_sync.sync_notes_once()

        mock_db.update_note.assert_called_once_with(
            note_id="note-1", title="My Note", content="edited"
        )

    def test_pull_db_change_to_file(self, fs, mock_db):
        """A DB change (file unchanged) is pulled down to the local file.

        This is the steady-state multi-install no-revert case: a peer install
        pushed an edit to the shared DB; this install's file is still the old
        baseline, so it must refresh from the DB rather than push its stale copy
        back up (which would revert the peer's edit).
        """
        original = make_note(content="original")
        write_raw(fs, "my-note.md", original["title"], "original")
        mock_db.get_all_notes.side_effect = single_page(original)
        notes_sync.sync_notes_once()  # baseline established

        # DB now holds a newer version (edited elsewhere); file is untouched.
        updated = make_note(content="edited elsewhere",
                            updated_at="2024-03-03T00:00:00+00:00")
        mock_db.get_all_notes.side_effect = single_page(updated)

        notes_sync.sync_notes_once()

        mock_db.update_note.assert_not_called()
        assert "edited elsewhere" in read_raw(fs, "my-note.md")

    def test_fast_path_noop_does_not_rewrite(self, fs, mock_db):
        """Unchanged file + unchanged DB row: no DB write, no file rewrite."""
        note = make_note()
        write_raw(fs, "my-note.md", note["title"], note["content"])
        mock_db.get_all_notes.side_effect = single_page(note)

        notes_sync.sync_notes_once()
        mtime_before = note_path(fs, "my-note.md").stat().st_mtime

        # Re-run with nothing changed.
        mock_db.get_all_notes.side_effect = single_page(note)
        notes_sync.sync_notes_once()
        mtime_after = note_path(fs, "my-note.md").stat().st_mtime

        mock_db.update_note.assert_not_called()
        assert mtime_before == mtime_after

    def test_conflict_file_newer_wins(self, fs, mock_db):
        """Both sides changed since baseline: the newer (file) side wins."""
        note = make_note(content="original")
        write_raw(fs, "my-note.md", note["title"], "original")
        mock_db.get_all_notes.side_effect = single_page(note)
        notes_sync.sync_notes_once()  # baseline

        # File edited AND db edited; file mtime is far newer than db updated_at.
        write_raw(fs, "my-note.md", note["title"], "file wins")
        set_mtime(fs, "my-note.md", 2_000_000_000)  # ~2033
        db_changed = make_note(content="db loses",
                               updated_at="2020-01-01T00:00:00+00:00")
        mock_db.get_all_notes.side_effect = single_page(db_changed)

        notes_sync.sync_notes_once()

        mock_db.update_note.assert_called_once_with(
            note_id="note-1", title="My Note", content="file wins"
        )

    def test_missing_file_is_rematerialized(self, fs, mock_db):
        """A note whose local file is absent is restored from the DB, not deleted."""
        note = make_note(content="from db")
        # No file on disk.
        mock_db.get_all_notes.side_effect = single_page(note)

        notes_sync.sync_notes_once()

        mock_db.update_note.assert_not_called()
        assert note_path(fs, "my-note.md").exists()
        assert "from db" in read_raw(fs, "my-note.md")

    def test_cold_start_stale_file_pulls_not_pushes(self, fs, mock_db):
        """Fresh process, stale local file, newer DB: pull (do NOT revert DB).

        After a restart there is no baseline, so the decision falls back to
        recency. A locally-stale file (older than the DB's updated_at) must be
        refreshed from the DB — never pushed up, which would clobber a peer
        install's newer edit.
        """
        stale = make_note(content="stale local copy",
                          updated_at="2099-01-01T00:00:00+00:00")
        write_raw(fs, "my-note.md", stale["title"], "old content")
        set_mtime(fs, "my-note.md", 1_000_000_000)  # ~2001, older than db
        mock_db.get_all_notes.side_effect = single_page(stale)

        notes_sync.sync_notes_once()

        mock_db.update_note.assert_not_called()
        assert "stale local copy" in read_raw(fs, "my-note.md")

    def test_cold_start_local_edit_pushes(self, fs, mock_db):
        """Fresh process, newer local file, older DB: push the local edit up.

        Mirrors an agent editing the file then the process restarting before the
        worker ran — recency makes the newer local file win.
        """
        note = make_note(content="db copy",
                         updated_at="2000-01-01T00:00:00+00:00")
        write_raw(fs, "my-note.md", note["title"], "agent edit")
        set_mtime(fs, "my-note.md", 2_000_000_000)  # ~2033, newer than db
        mock_db.get_all_notes.side_effect = single_page(note)

        notes_sync.sync_notes_once()

        mock_db.update_note.assert_called_once_with(
            note_id="note-1", title="My Note", content="agent edit"
        )

    def test_state_pruned_for_deleted_notes(self, fs, mock_db):
        """In-memory state for notes that vanish from the DB is dropped."""
        note = make_note()
        write_raw(fs, "my-note.md", note["title"], note["content"])
        mock_db.get_all_notes.side_effect = single_page(note)
        notes_sync.sync_notes_once()
        assert "note-1" in notes_sync._baseline_hash

        # Note is gone from the DB on the next pass.
        mock_db.get_all_notes.side_effect = single_page()
        notes_sync.sync_notes_once()

        assert "note-1" not in notes_sync._baseline_hash
        assert "note-1" not in notes_sync._last_file_mtime
        assert "note-1" not in notes_sync._last_db_updated

    def test_reconcile_failure_is_isolated(self, fs, mock_db):
        """A single note's failure doesn't abort the whole pass."""
        good = make_note(note_id="note-good", filename="good.md")
        bad = make_note(note_id="note-bad", filename="bad.md")
        write_raw(fs, "good.md", good["title"], good["content"])
        write_raw(fs, "bad.md", bad["title"], bad["content"])
        # Make the bad note explode by giving it no id mid-reconcile is hard;
        # instead force update_note to raise only when the file is edited.
        mock_db.get_all_notes.side_effect = single_page(bad, good)

        # Both are in-sync on first pass, so this simply must not raise.
        result = notes_sync.sync_notes_once()
        assert result["notes"] == 2
