"""Measure learner triggers and resolved evidence without paid provider calls."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fluid.codemode import continual
from fluid.codemode.continual import Synthesizer, low_value_entry
from fluid.codemode.session import CodeModeSession
from fluid.codemode.skills import SkillStore
from fluid.permissions.engine import PermissionEngine
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry
from tests.unit.test_learner_coverage import NoChanges, read_cell, set_budget


@pytest.mark.parametrize("urgent", [False, True])
def test_new_instances_observe_shared_spent_budget_and_rollover(tmp_path, monkeypatch, urgent):
    clock = [time.time()]
    monkeypatch.setattr(continual, "time", SimpleNamespace(time=lambda: clock[0]))
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "remember", "text": "Fixture preference: retain concise summaries."})
    limit = continual.MAX_INFERENCE_CALLS_PER_DAY - continual.CONSOLIDATE_RESERVED_CALLS
    set_budget(store, day=int(clock[0] // 86400), calls=limit, last_at=clock[0] - 3600)
    for _ in range(3):
        learner = Synthesizer(NoChanges(), "fake", store, min_new_entries=1)
        if urgent:
            learner.mark_urgent()
        assert not learner.due()
        assert store.synth_cursor() == 0
    clock[0] += 86400
    assert learner.due(), "retained evidence becomes due after UTC rollover"


def test_unknown_shared_budget_falls_back_to_atomic_claim(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "remember", "text": "Fixture preference."})
    learner = Synthesizer(NoChanges(), "fake", store, min_new_entries=12)
    learner.mark_urgent()
    def unavailable():
        raise OSError("fixture unavailable")
    monkeypatch.setattr(store._facts, "inference_budget", unavailable)
    assert learner.due()


def test_read_noise_does_not_trigger_tiny_substantive_batch(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "user", "text": "Inspect the fixture."})
    for i in range(11):
        store.journal_append(read_cell(i))
    learner = Synthesizer(NoChanges(), "fake", store, min_new_entries=12)
    assert not learner.due()
    for i in range(11):
        store.journal_append({"kind": "user", "text": f"Substantive fixture request {i}."})
    assert learner.due()


def test_sparse_batch_eventually_due_and_force_run_is_preserved(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(continual, "time", SimpleNamespace(time=lambda: clock[0]))
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "user", "text": "Useful sparse preference."})
    learner = Synthesizer(NoChanges(), "fake", store, min_new_entries=12)
    assert not learner.due()
    clock[0] += 3601
    assert learner.due()


@pytest.mark.parametrize("signal", [{"kind": "friction", "text": "Fixture missing path."},
                                    {"kind": "cell", "code": "1 / 0", "error": "ZeroDivisionError"}])
def test_friction_and_failures_survive_signal_batching(tmp_path, signal):
    store = SkillStore(tmp_path)
    store.journal_append(signal)
    for i in range(11):
        store.journal_append(read_cell(i))
    assert Synthesizer(NoChanges(), "fake", store, min_new_entries=12).due()
    assert not low_value_entry(store.journal_entries()[0])


async def test_force_pass_and_read_only_drain_still_work(tmp_path):
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "user", "text": "Retain the useful fixture preference."})
    provider = NoChanges()
    learner = Synthesizer(provider, "fake", store)
    assert not learner.due()
    await learner.run()
    assert len(provider.prompts) == 1 and store.synth_cursor() == 1
    other = SkillStore(tmp_path / "reads")
    for i in range(12):
        other.journal_append(read_cell(i))
    learner = Synthesizer(NoChanges(), "fake", other, min_new_entries=12)
    assert learner.due()
    await learner.run()
    assert learner._provider.prompts == [] and other.synth_cursor() == 12


@pytest.mark.parametrize("external", [False, True])
async def test_kernel_resolved_read_paths_drive_journal_not_code_literals(tmp_path, external):
    project = tmp_path / "project"
    project.mkdir()
    (project / "fixture.py").write_text("value = 1\n")
    store = SkillStore(tmp_path / "memory")
    store.journal_context(cwd=str(project))
    registry = ToolRegistry()
    register_builtins(registry)
    session = CodeModeSession(registry, skills=store)
    class Loop:
        permissions = PermissionEngine("never_ask")
        hooks = None
        ctx = ToolCtx(cwd=str(project))
        async def _emit(self, event):
            pass
        async def _await_permission(self, *args):
            return "allow", None, None
    from fluid.codemode.skills import names_path_outside
    target = str(Path(__file__).resolve()) if external else str(project / "fixture.py")
    expected_outside = names_path_outside(json.dumps({"file_path": target}), str(project))
    code = (f"from tools import Read\nprint(Read(file_path={target!r}))" if external else
            f"from tools import Read\nroot={str(project)!r}\nprint(Read(file_path=root+'/fixture.py'))")
    try:
        result = await session.run_python(sid="fixture", turn_id="t", input={"code": code}, loop=Loop())
        assert not result.is_error
        entry = next(e for e in store.journal_entries() if e.get("kind") == "cell")
        assert bool(entry.get("outside_cwd")) == expected_outside
        assert entry["calls"] == [{"name": "Read", "ok": True, "outside_cwd": expected_outside}]
        assert low_value_entry(entry) is not expected_outside
        assert "target" not in entry["calls"][0] and "input" not in entry["calls"][0]
    finally:
        await session.aclose()


@pytest.mark.parametrize("calls", [[{"name": "Read", "ok": True}],
                                  [{"name": "Read", "ok": True, "outside_cwd": False}, {}]])
def test_legacy_or_partial_call_metadata_keeps_conservative_fallback(tmp_path, calls):
    store = SkillStore(tmp_path)
    store.journal_context(cwd=str(tmp_path))
    store.journal_append({"kind": "cell", "code": "Read(file_path='/home/owner/config')",
                          "host_calls": 1, "tools": ["Read"], "calls": calls})
    entry = store.journal_entries()[0]
    assert entry["outside_cwd"] and not low_value_entry(entry)


def test_resolved_external_read_is_not_discarded(tmp_path):
    store = SkillStore(tmp_path)
    store.journal_context(cwd=str(tmp_path))
    store.journal_append({"kind": "cell", "code": "Read(file_path=external)",
                          "host_calls": 1, "tools": ["Read"],
                          "calls": [{"name": "Read", "ok": True, "outside_cwd": True}]})
    assert store.journal_entries()[0]["outside_cwd"]
    assert not low_value_entry(store.journal_entries()[0])


async def test_explicit_remember_persists_while_shared_inference_budget_is_spent(tmp_path):
    from fluid.tools.builtins.remember import handler
    store = SkillStore(tmp_path)
    set_budget(store, day=int(time.time() // 86400), calls=continual.MAX_INFERENCE_CALLS_PER_DAY,
               last_at=time.time())
    result = await handler({"text": "The fixture owner prefers reproducible test reports.", "user_requested": True},
                           ToolCtx(extra={"memory_store": store}))
    assert not result.is_error
    assert store.memories()[0].protected


@pytest.mark.parametrize("mode", ["maximum", "pressure", "urgent"])
def test_backlog_bounds_and_urgency_do_not_starve_substantive_evidence(tmp_path, monkeypatch, mode):
    store = SkillStore(tmp_path)
    store.journal_append({"kind": "user", "text": "Retain useful fixture evidence."})
    for i in range(19):
        store.journal_append(read_cell(i))
    learner = Synthesizer(NoChanges(), "fake", store, max_entries=20 if mode == "maximum" else 400)
    if mode == "pressure":
        monkeypatch.setattr(store, "_journal_size", lambda: continual.JOURNAL_MAX_BYTES)
    if mode == "urgent":
        learner.mark_urgent()
    assert learner.due()


async def test_controlled_replay_retention_cost_and_prompt_allowance(tmp_path):
    import runpy
    namespace = runpy.run_path(str(Path(__file__).parents[3] / "scripts/measure_learner_yield.py"))
    result = await namespace["replay"](namespace["generated"](), tmp_path / "replay",
                                        SkillStore, Synthesizer, continual)
    assert result["controlled_gold_facts_retained"] == 4
    assert result["model_calls"] <= 3
    assert result["estimated_input_tokens"] <= 5000  # measured 4603 + 397 allowance
    config = json.loads((Path(__file__).parents[1] / "fixtures/learner_yield.json").read_text())
    for key, prompt in (("synthesis", continual._SYNTH_SYSTEM),
                        ("consolidation", continual._CONSOLIDATE_SYSTEM)):
        assert len(prompt) <= config["prompt_allowances"][key]
        paragraphs = [part.strip() for part in prompt.split("\n\n") if part.strip()]
        assert len(paragraphs) == len(set(paragraphs))



def test_accounting_separates_unpaid_budget_attempts_and_consolidation(tmp_path):
    import runpy
    namespace = runpy.run_path(str(Path(__file__).parents[3] / "scripts/measure_learner_yield.py"))
    source = tmp_path / "usage.jsonl"
    rows = [{"ts": 1, "kind": "synthesis", "error": "InferenceBudgetExhausted", "changed": []},
            {"ts": 2, "kind": "synthesis", "usage": {"input_tokens": 100, "output_tokens": 5}, "changed": ["memory:m1"]},
            {"ts": 3, "kind": "consolidation", "usage": {"input_tokens": 10}, "changed": ["memory:m2"]}]
    source.write_text("\n".join(json.dumps(row) for row in rows))
    result = namespace["usage_accounting"](source)["by_kind"]
    assert result["synthesis"]["attempt_rows"] == 2
    assert result["synthesis"]["usage_rows"] == 1
    assert result["synthesis"]["error_buckets"] == {"budget_exhausted": 1}
    assert result["synthesis"]["recorded_tokens_per_accepted_memory_update"] == 105
    assert result["consolidation"]["accepted_memory_updates"] == 1
