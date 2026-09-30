"""Portable audit skill discovery and offline accounting regression controls."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".fluid/skills/session-harness-audit"
spec = importlib.util.spec_from_file_location("audit_metrics", SKILL / "scripts/metrics.py")
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def call(identifier="c1", **kw):
    row = dict(call_id=identifier, provider="synthetic", model="model", engine="native",
               input_semantics="exclusive", input_tokens=100, cache_read_tokens=200,
               cache_creation_tokens=0, output_tokens=25, context_limit_tokens=1000)
    return {**row, **kw}


def snapshot(*calls, **kw):
    return {"schema_version": 1, "sessions": [dict(key="s1", cohort="primary",
                                                      calls=list(calls), **kw)]}


def primary(data):
    return metrics.analyze(data)["cohorts"]["primary"]


def test_exclusive_and_inclusive_counts_agree_without_double_counting():
    a = primary(snapshot(call()))
    b = primary(snapshot(call(input_semantics="inclusive", input_tokens=300)))
    assert a == b
    assert a["prompt_tokens"]["sum"] == 300
    assert a["weighted_cache_read_fraction"] == pytest.approx(2 / 3)
    assert a["context_utilization"]["median"] == .3


def test_weighted_fraction_not_average_and_warm_leg_boundaries():
    rows = [call("c1", input_tokens=900, cache_read_tokens=100),
            call("c2", input_tokens=0, cache_read_tokens=100),
            call("c3", model="other", input_tokens=0, cache_read_tokens=100)]
    result = metrics.analyze(snapshot(*rows))
    assert result["cohorts"]["primary"]["weighted_cache_read_fraction"] == .25
    assert result["groups"][0]["warm_candidates"]["calls"] == 1
    assert result["groups"][1]["warm_candidates"]["calls"] == 0


def test_unknown_and_zero_are_different_and_pair_denominators_match():
    row = call(input_semantics="inclusive", input_tokens=2000, cache_read_tokens=None)
    report = primary(snapshot(call("c1"), {**row, "call_id": "c2"}))
    assert report["prompt_tokens"]["sum"] == 2300
    assert report["cache_read_coverage"] == .5
    assert report["prompt_tokens_paired"] == 300
    assert report["weighted_cache_read_fraction"] == pytest.approx(2 / 3)
    unknown = primary(snapshot(call(cache_read_tokens=0, cache_read_reported=False)))
    assert unknown["prompt_tokens"]["sum"] is None
    assert unknown["cache_read_unknown_calls"] == 1
    zero = primary(snapshot(call(cache_read_tokens=0)))
    assert zero["weighted_cache_read_fraction"] == 0


@pytest.mark.parametrize("field,value", [("input_tokens", -1), ("input_tokens", True),
                                          ("cache_read_tokens", 2.5),
                                          ("output_tokens", "25")])
def test_invalid_counts_are_not_silently_normalized(field, value):
    with pytest.raises(ValueError):
        metrics.analyze(snapshot(call(**{field: value})))


@pytest.mark.parametrize("change", [{"input_semantics": "bad"},
    {"input_semantics": "inclusive", "input_tokens": 10},
    {"cache_read_reported": "false"}, {"transport_failed": "false"}])
def test_invalid_semantics_or_flags_are_rejected(change):
    with pytest.raises(ValueError):
        metrics.analyze(snapshot(call(**change)))


def test_duplicate_calls_and_sessions_rejected_not_double_counted():
    with pytest.raises(ValueError, match="deduplicate"):
        metrics.analyze(snapshot(call(), call()))
    data = snapshot(call())
    data["sessions"].append(deepcopy(data["sessions"][0]))
    with pytest.raises(ValueError, match="unique"):
        metrics.analyze(data)


def test_censored_empty_session_and_failed_attempt_are_visible():
    empty = primary(snapshot())
    assert empty["sessions"] == 1 and empty["calls"] == 0
    assert empty["weighted_cache_read_fraction"] is None
    report = metrics.analyze(snapshot(call("bad", transport_failed=True), call("ok")))
    assert report["cohorts"]["primary"]["transport_failed_calls"] == 1
    assert report["cohorts"]["primary"]["prompt_tokens"]["sum"] == 300
    assert report["groups"][0]["warm_candidates"]["calls"] == 0


def test_compaction_reporting_never_invents_token_savings():
    events = [dict(tier=1, reason="trigger", before_tokens=None, after_tokens=None),
              dict(tier=3, reason="cli_auto_compact", before_tokens=900, after_tokens=500)]
    report = primary(snapshot(call(), compactions=events))["compactions"]
    assert report["token_pairs_unknown"] == 1
    assert report["reported_tokens_reclaimed"] == 400
    assert report["reported_reduction_fraction"] == pytest.approx(400 / 900)
    assert metrics.compaction_summary([dict(before_tokens=0, after_tokens=0)])[
        "reported_tokens_reclaimed"] is None


def test_cohorts_and_engines_are_not_pooled_for_comparison():
    before = metrics.analyze(snapshot(call()))
    data = snapshot(call(cache_read_tokens=0))
    data["sessions"].append(dict(key="child", cohort="child", calls=[call(engine="delegated")]))
    now = metrics.analyze(data)
    comparison = metrics.compare(now, before)
    assert len(comparison["matched_groups"]) == 1
    assert comparison["matched_groups"][0]["cache_fraction_delta"] == pytest.approx(-2 / 3)
    assert comparison["unmatched_current_groups"][0]["cohort"] == "child"
    assert now["cohorts"]["primary"]["calls"] == 1
    assert now["cohorts"]["child"]["calls"] == 1


def test_cli_does_not_overwrite_inputs_or_evidence(tmp_path, capsys):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot(call())), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(SystemExit):
        metrics.main([str(path), "--output", str(path)])
    assert path.read_bytes() == original
    output = tmp_path / "metrics.json"
    assert metrics.main([str(path), "--baseline", str(path), "--output", str(output)]) == 0
    document = json.loads(output.read_text())
    assert document["comparison"]["matched_groups"][0]["cache_fraction_delta"] == 0
    with pytest.raises(SystemExit):
        metrics.main([str(path), "--output", str(output)])
    assert document == json.loads(output.read_text())


def test_no_arbitrary_source_metadata_echoed_or_input_mutated():
    data = snapshot(call())
    data["private_user_text"] = "PRIVATE_SENTINEL"
    data["sessions"][0]["calls"][0]["prompt"] = "PRIVATE_SENTINEL"
    before = deepcopy(data)
    result = metrics.analyze(data)
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    assert data == before


def test_real_prose_discovery_and_scope_precedence(tmp_path):
    from fluid.context.instructions import discover_skills
    skills = discover_skills(ROOT, include_global=False, include_plugins=False)
    found = next(s for s in skills if s.name == "session-harness-audit")
    assert found.scope == "project" and not found.body_loaded
    assert "cache" in found.description and "compaction" in found.description
    assert Path(found.path) == SKILL / "SKILL.md"
    local = tmp_path / ".fluid/skills/session-harness-audit"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\nname: session-harness-audit\ndescription: Local audit\n---\nBody\n")
    fresh = discover_skills(tmp_path, include_global=False, include_plugins=False)
    assert next(s for s in fresh if s.name == found.name).description == "Local audit"




@pytest.mark.asyncio
async def test_real_skill_tool_loads_body_with_scope_gates(tmp_path, monkeypatch):
    from fluid.tools.builtins import skill
    from fluid.tools.registry import ToolCtx
    # The real load telemetry stays inside our disposable HOME, not the owner store.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    outcome = await skill.handler({"name": "session-harness-audit"},
        ToolCtx(cwd=str(ROOT), extra={"setting_sources": ["project"]}))
    assert not outcome.is_error
    assert "## 1. Establish the audit boundary" in outcome.text
    assert "verification-matrix.md" in outcome.text
    usage = json.loads((home / ".fluid/skills/.usage.json").read_text())
    assert usage[str(SKILL / "SKILL.md")]["loads"] == 1
    blocked = await skill.handler({"name": "session-harness-audit"},
        ToolCtx(cwd=str(ROOT), extra={"setting_sources": []}))
    assert blocked.is_error and "disabled by setting_sources" in blocked.text


def test_context_stats_and_unknown_semantics_not_fabricated():
    report = primary(snapshot(call("c1"), call("c2", input_semantics="unknown")))
    assert report["unknown_prompt_calls"] == 1
    assert report["prompt_tokens"]["n"] == 1
    assert metrics.stats([1, 3, 2, 4])["p95"] == 4
    assert metrics.stats([])["median"] is None


@pytest.mark.parametrize("data", [[], None, {"schema_version": 1.0, "sessions": []}])
def test_schema_root_and_version_are_checked(data):
    with pytest.raises(ValueError):
        metrics.analyze(data)


def test_skill_references_and_requested_domain_coverage():
    body = (SKILL / "SKILL.md").read_text()
    for path in ["references/verification-matrix.md", "references/measurement-contract.md",
                 "scripts/metrics.py"]:
        assert path in body and (SKILL / path).is_file()
    matrix = (SKILL / "references/verification-matrix.md").read_text()
    for term in ["cache", "Context", "HistoryExpand", "Remember", "Recall",
                 "Forget", "save_skill", "selftest", "Bash-only", "unverified"]:
        assert term in body + matrix
