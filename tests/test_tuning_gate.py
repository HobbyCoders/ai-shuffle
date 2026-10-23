"""#95: a tuning default ships only with a measured win. Offline; no provider calls."""
import json

import pytest

from app.core import local_model_settings, model_effort
from scripts import tuning_gate
from scripts.tuning_gate import ROOT, check_ledger, load_ledger, verdict

EVIDENCE = ROOT / "docs" / "benchmarks" / "effort-gpt-6.1-sol-2026-10-16.json"

# Every route shape the capability code distinguishes, as (provider row, route).
PROVIDERS = [
    (None, "anthropic"),
    (None, "delegated"),
    ({"provider_type": "openai"}, None),
    ({"provider_type": "chatgpt"}, None),
    ({"provider_type": "custom", "auth_mode": "chatgpt"}, None),
    ({"provider_type": "xai"}, None),
    ({"provider_type": "grok"}, None),
    ({"provider_type": "custom", "auth_mode": "grok"}, None),
    ({"provider_type": "openrouter"}, None),
    ({"provider_type": "custom", "api_dialect": "openai"}, None),
    ({"provider_type": "custom"}, None),
]


def _known_models():
    names = set()
    for table in ("_ANTHROPIC", "_CLI", "_OPENAI", "_XAI", "_ALIASES"):
        names |= set(getattr(model_effort, table))
    for entry in load_ledger()["defaults"]:
        names.add(entry["applies"]["model"])
    return sorted(names)


def _applied_defaults():
    """Every (model, route, provider, value) the app would start a never-picked composer at."""
    full = {"version": 1, "effort_levels": list(model_effort.EFFORT_LEVELS), "default_effort": None}
    found = set()
    for model in _known_models():
        for provider, route in PROVIDERS:
            # Built-in tables, then a catalog that offers every level, so a
            # default keyed on the model name cannot hide behind capabilities.
            entries = [{}] if provider is None else [{}, {"id": model, "reasoning_capabilities": full}]
            for entry in entries:
                cap = model_effort.effort_capabilities(model, provider, entry, route=route)
                if "initial_effort" in cap:
                    effective = route or model_effort.effort_route(provider)
                    kind = (provider or {}).get("auth_mode") or (provider or {}).get("provider_type") or effective
                    found.add((model, effective, kind, cap["initial_effort"]))
    return found


def test_every_applied_default_is_in_the_ledger(monkeypatch):
    from app.core import claude_auth_mode
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: claude_auth_mode.MODE_SUBSCRIPTION)
    listed = {(a["model"], a["route"], a["provider"], a["value"])
              for a in (e["applies"] for e in load_ledger()["defaults"])}
    applied = _applied_defaults()
    assert applied, "the detector must see the known GPT-6.1 Sol default"
    unlisted = applied - listed
    assert not unlisted, (
        f"Tuning defaults without measured evidence: {sorted(unlisted)}. Benchmark them "
        "(scripts/effort_benchmark.py), check with scripts/tuning_gate.py and add them "
        "to docs/benchmarks/tuning-ledger.json, or remove them.")
    assert listed <= applied, f"Ledger lists defaults the app no longer applies: {sorted(listed - applied)}"


def test_every_ledger_default_wins_on_its_committed_evidence():
    assert check_ledger() == []
    assert tuning_gate.main(["--check-ledger"]) == 0


def test_unshipped_effort_arms_have_no_measured_win():
    data = json.loads(EVIDENCE.read_text())
    assert verdict(data, "default", "high")["verdict"] == "win"
    for arm in ("low", "medium", "xhigh"):
        got = verdict(data, "default", arm)
        assert got["verdict"] == "no measured win", arm
        assert any("fewer passes" in r for r in got["reasons"]), arm


def test_local_presets_are_opt_ins_with_sources_and_no_hidden_defaults():
    opt_in = {e["id"].removeprefix("local.") for e in load_ledger()["opt_in"]}
    assert set(local_model_settings._PRESETS) == opt_in
    row = {"provider_type": "custom", "api_dialect": "openai",
           "models_json": [{"id": "plain"}, {"id": "declared", "local_settings": {"server": "vllm"}}]}
    # Neither an untuned model nor a bare server declaration sends anything.
    assert local_model_settings.local_request_extra(row, "plain") == {}
    assert local_model_settings.local_request_extra(row, "declared") == {}


def _rows(base, cand, *, passed=(True, True), tasks=4, reps=3):
    rows = []
    for t in range(tasks):
        for rep in range(reps):
            for config, metrics, ok in (("default", base, passed[0]), ("tuned", cand, passed[1])):
                rows.append({"task": f"t{t}", "kind": "coding", "config": config, "repetition": rep,
                             "passed": ok if not callable(ok) else ok(t, rep), **metrics(t, rep)})
    return {"rows": rows}


def _m(seconds, out=100, inp=1000):
    return lambda t, rep: {"wall_seconds": seconds + t + rep * .1, "output_tokens": out + t,
                           "input_tokens": inp + t}


def test_gate_rules():
    win = verdict(_rows(_m(20), _m(10)), "default", "tuned")
    assert win["verdict"] == "win" and win["improved"] == ["wall_seconds"]

    fewer = verdict(_rows(_m(20), _m(10), passed=(True, lambda t, rep: t != 0)), "default", "tuned")
    assert fewer["verdict"] == "no measured win"
    assert fewer["passed"] == {"baseline": 12, "candidate": 9, "trials": 12}

    traded = verdict(_rows(_m(20, out=100), _m(10, out=400)), "default", "tuned")
    assert traded["verdict"] == "no measured win" and traded["regressed"] == ["output_tokens"]

    same = verdict(_rows(_m(20), _m(20)), "default", "tuned")
    assert same["verdict"] == "no measured win"
    assert "no metric's 95% interval is wholly below zero" in same["reasons"]


def test_gate_refuses_unpaired_or_duplicated_arms():
    data = _rows(_m(20), _m(10))
    with pytest.raises(ValueError, match="same tasks"):
        verdict({"rows": [r for r in data["rows"] if not (r["config"] == "tuned" and r["task"] == "t0")]},
                "default", "tuned")
    with pytest.raises(ValueError, match="duplicate"):
        verdict({"rows": data["rows"] + data["rows"][:1]}, "default", "tuned")
    with pytest.raises(ValueError, match="no 'default' or 'missing' arm"):
        verdict(data, "default", "missing")


def test_verdict_is_deterministic():
    data = json.loads(EVIDENCE.read_text())
    assert verdict(data, "default", "high") == verdict(
        {"rows": list(reversed(data["rows"]))}, "default", "high")
