"""The memory learner that never wrote a fact (issue #1237, 2026-09-22).

Both deployed workers ran the learner for eleven days without one non-empty
``changed`` row in ``.synthesis-log.jsonl``. These tests drive the real path —
journal appends through ToolJournal, the kick, the synthesis pass, the reap and
``memories.json`` — with a fake inference client that answers the way real
providers did:

* the Codex backend rejected every learner request with HTTP 400; fluid
  providers report that as an ``error`` chunk, which the learner ignored, so
  each rejection looked like an empty completion and was retried on the next
  kick (48 calls in five minutes, half of them consolidation calls riding the
  failed passes);
* a pass that did get an answer never showed the model the journal, because
  95 saved facts filled the 32,000-character prompt cut first;
* a journal that no pass consumed filled to its 1 MiB cap and then refused
  every new entry.
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace

import pytest

from fluid._filelock import file_lock
from fluid.codemode import SkillStore, Synthesizer
from fluid.codemode import continual as continual_mod
from fluid.codemode import skills as skills_mod
from fluid.codemode.journal import ToolJournal
from fluid.providers.base import StreamChunk

PROJECT = "project:p1"
# The learner's request cap (continual.PROMPT_CHAR_LIMIT; 32,000 before this fix too).
REQUEST_CHARS = 32_000


# ─── a fake inference client with realistic replies ──────────────────────────


def reply_text(text: str, *, output_tokens: int = 60, input_tokens: int = 9000):
    def chunks(request):
        yield StreamChunk("message_start", {"usage": {"input_tokens": input_tokens}})
        for start in range(0, len(text), 40):
            yield StreamChunk("text_delta", {"text": text[start:start + 40]})
        yield StreamChunk("usage", {
            "usage": {"output_tokens": output_tokens}, "stop_reason": "end_turn",
        })
        yield StreamChunk("message_stop", {})
    return chunks


def reply_reasoning_only(output_tokens: int = 900):
    """A reasoning model that spends its output on thinking and says nothing."""
    def chunks(request):
        yield StreamChunk("message_start", {"usage": {"input_tokens": 9000}})
        for _ in range(3):
            yield StreamChunk("thinking_delta", {"text": "Considering the journal..."})
        yield StreamChunk("usage", {
            "usage": {"output_tokens": output_tokens}, "stop_reason": "end_turn",
        })
        yield StreamChunk("message_stop", {})
    return chunks


def reply_http_error(status: int = 400, body: str = '{"detail":"Unsupported parameter: max_output_tokens"}'):
    """What fluid's HTTP providers yield for a non-200 answer."""
    def chunks(request):
        yield StreamChunk("error", {"status": status, "message": body, "retriable": False})
    return chunks


class FakeInference:
    name = "fake"

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.requests: list = []

    async def stream(self, request):
        self.requests.append(request)
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        for chunk in reply(request):
            yield chunk


def prompt_of(request) -> str:
    return request.messages[0].content[0].text


def log_rows(store: SkillStore) -> list[dict]:
    path = store.root / ".synthesis-log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def memories_doc(store: SkillStore) -> dict:
    return json.loads(store.memories_path.read_text(encoding="utf-8"))


def desktop_shaped_memories(store: SkillStore, n: int = 95) -> None:
    """The desktop store on 2026-09-22: 95 saved facts, mostly explicit, most
    of them project-scoped, a few hundred characters each."""
    filler = (
        "the worker keeps its data under the application directory and the "
        "managed database listens on the configured port; restart the "
        "service after changing the connection settings, and prefer the "
        "bundled interpreter over the system one when running the tests "
        "for this repository because the system one lacks the test deps"
    )
    for i in range(n):
        store.save_memory(
            f"Fact {i}: {filler}.",
            scope=PROJECT if i % 4 else "global",
            provenance="user",
            protected=True,
        )


