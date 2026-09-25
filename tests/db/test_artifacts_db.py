"""Unit tests for app/db/artifacts.py — content-addressed artifact registry."""

from app.db import artifacts as ar


def test_upsert_idempotent_first_metadata_wins(mock_db):
    first = ar.upsert_artifact("h1", 100, "text/plain", "a.txt", "s1")
    assert first["hash"] == "h1"
    assert first["size"] == 100
    # Same hash, different metadata — no new row, original metadata preserved.
    second = ar.upsert_artifact("h1", 999, "image/png", "b.png", "s2")
    assert second["size"] == 100
    assert second["mime"] == "text/plain"
    assert second["filename"] == "a.txt"
    assert second["created_by_session"] == "s1"


def test_get_missing_returns_none(mock_db):
    assert ar.get_artifact("does-not-exist") is None


def test_get_returns_stored_row(mock_db):
    ar.upsert_artifact("h2", 42, None, None, None)
    row = ar.get_artifact("h2")
    assert row["hash"] == "h2"
    assert row["size"] == 42
    assert row["mime"] is None
