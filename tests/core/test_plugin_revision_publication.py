"""Real PluginService/native-loader publication and concurrency regressions."""
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core import plugin_install
from fluid.context.plugins import load_plugin
from tests.core.test_plugin_source_updates import runtime_text, setup, sources, write  # noqa: F401


def test_failed_activation_and_failed_removal_keep_original_tree(sources, monkeypatch):
    svc, _, skill, info = setup(sources, "directory")
    original_record = svc._read_json_file(svc.INSTALLED_FILE, {})
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    remove = plugin_install._force_rmtree

    def sharing_violation(path):
        if Path(path) == Path(info.install_path):
            raise PermissionError("Injected Windows sharing violation")
        return remove(path)

    def activation_failure(*args):
        raise OSError("Injected activation failure")

    monkeypatch.setattr(plugin_install, "_force_rmtree", sharing_violation)
    monkeypatch.setattr(svc, "enable_plugin", activation_failure)
    with pytest.raises(OSError):
        svc.update_plugin(info.id)
    copies = list(svc.CACHE_DIR.rglob("SKILL.md"))
    assert any("First revision" in path.read_text() for path in copies), "Only good copy was deleted"
    assert svc._read_json_file(svc.INSTALLED_FILE, {}) == original_record
    assert "First revision" in runtime_text(svc)


def test_failed_earlier_update_cannot_undo_later_completed_update(sources, monkeypatch):
    svc, _, skill, info = setup(sources, "directory")
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    paused, resume, completed_b = threading.Event(), threading.Event(), threading.Event()
    enable = svc.enable_plugin
    outcomes = {}

    def activate(*args):
        if threading.current_thread().name == "update-a":
            paused.set()
            assert resume.wait(10), "Test did not release update A"
            raise OSError("Injected A activation failure")
        return enable(*args)

    def update(label):
        try:
            svc.update_plugin(info.id)
            outcomes[label] = runtime_text(svc)
        except Exception as exc:
            outcomes[label] = exc
        finally:
            if label == "b":
                completed_b.set()

    monkeypatch.setattr(svc, "enable_plugin", activate)
    a = threading.Thread(target=update, args=("a",), name="update-a")
    b = threading.Thread(target=update, args=("b",), name="update-b")
    a.start()
    try:
        assert paused.wait(10), "A never reached activation"
        write(skill, skill.read_text().replace("Second revision", "Third revision"))
        b.start()
        # On the old code B completes while A is paused. Under serialization B
        # waits; releasing A must then let B consume the latest source content.
        completed_b.wait(.3)
    finally:
        resume.set()
        a.join(10)
        if b.ident is not None:
            b.join(10)
    assert not a.is_alive() and not b.is_alive()
    assert isinstance(outcomes["a"], OSError)
    assert isinstance(outcomes["b"], str) and "Third revision" in outcomes["b"]
    assert "Third revision" in runtime_text(svc), "A rolled back the completed B revision"


def test_native_reader_sees_complete_plugin_during_publication(sources, monkeypatch):
    svc, _, skill, info = setup(sources, "directory")
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    replace, observations = Path.replace, []

    def observe(path, destination):
        result = replace(path, destination)
        if path == Path(info.install_path) or path.name == "candidate":
            plugin = load_plugin("sample", "sample", home=svc.PLUGINS_DIR)
            observations.append(None if not plugin or not plugin.skills else Path(plugin.skills[0].path).read_text())
        return result

    monkeypatch.setattr(Path, "replace", observe)
    svc.update_plugin(info.id)
    assert observations, "Publication observation hook did not run"
    assert all(value is not None and "First revision" in value for value in observations), observations
    assert "Second revision" in runtime_text(svc)


@pytest.mark.parametrize("damaged", [False, True])
def test_same_revision_reuses_good_tree_and_repairs_corrupt_key_at_new_path(sources, damaged):
    svc, _, _, info = setup(sources, "directory")
    before = Path(info.install_path)
    identity = before.stat().st_ino
    if damaged:
        write(before / "skills/sample/SKILL.md", "Corrupted installed file")
    result = svc.update_plugin(info.id)
    if damaged:
        assert Path(result.install_path) != before
        assert (before / "skills/sample/SKILL.md").read_text() == "Corrupted installed file"
    else:
        assert Path(result.install_path) == before
        assert before.stat().st_ino == identity
    assert "First revision" in runtime_text(svc)


def test_missing_revision_is_repaired_even_when_key_is_unchanged(sources):
    svc, _, _, info = setup(sources, "directory")
    plugin_install._force_rmtree(Path(info.install_path))
    report = svc.get_update_report(refresh_sources=True)
    assert [update.plugin_id for update in report["updates"]] == [info.id]
    svc.update_all_plugins()
    assert "First revision" in runtime_text(svc)


def test_retired_trees_are_not_loaded_and_sharing_violations_are_retried(sources, monkeypatch):
    svc, _, skill, info = setup(sources, "directory")
    old = Path(info.install_path)
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    updated = svc.update_plugin(info.id)
    assert Path(updated.install_path) != old and old.is_dir()
    assert "Second revision" in runtime_text(svc)
    remove, attempts = plugin_install._force_rmtree, []

    def sharing_violation(path):
        if path == old:
            attempts.append(path)
            raise PermissionError("Injected loaded Windows tree")
        return remove(path)

    monkeypatch.setattr(plugin_install, "_CACHE_RETENTION_SECONDS", 0)
    with monkeypatch.context() as patch:
        patch.setattr(plugin_install, "_force_rmtree", sharing_violation)
        svc.update_plugin(info.id)
    assert attempts and old.is_dir()
    assert "Second revision" in runtime_text(svc)
    svc.update_plugin(info.id)
    assert not old.exists()
    assert "Second revision" in runtime_text(svc)


