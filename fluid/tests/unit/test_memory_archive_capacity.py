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


def test_loss_counters_are_local_not_imported_from_replicas(tmp_path, monkeypatch):
    source = SkillStore(tmp_path / "source")
    fill(source, monkeypatch)
    packet = source._facts.export()
    assert "losses" not in packet
    assert source._facts.snapshot()["losses"]["archive_dropped"] == 2

    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(packet)
    # The source's two deletion events are local accounting, not replicated.
    assert replica._facts.snapshot().get("losses", {}).get("archive_dropped", 0) == 0


def test_stale_replica_replay_does_not_add_archive_loss_events(tmp_path, monkeypatch):
    source = SkillStore(tmp_path / "source")
    fill(source, monkeypatch)
    packet = source._facts.export()
    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(packet)
    replica.save_memory("A newer durable receipt on the replica", durable=True)
    retained_ids = {e.id for e in replica.memories() + replica.archived_memories()}
    first = memory_health.check(replica)
    assert capacity(first)["detail"]["archive_dropped_total"] == 1

    # Replaying a packet must not report new loss when the retained facts
    # do not change: the same deleted copy is not counted twice (#956).
    for _ in range(10):
        replica._facts.merge(packet)
        assert {e.id for e in replica.memories() + replica.archived_memories()} == retained_ids
    result = memory_health.check(
        replica, now=first["checked_at"] + memory_health.LOSS_SAMPLE_EVERY, previous=first)
    signal = capacity(result)
    assert signal["state"] == "attention" and signal["alert"]
    assert signal["detail"]["archive_dropped_total"] == 1
    assert signal["detail"]["archive_dropped_in_window"] == 0
    assert "not the number of distinct facts lost" in signal["reason"]
    assert "Recall cannot recover a deleted copy" in signal["reason"]


def test_replay_neither_counts_nor_refreshes_time_but_a_new_distinct_loss_does(tmp_path, monkeypatch):
    source = SkillStore(tmp_path / "source")
    fill(source, monkeypatch)
    packet = source._facts.export()
    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(packet)
    replica.save_memory("A newer durable receipt on the replica", durable=True)
    losses = replica._facts.snapshot()["losses"]
    assert losses["archive_dropped"] == 1
    stamp = losses["last_archive_dropped_at"]
    for _ in range(10):
        replica._facts.merge(packet)
    losses = replica._facts.snapshot()["losses"]
    # Replays of an already-deleted copy leave both the count and the time,
    # so a quiet store does not look as if it were still losing facts.
    assert losses["archive_dropped"] == 1 and losses["last_archive_dropped_at"] == stamp
    # A genuinely new deletion is still counted and stamped.
    replica.save_memory("A second newer durable receipt on the replica", durable=True)
    losses = replica._facts.snapshot()["losses"]
    assert losses["archive_dropped"] == 2 and losses["last_archive_dropped_at"] >= stamp


def test_expired_copy_replayed_by_a_replica_is_counted_once(tmp_path, monkeypatch):
    source = SkillStore(tmp_path / "source")
    source.save_memory("An inferred receipt that will expire")
    packet = source._facts.export()
    replica = SkillStore(tmp_path / "replica")
    future = time.time() + 1000 * memory_health.DAY
    monkeypatch.setattr(facts.time, "time", lambda: future)
    for _ in range(5):
        replica._facts.merge(packet)
    assert replica._facts.snapshot()["losses"]["expired_archived"] == 1


def _copy(origin, n, at, expires_at, topic):
    return {"id": f"m{n}", "origin": origin, "text": f"Synthetic {topic} receipt {n} for replay accounting",
            "scope": "global", "provenance": "session", "revision": 1, "clock": {origin: 1},
            "created_at": at, "updated_at": at, "expires_at": expires_at, "uses": 0}


def _packet(store, origin, entries):
    return {"schema": facts.SCHEMA, "owner": store._facts.owner, "store_id": origin,
            "entries": entries, "tombstones": [], "forgotten": ""}


# Past the 2048 keys the first version of this fix kept, so an evicting
# list forgets the first packet's copies before it is replayed.
FLOOD = 2100


def test_expired_copies_replayed_after_many_later_deletions_stay_counted_once(tmp_path):
    replica = SkillStore(tmp_path)
    old = time.time() - 2 * facts.ARCHIVE_RETENTION
    first = _packet(replica, "peer-a", [_copy("peer-a", n, old, old, "expired") for n in range(40)])
    replica._facts.merge(first)
    losses = replica._facts.snapshot()["losses"]
    assert losses["expired_archived"] == 40
    flood = _packet(replica, "peer-b", [_copy("peer-b", n, old, old, "expired") for n in range(FLOOD)])
    replica._facts.merge(flood)
    losses = replica._facts.snapshot()["losses"]
    total, stamp = losses["expired_archived"], losses["last_expired_archived_at"]
    assert total == 40 + FLOOD
    for _ in range(3):
        replica._facts.merge(first)
        losses = replica._facts.snapshot()["losses"]
        assert losses["expired_archived"] == total
        assert losses["last_expired_archived_at"] == stamp
    assert replica.memories() == [] and replica.archived_memories() == []


