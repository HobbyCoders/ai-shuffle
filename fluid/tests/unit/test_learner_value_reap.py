"""A busy worker's learner still dropped most of its journal unread (#688).

The overflow reap dropped entries in arrival order, so the corrections and
preferences in user, assistant and friction entries went as fast as the
read-only cells around them, and the 48-call budget was fixed. These tests
cover the value-ordered reap, the cursor and read lease around it, the
per-kind loss record and health detail, and the configurable budget.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fluid.codemode import SkillStore, Synthesizer, memory_health
from fluid.codemode import continual as continual_mod
from fluid.codemode import skills as skills_mod
from fluid.codemode.continual import (
    LearnerBudget,
    learner_budget,
    min_pass_interval,
    paced_call_allowance,
)
from fluid.codemode.skills import (
    JOURNAL_LOSS_FILE,
    journal_drop_tier,
    read_journal_loss,
)
from fluid.codemode.workflow_loss import load_loss, session_lost

from .test_learner_coverage import (
    DAY,
    MIDDAY,
    NoChanges,
    _busy_store,
    edit_cell,
    read_cell,
    rows,
    set_budget,
)

PAD = "p" * 300


def _mark(entry: dict, mark: str) -> dict:
    return {**entry, "mark": mark, "pad": PAD}


def friction(i: int) -> dict:
    return {"kind": "friction", "signal": "error_streak", "name": "Bash", "text": f"failed {i}"}


def failed_cell(i: int) -> dict:
    return {**edit_cell(i), "error": "AssertionError"}


def marks(store: SkillStore, start: int = 0) -> list[str]:
    return [e.get("mark") for e in store.journal_entries(start)]


def overflow(store: SkillStore, monkeypatch, share: float = 0.95) -> None:
    """Shrink the cap so the journal sits at ``share`` of it."""
    monkeypatch.setattr(skills_mod, "JOURNAL_MAX_BYTES", int(store._journal_size() / share))


def _mixed(store: SkillStore, rounds: int = 6) -> dict[str, list[str]]:
    """Each round: a read-only cell, a Grep call (tier 0), an edit cell
    (tier 1), and a user line, an assistant line, a friction entry and a
    failed cell (tier 2). Returns the marks per tier, oldest first."""
    tiers: dict[str, list[str]] = {"0": [], "1": [], "2": []}
    for i in range(rounds):
        for tier, entry in (
            ("0", read_cell(i)),
            ("0", {"kind": "tool", "name": "Grep", "input": "{}", "output": "x"}),
            ("1", edit_cell(i)),
            ("2", {"kind": "user", "text": f"please use pnpm {i}"}),
            ("2", {"kind": "assistant", "text": f"done {i}"}),
            ("2", friction(i)),
            ("2", failed_cell(i)),
        ):
            mark = f"{tier}:{len(tiers[tier])}"
            tiers[tier].append(mark)
            store.journal_append(_mark(entry, mark))
    return tiers


# ─── the reap order ─────────────────────────────────────────────────────────

def test_drop_tiers():
    assert journal_drop_tier(read_cell(1)) == 0
    assert journal_drop_tier({"kind": "tool", "name": "Grep", "input": "{}", "output": "x"}) == 0
    assert journal_drop_tier(edit_cell(1)) == 1
    assert journal_drop_tier({"kind": "tool", "name": "Bash", "input": "{}", "output": "ok"}) == 1
    # The read-only filter's own exceptions are not low value here either.
    assert journal_drop_tier({**read_cell(1), "outside_cwd": True}) == 1
    for entry in (failed_cell(1), {**read_cell(1), "calls": [{"name": "Read", "ok": False}]},
                  {"kind": "tool", "name": "Grep", "error": True}, friction(1),
                  {"kind": "user", "text": "x"}, {"kind": "assistant", "text": "y"},
                  {"kind": "something-new"}, None):
        assert journal_drop_tier(entry) == 2, entry


def test_backlog_reap_drops_the_lowest_value_first(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "remember", "text": "The user's staging host is stage-2."})
    tiers = _mixed(store)
    overflow(store, monkeypatch)
    dropped = store.reap_journal()
    kept = marks(store)
    assert dropped > 0
    assert not set(tiers["0"]) & set(kept), "read-only work goes first"
    assert set(tiers["2"]) <= set(kept), "conversation, friction and failures go last"
    assert store.journal_entries()[0]["kind"] == "remember", "explicit entries never go"
    edits = [m for m in tiers["1"] if m in kept]
    assert edits and edits == tiers["1"][-len(edits):], "within a tier, oldest first"
    assert store._journal_size() <= skills_mod.JOURNAL_MAX_BYTES * skills_mod.JOURNAL_REAP_FRACTION
    record = read_journal_loss(store.root / JOURNAL_LOSS_FILE)
    assert record["dropped_unsynthesized"] == dropped
    assert record["dropped_by_kind"] == {
        "cell": len(tiers["0"]) // 2 + len(tiers["1"]) - len(edits), "tool": len(tiers["0"]) // 2,
    }
    assert record["dropped_low_value"] == dropped, "only successful cells and tool calls went"


def test_high_value_entries_go_only_when_nothing_else_is_left(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    for i in range(12):
        store.journal_append(_mark({"kind": "user", "text": f"request {i}"}, f"u{i}"))
        store.journal_append(_mark({"kind": "assistant", "text": f"reply {i}"}, f"a{i}"))
    overflow(store, monkeypatch)
    dropped = store.reap_journal()
    kept = marks(store)
    assert dropped and kept == [m for i in range(12) for m in (f"u{i}", f"a{i}")][dropped:]
    record = read_journal_loss(store.root / JOURNAL_LOSS_FILE)
    assert sum(record["dropped_by_kind"].values()) == dropped
    assert set(record["dropped_by_kind"]) == {"user", "assistant"}
    assert "dropped_low_value" not in record


def test_dropped_failed_cells_are_not_counted_as_low_value(tmp_path, monkeypatch):
    # A failed cell is kept for last with conversation (tier 2) but its loss
    # is still filed under "cell": the low-value count tells the two apart.
    store = SkillStore(tmp_path)
    for i in range(2):
        store.journal_append(_mark(read_cell(i), f"r{i}"))
    for i in range(12):
        store.journal_append(_mark(failed_cell(i), f"f{i}"))
    overflow(store, monkeypatch)
    dropped = store.reap_journal()
    record = read_journal_loss(store.root / JOURNAL_LOSS_FILE)
    failed_lost = sum(1 for i in range(12) if f"f{i}" not in marks(store))
    assert failed_lost, "the scenario must drop failed cells"
    assert record["dropped_by_kind"] == {"cell": dropped}
    assert record["dropped_low_value"] == dropped - failed_lost == 2


def test_a_dropped_user_receipt_still_marks_its_episode_incomplete(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    store.journal_context(session_id="receipt", project="proj")
    store.journal_append(_mark({"kind": "user", "text": "workflow accepted: deploy-preview"}, "r"))
    store.journal_context(session_id="cells", project="proj")
    for i in range(10):
        store.journal_append(_mark(edit_cell(i), f"c{i}"))
    store.journal_context(session_id="later", project="proj")
    for i in range(3):
        store.journal_append(_mark({"kind": "user", "text": f"later {i}"}, f"l{i}"))
    # Past the cells' share of the bytes: the oldest prose, the receipt, goes too.
    monkeypatch.setattr(skills_mod, "JOURNAL_REAP_FRACTION", 0.2)
    overflow(store, monkeypatch)
    store.reap_journal()
    kept = marks(store)
    assert "r" not in kept and not any(m.startswith("c") for m in kept)
    assert kept == ["l0", "l1", "l2"]
    lost = load_loss(store.root)
    assert session_lost(lost, "proj", "receipt"), "a dropped receipt makes its episode lossy"
    assert session_lost(lost, "proj", "cells")
    assert not session_lost(lost, "proj", "later")


# ─── the cursor ─────────────────────────────────────────────────────────────

def test_a_middle_reap_keeps_the_cursor_on_the_first_unread_line(tmp_path, monkeypatch):
    """No entry is read twice and none is skipped: every unread entry is
    either read once, in order, or counted as dropped."""
    store = SkillStore(tmp_path)
    for i in range(4):
        store.journal_append(_mark(edit_cell(i), f"read{i}"))
    store.set_synth_cursor(4)
    _mixed(store)
    unread = marks(store, 4)
    before = read_journal_loss(store.root / JOURNAL_LOSS_FILE) or {}
    overflow(store, monkeypatch, share=1.0)
    store.reap_journal()
    cursor = store.synth_cursor()
    record = read_journal_loss(store.root / JOURNAL_LOSS_FILE)
    # The consumed prefix went first and moved the cursor back by its size.
    assert marks(store)[:cursor] == [m for m in ("read0", "read1", "read2", "read3")
                                     if m in marks(store)]
    seen: list[str] = []
    while True:
        indexed, reached = store.journal_slice_indexed(cursor, 5)
        if not indexed:
            break
        seen += [e["mark"] for _, e in indexed]
        cursor = reached
    assert seen == [m for m in unread if m in seen], "in order"
    assert len(seen) == len(set(seen)), "no entry read twice"
    dropped = record["dropped_unsynthesized"] - before.get("dropped_unsynthesized", 0)
    assert len(unread) - len(seen) == dropped, "every entry not read was counted as dropped"
    assert set(seen) < set(unread)


def test_a_reap_during_a_pass_leaves_its_slice_alone_and_moves_the_cursor_right(
    tmp_path, monkeypatch,
):
    store = SkillStore(tmp_path)
    for i in range(5):
        store.journal_append(_mark(edit_cell(i), f"old{i}"))
    store.set_synth_cursor(5)
    for i in range(6):
        store.journal_append(_mark(read_cell(i), f"slice{i}"))  # low value: first to go
    for i in range(30):
        store.journal_append(_mark(read_cell(i), f"after{i}"))
    token, cursor, indexed, reached = store.begin_synth_read(6)
    assert token and cursor == 5 and reached == 11
    assert [e["mark"] for _, e in indexed] == [f"slice{i}" for i in range(6)]
    # The model call is slow; an append at the cap reaps meanwhile.
    overflow(store, monkeypatch)
    assert store.reap_journal() > 0
    kept = marks(store)
    assert all(f"slice{i}" in kept for i in range(6)), "the leased slice is never dropped"
    assert "old0" not in kept, "the consumed prefix still goes"
    new_cursor = store.commit_synth_cursor(token, reached)
    after = marks(store, new_cursor)
    assert not any(m.startswith("slice") for m in after), "the slice is not read again"
    assert after == [m for m in kept if m.startswith("after")], "nothing after it is skipped"
    assert not store._lease_path.exists()


async def test_a_pass_whose_call_overlaps_a_reap_reads_every_kept_line_once(
    tmp_path, monkeypatch,
):
    """End to end: before the lease, a reap during the model call moved the
    lines and the cursor landed past lines no pass had read."""
    store = SkillStore(tmp_path)
    for i in range(10):
        store.journal_append(_mark(edit_cell(100 + i), f"c{i}"))
    store.set_synth_cursor(10)  # consumed by an earlier pass
    for i in range(10):
        store.journal_append(_mark(edit_cell(i), f"e{i}"))

    class ReapsDuringTheCall(NoChanges):
        async def stream(self, request):
            if not self.prompts:
                for i in range(30):
                    store.journal_append(_mark(read_cell(i), f"r{i}"))
                for i in range(5):
                    store.journal_append(_mark({"kind": "user", "text": f"next {i}"}, f"u{i}"))
                cap = skills_mod.JOURNAL_MAX_BYTES
                overflow(store, monkeypatch, share=1.0)
                assert store.reap_journal() > 0
                # Back to a roomy cap: the pass's own reap keeps what it read.
                monkeypatch.setattr(skills_mod, "JOURNAL_MAX_BYTES", cap)
            async for chunk in super().stream(request):
                yield chunk

    provider = ReapsDuringTheCall()
    synth = Synthesizer(provider, "gpt-test", store, min_new_entries=1, max_entries=10)
    await synth._run_safe()
    assert len(provider.prompts) == 1
    assert all(f"f{i}.py" in provider.prompts[0] for i in range(10))
    kept = marks(store)
    assert not any(m.startswith("c") for m in kept), "the consumed prefix went"
    assert [m for m in kept if m.startswith("e")] == [f"e{i}" for i in range(10)], (
        "the slice being read is not dropped (or counted as dropped unread)")
    # The next pass starts right after the slice: every kept line after it,
    # and nothing of the slice again.
    cursor = store.synth_cursor()
    assert kept[cursor - 1] == "e9"
    rest = marks(store, cursor)
    assert rest == [m for m in kept if m[0] in "ru"] and rest[-5:] == [f"u{i}" for i in range(5)]
    assert not store._lease_path.exists()


def test_a_stale_or_lost_lease(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    for i in range(20):
        store.journal_append(_mark(read_cell(i), f"r{i}"))
    token, cursor, _, reached = store.begin_synth_read(5)
    # A lease past its TTL (a crashed pass) protects nothing.
    lease = json.loads(store._lease_path.read_text())
    lease["at"] = time.time() - skills_mod.SYNTH_LEASE_TTL - 1
    store._lease_path.write_text(json.dumps(lease))
    overflow(store, monkeypatch)
    store.reap_journal()
    assert "r0" not in marks(store)
    # Its pass can no longer tell where its lines went: it keeps the cursor
    # and the slice is read again rather than lines skipped.
    assert store.commit_synth_cursor(token, reached) == cursor == 0
    # Without a lease at all (the lock was busy), the position is used as is.
    assert store.commit_synth_cursor(None, 3) == 3
    store.end_synth_read(None)


# ─── the per-kind loss record ───────────────────────────────────────────────

def test_loss_record_reads_old_records_and_merges_kinds(tmp_path):
    path = tmp_path / JOURNAL_LOSS_FILE
    path.write_text(json.dumps({"dropped_unsynthesized": 7, "schema": 1}))
    assert "dropped_by_kind" not in read_journal_loss(path), "an old record has no split"
    path.write_text(json.dumps({"dropped_unsynthesized": 7, "schema": 1,
                                "dropped_by_kind": {"user": 2, "cell": 5, "bogus": 9, "tool": -1}}))
    assert read_journal_loss(path)["dropped_by_kind"] == {"user": 2, "cell": 5}
    merged = skills_mod._merge_loss(read_journal_loss(path), {
        "dropped_unsynthesized": 3, "dropped_by_kind": {"user": 1, "friction": 2},
    })
    assert merged["dropped_unsynthesized"] == 10
    assert merged["dropped_by_kind"] == {"user": 3, "cell": 5, "friction": 2}
    assert skills_mod.journal_loss_kind("subagent") == "other"


# ─── health ─────────────────────────────────────────────────────────────────

def _write_loss(store, dropped, by_kind=None):
    record = {"dropped_unsynthesized": dropped, "schema": 1}
    if by_kind is not None:
        record["dropped_by_kind"] = by_kind
    (store.root / JOURNAL_LOSS_FILE).write_text(json.dumps(record))


def test_health_names_dropped_user_assistant_and_friction_entries(tmp_path):
    now = MIDDAY
    store = _busy_store(tmp_path, now)
    _write_loss(store, 1_000, {"cell": 1_000})
    first = memory_health.check(store, now=now - 3 * DAY)
    assert first["signals"][0]["detail"]["dropped_3d_by_kind"] is None
    _write_loss(store, 4_390, {"cell": 4_300, "tool": 82, "user": 5, "assistant": 2, "friction": 1})
    learner = memory_health.check(store, now=now, previous=first)["signals"][0]
    assert learner["state"] == "attention" and learner["alert"], "conversation lost: notify"
    assert learner["detail"]["dropped_3d"] == 3_390
    assert learner["detail"]["dropped_3d_by_kind"] == {
        "cell": 3_300, "tool": 82, "user": 5, "assistant": 2, "friction": 1,
    }
    assert ("3390 were dropped unread. 8 user/assistant/friction entries dropped unread."
            in learner["reason"])


def test_health_without_high_value_loss_or_kind_history(tmp_path):
    now = MIDDAY
    store = _busy_store(tmp_path, now)
    _write_loss(store, 1_646)
    first = memory_health.check(store, now=now - 3 * DAY)
    # A state written by a build without the split has total samples only.
    first.pop("journal_loss_kind_samples")
    _write_loss(store, 1_646 + 3_390, {"cell": 3_390})
    learner = memory_health.check(store, now=now, previous=first)["signals"][0]
    assert learner["state"] == "attention" and learner["detail"]["dropped_3d"] == 3_390
    assert learner["detail"]["dropped_3d_by_kind"] is None
    assert "user/assistant/friction" not in learner["reason"]
    assert not learner["alert"], "an unknown split is shown, not notified"
    second = memory_health.check(store, now=now + DAY, previous={**first, **memory_health.check(
        store, now=now, previous=first)})
    assert second["signals"][0]["detail"]["dropped_3d_by_kind"] == {}


def _write_record(store, dropped, by_kind, low_value=None):
    record = {"dropped_unsynthesized": dropped, "schema": 1, "dropped_by_kind": by_kind}
    if low_value is not None:
        record["dropped_low_value"] = low_value
    (store.root / JOURNAL_LOSS_FILE).write_text(json.dumps(record))


def test_health_is_healthy_when_only_successful_cells_and_tool_calls_were_trimmed(tmp_path):
    # The Unraid store on 2026-11-01: 2,936 entries dropped unread, all code
    # cells and tool calls, while all conversation was read. That is the reap
    # trimming what it trims first, not a learner that cannot keep up.
    now = MIDDAY
    store = _busy_store(tmp_path, now)
    _write_record(store, 38_403, {"cell": 145, "tool": 3_273})  # written before the count
    first = memory_health.check(store, now=now - 3 * DAY)
    assert first["journal_loss_samples"] == [[now - 3 * DAY, 38_403, 0]]
    _write_record(store, 41_339, {"cell": 2_691, "tool": 3_663}, low_value=2_936)
    learner = memory_health.check(store, now=now, previous=first)["signals"][0]
    assert learner["detail"]["dropped_3d"] == learner["detail"]["dropped_3d_low_value"] == 2_936
    assert learner["state"] == "healthy" and not learner["alert"]
    assert learner["reason"].startswith("The learner is saving facts.")
    assert ("2936 low-value entries (successful code cells and tool calls) were trimmed unread"
            in learner["reason"]) and "no conversation was lost" in learner["reason"]
    # Failed cells among them are not low value: shown, not notified.
    _write_record(store, 41_339, {"cell": 2_691, "tool": 3_663}, low_value=2_900)
    learner = memory_health.check(store, now=now, previous=first)["signals"][0]
    assert learner["state"] == "attention" and not learner["alert"]


def test_trimming_without_a_comparable_baseline_is_not_called_harmless(tmp_path):
    now = MIDDAY
    store = _busy_store(tmp_path, now)
    _write_record(store, 41_339, {"cell": 2_691, "tool": 3_663}, low_value=2_936)
    # A state from the build before the count: [at, total] pairs only.
    previous = {"journal_loss_samples": [[now - 3 * DAY, 38_403]],
                "journal_loss_kind_samples": [[now - 3 * DAY, {"cell": 145, "tool": 3_273}]]}
    learner = memory_health.check(store, now=now, previous=previous)["signals"][0]
    assert learner["detail"]["dropped_3d_low_value"] is None
    assert learner["state"] == "attention" and not learner["alert"]


def test_a_reset_record_never_hides_conversation_loss(tmp_path):
    # The low-value count resets with the total it is compared to, and a
    # nonzero conversation loss is never called harmless.
    now = MIDDAY
    store = _busy_store(tmp_path, now)
    _write_record(store, 1_000, {"cell": 900, "user": 100}, low_value=900)
    first = memory_health.check(store, now=now - 3 * DAY)
    # The record was lost and rewritten: every counter restarts below its
    # baseline, so everything in it is this window's loss.
    _write_record(store, 600, {"cell": 550, "user": 50}, low_value=550)
    learner = memory_health.check(store, now=now, previous=first)["signals"][0]
    assert learner["detail"]["dropped_3d"] == 600 and learner["detail"]["dropped_3d_low_value"] == 550
    assert learner["state"] == "attention" and learner["alert"]
    assert "50 user/assistant/friction entries dropped unread" in learner["reason"]


def test_harmless_trimming_does_not_hide_failing_passes(tmp_path):
    store = SkillStore(tmp_path)
    for i in range(25):
        store.journal_append({"kind": "user", "text": f"Synthetic request {i}"})
    now = time.time()
    with (store.root / ".synthesis-log.jsonl").open("a") as fh:
        for row in ([{"ts": now - 60, "kind": "synthesis", "error": "provider error"}] * 3
                    + [{"ts": now - 30, "kind": "synthesis", "consumed": 20, "changed": []}]):
            fh.write(json.dumps(row) + "\n")
    _write_record(store, 0, {}, low_value=0)
    first = memory_health.check(store, now=now - 3 * DAY)
    _write_record(store, 30, {"cell": 30}, low_value=30)
    learner = memory_health.check(store, now=now, previous=first)["signals"][0]
    assert learner["state"] == "attention" and "3 of 4 learning passes" in learner["reason"]
    assert "30 low-value entries" in learner["reason"]


# ─── the daily budget: billing-aware default, setting overrides ─────────────

SUB = LearnerBudget(200, 8, "subscription_default")
METERED = LearnerBudget(48, 2, "metered_default")


def test_budget_follows_billing_and_the_setting_wins():
    assert learner_budget(None, "subscription") == SUB
    assert learner_budget(None, "metered") == METERED
    assert learner_budget(None, None) == METERED, "unknown billing is never a surprise bill"
    assert learner_budget(0, "subscription") == SUB, "0 is automatic"
    assert learner_budget(96, "subscription") == (96, 4, "setting")
    assert learner_budget(96, "metered") == (96, 4, "setting")
    assert learner_budget(1, None) == (continual_mod.LEARNER_DAILY_CALLS_MIN, 1, "setting")
    assert learner_budget(10**6, None) == (continual_mod.LEARNER_DAILY_CALLS_MAX,
                                           continual_mod.LEARNER_RESERVED_CALLS_MAX, "setting")
    assert learner_budget("lots", "metered") == METERED


def test_pacing_follows_the_effective_budget():
    day = 1792022400.0  # 00:00 UTC
    assert paced_call_allowance(day + 12 * 3600, SUB) == continual_mod.PACE_BURST_CALLS + 96
    assert paced_call_allowance(day + DAY - 1, SUB) == 192
    assert paced_call_allowance(day + 12 * 3600) == continual_mod.PACE_BURST_CALLS + 23
    assert min_pass_interval() == min_pass_interval(METERED) == continual_mod.MIN_PASS_INTERVAL
    assert min_pass_interval(learner_budget(12)) == continual_mod.MIN_PASS_INTERVAL
    assert min_pass_interval(SUB) == pytest.approx(continual_mod.MIN_PASS_INTERVAL * 46 / 192)
    assert min_pass_interval(learner_budget(1000)) == continual_mod.MIN_PASS_INTERVAL_FLOOR


def test_a_subscription_learner_stays_due_where_a_metered_one_is_spent(tmp_path, monkeypatch):
    monkeypatch.setattr(continual_mod, "time", SimpleNamespace(time=lambda: MIDDAY))
    store = SkillStore(tmp_path)
    for i in range(30):
        store.journal_append(edit_cell(i))
    set_budget(store, day=int(MIDDAY // DAY), calls=60, last_at=MIDDAY - 300)
    metered = Synthesizer(NoChanges(), "gpt-test", store, billing="metered")
    subscription = Synthesizer(NoChanges(), "gpt-test", store, billing="subscription")
    assert metered._budget() == METERED and not metered.due(), "48 a day is spent"
    assert subscription._budget() == SUB and subscription.due(), "60 of 200, 5 minutes later"
    set_budget(store, day=int(MIDDAY // DAY), calls=60, last_at=MIDDAY - 120)
    assert not subscription.due(), "still paced: the gap shrinks with the budget, not to zero"


async def test_a_raised_setting_wakes_a_learner_whose_day_was_spent(tmp_path, monkeypatch):
    from fluid.codemode import facts as facts_mod

    clock = [(MIDDAY // DAY) * DAY + 22 * 3600]  # late in the UTC day: pacing has room
    fake = SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic,
                           perf_counter=time.perf_counter, sleep=time.sleep)
    monkeypatch.setattr(continual_mod, "time", fake)
    monkeypatch.setattr(facts_mod, "time", fake)
    store = SkillStore(tmp_path)
    store.journal_append(edit_cell(1))
    set_budget(store, day=int(clock[0] // DAY), calls=46, last_at=clock[0] - 3600)
    setting = [0]
    model = NoChanges()
    synth = Synthesizer(model, "gpt-test", store, min_new_entries=1,
                        daily_calls=lambda: setting[0], billing="metered")
    await synth._run_safe()
    assert model.prompts == [] and rows(store)[-1]["ran"] is False, "48 a day is spent"
    assert synth._budget_spent_day == int(clock[0] // DAY)
    assert not synth.due()
    setting[0] = 96  # the owner raises the budget; same process, same learner
    clock[0] += continual_mod.BUDGET_REFRESH_SECONDS + 1
    assert synth.due(), "the latch re-reads the budget instead of sleeping until midnight"
    assert synth._budget_spent_day is None
    await synth._run_safe()
    assert len(model.prompts) == 1, "a pass runs the same day"
    assert rows(store)[-1]["budget"] == {"daily": 96, "reserved": 4, "source": "setting"}
    assert store._facts.inference_budget()["calls"] == 47


def test_a_spent_latch_rereads_the_budget_only_when_it_is_refreshed(tmp_path, monkeypatch):
    clock = [(MIDDAY // DAY) * DAY + 22 * 3600]
    monkeypatch.setattr(continual_mod, "time", SimpleNamespace(time=lambda: clock[0]))
    store = SkillStore(tmp_path)
    store.journal_append(edit_cell(1))
    set_budget(store, day=int(clock[0] // DAY), calls=46, last_at=clock[0] - 3600)
    setting = [0]
    synth = Synthesizer(NoChanges(), "gpt-test", store, billing="metered", min_new_entries=1,
                        daily_calls=lambda: setting[0])
    resolves = []
    real_resolve = synth._resolve
    synth._resolve = lambda value: resolves.append(1) or real_resolve(value)
    synth._budget(refresh=True)
    synth._latch_spent_day()  # as a refused claim does
    reads = []
    real = store._facts.inference_budget
    store._facts.inference_budget = lambda: reads.append(1) or real()
    resolves.clear()
    for _ in range(50):  # due() runs per journaled cell
        assert not synth.due()
    assert resolves == [] and reads == [], "cached: no setting or store read per cell"
    clock[0] += continual_mod.BUDGET_REFRESH_SECONDS + 1
    assert not synth.due() and len(resolves) == 2 and reads == [], "unchanged budget: still spent"
    setting[0] = 12  # lowered: still spent
    clock[0] += continual_mod.BUDGET_REFRESH_SECONDS + 1
    assert not synth.due() and reads == []
    setting[0] = 96  # raised: the store's calls decide
    clock[0] += continual_mod.BUDGET_REFRESH_SECONDS + 1
    assert synth.due() and len(reads) == 2, "the latch check, then pacing"


async def test_the_setting_overrides_and_applies_from_the_next_pass(tmp_path):
    store = SkillStore(tmp_path)
    setting = [0]
    synth = Synthesizer(NoChanges(), "gpt-test", store, min_new_entries=1,
                        daily_calls=lambda: setting[0], billing="subscription")
    store.journal_append(edit_cell(1))
    await synth._run_safe()
    assert rows(store)[-1]["budget"] == {
        "daily": 200, "reserved": 8, "source": "subscription_default",
    }
    setting[0] = 96  # the owner changes it; no restart, no new learner
    store.journal_append(edit_cell(2))
    await synth._run_safe()
    assert rows(store)[-1]["budget"] == {"daily": 96, "reserved": 4, "source": "setting"}
    # An unreadable setting is unset, never a failed pass.
    synth._daily_calls = lambda: 1 / 0
    store.journal_append(edit_cell(3))
    await synth._run_safe()
    assert rows(store)[-1]["budget"]["source"] == "subscription_default"
    assert "error" not in rows(store)[-1]


async def test_mixed_providers_on_one_store_decide_per_pass(tmp_path):
    store = SkillStore(tmp_path)
    for i in range(3):
        store.journal_append(edit_cell(i))
    set_budget(store, day=int(time.time() // DAY), calls=48, last_at=time.time() - 3600)
    metered_model, subscription_model = NoChanges(), NoChanges()
    await Synthesizer(metered_model, "gpt-test", store, min_new_entries=1,
                      billing="metered")._run_safe()
    assert metered_model.prompts == [], "a metered pass stops at 48"
    assert rows(store)[-1]["budget"]["source"] == "metered_default"
    assert rows(store)[-1]["ran"] is False
    await Synthesizer(subscription_model, "gpt-test", store, min_new_entries=1,
                      billing="subscription")._run_safe()
    assert len(subscription_model.prompts) == 1, "the same store, a subscription pass runs"
    assert rows(store)[-1]["budget"]["source"] == "subscription_default"
    assert store._facts.inference_budget()["calls"] == 49


async def test_the_daily_claim_never_exceeds_the_effective_limit(tmp_path):
    store = SkillStore(tmp_path)
    claimed = 0
    while store._facts.claim_inference_call(reserve=SUB.reserved, limit=SUB.daily):
        claimed += 1
    assert claimed == SUB.daily - SUB.reserved
    while store._facts.claim_inference_call(reserve=0, limit=SUB.daily):  # consolidation
        claimed += 1
    assert claimed == SUB.daily
    assert not store._facts.claim_inference_call(reserve=0, limit=SUB.daily)
    store.journal_append(edit_cell(1))
    model = NoChanges()
    synth = Synthesizer(model, "gpt-test", store, min_new_entries=1, billing="subscription")
    await synth._run_safe()
    assert model.prompts == [] and store._facts.inference_budget()["calls"] == SUB.daily
    # The metered default claims against the fact store's own cap (48).
    seen = []
    store._facts.claim_inference_call = lambda **kw: seen.append(kw) or True
    await Synthesizer(NoChanges(), "gpt-test", store, billing="metered")._complete("x")
    await Synthesizer(NoChanges(), "gpt-test", store, billing="subscription")._complete("x")
    await Synthesizer(NoChanges(), "gpt-test", store, daily_calls=96)._complete("x")
    assert seen == [{"reserve": 2}, {"reserve": 8, "limit": 200}, {"reserve": 4, "limit": 96}]


def test_the_loader_passes_the_configured_budget(tmp_path):
    from fluid.config.loader import _wire_continual
    from fluid.config.model import HarnessConfig

    config = HarnessConfig.from_dict({
        "skills_dir": str(tmp_path), "continual": True, "setting_sources": [],
        "learner_daily_calls": 150,
    })
    _, synth = _wire_continual(config, ctx=SimpleNamespace(extra={}), bus=None,
                               session_id="s", model="gpt-test", client=NoChanges())
    assert synth._budget() == (150, 6, "setting")
