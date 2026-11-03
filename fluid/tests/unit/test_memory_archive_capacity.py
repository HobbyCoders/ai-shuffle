"""Capacity distinguishes actionable refusals, recent cleanup and quiet history."""
import time

import pytest
from fluid.codemode import facts, memory_health, skills
from fluid.codemode.skills import SkillStore


def capacity(result):
    return next(s for s in result["signals"] if s["id"] == "capacity")


def fill(store, monkeypatch):
    monkeypatch.setattr(facts, "MAX_DURABLE", 2)
    monkeypatch.setattr(skills, "MAX_ARCHIVED_MEMORIES", 2)
    for i in range(6):
        store.save_memory(f"Durable backend receipt number {i}", durable=True)


def test_real_overflow_cannot_report_room_to_spare(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    s = capacity(memory_health.check(store))
    assert s["state"] == "attention" and s["alert"]
    assert s["detail"]["archive_dropped_total"] == 2
    assert s["detail"]["archive_cap"] == 2
    assert "recorded 2 permanent deletions" in s["reason"]
    assert "2 at the archive limit; 0 on expiry" in s["reason"]
    assert "not the number of distinct facts lost" in s["reason"]
    assert "Recall cannot recover a deleted copy" in s["reason"]
    assert "Deletions were recorded within the past 3 days" in s["reason"]
    assert "up to" not in s["reason"] and "nothing to change" not in s["reason"]
    assert not s["detail"]["archive_loss_counter_reset"]


def test_loss_growth_is_sampled_and_quiet_full_tiers_do_not_alert_forever(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    now = time.time()
    first = memory_health.check(store, now=now)
    store.save_memory("One more backend receipt", durable=True)
    second = memory_health.check(store, now=now + memory_health.LOSS_SAMPLE_EVERY, previous=first)
    assert capacity(second)["state"] == "attention"
    assert capacity(second)["detail"]["archive_dropped_in_window"] == 1
    assert len(second["archive_loss_samples"]) == 2
    quiet = capacity(memory_health.check(store, now=now + 30 * memory_health.DAY, previous=second))
    assert quiet["state"] == "healthy" and not quiet["alert"]
    assert quiet["detail"]["archive_dropped_total"] == 3
    assert "recorded 3 permanent deletions" in quiet["reason"]
    assert "No further archive deletions were recorded in the past 3 days" in quiet["reason"]


def test_counter_reset_is_unknown_growth_not_a_negative_loss(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    previous = memory_health.check(store)
    previous["archive_loss_samples"] = [[time.time() - 100, 999]]
    s = capacity(memory_health.check(store, previous=previous))
    assert s["detail"]["archive_loss_counter_reset"]
    assert s["detail"]["archive_dropped_in_window"] is None
    assert "The deletion record restarted, so earlier losses are unknown" in s["reason"]
    assert "recorded 2 permanent deletions" in s["reason"]


def test_legacy_counter_without_timestamp_is_visible_without_attention(tmp_path):
    _, detail = memory_health._archive_loss_samples({"losses": {"archive_dropped": 80}}, time.time(), {})
    s = memory_health._capacity_signal(SkillStore(tmp_path), [], [], detail)
    assert s["state"] == "healthy" and not s["alert"]
    assert s["detail"]["archive_dropped_in_window"] is None
    assert "recorded 80 permanent deletions" in s["reason"]
    assert "Some older deletion times are unknown" in s["reason"]


def test_counter_reset_to_zero_remains_visible_without_a_bell(tmp_path):
    now = time.time()
    _, detail = memory_health._archive_loss_samples({}, now, {"archive_loss_samples": [[now - 100, 12]]})
    s = memory_health._capacity_signal(SkillStore(tmp_path), [], [], detail)
    assert s["state"] == "healthy" and not s["alert"]
    assert "The deletion record restarted, so earlier losses are unknown" in s["reason"]
    assert "No automatic archive deletions are recorded since the restart" in s["reason"]


def test_expiry_deletions_are_counted_through_actual_maintenance(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    now = time.time() + 1000 * memory_health.DAY
    monkeypatch.setattr(facts.time, "time", lambda: now)
    s = capacity(memory_health.check(store, now=now))
    assert s["detail"]["archive_dropped_total"] == 2
    assert s["detail"]["archive_expired_total"] == 4  # two live + two already archived
    assert s["detail"]["archive_deleted_total"] == 6
    assert "2 at the archive limit; 4 on expiry" in s["reason"]
    assert s["alert"]


def test_expiry_counter_does_not_invent_growth_against_legacy_overflow_sample(tmp_path):
    now = time.time()
    _, detail = memory_health._archive_loss_samples(
        {"losses": {"archive_dropped": 2, "expired_archived": 90}}, now,
        {"archive_loss_samples": [[now - 30, 2]]})
    assert detail["archive_dropped_in_window"] is None
    assert not detail["archive_loss_recent"] and detail["archive_loss_time_unknown"]


def test_full_owner_capacity_precedes_loss_and_new_refusal_notifies_in_old_stretch(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "MAX_EXPLICIT", 2)
    store = SkillStore(tmp_path)
    store.save_memory("First explicit owner rule", protected=True)
    store.save_memory("Second explicit owner rule", protected=True)
    fill(store, monkeypatch)
    now = time.time()
    first = memory_health.run(store, now=now)
    memory_health.mark_notified(store, now=now)
    monkeypatch.setattr(facts.time, "time", lambda: now + 60)
    with pytest.raises(ValueError):
        store.save_memory("Third explicit owner rule", protected=True)
    second = memory_health.run(store, now=now + 60)
    s = capacity(second)
    assert s["reason"].startswith("Memory is full: 2 user-saved facts")
    assert "recorded 2 permanent deletions" in s["reason"]
    assert "nothing to change" not in s["reason"]
    assert first["alert_since"] != second["alert_since"]
    assert second["notify"]  # no 24h suppression behind the old loss/full alert
    memory_health.mark_notified(store, now=now + 60)
    assert not memory_health.run(store, now=now + 120)["notify"]


def test_new_storage_refusal_notifies_during_existing_loss_alert(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    now = time.time()
    memory_health.run(store, now=now)
    memory_health.mark_notified(store, now=now)
    monkeypatch.setattr(facts.time, "time", lambda: now + 60)
    store._note_save_failure(OSError("synthetic disk failure"))
    result = memory_health.run(store, now=now + 60)
    assert capacity(result)["reason"].startswith("Recent memory saves failed")
    assert result["notify"]


def test_archive_cleanup_excludes_live_and_archived_owner_kept_facts(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    old = store.save_memory("The owner's earlier explicit rule", protected=True)
    new = store.save_memory("The owner's explicit correction", protected=True, replaces=[old.id])
    live = store.save_memory("The owner's independent explicit rule", protected=True)
    fill(store, monkeypatch)
    snapshot = store._facts.snapshot()
    assert snapshot["losses"]["archive_dropped"] > 0
    assert old.text in {e["text"] for e in snapshot["archived"]}
    assert {new.text, live.text} <= {e["text"] for e in snapshot["entries"]}
    future = time.time() + 1000 * memory_health.DAY
    monkeypatch.setattr(facts.time, "time", lambda: future)
    snapshot = store._facts.snapshot()
    assert {old.text, new.text, live.text} <= {
        e["text"] for e in snapshot["entries"] + snapshot["archived"]}
