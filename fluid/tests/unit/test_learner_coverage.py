"""The learner read a third of a busy worker's journal (#103).

Its 48 calls a day were spent by 00:04-03:55 UTC, each pass showed about 12
entries, the overflow reap dropped about 1,130 entries a day unread, and the
health check still said "The learner is saving facts". These tests cover the
pacing that spreads the calls over the day, the filter that consumes
low-value entries without showing them, the bookkeeping of passes that made
no call, and the health signal for a journal that drops entries.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from fluid.codemode import SkillStore, Synthesizer, memory_health
from fluid.codemode import facts as facts_mod
from fluid.codemode import continual as continual_mod
from fluid.codemode.continual import low_value_entry, paced_call_allowance
from fluid.codemode.skills import JOURNAL_LOSS_FILE, JOURNAL_MAX_BYTES
from fluid.codemode.transactions import file_transaction
from fluid.providers.base import StreamChunk

DAY = 86400.0
MIDDAY = 1792065600.0  # 2026-10-15 12:00 UTC


class NoChanges:
    name = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def stream(self, request):
        self.prompts.append(request.messages[0].content[0].text)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 100}})
        yield StreamChunk("text_delta", {"text": "NO_CHANGES"})
        yield StreamChunk("usage", {"usage": {"output_tokens": 3}, "stop_reason": "end_turn"})


def rows(store) -> list[dict]:
    path = store.root / ".synthesis-log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def set_budget(store, **budget) -> None:
    with file_transaction(store._facts.lock):
        doc = store._facts._load()
        doc["inference_budget"] = budget
        store._facts._write(doc)


def read_cell(i: int) -> dict:
    return {"kind": "cell", "code": f"print(Read(file_path='f{i}.py'))", "stdout": "1\tx = 1\n" * 50,
            "host_calls": 1, "tools": ["Read"], "calls": [{"name": "Read", "ok": True}]}


def edit_cell(i: int) -> dict:
    return {"kind": "cell", "code": f"Edit(file_path='f{i}.py', old_string='a', new_string='b')",
            "stdout": "edited", "host_calls": 1, "tools": ["Edit", "Read"],
            "calls": [{"name": "Read", "ok": True}, {"name": "Edit", "ok": True}]}


# ─── pacing ─────────────────────────────────────────────────────────────────

def test_allowance_spreads_the_day_and_never_exceeds_the_synthesis_share():
    budget = 48 - continual_mod.CONSOLIDATE_RESERVED_CALLS
    day = 1792022400.0  # 00:00 UTC
    assert paced_call_allowance(day) == continual_mod.PACE_BURST_CALLS
    assert paced_call_allowance(day + 4 * 3600) == continual_mod.PACE_BURST_CALLS + budget // 6
    assert paced_call_allowance(day + 12 * 3600) == continual_mod.PACE_BURST_CALLS + budget // 2
    assert paced_call_allowance(day + DAY - 1) == budget


def test_due_waits_for_the_pace_not_only_the_entry_count(tmp_path, monkeypatch):
    clock = [MIDDAY]
    monkeypatch.setattr(continual_mod, "time", SimpleNamespace(time=lambda: clock[0]))
    store = SkillStore(tmp_path)
    for i in range(30):
        store.journal_append(edit_cell(i))
    synth = Synthesizer(NoChanges(), "gpt-test", store)
    day = int(MIDDAY // DAY)
    allowance = paced_call_allowance(MIDDAY)

    set_budget(store, day=day, calls=3, last_at=MIDDAY - 60)
    assert not synth.due(), "two calls never run within MIN_PASS_INTERVAL"
    set_budget(store, day=day, calls=allowance, last_at=MIDDAY - 3600)
    assert not synth.due(), "ahead of the day's pace"
    set_budget(store, day=day, calls=allowance - 1, last_at=MIDDAY - 3600)
    assert synth.due()
    set_budget(store, day=day - 1, calls=46, last_at=MIDDAY - DAY)
    assert synth.due(), "yesterday's calls do not count"

    # Ahead of the pace with a backlog near the overflow line: spend a later
    # call now rather than let the reap drop entries unread. Not past the cap.
    set_budget(store, day=day, calls=allowance, last_at=MIDDAY - 3600)
    monkeypatch.setattr(store, "_journal_size", lambda: int(JOURNAL_MAX_BYTES * 0.7))
    assert synth.due()
    set_budget(store, day=day, calls=46, last_at=MIDDAY - 3600)
    assert not synth.due(), "a spent budget waits quietly for the next day"
    assert rows(store) == []

    # A pending Remember entry is not paced.
    set_budget(store, day=day, calls=allowance, last_at=MIDDAY - 60)
    synth.mark_urgent()
    assert synth.due()


async def test_a_claim_records_when_it_was_made(tmp_path):
    store = SkillStore(tmp_path)
    before = time.time()
    assert store._facts.claim_inference_call()
    budget = store._facts.inference_budget()
    assert budget["calls"] == 1 and budget["last_at"] >= before


# ─── the low-value filter ───────────────────────────────────────────────────

def test_only_successful_read_only_work_is_low_value():
    assert low_value_entry(read_cell(1))
    assert low_value_entry({"kind": "tool", "name": "Grep", "input": "{}", "output": "x"})
    assert low_value_entry({**read_cell(1), "tools": ["mcp__builtins__TaskList"]})
    assert not low_value_entry({**read_cell(1), "error": "FileNotFoundError: f1.py"})
    assert not low_value_entry({**read_cell(1), "calls": [{"name": "Read", "ok": False}]})
    assert not low_value_entry({**read_cell(1), "code": "skills.summarize(Read(file_path='a'))"})
    assert not low_value_entry({"kind": "tool", "name": "Grep", "error": True})
    assert not low_value_entry(edit_cell(1))
    assert not low_value_entry({"kind": "cell", "code": "x = 1"})
    for kind in ("user", "assistant", "remember", "friction"):
        assert not low_value_entry({"kind": kind, "text": "t"})


async def test_low_value_entries_are_consumed_unshown_and_counted(tmp_path):
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "user", "text": "Rename the helper."})
    for i in range(10):
        store.journal_append(read_cell(i))
    store.journal_append(edit_cell(1))
    store.journal_append({"kind": "assistant", "text": "Renamed."})
    provider = NoChanges()
    await Synthesizer(provider, "gpt-test", store, min_new_entries=1).run()
    [prompt] = provider.prompts
    assert "Rename the helper." in prompt and "Edit(file_path='f1.py'" in prompt
    assert "Read(file_path='f0.py')" not in prompt
    row = rows(store)[-1]
    assert row["consumed"] == 3 and row["filtered"] == 10
    assert store.synth_cursor() == 13


async def test_a_slice_of_low_value_entries_costs_no_call(tmp_path):
    store = SkillStore(tmp_path)
    for i in range(25):
        store.journal_append(read_cell(i))
    provider = NoChanges()
    synth = Synthesizer(provider, "gpt-test", store)
    assert await synth.run() == []
    assert provider.prompts == []
    assert store.synth_cursor() == 25
    [row] = rows(store)
    assert row["ran"] is False and row["filtered"] == 25 and row["consumed"] == 0
    assert store.learner_yield().get("silent_passes", 0) == 0
    assert store._facts.inference_budget() == {}


async def test_a_working_cell_is_shown_clipped_and_a_failed_one_whole():
    long = "x = 1\n" * 1000
    ok = Synthesizer._render_entry({"kind": "cell", "code": long, "stdout": long}, 1)
    assert len("\n".join(ok)) < continual_mod.OK_CELL_CODE_CHARS + 2 * continual_mod.OK_CELL_OUTPUT_CHARS + 200
    failed = Synthesizer._render_entry({"kind": "cell", "code": long, "error": "boom"}, 1)
    assert long.rstrip("\n") in "\n".join(failed)


# ─── passes that made no call ───────────────────────────────────────────────

async def test_a_budget_skip_is_not_a_silent_pass(tmp_path):
    store = SkillStore(tmp_path)
    for i in range(25):
        store.journal_append(edit_cell(i))
    set_budget(store, day=int(time.time() // DAY), calls=48, last_at=time.time() - 3600)
    provider = NoChanges()
    synth = Synthesizer(provider, "gpt-test", store, min_new_entries=1)
    await synth._run_safe()
    assert provider.prompts == []
    row = rows(store)[-1]
    assert row["error"].startswith("InferenceBudgetExhausted") and row["ran"] is False
    assert store.learner_yield().get("silent_passes", 0) == 0
    assert store.synth_cursor() == 0, "the slice waits for the next day"


# ─── the health signal ──────────────────────────────────────────────────────

def _busy_store(tmp_path, now):
    """A worker whose journal overflows while the budget is spent: 46 passes
    a day that read 12 entries each, then a budget skip per session."""
    store = SkillStore(tmp_path)
    for i in range(10):
        store.journal_append({"kind": "user", "text": f"task {i}", "ts": now - 3600})
    with (tmp_path / ".synthesis-log.jsonl").open("a") as fh:
        for day in range(3):
            start = now - (day + 1) * DAY + 60
            for i in range(46):
                fh.write(json.dumps({"ts": start + i * 60, "kind": "synthesis", "changed": [],
                                     "consumed": 12}) + "\n")
            # One skip run on each of the last 3 UTC calendar days (the check
            # counts calendar days), ending just before `now` on day 0.
            for i in range(40):
                fh.write(json.dumps({
                    "ts": now - day * DAY - (i + 1) * 60, "kind": "synthesis", "changed": [],
                    "error": "InferenceBudgetExhausted: Daily inference-model budget reached",
                    "ran": False}) + "\n")
    store.note_learner_pass(True, now=now - 3600)
    return store


def _write_loss(store, dropped):
    (store.root / JOURNAL_LOSS_FILE).write_text(json.dumps({"dropped_unsynthesized": dropped, "schema": 1}))


def test_health_flags_a_journal_dropping_entries_while_the_budget_is_spent(tmp_path):
    # A fixed midday clock: the fixture's rows span 72 hours, and the check
    # counts calendar days, so a wall clock just after UTC midnight pushes
    # the oldest day out of the window.
    now = MIDDAY
    store = _busy_store(tmp_path, now)
    _write_loss(store, 1_646)
    # The first run has nothing to compare with yet: it records a sample and
    # still says the budget ran out.
    first = memory_health.check(store, now=now - 3 * DAY)
    learner = first["signals"][0]
    assert learner["detail"]["dropped_3d"] is None
    assert first["journal_loss_samples"] == [[now - 3 * DAY, 1_646, 0, ""]]

    _write_loss(store, 1_646 + 3 * 1_130)
    result = memory_health.check(store, now=now, previous=first)
    learner = result["signals"][0]
    assert learner["state"] == "attention" and result["state"] == "attention"
    assert learner["detail"]["dropped_3d"] == 3_390
    assert learner["detail"]["read_3d"] == 3 * 46 * 12
    assert learner["detail"]["budget_skips_3d"] == 120
    assert learner["detail"]["budget_spent_days_3d"] == 3
    assert "3390 were dropped unread" in learner["reason"]
    assert "budget ran out on 3 days" in learner["reason"]
    assert learner["detail"]["passes_7d"] == 138, "budget skips are not passes"


def test_health_is_healthy_but_says_so_when_the_budget_ran_out_without_loss(tmp_path):
    # A fixed midday clock: the fixture's rows span 72 hours, and the check
    # counts calendar days, so a wall clock just after UTC midnight pushes
    # the oldest day out of the window.
    now = MIDDAY
    store = _busy_store(tmp_path, now)
    _write_loss(store, 1_646)
    first = memory_health.check(store, now=now - 3 * DAY)
    result = memory_health.check(store, now=now, previous=first)
    learner = result["signals"][0]
    assert learner["state"] == "healthy" and learner["detail"]["dropped_3d"] == 0
    assert learner["reason"] == (
        "The learner is saving facts. The daily learning budget ran out on 3 days of the last 3."
    )


def test_loss_samples_stay_bounded_to_the_window(tmp_path):
    store = SkillStore(tmp_path)
    _write_loss(store, 5)
    now = time.time()
    previous = {"journal_loss_samples": [[now - h * 3600, 5] for h in range(1200, 0, -3)]}
    samples = memory_health.check(store, now=now, previous=previous)["journal_loss_samples"]
    assert len(samples) <= memory_health.LOSS_SAMPLES_KEPT
    assert samples[-1][:3] == [now, 5, 0]
    # A record that was reset counts from zero instead of going negative.
    _write_loss(store, 2)
    state = memory_health.check(store, now=now + 7200, previous={"journal_loss_samples": samples})
    assert state["signals"][0]["detail"]["dropped_3d"] == 2


# ─── review follow-ups: multi-project passes, window, urgency ───────────────

class Scripted(NoChanges):
    """Answers NO_CHANGES, except the calls listed in ``fail`` (1-based),
    which come back as an HTTP 500 error chunk."""

    def __init__(self, fail=()) -> None:
        super().__init__()
        self.fail = set(fail)

    async def stream(self, request):
        self.prompts.append(request.messages[0].content[0].text)
        if len(self.prompts) in self.fail:
            yield StreamChunk("message_start", {"usage": {"input_tokens": 100}})
            yield StreamChunk("error", {"status": 500, "message": "upstream failed"})
            return
        yield StreamChunk("message_start", {"usage": {"input_tokens": 100}})
        yield StreamChunk("text_delta", {"text": "NO_CHANGES"})
        yield StreamChunk("usage", {"usage": {"output_tokens": 3}, "stop_reason": "end_turn"})


def _two_projects(store, per=15):
    for i in range(2 * per):
        store.journal_append({**edit_cell(i), "project": "projA" if i < per else "projB"})


async def test_a_pass_stops_before_a_project_the_day_cannot_pay_for(tmp_path):
    store = SkillStore(tmp_path)
    _two_projects(store)
    now = time.time()
    set_budget(store, day=int(now // DAY), calls=45, last_at=now - 3600)
    provider = Scripted()
    await Synthesizer(provider, "gpt-test", store, min_new_entries=1)._run_safe()
    assert len(provider.prompts) == 1 and "f0.py" in provider.prompts[0]
    assert store.synth_cursor() == 15, "project A is read; project B waits"
    row = rows(store)[-1]
    assert row["consumed"] == 15 and row["deferred"] == 15 and row.get("ran", True) is True
    assert row["usage"]["input_tokens"] == 100


async def test_a_budget_stop_after_one_project_keeps_what_was_read(tmp_path, monkeypatch):
    # The budget looks open when the pass starts (another process spends it
    # meanwhile): project A's call is applied, project B's claim is refused.
    store = SkillStore(tmp_path)
    _two_projects(store)
    claims = iter([True, False])
    monkeypatch.setattr(store._facts, "claim_inference_call", lambda reserve=0: next(claims))
    provider = Scripted()
    synth = Synthesizer(provider, "gpt-test", store, min_new_entries=1)
    await synth._run_safe()
    assert len(provider.prompts) == 1
    assert store.synth_cursor() == 15
    row = rows(store)[-1]
    assert row["error"].startswith("InferenceBudgetExhausted")
    assert row["consumed"] == 15 and row["usage"]["input_tokens"] == 100
    assert "ran" not in row, "a call was made: this pass ran"
    assert store.learner_yield()["silent_passes"] == 1
    assert not synth.due(), "the instance waits for the next day"
    detail = memory_health.check(store)["signals"][0]["detail"]
    assert detail["read_3d"] == 15 and detail["passes_7d"] == 1


async def test_a_failure_on_a_later_project_consumes_the_answered_prefix(tmp_path):
    store = SkillStore(tmp_path)
    _two_projects(store)
    provider = Scripted(fail={2})
    await Synthesizer(provider, "gpt-test", store, min_new_entries=1)._run_safe()
    assert len(provider.prompts) == 2
    assert store.synth_cursor() == 15, "project A is not paid for again"
    row = rows(store)[-1]
    assert row["consumed"] == 15 and "HTTP 500" in row["error"] and row["retry_after_s"] > 0
    assert row["usage"]["input_tokens"] == 200
    assert store.learner_backoff("gpt-test"), "the failure still backs off"


async def test_interleaved_projects_consume_only_the_contiguous_answered_prefix(tmp_path):
    store = SkillStore(tmp_path)
    for i in range(20):
        store.journal_append({**edit_cell(i), "project": "projA" if i % 2 == 0 else "projB"})
    await Synthesizer(Scripted(fail={2}), "gpt-test", store, min_new_entries=1)._run_safe()
    # Line 2 belongs to projB, which failed: only line 1 is behind the cursor.
    assert store.synth_cursor() == 1


def test_the_request_size_follows_the_learner_models_window():
    small = continual_mod.PROMPT_CHAR_LIMIT
    large = continual_mod.LARGE_PROMPT_CHAR_LIMIT
    assert continual_mod.prompt_char_limit(None) == small
    assert continual_mod.prompt_char_limit(32_768) == small
    assert small < continual_mod.prompt_char_limit(64_000) < large
    assert continual_mod.prompt_char_limit(272_000) == large
    # A 32k-token window holds the default request (at a typical 3.5 chars
    # a token) plus the output room, as it did before the larger prompt.
    assert small / 3.5 + 16_000 + continual_mod.SYSTEM_PROMPT_TOKENS <= 32_768


def test_an_unknown_model_keeps_the_small_request_and_a_known_window_raises_it(tmp_path):
    store = SkillStore(tmp_path)
    local = Synthesizer(NoChanges(), "qwen3-coder:30b", store)
    hosted = Synthesizer(NoChanges(), "qwen3-coder:30b", store, context_window=272_000)
    table = Synthesizer(NoChanges(), "claude-opus-5", store)
    assert local._prompt_limit == continual_mod.PROMPT_CHAR_LIMIT
    assert hosted._prompt_limit == table._prompt_limit == continual_mod.LARGE_PROMPT_CHAR_LIMIT
    for i in range(300):
        store.journal_append({**edit_cell(i), "error": "x" * 400})
    assert local._journal_budget() < continual_mod.PROMPT_CHAR_LIMIT


async def test_a_remember_runs_unpaced_only_until_it_is_read(tmp_path, monkeypatch):
    clock = [MIDDAY]
    fake = SimpleNamespace(time=lambda: clock[0])
    monkeypatch.setattr(continual_mod, "time", fake)
    monkeypatch.setattr(facts_mod, "time", fake)  # claims stamp the same day
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "remember", "text": "The owner prefers X."})
    for i in range(200):
        store.journal_append({**edit_cell(i), "stdout": "y" * 300})
    provider = NoChanges()
    synth = Synthesizer(provider, "gpt-test", store)
    monkeypatch.setattr(synth, "_prompt_limit", 20_000)  # a backlog several passes long
    set_budget(store, day=int(MIDDAY // DAY), calls=paced_call_allowance(MIDDAY), last_at=MIDDAY - 60)
    assert not synth.due(), "paced out"
    synth.mark_urgent()
    passes = 0
    while synth.due() and passes < 20:
        await synth._run_safe()
        passes += 1
    assert passes == 1, "one call reads the Remember; the rest of the backlog is paced"
    assert "The owner prefers X." in provider.prompts[0]
    assert store.journal_count() - store.synth_cursor() > 0


async def test_a_remember_deferred_past_the_prompt_keeps_urgency(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    for i in range(60):
        store.journal_append({**edit_cell(i), "stdout": "y" * 300})
    store.journal_append({"kind": "remember", "text": "The owner prefers Y."})
    provider = NoChanges()
    synth = Synthesizer(provider, "gpt-test", store)
    monkeypatch.setattr(synth, "_prompt_limit", 20_000)
    synth.mark_urgent()
    await synth._run_safe()
    assert synth._urgent and synth.due(), "the Remember line is still unread"
    while synth._urgent:
        await synth._run_safe()
    assert any("The owner prefers Y." in p for p in provider.prompts)


def test_budget_spent_days_come_from_the_store_without_skip_rows(tmp_path):
    store = SkillStore(tmp_path)
    for _ in range(46):
        assert store._facts.claim_inference_call(reserve=2)
    assert not store._facts.claim_inference_call(reserve=2)
    today = int(time.time() // DAY)
    assert store._facts.inference_budget_spent_days() == [today]
    detail = memory_health.check(store)["signals"][0]["detail"]
    assert detail["budget_spent_days_3d"] == 1 and detail["budget_skips_3d"] == 0


def test_clipping_keeps_the_head_and_the_tail():
    out = "collecting...\n" + "." * 2000 + "\n=== 12 passed in 3.1s ==="
    rendered = "\n".join(Synthesizer._render_entry({"kind": "cell", "code": "x", "stdout": out}, 1))
    assert "collecting" in rendered and "12 passed in 3.1s" in rendered and "chars clipped" in rendered


def test_worker_reports_and_reads_outside_the_checkout_are_shown(tmp_path):
    assert not low_value_entry({**read_cell(1), "tools": ["TaskOutput"]})
    assert not low_value_entry({**read_cell(1), "tools": ["TaskGet"]})
    store = SkillStore(tmp_path)
    store.journal_context(session_id="s", project="p", cwd="/work/repo")
    store.journal_append({**read_cell(1), "code": "Read(file_path='/work/repo/src/a.py')"})
    store.journal_append({**read_cell(2), "code": "Read(file_path='/home/u/.config/app.toml')"})
    store.journal_append({"kind": "tool", "name": "Read", "input": '{"file_path": "/home/u/notes.md"}',
                          "output": "x"})
    # The platform's temp folder (not /tmp on Windows) counts as scratch.
    scratch_file = str(Path(tempfile.gettempdir()) / "clone-1" / "repo" / "a.py")
    store.journal_append({**read_cell(3), "code": f"Read(file_path={scratch_file!r})"})
    inside, home, notes, scratch = store.journal_entries()
    assert low_value_entry(inside) and "outside_cwd" not in inside
    assert low_value_entry(scratch) and "outside_cwd" not in scratch
    assert home["outside_cwd"] and not low_value_entry(home)
    assert notes["outside_cwd"] and not low_value_entry(notes)
