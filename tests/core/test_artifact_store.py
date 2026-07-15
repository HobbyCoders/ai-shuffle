"""Unit tests for app/core/artifact_store.py — content-addressed byte store."""

import hashlib

import pytest

from app.core import artifact_store


@pytest.fixture
def artifacts_dir(tmp_path, monkeypatch):
    d = tmp_path / "artifacts"
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", d)
    return d


def test_store_writes_sharded_file_and_db_row(mock_db, artifacts_dir):
    data = b"hello world"
    digest = hashlib.sha256(data).hexdigest()

    row = artifact_store.store_bytes(data, mime="text/plain", filename="a.txt",
                                     created_by_session="s1")

    assert row["hash"] == digest
    assert row["size"] == len(data)
    assert row["mime"] == "text/plain"
    assert row["filename"] == "a.txt"
    assert row["created_by_session"] == "s1"

    on_disk = artifacts_dir / digest[:2] / digest
    assert on_disk.exists()
    assert on_disk.read_bytes() == data


def test_second_identical_store_is_noop_same_row(mock_db, artifacts_dir):
    data = b"dedupe me"
    first = artifact_store.store_bytes(data, mime="text/plain", filename="a.txt")
    # First-writer metadata wins even if a later call differs.
    second = artifact_store.store_bytes(data, mime="image/png", filename="b.png")
    assert second["hash"] == first["hash"]
    assert second["mime"] == "text/plain"
    assert second["filename"] == "a.txt"
    # No leftover temp files in the shard dir.
    shard = artifacts_dir / first["hash"][:2]
    assert [p.name for p in shard.iterdir()] == [first["hash"]]


def test_artifact_path_present(mock_db, artifacts_dir):
    data = b"payload"
    row = artifact_store.store_bytes(data)
    path = artifact_store.artifact_path(row["hash"])
    assert path is not None
    assert path.read_bytes() == data


def test_artifact_path_rejects_bad_hash(artifacts_dir):
    assert artifact_store.artifact_path("not-hex") is None
    assert artifact_store.artifact_path("abc") is None  # too short
    assert artifact_store.artifact_path("Z" * 64) is None  # non-hex
    # Well-formed but absent on disk.
    assert artifact_store.artifact_path("a" * 64) is None


def test_hash_re_rejects_trailing_newline():
    """The ``\\Z`` anchor (not ``$``) must reject a trailing newline: with ``$``
    a 64-hex hash followed by ``\\n`` would still match (``$`` matches before a
    final newline), letting a smuggled newline through the content-hash gate."""
    valid = "a" * 64
    assert artifact_store._HASH_RE.match(valid) is not None
    assert artifact_store._HASH_RE.match(valid + "\n") is None
    # And artifact_path (which uses the regex) rejects the newline form too.
    assert artifact_store.artifact_path(valid + "\n") is None