def run_session(store: SkillStore, synth: Synthesizer, *, tools: int = 24, fact: str) -> None:
    """One realistic JSON-mode turn journaled through ToolJournal."""
    store.journal_context(session_id="ses_1", project=PROJECT)
    journal = ToolJournal(store, synth)
    journal.begin_turn()
    journal.record_event("user", f"Please remember this for next time: {fact}")
    for i in range(tools):
        journal.record_tool(
            "Bash", {"command": f"python -m pytest tests/unit/test_{i}.py -q"},
            f"{i + 3} passed in 0.{i}s", False,
        )
    journal.record_event("assistant", f"All suites pass. Noted: {fact}")


async def settle(synth: Synthesizer) -> None:
    if synth._task is not None:
        await synth._task


# ─── the proven root causes, end to end ──────────────────────────────────────


async def test_desktop_store_pass_sees_the_journal_and_writes_memories_json(tmp_path) -> None:
    """95 facts used to fill the whole 32,000-char prompt, so the journal
    never reached the model and every pass answered NO_CHANGES."""
    store = SkillStore(tmp_path)
    desktop_shaped_memories(store)
    old_memory_lines = sum(
        len(f"[{m.id}] recalls={m.uses} scope={m.scope}: {m.text}") + 1
        for m in store.memories()
    )
    assert old_memory_lines > REQUEST_CHARS  # the broken shape

    fact = "run pytest with PYTHONPATH=fluid from the repository root"
    provider = FakeInference(reply_text(
        '<memory scope="project">The user runs pytest with PYTHONPATH=fluid '
        'from the repository root.</memory>'
    ))
    synth = Synthesizer(provider, "gpt-test", store, project=PROJECT, session_model="gpt-test")
    run_session(store, synth, fact=fact)
    await settle(synth)

    prompt = prompt_of(provider.requests[0])
    assert len(prompt) <= REQUEST_CHARS
    assert f"[user] Please remember this for next time: {fact}" in prompt
    assert "[assistant] All suites pass." in prompt
    assert "older facts not listed" in prompt
    assert " explicit: Fact " in prompt  # explicit saves are marked read-only
    # Explicit facts cannot be merged or retired, so 95 of them do not make a
    # consolidation call due (they used to, every week).
    assert len(provider.requests) == 1

    learned = [e for e in memories_doc(store)["entries"] if not e.get("protected")]
    assert [e["text"] for e in learned] == [
        "The user runs pytest with PYTHONPATH=fluid from the repository root."
    ]
    assert learned[0]["scope"] == PROJECT and learned[0]["provenance"] == "user"
    row = log_rows(store)[-1]
    assert row["changed"] == [f"memory:{learned[0]['id']}"]
    assert row["stop_reason"] == "end_turn" and row["consumed"] >= 20
    assert "error" not in row
    assert store.synth_cursor() <= store.journal_count()
    assert store.journal_count() - store.synth_cursor() == 0


