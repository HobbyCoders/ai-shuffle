"""Capacity includes permanent archive loss, including the first observation."""
import time
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
    result = memory_health.check(store)
    s = capacity(result)
    assert s["state"] == "attention" and s["alert"]
    assert s["detail"]["archive_dropped_total"] == 2
    assert s["detail"]["archive_cap"] == 2
    assert "permanently" in s["reason"]
    assert "upper bound" in s["reason"]
    assert not s["detail"]["archive_loss_counter_reset"]


def test_loss_growth_is_sampled_and_quiet_history_does_not_alert_forever(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    now = time.time()
    first = memory_health.check(store, now=now)
    store.save_memory("One more backend receipt", durable=True)
    second = memory_health.check(store, now=now + memory_health.LOSS_SAMPLE_EVERY, previous=first)
    s = capacity(second)
    assert s["state"] == "attention"
    assert s["detail"]["archive_dropped_in_window"] == 1
    assert len(second["archive_loss_samples"]) == 2
    # Quiet, full tiers remain visible, but historical losses are not a new alert.
    quiet = memory_health.check(store, now=now + 30 * memory_health.DAY, previous=second)
    assert not capacity(quiet)["alert"]
    assert capacity(quiet)["detail"]["archive_dropped_total"] == 3


def test_counter_reset_is_unknown_growth_not_a_negative_loss(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    previous = memory_health.check(store)
    previous["archive_loss_samples"] = [[time.time() - 100, 999]]
    s = capacity(memory_health.check(store, previous=previous))
    assert s["detail"]["archive_loss_counter_reset"]
    assert s["detail"]["archive_dropped_in_window"] is None


def test_legacy_counter_without_timestamp_cannot_claim_no_loss(tmp_path):
    store = SkillStore(tmp_path)
    _, detail = memory_health._archive_loss_samples({"losses": {"archive_dropped": 80}}, time.time(), {})
    signal = memory_health._capacity_signal(store, [], [], detail)
    assert signal["state"] == "attention" and signal["alert"]
    assert signal["detail"]["archive_dropped_in_window"] is None
    assert "timing is unavailable" in signal["reason"]
    assert "being" not in signal["reason"]
