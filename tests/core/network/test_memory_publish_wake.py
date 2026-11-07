"""Committed fact/skill writes wake one narrow scan, never peer application."""
from unittest.mock import Mock

import pytest

from app.core import fluid_memory, resource_sync
from app.network import memory_records, startup, transport
from fluid.codemode import transactions
from fluid.codemode.skills import SkillStore


@pytest.fixture
def writes(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: root)
    monkeypatch.setattr(fluid_memory, "prose_skills_root", lambda: tmp_path / "prose")
    # Keep boot wiring local to this test, including its process-wide Fluid hook.
    monkeypatch.setattr(transactions, "_change_callback", None, raising=False)
    monkeypatch.setattr(memory_records, "_write_window", None)
    monkeypatch.setattr(resource_sync, "_emit", resource_sync._emit)
    monkeypatch.setattr(resource_sync, "_network_hooked", False, raising=False)
    startup._install_resource_hook()
    requests = []

    def request(collections, **kwargs):
        assert not getattr(transactions._depth, "held", set()), "publish requested inside writer lock"
        requests.append(collections)

    monkeypatch.setattr(transport, "request_scan", request)
    return SkillStore(root), requests


def test_every_fact_mutation_requests_memory_after_commit(writes):
    store, requests = writes

    def changed(action):
        requests.clear()
        result = action()
        assert requests == [["memory"]]
        assert store._facts.path.exists()
        return result

    fact = changed(lambda: store.save_memory("Use amber widgets", when="Choosing widgets"))
    fact = changed(lambda: store.save_memory("Use violet widgets", memory_id=fact.id,
                                            expected_revision=fact.revision))
    changed(lambda: store.annotate_memory(fact.id, when="Selecting a widget",
                                         expected_revision=fact.revision,
                                         expected_when_at=store._facts.snapshot()["entries"][0]["when_at"]))
    other = changed(lambda: store.save_memory("Keep spare widgets", when="Restocking"))
    changed(lambda: store.retire_memory(other.id, reason="learner"))
    changed(lambda: store.delete_memory(fact.id, explicit=True))
    incoming = SkillStore(store.root.parent / "import")
    incoming.save_memory("Imported widget guidance", when="Importing")
    changed(lambda: store._facts.merge(incoming._facts.export()))


def test_nested_transaction_defers_and_coalesces_notifications(writes):
    store, requests = writes
    with transactions.file_transaction(store._facts.lock):
        for i in range(10):
            store.save_memory(f"Widget number {i}", when="Counting widgets")
        assert requests == []
    assert requests == [["memory"]]


def test_failed_atomic_write_does_not_wake(writes, monkeypatch):
    store, requests = writes
    from fluid.codemode import skills
    monkeypatch.setattr(skills, "atomic_write_text", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        store.save_memory("Unsaved widget", when="Testing failure")
    assert requests == []


def test_python_skills_and_api_owner_stores_wake_but_unrelated_stores_do_not(writes):
    store, requests = writes
    source = '\"\"\"Widget helper.\"\"\"\n'
    store.save("widgets", source)
    assert requests == [["memory"]]
    requests.clear()
    assert store.delete("widgets")
    assert requests == [["memory"]]
    requests.clear()
    fluid_memory.store_for_owner("api:someone").save_memory("Scoped widget", when="Using API")
    assert requests == [["memory"]]
    requests.clear()
    SkillStore(store.root.parent / "unrelated").save_memory("Other widget", when="Standalone")
    assert requests == []


def test_peer_apply_suppresses_wakes_but_next_local_edit_wakes(writes, monkeypatch):
    store, requests = writes
    peer = SkillStore(store.root.parent / "peer")
    peer.save_memory("Peer widget preference", when="Selecting widgets")
    payload = peer._facts.export()
    monkeypatch.setattr(memory_records.prose_skills, "load_manifest", lambda: {})
    monkeypatch.setattr(memory_records, "_apply", lambda ids, ctx, manifest: store._facts.merge(payload))
    memory_records.apply_to_files(["peer-facts"], ctx=object())
    assert requests == []
    assert any(e.text == "Peer widget preference" for e in store._facts.entries())
    store.save_memory("Local widget preference", when="Selecting widgets")
    assert requests == [["memory"]]


def test_committed_save_still_notifies_if_a_later_step_raises(writes):
    store, requests = writes
    with pytest.raises(RuntimeError, match="after commit"):
        with transactions.file_transaction(store._facts.lock):
            store.save_memory("Committed widget", when="Recovering")
            raise RuntimeError("after commit")
    assert requests == [["memory"]]


def test_listener_failure_cannot_fail_a_committed_save(writes, monkeypatch):
    store, requests = writes
    monkeypatch.setattr(transport, "request_scan", Mock(side_effect=RuntimeError("stopping")))
    fact = store.save_memory("Durable widget", when="Recovering")
    assert store._facts.entries()[0].id == fact.id


def test_recall_and_inference_budget_do_not_request_urgent_snapshots(writes):
    store, requests = writes
    fact = store.save_memory("Amber widget guidance", when="Choosing widgets")
    requests.clear()
    store.note_memories_recalled([fact.id])
    store._facts.claim_inference_call()
    assert requests == []


def test_memory_requests_stay_narrow_and_coalesce(monkeypatch):
    monkeypatch.setattr(transport, "_dirty_collections", set())
    monkeypatch.setattr(transport, "_full_scan_requested", False)
    monkeypatch.setattr(transport, "_scan_kick_from_thread", lambda: None)
    for _ in range(10):
        transport.request_scan(["memory"])
    assert transport._dirty_collections == {"memory"}
    assert not transport._full_scan_requested


def test_skill_curation_requests_a_scan_for_archiving(writes):
    store, requests = writes
    store.save("widgets", '"""Widget helper."""\n')
    requests.clear()
    store.curate(force=True, now=10**12)
    assert requests == [["memory"]]
    assert not store._path("widgets").exists()


def test_slow_burst_waits_for_quiet_but_continuous_writes_cannot_starve(monkeypatch):
    monkeypatch.setattr(memory_records, "_write_window", (10, 10))
    now = 10.05
    monkeypatch.setattr(memory_records, "monotonic", lambda: now)
    assert not memory_records._publish_due()
    memory_records._write_window = (10, 10.09)
    now = 10.12  # after the first debounce, but the last write is still recent
    assert not memory_records._publish_due()
    now = 10.2
    assert memory_records._publish_due()
    assert memory_records._write_window is None
    memory_records._write_window = (10, 12)
    now = 12
    assert memory_records._publish_due()  # two-second upper bound during sustained writes


def test_narrow_scan_refreshes_files_before_scanning_memory(monkeypatch):
    from app.network import engine
    ctx = Mock()
    monkeypatch.setattr(engine, "_ensure_safe_write_key", lambda ctx: ctx)
    refreshed = []
    monkeypatch.setattr(memory_records, "publish", lambda **kwargs: refreshed.append(kwargs) or 1)
    monkeypatch.setattr(engine, "scan_collection", lambda *args, **kwargs: 0)
    monkeypatch.setattr(engine, "kick", lambda: None)
    assert engine.scan_all(ctx=ctx, collections=["memory"], include_catalog=False) == 1
    assert refreshed == [{"ctx": ctx}]