async def test_provider_rejection_is_logged_backs_off_and_skips_consolidation(
    tmp_path, caplog,
) -> None:
    store = SkillStore(tmp_path)
    for i in range(10):  # enough inferred facts that consolidation is due
        store.save_memory(f"Inferred convention number {i} about the build.", scope=PROJECT)
    assert store.last_consolidated_at() == 0.0
    codex = FakeInference(reply_http_error())
    synth = Synthesizer(codex, "gpt-codex", store, project=PROJECT)
    run_session(store, synth, fact="use the bundled interpreter")

    with caplog.at_level(logging.WARNING, logger="fluid.codemode.continual"):
        await settle(synth)

    assert len(codex.requests) == 1, "a failed pass must not be followed by a consolidation call"
    row = log_rows(store)[-1]
    assert row["kind"] == "synthesis" and row["consumed"] == 0
    assert row["provider_status"] == 400
    assert "HTTP 400" in row["error"] and "Unsupported parameter: max_output_tokens" in row["error"]
    assert row["retry_after_s"] == continual_mod.FAILURE_BACKOFF_BASE
    assert row["model"] == "gpt-codex"
    assert any("Memory learner synthesis pass failed" in r.message for r in caplog.records)
    assert store.synth_cursor() == 0

    # The backoff lives in the store: a new session's synthesizer for the
    # same model waits too, while another model is unaffected.
    assert not synth.due()
    assert not Synthesizer(codex, "gpt-codex", store).due()
    assert Synthesizer(FakeInference(), "claude-test", store).due()

    # Past the delay the next failure doubles it.
    meta = json.loads((tmp_path / "meta.json").read_text())
    meta["learner_backoff"]["gpt-codex"]["until"] = 0
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    assert synth.due()
    await synth._run_safe()
    assert log_rows(store)[-1]["retry_after_s"] == 2 * continual_mod.FAILURE_BACKOFF_BASE

    # Recovery: the provider answers, the fact lands, the backoff clears and
    # consolidation finally runs (one more call).
    meta = json.loads((tmp_path / "meta.json").read_text())
    meta["learner_backoff"]["gpt-codex"]["until"] = 0
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    fixed = FakeInference(
        reply_text("<memory>The user prefers the bundled interpreter.</memory>"),
        reply_text("NO_CHANGES"),
    )
    healed = Synthesizer(fixed, "gpt-codex", store, project=PROJECT)
    changed = await healed._run_safe()
    assert changed and changed[0].startswith("memory:")
    assert len(fixed.requests) == 2
    assert store.learner_backoff("gpt-codex") == {}
    assert store.last_consolidated_at() > 0
    assert any(
        e["text"] == "The user prefers the bundled interpreter."
        for e in memories_doc(store)["entries"]
    )


async def test_a_day_of_rejections_costs_a_handful_of_calls_not_the_budget(
    tmp_path, monkeypatch,
) -> None:
    """2026-09-13 on the desktop: 48 calls between 19:45:16 and 19:50:10 UTC.
    A kick every five minutes for a whole day of a failing provider now costs
    about ten calls, leaving the budget for when the provider recovers."""
    clock = SimpleNamespace(now=time.time())
    fake_time = SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now)
    monkeypatch.setattr(continual_mod, "time", fake_time)
    store = SkillStore(tmp_path)
    for i in range(10):
        store.save_memory(f"Inferred convention number {i} about the build.", scope=PROJECT)
    codex = FakeInference(reply_http_error())
    synth = Synthesizer(codex, "gpt-codex", store, project=PROJECT)
    for i in range(30):
        store.journal_append({"kind": "cell", "code": f"x = {i}", "host_calls": 0})
    for _ in range(288):  # every five minutes for 24 hours
        clock.now += 300
        if synth.due():
            await synth._run_safe()
    assert 5 <= len(codex.requests) <= 12
    budget = memories_doc(store)["inference_budget"]
    assert budget["calls"] == len(codex.requests) < 48
    assert all(r["kind"] == "synthesis" for r in log_rows(store))


async def test_reasoning_only_answer_is_a_logged_failure_not_a_verdict(tmp_path) -> None:
    store = SkillStore(tmp_path)
    provider = FakeInference(reply_reasoning_only(900))
    synth = Synthesizer(provider, "gpt-reasoner", store, project=PROJECT)
    run_session(store, synth, fact="the preview runs on port 5178")
    await settle(synth)
    row = log_rows(store)[-1]
    assert row["error"] == "synthesis returned no text (stop_reason=end_turn, output_tokens=900)"
    assert row["consumed"] == 0 and row["retry_after_s"] > 0
    assert store.synth_cursor() == 0
    assert [e for e in memories_doc(store)["entries"]] == []


# ─── the journal backlog: a full journal no pass consumed ────────────────────


