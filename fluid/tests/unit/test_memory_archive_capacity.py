"""Capacity distinguishes actionable refusals, recent cleanup and quiet history.

The archive has no size limit: the only automatic permanent deletion is an
archived agent fact removed ARCHIVE_RETENTION after it expired. These tests
drive that real path (facts saved in the past, then a save today runs the
maintenance that removes them) and read the loss record it writes.
"""
import time

import pytest
from fluid.codemode import facts, memory_health
from fluid.codemode.skills import SkillStore


def capacity(result):
    return next(s for s in result["signals"] if s["id"] == "capacity")


def lose(store, monkeypatch, count, topic="receipt"):
    """Make ``count`` real automatic deletions: save agent facts long enough
    ago that they expired and outlived the archive's retention, then save one
    fact today so maintenance removes them."""
    real = time.time
    then = real() - facts.AGENT_TTL - facts.ARCHIVE_RETENTION - 2 * memory_health.DAY
    monkeypatch.setattr(facts.time, "time", lambda: then)
    for i in range(count):
        store.save_memory(f"Old backend {topic} number {i}", durable=True)
    monkeypatch.setattr(facts.time, "time", real)
    store.save_memory(f"Current backend {topic} after {count} old ones", durable=True)


def fill(store, monkeypatch):
    lose(store, monkeypatch, 2)
    for i in range(3):
        store.save_memory(f"Durable backend receipt number {i}", durable=True)


def test_recent_cleanup_cannot_report_room_to_spare(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    s = capacity(memory_health.check(store))
    assert s["state"] == "attention" and s["alert"]
    assert s["detail"]["archive_expired_total"] == 2
    assert s["detail"]["archive_dropped_total"] == 0
    assert "recorded 2 permanent deletions" in s["reason"]
    assert "(a year after they expired)" in s["reason"]
    assert "archive limit" not in s["reason"]
    assert "not the number of distinct facts lost" in s["reason"]
    assert "Recall cannot recover a deleted copy" in s["reason"]
    assert "Deletions were recorded within the past 3 days" in s["reason"]
    assert "up to" not in s["reason"] and "nothing to change" not in s["reason"]
    assert not s["detail"]["archive_loss_counter_reset"]


def test_an_older_builds_overflow_count_is_still_reported(tmp_path):
    _, detail = memory_health._archive_loss_samples(
        {"losses": {"archive_dropped": 5, "expired_archived": 2}}, time.time(), {})
    s = memory_health._capacity_signal(SkillStore(tmp_path), [], [], detail)
    assert "recorded 7 permanent deletions" in s["reason"]
    assert "5 at an older version's archive limit; 2 a year after they expired" in s["reason"]


def test_the_archive_has_no_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "MAX_AGENT_LIVE", 2)
    store = SkillStore(tmp_path)
    for i in range(12):
        store.save_memory(f"Durable backend receipt number {i}", durable=True)
    assert len(store.memories()) == 2
    assert len(store.archived_memories()) == 10
    assert "losses" not in store._facts.snapshot()


def test_loss_counters_are_local_not_imported_from_replicas(tmp_path, monkeypatch):
    source = SkillStore(tmp_path / "source")
    fill(source, monkeypatch)
    packet = source._facts.export()
    assert "losses" not in packet
    assert source._facts.snapshot()["losses"]["expired_archived"] == 2

    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(packet)
    # The source's two deletion events are local accounting, not replicated.
    assert replica._facts.snapshot().get("losses", {}).get("expired_archived", 0) == 0


class Clock:
    def __init__(self, monkeypatch):
        self.now = time.time()
        monkeypatch.setattr(facts.time, "time", lambda: self.now)

    def past_retention(self):
        self.now += facts.ARCHIVE_RETENTION + 3 * memory_health.DAY


def _copy(origin, n, at, expires_at, topic):
    return {"id": f"m{n}", "origin": origin, "text": f"Synthetic {topic} receipt {n} for replay accounting",
            "scope": "global", "provenance": "session", "revision": 1, "clock": {origin: 1},
            "created_at": at, "updated_at": at, "expires_at": expires_at, "uses": 0}


def _packet(store, origin, entries):
    return {"schema": facts.SCHEMA, "owner": store._facts.owner, "store_id": origin,
            "entries": entries, "tombstones": [], "forgotten": ""}


def _held(store):
    return {e.id for e in store.memories() + store.archived_memories()}


