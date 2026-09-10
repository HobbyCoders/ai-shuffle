"""Installation identity must not change during concurrent first use."""
import multiprocessing
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def identity(monkeypatch, tmp_path):
    from app.core import config

    monkeypatch.setattr(config, "_cached_persisted_instance_id", None)
    settings = config.Settings(
        _env_file=None, instance_id=None, service_name="test-install",
        data_dir=tmp_path, workspace_dir=tmp_path,
    )
    return config, settings


@pytest.mark.parametrize("contents", [b"", b"existing-lock-marker"])
def test_lock_acquisition_does_not_write_into_the_locked_range(identity, tmp_path, contents):
    config, _settings = identity
    path = tmp_path / ".instance_id.lock"
    path.write_bytes(contents)
    with config._lock_instance_id_file(path):
        assert path.stat().st_size == len(contents)
    assert path.read_bytes() == contents


@pytest.mark.parametrize("override", ["operator-id", "  operator-id  "])
def test_explicit_identity_bypasses_file_and_cache(identity, tmp_path, override):
    config, settings = identity
    (tmp_path / ".instance_id").write_text("persisted-id\n")
    config._cached_persisted_instance_id = "cached-id"
    settings.instance_id = override
    assert settings.effective_instance_id == "operator-id"
    assert (tmp_path / ".instance_id").read_text() == "persisted-id\n"
    assert config._cached_persisted_instance_id == "cached-id"
    assert not (tmp_path / ".instance_id.lock").exists()


@pytest.mark.parametrize("override", [None, "", "  "])
def test_existing_identity_is_preserved_without_writing(identity, tmp_path, override):
    config, settings = identity
    settings.instance_id = override
    (tmp_path / ".instance_id").write_text("  historical-service-aabbccdd\n")
    with patch.object(Path, "mkdir", side_effect=PermissionError("read-only volume")):
        assert settings.effective_instance_id == "historical-service-aabbccdd"
    assert (tmp_path / ".instance_id").read_text() == "  historical-service-aabbccdd\n"
    assert not (tmp_path / ".instance_id.lock").exists()


@pytest.mark.parametrize("existing", [None])
def test_new_identity_survives_cache_reset(identity, tmp_path, existing):
    config, settings = identity
    if existing is not None:
        (tmp_path / ".instance_id").write_text(existing)
    result = settings.effective_instance_id
    assert result.startswith("test-install-")
    assert len(result.removeprefix("test-install-")) == 8
    assert (tmp_path / ".instance_id").read_text() == result + "\n"
    config._cached_persisted_instance_id = None
    settings.service_name = "renamed-service"
    assert settings.effective_instance_id == result


