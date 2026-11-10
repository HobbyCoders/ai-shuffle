"""The copy-on-write database copy an update takes (docker/container_runtime.py).

The state machine runs everywhere with the block clone replaced by a plain
copy. One test drives a real PostgreSQL through copy, migrate and put-back; it
needs PostgreSQL 16 and a filesystem that shares blocks (btrfs, XFS reflink),
and skips elsewhere.
"""
import importlib.util
import json
import os
import shutil
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("fcntl", reason="The container supervisor is Linux-only")
_spec = importlib.util.spec_from_file_location(
    "database_clone_runtime", Path(__file__).resolve().parents[2] / "docker" / "container_runtime.py",
)
assert _spec is not None and _spec.loader is not None
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)
SNAPSHOT = "ai_shuffle_rollback_" + "c" * 12
FAILED = "ai_shuffle_failed_" + "c" * 12


def _exchange_works(directory):
    first, second = directory / "exchange-a", directory / "exchange-b"
    first.mkdir()
    second.mkdir()
    try:
        runtime._exchange(first, second)
        return True
    except (OSError, AttributeError):
        return False
    finally:
        first.rmdir()
        second.rmdir()


def _copy_instead_of_clone(source, target, mode):
    with open(source, "rb") as reader:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as writer:
            shutil.copyfileobj(reader, writer)