def test_legacy_fallback_never_loads_managed_or_retired_trees(sources):
    import hashlib

    svc, _, _, info = setup(sources, "directory")
    base = Path(info.install_path).parent
    legacy = base / "1.0.0"
    plugin_install.shutil.copytree(info.install_path, legacy)
    markers = base / ".retired"
    markers.mkdir(exist_ok=True)
    (markers / hashlib.sha256(legacy.name.encode()).hexdigest()).touch()
    # No record: neither an orphan managed revision nor a retired legacy copy
    # may accidentally become the latest-version fallback.
    svc._write_json_file(svc.INSTALLED_FILE, {"plugins": {}})
    assert load_plugin("sample", "sample", home=svc.PLUGINS_DIR) is None


_CHILD = """
import sys, time, os
from pathlib import Path
home = Path(sys.argv[1])
Path.home = classmethod(lambda cls: home.parent)
from app.core import plugin_service
plugin_service._get_fluid_dir = lambda: home
svc = plugin_service.PluginService()
"""


def _child(svc, code, *paths):
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD + code, str(svc.FLUID_DIR), *map(str, paths)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_for_file(path, child):
    deadline = time.monotonic() + 10
    while not path.exists():
        assert child.poll() is None, child.communicate()
        assert time.monotonic() < deadline, "Child did not reach the test barrier"
        time.sleep(.01)


def test_process_death_before_record_switch_keeps_old_record_and_releases_lock(sources):
    svc, _, skill, info = setup(sources, "directory")
    original = svc._read_json_file(svc.INSTALLED_FILE, {})
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    code = """
write_json = svc._write_json_file
def die_before_commit(path, data):
    if path == svc.INSTALLED_FILE:
        os._exit(71)
    return write_json(path, data)
svc._write_json_file = die_before_commit
svc.update_plugin('sample@sample')
"""
    child = _child(svc, code)
    try:
        out, err = child.communicate(timeout=15)
        assert child.returncode == 71, (out, err)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert svc._read_json_file(svc.INSTALLED_FILE, {}) == original
    assert "First revision" in runtime_text(svc)
    assert len(list(Path(info.install_path).parent.glob(".revision-*"))) == 2
    # The old OS lock is released by process exit; orphan content is reusable.
    svc.update_plugin(info.id)
    assert "Second revision" in runtime_text(svc)


def test_plugin_update_lock_covers_other_processes_before_source_refresh(sources, tmp_path, monkeypatch):
    svc, _, skill, info = setup(sources, "directory")
    write(skill, skill.read_text().replace("First revision", "Second revision"))
    paused, resume, failures = threading.Event(), threading.Event(), []
    ready, refreshed = tmp_path / "child-ready", tmp_path / "child-refreshed"

    def fail_a(*args):
        paused.set()
        assert resume.wait(10)
        raise OSError("Injected A activation failure")

    def update_a():
        try:
            svc.update_plugin(info.id)
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(svc, "enable_plugin", fail_a)
    a = threading.Thread(target=update_a)
    a.start()
    child = None
    try:
        assert paused.wait(10)
        write(skill, skill.read_text().replace("Second revision", "Third revision"))
        code = """
sync = svc.sync_marketplace
def observe_sync(*args):
    Path(sys.argv[3]).touch()
    return sync(*args)
svc.sync_marketplace = observe_sync
Path(sys.argv[2]).touch()
svc.update_plugin('sample@sample')
"""
        child = _child(svc, code, ready, refreshed)
        _wait_for_file(ready, child)
        # B has entered the real service but cannot even refresh while A owns
        # the plugin lock. This checks the scope, not just the record write.
        # Allow B to run: checking immediately after its startup marker could
        # otherwise accidentally pass even with a broken cross-process lock.
        threading.Event().wait(.3)
        assert not refreshed.exists()
        resume.set()
        out, err = child.communicate(timeout=15)
        assert child.returncode == 0, (out, err)
    finally:
        resume.set()
        a.join(10)
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()
    assert not a.is_alive() and failures and isinstance(failures[0], OSError)
    assert refreshed.exists()
    assert "Third revision" in runtime_text(svc)



def test_json_record_publication_never_shares_an_incomplete_temporary_file(sources, monkeypatch):
    import json

    svc, _ = sources
    target = svc.PLUGINS_DIR / "publication-probe.json"
    assert svc._write_json_file(target, {"initial": True})
    dump, paused, resume = json.dump, threading.Event(), threading.Event()
    results = []

    def pause_first(data, stream, **kwargs):
        if "first" in data:
            stream.write('{"first":')
            stream.flush()
            paused.set()
            assert resume.wait(10)
            stream.write('1}')
        else:
            dump(data, stream, **kwargs)

    monkeypatch.setattr(json, "dump", pause_first)
    a = threading.Thread(target=lambda: results.append(svc._write_json_file(target, {"first": 1})))
    a.start()
    try:
        assert paused.wait(10)
        assert svc._write_json_file(target, {"second": 2})
        assert json.loads(target.read_text()) == {"second": 2}
    finally:
        resume.set()
        a.join(10)
    assert not a.is_alive() and results == [True]
    assert json.loads(target.read_text()) == {"first": 1}