def test_overflow_copies_replayed_after_many_later_deletions_stay_counted_once(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "MAX_ARCHIVED_MEMORIES", 2)
    replica = SkillStore(tmp_path)
    now = time.time()
    later = now + 365 * memory_health.DAY
    # Newer copies fill the store; the first packet's older copies rank last
    # and are dropped at the archive limit each time they arrive.
    keep = _packet(replica, "peer-k", [_copy("peer-k", n, now, later, "kept") for n in range(60)])
    replica._facts.merge(keep)
    retained = {e.id for e in replica.memories() + replica.archived_memories()}
    first = _packet(replica, "peer-a", [_copy("peer-a", n, now - 9 * memory_health.DAY, later, "stale")
                                        for n in range(40)])
    replica._facts.merge(first)
    base = replica._facts.snapshot()["losses"]["archive_dropped"]
    assert {e.id for e in replica.memories() + replica.archived_memories()} == retained
    flood = _packet(replica, "peer-b", [_copy("peer-b", n, now - 8 * memory_health.DAY, later, "flood")
                                        for n in range(FLOOD)])
    replica._facts.merge(flood)
    losses = replica._facts.snapshot()["losses"]
    total, stamp = losses["archive_dropped"], losses["last_archive_dropped_at"]
    assert total == base + FLOOD
    for _ in range(3):
        replica._facts.merge(first)
        losses = replica._facts.snapshot()["losses"]
        assert losses["archive_dropped"] == total
        assert losses["last_archive_dropped_at"] == stamp
    assert {e.id for e in replica.memories() + replica.archived_memories()} == retained


def test_replay_record_survives_a_restart_and_stays_fixed_size(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    for i in range(4):
        store.save_memory(f"Extra durable backend receipt {i}", durable=True)
    losses = store._facts.snapshot()["losses"]
    assert losses["archive_dropped"] == 6
    assert len(losses["counted"]) == 2 * facts.BLOOM_BYTES
    reopened = SkillStore(tmp_path)
    packet = store._facts.export()
    old = time.time() - 2 * facts.ARCHIVE_RETENTION
    replay = _packet(reopened, "peer-a", [_copy("peer-a", n, old, old, "expired") for n in range(3)])
    reopened._facts.merge(replay)
    reopened._facts.merge(replay)
    reopened._facts.merge(packet)
    losses = reopened._facts.snapshot()["losses"]
    assert losses["expired_archived"] == 3 and losses["archive_dropped"] == 6
    assert len(losses["counted"]) == 2 * facts.BLOOM_BYTES


def test_same_id_new_text_and_same_text_new_id_are_separate_losses(tmp_path):
    replica = SkillStore(tmp_path)
    old = time.time() - 2 * facts.ARCHIVE_RETENTION
    copy = _copy("peer-a", 1, old, old, "expired")
    replica._facts.merge(_packet(replica, "peer-a", [copy]))
    edited = dict(copy, text="A rewritten synthetic receipt for replay accounting",
                  revision=2, clock={"peer-a": 2})
    replica._facts.merge(_packet(replica, "peer-a", [edited]))
    replica._facts.merge(_packet(replica, "peer-a", [dict(copy, id="m2")]))
    replica._facts.merge(_packet(replica, "peer-a", [copy, edited, dict(copy, id="m2")]))
    assert replica._facts.snapshot()["losses"]["expired_archived"] == 3


def test_repeated_local_save_deduplicates_before_archive_eviction(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    existing = store.archived_memories()[0]
    before = store._facts.snapshot()
    ids = {e["id"] for e in before["entries"] + before["archived"]}
    for _ in range(10):
        saved = store.save_memory(existing.text, durable=True)
        assert saved.id == existing.id
    after = store._facts.snapshot()
    assert {e["id"] for e in after["entries"] + after["archived"]} == ids
    assert after["losses"] == before["losses"]


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


def test_cleanup_just_after_check_start_is_recent_not_quiet(tmp_path):
    now = time.time()
    _, detail = memory_health._archive_loss_samples(
        {"losses": {"expired_archived": 1, "last_expired_archived_at": now + 1}}, now, {})
    assert detail["archive_loss_recent"] and not detail["archive_loss_time_unknown"]


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


def test_healthy_capacity_with_past_or_unknown_loss_is_a_readable_note(tmp_path, monkeypatch):
    # Quiet history: real deletions, then nothing for longer than the window.
    store = SkillStore(tmp_path / "quiet")
    fill(store, monkeypatch)
    now = time.time()
    first = memory_health.check(store, now=now)
    assert capacity(first)["state"] == "attention" and not capacity(first)["note"]
    quiet = memory_health.check(store, now=now + 30 * memory_health.DAY, previous=first)
    s = capacity(quiet)
    assert s["state"] == "healthy" and not s["alert"] and s["note"]
    view = next(v for v in memory_health.summary(quiet)["signals"] if v["id"] == "capacity")
    assert view["note"] and "recorded 2 permanent deletions" in view["reason"]

    # A counter without timing history (written by an older build).
    _, detail = memory_health._archive_loss_samples({"losses": {"archive_dropped": 80}}, now, {})
    legacy = memory_health._capacity_signal(SkillStore(tmp_path / "legacy"), [], [], detail)
    assert legacy["state"] == "healthy" and legacy["note"]
    assert "Some older deletion times are unknown" in legacy["reason"]

    # Nothing was ever deleted: no note.
    empty = capacity(memory_health.check(SkillStore(tmp_path / "empty")))
    assert empty["state"] == "healthy" and not empty["note"]
