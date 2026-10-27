"""The cold-pricing field check reads recorded decisions the way the engine prices them."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cache_cold_audit.py"
spec = importlib.util.spec_from_file_location("cache_cold_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit)


def _usage(seq, *, reason, cold_reason, gap, resumed=False, static=False,
           read=900, uncached=100, auxiliary=False):
    raw = {"compaction_decision": {
        "reason": reason, "cold_reason": cold_reason,
        "cache_signal": {"gap_s": gap, "resumed": resumed, "static_changed": static},
    }}
    if auxiliary:
        raw["auxiliary_source"] = "summarizer"
    return {"seq": seq, "event": {
        "_cls": "Usage", "input_tokens": uncached, "cache_read_tokens": read,
        "cache_creation_tokens": 0, "raw": raw,
    }}


def _write(tmp_path: Path, rows) -> Path:
    path = tmp_path / "s1" / "events.jsonl"
    path.parent.mkdir()
    lines = [json.dumps(r) for r in rows]
    lines.append(json.dumps({"seq": 99, "event": {"_cls": "TextDelta", "text": "x"}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_short_unpark_priced_cold_is_counted_and_repriced_warm(tmp_path):
    _write(tmp_path, [
        # Legacy rule: a 20 s unpark priced cold and committed.
        _usage(1, reason="cold_opportunistic", cold_reason="resumed", gap=20.0,
               resumed=True, read=300, uncached=700),
        # A 20 s unpark that rewrote nothing: the cache really was warm.
        _usage(2, reason="below_threshold", cold_reason="resumed", gap=20.0, resumed=True),
        # Genuinely cold: a long gap, an unknown gap and a static change.
        _usage(3, reason="cold_commit", cold_reason="gap", gap=400.0),
        _usage(4, reason="cold_opportunistic", cold_reason="resumed", gap=None, resumed=True),
        _usage(5, reason="cold_commit", cold_reason="static_changed", gap=10.0, static=True),
        # Side calls are not the session's prompt.
        _usage(6, reason="cold_commit", cold_reason="resumed", gap=5.0, resumed=True,
               auxiliary=True),
    ])
    summary = audit.summarize(
        [row for path in audit.event_files([tmp_path]) for row in audit.decisions(path)]
    )
    assert summary["decisions"] == 5
    assert summary["recorded"] == {"cold_commits": 4, "short_gap": 1}
    assert summary["current_rule"] == {"cold_commits": 3, "short_gap": 0}
    short = next(g for g in summary["table"]
                 if g["recorded"] == "resumed" and g["gap"] == "<=300s")
    assert short["current"] == "warm"
    assert short["cold_commits"] == 1
    assert short["idle_calls"] == 1 and short["idle_median_hit"] == 0.9


def test_cli_prints_the_field_check(tmp_path, capsys):
    path = _write(tmp_path, [
        _usage(1, reason="cold_opportunistic", cold_reason="resumed", gap=20.0, resumed=True),
    ])
    assert audit.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "(recorded): 1/1 (100.0%)" in out
    assert "(current rule): 0/0" in out
    assert audit.main([str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sessions"] == 1


def test_audit_uses_recorded_route_policy_including_no_idle_expiry(tmp_path):
    rows = []
    for seq, ttl in enumerate((None, 1800, 300), 1):
        row = _usage(seq, reason="cold_commit", cold_reason="gap", gap=1000)
        row["event"]["raw"]["compaction_decision"]["cache_signal"]["ttl_seconds"] = ttl
        rows.append(row)
    data = list(audit.decisions(_write(tmp_path, rows)))
    assert [row["current"] for row in data] == ["", "", "gap"]
    assert [audit.short_gap(row) for row in data] == [True, True, False]
    summary = audit.summarize(data)
    assert {g["gap"] for g in summary["table"]} == {"no idle expiry", "<=1800s", ">300s"}
    assert summary["recorded"] == {"cold_commits": 3, "short_gap": 2}
    assert summary["current_rule"] == {"cold_commits": 1, "short_gap": 0}