def _cluster(directory, content):
    """A directory shaped like a PostgreSQL 16 cluster."""
    (directory / "global").mkdir(parents=True)
    (directory / "base" / "1").mkdir(parents=True)
    (directory / "PG_VERSION").write_text(runtime.MAJOR + "\n")
    (directory / "global" / "pg_control").write_bytes(b"control")
    (directory / "base" / "1" / "relation").write_text(content)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A Database over a fake cluster: the server is 'running' but nothing is
    started or stopped, and blocks are copied instead of shared."""
    if not _exchange_works(tmp_path):
        pytest.skip("this filesystem cannot swap two directories in one step")
    monkeypatch.setattr(runtime, "_clone_file", _copy_instead_of_clone)
    database = runtime.Database(SimpleNamespace(effective_data_dir=tmp_path))
    database.pg.mkdir()
    _cluster(database.data, "before the update")
    database.process = Mock()
    database.process.poll.return_value = None
    database.clone_copies = True
    database.admin_script = Mock()
    database.query = Mock(return_value="")
    database._stop_server = Mock(return_value=True)
    database._start_server = Mock()
    database._shut_down = Mock(return_value=True)
    database._discard_tree = lambda path: shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
    return database


def _relation(database):
    return (database.data / "base" / "1" / "relation").read_text()


def test_a_clone_is_taken_only_on_an_image_whose_own_supervisor_can_put_it_back(db, monkeypatch):
    assert db._clone_supported()
    db.clone_copies = False  # an image built before database-clone-v1
    assert not db._clone_supported()
    db.clone_copies = True
    monkeypatch.setenv("AI_SHUFFLE_PERF_DATABASE_CLONE", "off")
    assert not db._clone_supported()
    monkeypatch.delenv("AI_SHUFFLE_PERF_DATABASE_CLONE")
    db.process = None  # a database this supervisor does not run
    assert not db._clone_supported()


def test_the_image_declares_the_capability_the_supervisor_looks_for():
    contract_spec = importlib.util.spec_from_file_location(
        "clone_contract", Path(__file__).resolve().parents[2] / "docker" / "update_contract.py")
    contract = importlib.util.module_from_spec(contract_spec)
    contract_spec.loader.exec_module(contract)
    assert runtime.CLONE_CAPABILITY in contract.PROVIDES
    assert runtime.CLONE_CAPABILITY not in contract.REQUIRES, "older images must keep taking updates"


def test_a_volume_that_cannot_share_blocks_copies_the_usual_way(db, monkeypatch):
    def unsupported(source, target, mode):
        raise OSError(95, "Operation not supported")
    monkeypatch.setattr(runtime, "_clone_file", unsupported)
    assert not db._clone_supported()
    db.query = Mock(side_effect=["1024", "1024"])
    db.snapshot(SNAPSHOT)
    assert any("STRATEGY FILE_COPY" in call.args[0] for call in db.admin_script.call_args_list)
    db._stop_server.assert_not_called()
    assert not (db.pg / runtime.CLONE_DIRECTORY / SNAPSHOT).exists()


def test_copy_then_put_back_restores_the_cluster_and_keeps_what_the_update_left(db):
    free, need = db.snapshot_space()
    assert need == runtime.SNAPSHOT_MARGIN, "a clone needs no room for a second copy"
    db.snapshot(SNAPSHOT)
    clone = db.pg / runtime.CLONE_DIRECTORY / SNAPSHOT
    assert json.loads((clone / runtime.CLONE_MARKER).read_text())["name"] == SNAPSHOT
    assert not (db.data / runtime.CLONE_MARKER).exists()
    assert db.update_databases() == [SNAPSHOT]
    db._stop_server.assert_called_once()
    db._start_server.assert_called_once()
    assert not any("STRATEGY" in call.args[0] for call in db.admin_script.call_args_list)

    (db.data / "base" / "1" / "relation").write_text("migrated by the update")
    db.restore_snapshot(SNAPSHOT, FAILED)
    assert _relation(db) == "before the update"
    assert not (db.data / runtime.CLONE_MARKER).exists()
    kept = db.pg / runtime.CLONE_DIRECTORY / FAILED
    assert (kept / "base" / "1" / "relation").read_text() == "migrated by the update"
    assert db.update_databases() == [FAILED]

    db.restore_snapshot(SNAPSHOT, FAILED)  # repeated after an interruption: nothing more to do
    assert _relation(db) == "before the update"
    db.drop_update_database(FAILED)
    assert db.update_databases() == []


@pytest.mark.parametrize("fail_at", ["set-aside rename", "marker removal"])
def test_an_interrupted_put_back_finishes_when_repeated(db, monkeypatch, fail_at):
    db.snapshot(SNAPSHOT)
    (db.data / "base" / "1" / "relation").write_text("migrated by the update")
    rename, unlink = os.rename, Path.unlink

    def failing_rename(source, target):
        if str(target).endswith(FAILED):
            raise OSError("the supervisor stopped here")
        return rename(source, target)

    def failing_unlink(path, *args, **kwargs):
        if path.name == runtime.CLONE_MARKER:
            raise OSError("the supervisor stopped here")
        return unlink(path, *args, **kwargs)

    if fail_at == "set-aside rename":
        monkeypatch.setattr(runtime.os, "rename", failing_rename)
    else:
        monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(OSError):
        db.restore_snapshot(SNAPSHOT, FAILED)
    monkeypatch.undo()
    # The swap happened: the cluster is the copy and says so.
    assert _relation(db) == "before the update"
    assert (db.data / runtime.CLONE_MARKER).is_file()

    db._discard_tree = lambda path: shutil.rmtree(path)
    db.restore_snapshot(SNAPSHOT, FAILED)
    assert _relation(db) == "before the update"
    assert not (db.data / runtime.CLONE_MARKER).exists()
    assert db.update_databases() == [FAILED]
    assert (db.pg / runtime.CLONE_DIRECTORY / FAILED / "base" / "1" / "relation").read_text() == "migrated by the update"


def test_a_new_copy_forgets_an_earlier_attempt_of_the_same_update(db):
    """A retried update reuses its copy's name. What an earlier put-back set
    aside must not make the next put-back believe it already ran."""
    db.snapshot(SNAPSHOT)
    (db.data / "base" / "1" / "relation").write_text("first attempt")
    db.restore_snapshot(SNAPSHOT, FAILED)  # leaves FAILED until it is dropped
    db.snapshot(SNAPSHOT)
    assert db.update_databases() == [SNAPSHOT]
    (db.data / "base" / "1" / "relation").write_text("second attempt")
    db.restore_snapshot(SNAPSHOT, FAILED)
    assert _relation(db) == "before the update"


def test_a_damaged_copy_is_never_swapped_in(db):
    db.snapshot(SNAPSHOT)
    (db.pg / runtime.CLONE_DIRECTORY / SNAPSHOT / "global" / "pg_control").unlink()
    (db.data / "base" / "1" / "relation").write_text("migrated by the update")
    with pytest.raises(runtime.RuntimeFailure, match="damaged"):
        db.restore_snapshot(SNAPSHOT, FAILED)
    assert _relation(db) == "migrated by the update"


def test_an_unfinished_clone_is_not_a_copy(db, monkeypatch):
    """Stopped half way through the clone: nothing under the copy's name, and
    the next start clears the remains."""
    def stop_half_way(source, target):
        os.mkdir(target)
        raise OSError("no space left")
    monkeypatch.setattr(runtime, "_clone_tree", stop_half_way)
    db.query = Mock(side_effect=["1024", "1024"])
    db.snapshot(SNAPSHOT)  # falls back to the SQL copy
    db._start_server.assert_called_once()
    assert any("STRATEGY FILE_COPY" in call.args[0] for call in db.admin_script.call_args_list)
    copies = db.pg / runtime.CLONE_DIRECTORY
    assert list(copies.iterdir()) == []

    (copies / f".{SNAPSHOT}.partial").mkdir()  # left by a killed supervisor
    db.query = Mock(return_value="")
    assert db.update_databases() == []
    assert list(copies.iterdir()) == []


def test_a_cluster_that_did_not_stop_cleanly_is_not_cloned(db):
    db._shut_down = Mock(return_value=False)
    db.query = Mock(side_effect=["1024", "1024"])
    db.snapshot(SNAPSHOT)
    db._start_server.assert_called_once()
    assert not (db.pg / runtime.CLONE_DIRECTORY / SNAPSHOT).exists()
    assert any("STRATEGY FILE_COPY" in call.args[0] for call in db.admin_script.call_args_list)


@pytest.mark.parametrize("entry", ["symlink", "hard link"])
def test_links_inside_the_cluster_stop_the_clone(tmp_path, monkeypatch, entry):
    monkeypatch.setattr(runtime, "_clone_file", _copy_instead_of_clone)
    source = tmp_path / "data"
    _cluster(source, "rows")
    outside = tmp_path / "outside"
    outside.write_text("not the database")
    if entry == "symlink":
        (source / "base" / "link").symlink_to(outside)
    else:
        os.link(outside, source / "base" / "link")
    with pytest.raises(runtime.RuntimeFailure, match="cannot be cloned"):
        runtime._clone_tree(str(source), str(tmp_path / "copy"))


def test_the_clone_keeps_the_tree_and_its_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_clone_file", _copy_instead_of_clone)
    source = tmp_path / "data"
    _cluster(source, "rows")
    os.chmod(source / "base" / "1" / "relation", 0o600)
    os.chmod(source / "base", 0o700)
    files, size = runtime._clone_tree(str(source), str(tmp_path / "copy"))
    assert files == 3 and size == sum(p.stat().st_size for p in source.rglob("*") if p.is_file())
    assert (tmp_path / "copy" / "base" / "1" / "relation").read_text() == "rows"
    assert (tmp_path / "copy" / "base" / "1" / "relation").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "copy" / "base").stat().st_mode & 0o777 == 0o700


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_real_postgresql_copy_migrate_and_put_back(tmp_path):
    """The producing path: a real PostgreSQL 16 cluster is cloned, changed as a
    migration would change it, and put back, also across a supervisor restart."""
    if shutil.which("initdb") is None and not Path(f"/usr/lib/postgresql/{runtime.MAJOR}/bin/initdb").exists():
        pytest.skip("PostgreSQL is not installed")
    probe = tmp_path / "probe"
    probe.write_bytes(b"\0" * 4096)
    try:
        runtime._clone_file(probe, tmp_path / "probe-copy", 0o600)
    except OSError:
        pytest.skip("this filesystem cannot share file blocks")
    root = tmp_path / "data"
    root.mkdir()
    port = _free_port()
    db = runtime.Database(SimpleNamespace(effective_data_dir=root), port=port, disposable=True)
    db.open()

    def sql(text):
        return db.command(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1"], env=runtime.client_env(db.url),
                          text=text, timeout=120).strip()
    try:
        db.clone_copies = True
        if not db._clone_supported():
            pytest.skip("this filesystem cannot swap two directories in one step")
        sql("CREATE TABLE rows AS SELECT g AS id, md5(g::text) AS pad FROM generate_series(1, 20000) g; "
            "CREATE TABLE version(v text); INSERT INTO version VALUES ('before')")
        db.snapshot(SNAPSHOT)
        assert db.update_databases() == [SNAPSHOT]
        assert sql("SELECT v FROM version") == "before", "the server is back after the clone, on the same address"
        sql("UPDATE version SET v = 'migrated'; DROP TABLE rows")

        db.close()  # the container restarts during the first start
        db = runtime.Database(SimpleNamespace(effective_data_dir=root), port=port, disposable=True)
        db.open()
        assert db.update_databases() == [SNAPSHOT]
        db.restore_snapshot(SNAPSHOT, FAILED)
        assert sql("SELECT v FROM version") == "before"
        assert sql("SELECT count(*) FROM rows") == "20000"
        db.drop_update_database(FAILED)
        assert db.update_databases() == []
        deadline = time.monotonic() + 30
        copies = db.pg / runtime.CLONE_DIRECTORY
        while any(copies.iterdir()) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert list(copies.iterdir()) == [], "the set-aside cluster is removed in the background"
    finally:
        db.close()