@pytest.mark.parametrize("writable", [True, False])
def test_concurrent_first_use_returns_one_identity(identity, monkeypatch, writable):
    """Hold the first UUID until a second caller contends, without sleeps.

    Before the fix the second caller also generates a UUID. With the fix it
    attempts the mutex instead. Both schedules release the first caller and
    finish, so a passing test cannot merely avoid overlapping initialization.
    """
    config, settings = identity
    generating = threading.Event()
    contender = threading.Event()
    mutex = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            if generating.is_set():
                contender.set()
            mutex.acquire()

        def __exit__(self, *args):
            mutex.release()

    def next_uuid():
        if not generating.is_set():
            generating.set()
            assert contender.wait(10), "second caller never attempted initialization"
            return SimpleNamespace(hex="11111111" + "0" * 24)
        contender.set()
        return SimpleNamespace(hex="22222222" + "0" * 24)

    # raising=False deliberately lets this regression exercise the old code,
    # which has no mutex and must fail on the two different returned IDs.
    monkeypatch.setattr(config, "_persisted_instance_id_lock", ObservedLock(), raising=False)
    monkeypatch.setattr(uuid, "uuid4", next_uuid)
    if not writable:
        def deny_write(*args, **kwargs):
            raise PermissionError("read-only volume")
        monkeypatch.setattr(Path, "mkdir", deny_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(lambda: settings.effective_instance_id)
        assert generating.wait(10)
        second = executor.submit(lambda: settings.effective_instance_id)
        if not writable:
            for future in (first, second):
                with pytest.raises(RuntimeError, match="Cannot persist worker instance ID"):
                    future.result(timeout=15)
            assert config._cached_persisted_instance_id is None
            return
        values = [first.result(timeout=15), second.result(timeout=15)]
    assert values[0] == values[1] == settings.effective_instance_id
    if writable:
        assert (settings.data_dir / ".instance_id").read_text().strip() == values[0]


def _first_use_process(data_dir, barrier, results):
    """Force every process to observe the same absent/empty initial file."""
    from app.core import config

    path = Path(data_dir) / ".instance_id"
    original_exists, original_read = Path.exists, Path.read_text
    synchronized = False

    def synchronize():
        nonlocal synchronized
        if not synchronized:
            synchronized = True
            barrier.wait(timeout=15)

    def exists(self):
        present = original_exists(self)
        if self == path and not present:
            synchronize()
        return present

    def read_text(self, *args, **kwargs):
        try:
            value = original_read(self, *args, **kwargs)
        except FileNotFoundError:
            if self == path:
                synchronize()
            raise
        if self == path and not value.strip():
            synchronize()
        return value

    try:
        config._cached_persisted_instance_id = None
        with patch.object(Path, "exists", exists), patch.object(Path, "read_text", read_text):
            settings = config.Settings(_env_file=None, instance_id=None,
                data_dir=data_dir, workspace_dir=data_dir, service_name="test-install")
            result = settings.effective_instance_id
        results.put(("ok", result))
    except BaseException as exc:
        results.put(("error", repr(exc)))
        raise


@pytest.mark.parametrize("existing", [None])
def test_concurrent_processes_adopt_the_published_identity(tmp_path, existing):
    path = tmp_path / ".instance_id"
    if existing is not None:
        path.write_text(existing)
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(3), ctx.Queue()
    processes = [ctx.Process(target=_first_use_process, args=(str(tmp_path), barrier, results))
                 for _ in range(3)]
    try:
        for process in processes:
            process.start()
        values = [results.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert all(status == "ok" for status, _ in values), values
        assert len({value for _, value in values}) == 1, values
        assert path.read_text() == values[0][1] + "\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == [".instance_id", ".instance_id.lock"]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("existing", [None])
def test_publication_is_atomic(identity, tmp_path, monkeypatch, existing):
    config, settings = identity
    path = tmp_path / ".instance_id"
    if existing is not None:
        path.write_text(existing)
    replace = config.os.replace
    publications = []

    def observe_publication(source, destination):
        # Readers see the old file (or no file), never a partly written UUID.
        assert destination == path
        assert (path.read_text() if path.exists() else None) == existing
        payload = Path(source).read_text()
        assert payload.startswith("test-install-") and payload.endswith("\n")
        replace(source, destination)
        assert path.read_text() == payload
        publications.append(payload)

    monkeypatch.setattr(config.os, "replace", observe_publication)
    result = settings.effective_instance_id
    assert publications == [result + "\n"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [".instance_id", ".instance_id.lock"]


@pytest.mark.parametrize("operation", ["fsync", "replace"])
@pytest.mark.parametrize("existing", [None])
def test_failed_publication_leaves_original_and_cleans_temporary(
    identity, tmp_path, monkeypatch, caplog, operation, existing,
):
    config, settings = identity
    path = tmp_path / ".instance_id"
    if existing is not None:
        path.write_text(existing)

    def fail(*args):
        raise OSError("simulated interrupted publication")

    monkeypatch.setattr(config.os, operation, fail)
    with pytest.raises(RuntimeError, match="Cannot persist worker instance ID"):
        _ = settings.effective_instance_id
    assert config._cached_persisted_instance_id is None
    assert (path.read_text() if path.exists() else None) == existing
    assert not list(tmp_path.glob(".instance_id-*"))
    assert "using a process-local ID" not in caplog.text


@pytest.mark.parametrize("contents", ["", " \n"])
def test_empty_saved_identity_is_not_replaced(identity, tmp_path, contents):
    config, settings = identity
    path = tmp_path / ".instance_id"
    path.write_text(contents)
    with pytest.raises(RuntimeError, match="Saved worker instance ID is empty"):
        _ = settings.effective_instance_id
    assert path.read_text() == contents
    assert config._cached_persisted_instance_id is None


def test_unreadable_saved_identity_is_not_replaced(identity, tmp_path, monkeypatch):
    config, settings = identity
    path = tmp_path / ".instance_id"
    path.write_text("original-worker")
    original_read = Path.read_text
    def denied_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("volume inaccessible")
        return original_read(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", denied_read)
    with pytest.raises(RuntimeError, match="Cannot read saved worker instance ID"):
        _ = settings.effective_instance_id
    assert config._cached_persisted_instance_id is None
    assert original_read(path) == "original-worker"


def test_cached_identity_is_stable_even_if_file_disappears(identity, tmp_path):
    config, settings = identity
    result = settings.effective_instance_id
    (tmp_path / ".instance_id").unlink()
    assert settings.effective_instance_id == result
    assert not (tmp_path / ".instance_id").exists()