async def test_full_unconsumed_journal_drops_oldest_and_keeps_learning(
    tmp_path, monkeypatch, caplog,
) -> None:
    """The Unraid shape: every pass failed, the cursor never moved, the
    journal reached its cap and refused every new entry from then on."""
    monkeypatch.setattr(skills_mod, "JOURNAL_MAX_BYTES", 40_000)
    store = SkillStore(tmp_path)
    store.journal_context(session_id="ses_old", project=PROJECT)
    store.journal_append({"kind": "remember", "text": "The user's staging host is stage-2."})
    fat = "print('week-old evidence')\n" * 20
    while store._journal_size() < 39_000:
        store.journal_append({"kind": "cell", "code": fat, "host_calls": 0})
    assert store.synth_cursor() == 0
    before = skills_mod.journal_metrics()["dropped_unsynthesized"]

    provider = FakeInference(reply_text(
        '<memory scope="project">The preview server runs on port 5178.</memory>'
    ))
    synth = Synthesizer(provider, "gpt-test", store, project=PROJECT, min_new_entries=1)
    with caplog.at_level(logging.WARNING, logger="fluid.codemode.skills"):
        run_session(store, synth, tools=2, fact="the preview server runs on port 5178")
    assert any("backlog overflow" in r.message for r in caplog.records)
    assert skills_mod.journal_metrics()["dropped_unsynthesized"] > before
    assert store._journal_size() <= skills_mod.JOURNAL_MAX_BYTES
    kinds = [e["kind"] for e in store.journal_entries()]
    assert kinds[0] == "remember", "explicit entries are never dropped for room"
    assert kinds[-1] == "assistant", "new evidence lands again"

    # A pass that now works reads the fresh evidence, not only week-old cells.
    await settle(synth)
    while synth.due():
        await synth._run_safe()
    seen = "\n".join(prompt_of(r) for r in provider.requests)
    assert "the preview server runs on port 5178" in seen
    assert any("5178" in e["text"] for e in memories_doc(store)["entries"])


async def test_oversized_slice_consumes_only_what_the_prompt_showed(tmp_path) -> None:
    store = SkillStore(tmp_path)
    store.journal_context(session_id="ses_1", project=PROJECT)
    for i in range(40):
        store.journal_append({
            "kind": "cell", "code": f"# step {i}\n" + "x = compute()\n" * 80,
            "stdout": "ok", "host_calls": 1,
        })
    provider = FakeInference(reply_text("NO_CHANGES"))
    synth = Synthesizer(provider, "gpt-test", store, project=PROJECT, min_new_entries=1)
    await synth.run()
    first = log_rows(store)[-1]
    shown = first["consumed"]
    assert 0 < shown < 40 and first["deferred"] == 40 - shown
    assert store.synth_cursor() == shown
    prompt = prompt_of(provider.requests[0])
    assert "# step 0" in prompt and f"# step {shown}\n" not in prompt
    assert len(prompt) <= continual_mod.PROMPT_CHAR_LIMIT
    await synth.run()
    assert f"# step {shown}\n" in prompt_of(provider.requests[1])
    assert store.synth_cursor() > shown


# ─── parsing and observability ───────────────────────────────────────────────


async def test_memory_tags_accept_any_attribute_order(tmp_path) -> None:
    store = SkillStore(tmp_path)
    old = store.save_memory("The build uses make.", scope=PROJECT)
    store.journal_context(session_id="ses_1", project=PROJECT)
    store.journal_append({"kind": "user", "text": "We moved the build from make to just."})
    provider = FakeInference(reply_text(
        f'<memory scope="project" id="{old.id}">The build uses just, not make.</memory>\n'
        '<memory id="bad id!">ignored</memory>'
    ))
    synth = Synthesizer(provider, "gpt-test", store, project=PROJECT, min_new_entries=1)
    changed = await synth.run()
    assert changed == [f"memory:{old.id}"]
    assert [m.text for m in store.memories()] == ["The build uses just, not make."]
    assert "malformed id" in log_rows(store)[-1]["error"]


