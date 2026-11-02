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
    assert s["reason"] == (
        "Automatic archive cleanup has permanently deleted up to 2 older facts on this worker. "
        "Recall can no longer find those facts. It last happened within the past 3 days. "
        "Facts you asked to keep are excluded from this automatic cleanup. "
        "There is no retention setting yet; there is nothing to change here.")
    for jargon in ("sample", "Lifetime counter", "upper bound", "replay", "Review memory retention"):
        assert jargon not in s["reason"]
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
    assert "up to 3 older facts" in capacity(quiet)["reason"]
    assert "No further archive deletions were recorded in the past 3 days." in capacity(quiet)["reason"]
    assert "There is no retention setting yet; there is nothing to change here." in capacity(quiet)["reason"]


def test_counter_reset_is_unknown_growth_not_a_negative_loss(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    previous = memory_health.check(store)
    previous["archive_loss_samples"] = [[time.time() - 100, 999]]
    s = capacity(memory_health.check(store, previous=previous))
    assert s["detail"]["archive_loss_counter_reset"]
    assert s["detail"]["archive_dropped_in_window"] is None
    assert "The deletion record restarted, so earlier losses are unknown." in s["reason"]
    assert "Since that restart, automatic archive cleanup has permanently deleted up to 2 older facts" in s["reason"]


def test_legacy_counter_without_timestamp_cannot_claim_no_loss(tmp_path):
    store = SkillStore(tmp_path)
    _, detail = memory_health._archive_loss_samples({"losses": {"archive_dropped": 80}}, time.time(), {})
    signal = memory_health._capacity_signal(store, [], [], detail)
    assert signal["state"] == "attention" and signal["alert"]
    assert signal["detail"]["archive_dropped_in_window"] is None
    assert "up to 80 older facts" in signal["reason"]
    assert "We do not know when this happened." in signal["reason"]
    assert "Recall can no longer find those facts." in signal["reason"]
    assert "being" not in signal["reason"]


def test_counter_reset_to_zero_is_not_claimed_as_no_loss(tmp_path):
    now = time.time()
    _, detail = memory_health._archive_loss_samples({}, now, {"archive_loss_samples": [[now - 100, 12]]})
    signal = memory_health._capacity_signal(SkillStore(tmp_path), [], [], detail)
    assert signal["state"] == "attention" and signal["alert"]
    assert signal["reason"] == (
        "The deletion record restarted, so earlier losses are unknown. "
        "No automatic archive deletions are recorded since the restart. "
        "Facts you asked to keep are excluded from this automatic cleanup. "
        "There is no retention setting yet; there is nothing to change here.")


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
    # Expiration cleanup uses the same protected exclusion as capacity cleanup.
    future = time.time() + 1000 * memory_health.DAY
    monkeypatch.setattr(facts.time, "time", lambda: future)
    snapshot = store._facts.snapshot()
    assert {old.text, new.text, live.text} <= {
        e["text"] for e in snapshot["entries"] + snapshot["archived"]}