def test_expired_copy_replayed_by_a_replica_is_counted_once(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    source = SkillStore(tmp_path / "source")
    source.save_memory("An inferred receipt that will expire")
    packet = source._facts.export()
    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(packet)
    clock.now += facts.AGENT_TTL
    clock.past_retention()
    for _ in range(5):
        replica._facts.merge(packet)
    assert replica._facts.snapshot()["losses"]["expired_archived"] == 1


def test_copy_never_held_here_is_not_a_local_loss(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    replica = SkillStore(tmp_path)
    ended = clock.now - 2 * facts.ARCHIVE_RETENTION
    replica._facts.merge(_packet(replica, "peer-a", [_copy("peer-a", n, ended, ended, "expired")
                                                    for n in range(40)]))
    # The peer that kept them counts its own deletions; this store never did.
    assert "losses" not in replica._facts.snapshot()
    assert _held(replica) == set()


# Past the 2048 keys the first version of this fix kept, so an evicting
# list would forget the first packet's copies before it is replayed.
FLOOD = 2100


def test_expired_copies_replayed_after_many_later_deletions_stay_counted_once(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    replica = SkillStore(tmp_path)
    first = _packet(replica, "peer-a", [_copy("peer-a", n, clock.now, clock.now + memory_health.DAY, "first")
                                        for n in range(40)])
    replica._facts.merge(first)
    clock.past_retention()
    assert replica._facts.snapshot()["losses"]["expired_archived"] == 40
    replica._facts.merge(_packet(replica, "peer-b", [
        _copy("peer-b", n, clock.now, clock.now + memory_health.DAY, "flood") for n in range(FLOOD)]))
    clock.past_retention()
    losses = replica._facts.snapshot()["losses"]
    total, stamp = losses["expired_archived"], losses["last_expired_archived_at"]
    assert total == 40 + FLOOD
    for _ in range(3):
        clock.now += memory_health.DAY
        replica._facts.merge(first)
        losses = replica._facts.snapshot()["losses"]
        assert losses["expired_archived"] == total
        assert losses["last_expired_archived_at"] == stamp
    assert _held(replica) == set()


def test_new_losses_still_count_after_two_hundred_thousand_deletions(tmp_path, monkeypatch):
    # A lifetime replay record of fixed size (the round-1 filter) saturates
    # well before this and then takes new losses for replays.
    clock = Clock(monkeypatch)
    replica = SkillStore(tmp_path)
    batch, rounds = 2500, 81
    for r in range(rounds):
        replica._facts.merge(_packet(replica, f"peer-{r}", [
            _copy(f"peer-{r}", n, clock.now, clock.now + memory_health.DAY, "bulk") for n in range(batch)]))
        clock.past_retention()
        replica._facts.snapshot()
    losses = replica._facts.snapshot()["losses"]
    assert losses["expired_archived"] == batch * rounds > 200_000
    stamp = losses["last_expired_archived_at"]
    replica._facts.merge(_packet(replica, "peer-new", [
        _copy("peer-new", n, clock.now, clock.now + memory_health.DAY, "fresh") for n in range(1000)]))
    clock.past_retention()
    losses = replica._facts.snapshot()["losses"]
    assert losses["expired_archived"] == batch * rounds + 1000
    assert losses["last_expired_archived_at"] > stamp
    assert capacity(memory_health.check(replica, now=clock.now))["state"] == "attention"


def test_loss_record_keeps_no_replay_memory_and_survives_a_restart(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    lose(store, monkeypatch, 4, topic="extra receipt")
    clock = Clock(monkeypatch)
    packet = store._facts.export()
    losses = store._facts.snapshot()["losses"]
    assert set(losses) == {"expired_archived", "last_expired_archived_at"}
    assert losses["expired_archived"] == 6
    reopened = SkillStore(tmp_path)
    clock.now += 1
    for _ in range(3):
        reopened._facts.merge(packet)
    assert reopened._facts.snapshot()["losses"] == losses


def test_held_fact_replaced_and_dropped_counts_but_its_replays_do_not(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    replica = SkillStore(tmp_path)
    soon = clock.now + memory_health.DAY
    copy = _copy("peer-a", 1, clock.now, soon, "held")
    twin = dict(copy, id="m2")  # the same text under another ID
    replica._facts.merge(_packet(replica, "peer-a", [copy, twin]))
    clock.past_retention()
    assert replica._facts.snapshot()["losses"]["expired_archived"] == 2
    kept = _copy("peer-a", 3, clock.now, clock.now + 365 * memory_health.DAY, "kept")
    replica._facts.merge(_packet(replica, "peer-a", [kept]))
    # A peer's newer version of a fact held here arrives already expired:
    # this store loses a fact it held, so that counts once.
    ended = clock.now - 2 * facts.ARCHIVE_RETENTION
    edited = dict(kept, text="A rewritten synthetic receipt for replay accounting", revision=2,
                  clock={"peer-a": 2}, expires_at=ended)
    replica._facts.merge(_packet(replica, "peer-a", [edited]))
    assert replica._facts.snapshot()["losses"]["expired_archived"] == 3
    for _ in range(3):
        replica._facts.merge(_packet(replica, "peer-a", [copy, twin, edited]))
    assert replica._facts.snapshot()["losses"]["expired_archived"] == 3


def test_repeated_local_save_deduplicates_and_records_no_loss(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    existing = store.memories()[0]
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
    lose(store, monkeypatch, 1, topic="later receipt")
    second = memory_health.check(store, now=now + memory_health.LOSS_SAMPLE_EVERY, previous=first)
    assert capacity(second)["state"] == "attention"
    assert capacity(second)["detail"]["archive_dropped_in_window"] == 1
    assert len(second["archive_loss_samples"]) == 2
    quiet = capacity(memory_health.check(store, now=now + 30 * memory_health.DAY, previous=second))
    assert quiet["state"] == "healthy" and not quiet["alert"]
    assert quiet["detail"]["archive_expired_total"] == 3
    assert "recorded 3 permanent deletions" in quiet["reason"]
    assert "No further archive deletions were recorded in the past 3 days" in quiet["reason"]


def test_counter_reset_is_unknown_growth_not_a_negative_loss(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fill(store, monkeypatch)
    previous = memory_health.check(store)
    previous["archive_loss_samples"] = [[time.time() - 100, 0, 999]]
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
    assert s["detail"]["archive_dropped_total"] == 0
    # The two old ones, then every fact saved today: idle past its TTL and
    # then a year in the archive.
    assert s["detail"]["archive_expired_total"] == 6
    assert s["detail"]["archive_deleted_total"] == 6
    assert "(a year after they expired)" in s["reason"]
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
    assert snapshot["losses"]["expired_archived"] > 0
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