async def test_forget_scrubs_legacy_previews_but_keeps_the_pass_log(tmp_path) -> None:
    store = SkillStore(tmp_path)
    fact = store.save_memory("A throwaway fact.", scope="global", protected=True)
    log = tmp_path / ".synthesis-log.jsonl"
    log.write_text(
        '{"ts": 1, "kind": "synthesis", "changed": [], "output_head": "A throwaway fact."}\n'
        '{"ts": 2, "kind": "synthesis", "changed": [], "error": "provider error (HTTP 400): x"}\n',
        encoding="utf-8",
    )
    assert store.delete_memory(fact.id, explicit=True)
    rows = log_rows(store)
    assert [r["ts"] for r in rows] == [1, 2]
    assert "output_head" not in rows[0] and "throwaway" not in log.read_text()
    # Maintenance repeats the purge every five minutes; the log must survive.
    store.purge_forgotten_evidence()
    assert [r["ts"] for r in log_rows(store)] == [1, 2]


async def test_a_held_synthesis_lock_leaves_a_trace(tmp_path) -> None:
    store = SkillStore(tmp_path)
    for i in range(3):
        store.journal_append({"kind": "cell", "code": f"x = {i}", "host_calls": 0})
    provider = FakeInference(reply_text("NO_CHANGES"))
    synth = Synthesizer(provider, "gpt-test", store, min_new_entries=1, pass_timeout=0.0)
    with file_lock(tmp_path / "synthesis.lock") as held:
        assert held
        assert await synth.run() == []
        assert await synth.run() == []  # rate-limited: one row, not two
    assert provider.requests == []
    rows = log_rows(store)
    assert len(rows) == 1 and rows[0]["error"].startswith("skipped: synthesis.lock has been held")
    assert synth.last_pass_skipped


async def test_failed_consolidation_waits_instead_of_riding_every_pass(tmp_path) -> None:
    """2026-09-17 on the desktop: 15 good synthesis passes, each followed by
    a failed consolidation call (26 in all)."""
    store = SkillStore(tmp_path)
    for i in range(10):
        store.save_memory(f"Inferred convention number {i} about the build.", scope=PROJECT)
    provider = FakeInference(
        reply_text("NO_CHANGES"), reply_http_error(429, "rate limited"),
        reply_text("NO_CHANGES"), reply_text("NO_CHANGES"),
    )
    synth = Synthesizer(provider, "gpt-test", store, project=PROJECT, min_new_entries=1)
    for round_ in range(3):
        store.journal_append({"kind": "user", "text": f"round {round_}"})
        await synth._run_safe()
    kinds = [r["kind"] for r in log_rows(store)]
    assert kinds == ["synthesis", "consolidation", "synthesis", "synthesis"]
    failed = log_rows(store)[1]
    assert failed["provider_status"] == 429
    assert failed["retry_after_s"] == continual_mod.CONSOLIDATE_RETRY_SECONDS
    assert len(provider.requests) == 4
    assert store.consolidation_retry_at() > time.time()


def test_kick_outside_an_event_loop_says_so_once(tmp_path, caplog) -> None:
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "cell", "code": "x = 1", "host_calls": 0})
    synth = Synthesizer(FakeInference(), "gpt-test", store, min_new_entries=1)
    with caplog.at_level(logging.WARNING, logger="fluid.codemode.continual"):
        synth.kick()
        synth.kick()
    assert len([r for r in caplog.records if "no running event loop" in r.message]) == 1


@pytest.mark.parametrize("n", [0, 3])
def test_unsynthesized_backlog_below_the_overflow_line_is_kept(tmp_path, monkeypatch, n) -> None:
    store = SkillStore(tmp_path)
    for i in range(10):
        store.journal_append({"kind": "cell", "code": f"{i}:" + "q" * 200, "host_calls": 0})
    store.set_synth_cursor(n)
    size = store._journal_size()
    monkeypatch.setattr(skills_mod, "JOURNAL_MAX_BYTES", int(size / 0.8))
    store.reap_journal()
    # Only consumed lines may go below the overflow line.
    assert store.journal_count() >= 10 - n
    assert store.synth_cursor() == 0 or store.journal_count() == 10


# ─── a learner delete is not a user Forget ──────────────────────────────────


def _cells(store: SkillStore, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        store.journal_append({"kind": "cell", "code": f"x = {i}", "host_calls": 0})


async def test_learner_delete_keeps_journal_and_cursor(tmp_path) -> None:
    """A learner <delete-memory> used to run as a user Forget: it purged every
    journal line older than now (the unsynthesized backlog included) and the
    pass then wrote its pre-purge cursor back, 40 lines past the empty
    journal, so the next 40 entries were never read."""
    store = SkillStore(tmp_path)
    fact = store.save_memory("The staging box is called Atlas.", scope="global")
    store.journal_context(session_id="ses_1", project=PROJECT)
    _cells(store, 45)
    provider = FakeInference(reply_text(f'<delete-memory id="{fact.id}"/>'))
    synth = Synthesizer(provider, "gpt-test", store, project=PROJECT, min_new_entries=10)
    assert await synth.run() == [f"-memory:{fact.id}"]

    assert fact.id not in [m.id for m in store.memories()]
    assert not store._facts.snapshot().get("forgotten_before")
    consumed = log_rows(store)[-1]["consumed"]
    assert 0 < consumed < 45
    assert store.journal_count() == 45
    assert store.synth_cursor() == consumed
    # The maintenance sweep must not finish the wipe either.
    store.reap()
    store.purge_forgotten_evidence()
    assert store.journal_count() == 45 and store.synth_cursor() == consumed

    _cells(store, 10, start=45)
    assert store.journal_count() - store.synth_cursor() == 55 - consumed
    assert synth.due()
    indexed, _ = store.journal_slice_indexed(store.synth_cursor(), 40)
    assert indexed[0][1]["code"] == f"x = {consumed}"


async def test_user_forget_still_purges_evidence_and_rebases_cursor(tmp_path) -> None:
    store = SkillStore(tmp_path)
    fact = store.save_memory("The staging box is called Atlas.", scope="global", protected=True)
    _cells(store, 10)
    store.set_synth_cursor(6)
    assert store.delete_memory(fact.id, explicit=True)
    assert store._facts.snapshot().get("forgotten_before")
    assert store.journal_count() == 0
    assert store.synth_cursor() == 0
    time.sleep(0.02)  # later entries carry a later timestamp than the cutoff
    _cells(store, 3, start=10)
    store.purge_forgotten_evidence()  # the maintenance sweep repeats it
    assert store.journal_count() == 3 and store.synth_cursor() == 0
    assert [e["code"] for e in store.journal_entries()] == ["x = 10", "x = 11", "x = 12"]


async def test_cursor_past_the_journal_end_is_clamped(tmp_path) -> None:
    # The state older builds left behind after a learner delete: the cursor
    # points past the end, pending is negative and the learner is never due.
    store = SkillStore(tmp_path)
    _cells(store, 10)
    store.set_synth_cursor(40)
    synth = Synthesizer(FakeInference(), "gpt-test", store, min_new_entries=1)
    assert not synth.due()
    store.reap()  # the five-minute maintenance sweep
    assert store.synth_cursor() == store.journal_count() == 10
    _cells(store, 1, start=10)
    assert synth.due()


async def test_user_forget_during_a_pass_does_not_strand_the_cursor(tmp_path) -> None:
    store = SkillStore(tmp_path)
    fact = store.save_memory("The staging box is called Atlas.", scope="global", protected=True)
    store.journal_context(session_id="ses_1", project=PROJECT)
    _cells(store, 30)

    synth = Synthesizer(FakeInference(reply_text("NO_CHANGES")), "gpt-test", store,
                        project=PROJECT, min_new_entries=1)
    apply = synth._apply

    async def apply_while_the_user_forgets(text, group):
        # Another session's Forget lands while this pass applies its output.
        store.delete_memory(fact.id, explicit=True)
        return await apply(text, group)

    synth._apply = apply_while_the_user_forgets
    await synth.run()
    assert store.journal_count() == 0
    assert 0 <= store.synth_cursor() <= store.journal_count()
