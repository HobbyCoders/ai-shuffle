"""Summarise a bench run directory into ``summary.json`` and ``report.html``.

Per run:

* ``wall_s``      prompt sent → agent finished (process exit / app turn idle).
* ``model_s``     union of model-call intervals.
* ``tool_s``      union of tool intervals not already covered by a model call.
* ``overhead_s``  wall time covered by neither: harness startup, prompt
                  assembly, persistence, scheduling, waiting between steps.
* ``ttft_s``      median time to first streamed token per call (exact arms).
* tokens          ``input`` (uncached), ``cached``, ``output``, ``reasoning``;
                  ``cache_hit`` = cached / (input + cached).
* ``est_cost``    tokens priced at one list-price row for every arm (the
                  subscription bills none of it; this is what it would cost).
"""
from __future__ import annotations

import html
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

# gpt-6.1-sol has no row in fluid's pricing table (it falls through to the
# unpriced "gpt-6" row). gpt-6-sol's list price stands in, for every arm.
PRICE = {"input": 2.00, "cached": 0.20, "output": 10.00,
         "source": "gpt-6-sol list price (developers.openai.com/api/docs/pricing, 2026-09-23), per 1M tokens"}

ARM_LABEL = {"cc": "Claude Code", "fluid": "Fluid (bare CLI)", "app": "Fluid in AI Shuffle"}
ARM_COLOR = {"cc": "#eb6834", "fluid": "#2a78d6", "app": "#1baf7a"}


def _union(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(i for i in intervals if i[0] is not None and i[1] is not None and i[1] >= i[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _length(intervals: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in intervals)


def _clip(intervals, lo, hi):
    return [(max(a, lo), min(b, hi)) for a, b in intervals if b > lo and a < hi]


def run_metrics(r: dict[str, Any]) -> dict[str, Any]:
    tr = r.get("trace") or {}
    calls = tr.get("calls") or []
    tools = tr.get("tools") or []
    t0, t1 = r.get("t_start"), r.get("t_end")
    wall = r.get("wall_s")
    m: dict[str, Any] = {
        "task": r["task"], "arm": r["arm"], "trial": r["trial"],
        "passed": bool((r.get("grade") or {}).get("passed")),
        "score": (r.get("grade") or {}).get("score", 0.0),
        "wall_s": wall, "timed_out": bool(r.get("timed_out")),
        "harness_error": r.get("harness_error"), "contamination": r.get("contamination") or [],
    }
    model_iv = _union([(c.get("t_start"), c.get("t_end")) for c in calls])
    tool_iv = _union([(t.get("t_start"), t.get("t_end")) for t in tools])
    if t0 and t1:
        model_iv = _clip(model_iv, t0, t1)
        tool_iv = _clip(tool_iv, t0, t1)
    both = _union(model_iv + tool_iv)
    m["model_s"] = round(_length(model_iv), 1)
    m["tool_s"] = round(_length(both) - _length(model_iv), 1)
    m["overhead_s"] = round(max((wall or 0) - _length(both), 0.0), 1) if wall else None
    ttfts = [c["t_first"] - c["t_start"] for c in calls
             if c.get("exact") and c.get("t_first") and c.get("t_start")]
    m["ttft_s"] = round(statistics.median(ttfts), 2) if ttfts else None
    m["call_s_median"] = round(statistics.median(
        [c["t_end"] - c["t_start"] for c in calls if c.get("t_end") and c.get("t_start")]), 2) if calls else None
    main = [c for c in calls if c.get("role") == "main"]
    m["calls"] = len(main)
    m["side_calls"] = len(calls) - len(main)
    for key in ("input", "cached", "output", "reasoning"):
        m[key] = sum(int(c.get(key) or 0) for c in calls)
    prompt = m["input"] + m["cached"]
    m["prompt_tokens"] = prompt
    m["cache_hit"] = round(m["cached"] / prompt, 3) if prompt else None
    m["first_prompt_tokens"] = (int(main[0].get("input") or 0) + int(main[0].get("cached") or 0)) if main else None
    m["est_cost"] = round((m["input"] * PRICE["input"] + m["cached"] * PRICE["cached"]
                           + m["output"] * PRICE["output"]) / 1e6, 4)
    m["tool_calls"] = len(tools)
    m["tool_errors"] = sum(1 for t in tools if t.get("is_error"))
    names: dict[str, int] = defaultdict(int)
    for t in tools:
        names[str(t.get("name"))] += 1
    m["tool_names"] = dict(sorted(names.items(), key=lambda kv: -kv[1]))
    m["call_errors"] = sum(1 for c in calls if c.get("error"))
    m["retries"] = sum(int(c.get("retries") or 0) for c in calls) + int(tr.get("provider_retries") or 0)
    m["diff_files"] = len(((r.get("grade") or {}).get("diff") or {}).get("files") or [])
    m["tests"] = ((r.get("grade") or {}).get("tests") or {}).get("counts")
    return m


def load_runs(out: Path) -> list[dict[str, Any]]:
    runs = []
    for p in sorted((out / "runs").glob("*/*/t*/result.json")):
        try:
            runs.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return runs


def _med(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(vals), 2) if vals else None


def _mean(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.fmean(vals), 3) if vals else None


AGG_KEYS = ("wall_s", "model_s", "tool_s", "overhead_s", "ttft_s", "call_s_median", "calls",
            "side_calls", "prompt_tokens", "first_prompt_tokens", "input", "cached", "output",
            "reasoning", "cache_hit", "est_cost", "tool_calls", "tool_errors", "retries")


def aggregate(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, list] = defaultdict(list)
    by_task_arm: dict[tuple[str, str], list] = defaultdict(list)
    for m in metrics:
        by_arm[m["arm"]].append(m)
        by_task_arm[(m["task"], m["arm"])].append(m)
    arms = {}
    for arm, ms in by_arm.items():
        solved = [m for m in ms if m["passed"]]
        a = {"runs": len(ms), "passed": len(solved), "pass_rate": round(len(solved) / len(ms), 3),
             "mean_score": _mean([m["score"] for m in ms]),
             "timeouts": sum(m["timed_out"] for m in ms),
             "harness_errors": sum(1 for m in ms if m["harness_error"]),
             "contaminated": sum(1 for m in ms if m["contamination"]),
             "total_est_cost": round(sum(m["est_cost"] for m in ms), 3),
             "total_wall_s": round(sum(m["wall_s"] or 0 for m in ms), 1)}
        a["cost_per_solved"] = round(a["total_est_cost"] / len(solved), 3) if solved else None
        a["wall_per_solved_s"] = round(a["total_wall_s"] / len(solved), 1) if solved else None
        for key in AGG_KEYS:
            a[f"median_{key}"] = _med([m[key] for m in ms])
        arms[arm] = a
    tasks: dict[str, dict[str, Any]] = defaultdict(dict)
    for (task, arm), ms in by_task_arm.items():
        tasks[task][arm] = {"runs": len(ms), "passed": sum(m["passed"] for m in ms),
                            **{f"median_{k}": _med([m[k] for m in ms]) for k in AGG_KEYS}}
    # Paired wall-time ratios per task (median over trials), vs Claude Code.
    ratios: dict[str, list[float]] = defaultdict(list)
    for task, per in tasks.items():
        base = (per.get("cc") or {}).get("median_wall_s")
        for arm, a in per.items():
            if arm != "cc" and base and a.get("median_wall_s"):
                ratios[arm].append(a["median_wall_s"] / base)
    paired = {arm: {"tasks": len(v), "median_wall_ratio_vs_cc": _med(v),
                    "geo_mean_wall_ratio_vs_cc": round(statistics.geometric_mean(v), 3) if v else None}
              for arm, v in ratios.items()}
    return {"arms": arms, "tasks": dict(tasks), "paired_vs_cc": paired, "price": PRICE}


# ─── HTML ───────────────────────────────────────────────────────────────────

_CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--surface-2:#f2f1ee;--text-primary:#0b0b0b;
--text-secondary:#52514e;--grid:#e4e3df;--good:#008300;--bad:#c62828;
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--surface-1);
color:var(--text-primary);margin:0;padding:24px;max-width:1200px}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
--surface-1:#1a1a19;--surface-2:#252523;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#3a3936}}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.muted{color:var(--text-secondary)}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
.tile{background:var(--surface-2);border-radius:8px;padding:12px 16px;min-width:220px}
.tile .k{font-size:12px;color:var(--text-secondary)}.tile .v{font-size:26px;font-weight:600}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:baseline}
table{border-collapse:collapse;width:100%;margin:8px 0;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--grid);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--text-secondary);font-weight:500}
.pass{color:var(--good)}.fail{color:var(--bad)}
.legend{display:flex;gap:16px;margin:6px 0;color:var(--text-secondary)}
svg text{fill:var(--text-secondary);font-size:12px}
details{margin:6px 0}summary{cursor:pointer}
code{font-size:12px}
"""


def _fmt(v, digits=1):
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:,.{digits}f}"
    if isinstance(v, int):
        return f"{v:,}"
    return html.escape(str(v))


def _bars(tasks: dict, arms: list[str], key: str, unit: str) -> str:
    """Grouped horizontal bars: one group per task, one bar per arm."""
    rows = sorted(tasks)
    vmax = max([tasks[t].get(a, {}).get(key) or 0 for t in rows for a in arms] + [1])
    bar_h, gap, group_gap, left, width = 12, 2, 14, 210, 640
    height = len(rows) * (len(arms) * (bar_h + gap) + group_gap) + 20
    out = [f'<svg viewBox="0 0 {left + width + 80} {height}" width="100%" role="img" '
           f'aria-label="{html.escape(key)} per task and arm">']
    y = 10
    for t in rows:
        out.append(f'<text x="{left - 8}" y="{y + (len(arms) * (bar_h + gap)) / 2 + 4}" '
                   f'text-anchor="end">{html.escape(t)}</text>')
        for a in arms:
            v = tasks[t].get(a, {}).get(key)
            if v is not None:
                w = max(2, width * v / vmax)
                out.append(f'<rect x="{left}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="3" '
                           f'fill="{ARM_COLOR.get(a, "#888")}"><title>{html.escape(ARM_LABEL.get(a, a))} · '
                           f'{html.escape(t)}: {v:,.1f} {unit}</title></rect>')
                out.append(f'<text x="{left + w + 4}" y="{y + bar_h - 2}">{v:,.0f}</text>')
            y += bar_h + gap
        y += group_gap
    out.append("</svg>")
    return "".join(out)


def render_html(summary: dict[str, Any], metrics: list[dict[str, Any]], config: dict[str, Any]) -> str:
    arms = [a for a in ("cc", "fluid", "app") if a in summary["arms"]]
    legend = "".join(f'<span><span class="sw" style="background:{ARM_COLOR[a]}"></span>{ARM_LABEL[a]}</span>'
                     for a in arms)
    p = []
    p.append(f"<h1>Harness head-to-head: Claude Code vs Fluid</h1><p class='muted'>Model "
             f"<code>{_fmt(config.get('model'))}</code> at effort <code>{_fmt(config.get('effort'))}</code> on every arm, "
             f"same ChatGPT subscription. Claude Code {_fmt(config.get('claude_version'))}; repo "
             f"<code>{_fmt((config.get('repo_head') or '')[:10])}</code>. {len(metrics)} runs.</p>")
    p.append('<div class="tiles">')
    for a in arms:
        s = summary["arms"][a]
        p.append(f'<div class="tile"><div class="k"><span class="sw" style="background:{ARM_COLOR[a]}"></span>'
                 f'{ARM_LABEL[a]}</div><div class="v">{s["passed"]}/{s["runs"]} solved</div>'
                 f'<div class="muted">median wall {_fmt(s["median_wall_s"])} s · est. ${_fmt(s["total_est_cost"], 2)} total'
                 f'</div></div>')
    p.append("</div>")
    paired = summary.get("paired_vs_cc") or {}
    if paired:
        p.append("<p>" + " · ".join(
            f"{ARM_LABEL[a]} takes <b>{_fmt(v['geo_mean_wall_ratio_vs_cc'], 2)}×</b> Claude Code's wall time "
            f"(geometric mean over {v['tasks']} tasks, per-task medians)" for a, v in paired.items()) + "</p>")

    rows = [
        ("Solved", "passed", 0), ("Pass rate", "pass_rate", 2), ("Wall time per solved task (s)", "wall_per_solved_s", 0),
        ("Median wall (s)", "median_wall_s", 0), ("  model time (s)", "median_model_s", 0),
        ("  tool time (s)", "median_tool_s", 0), ("  harness overhead (s)", "median_overhead_s", 0),
        ("Median TTFT per call (s)", "median_ttft_s", 2), ("Median call duration (s)", "median_call_s_median", 1),
        ("Median model calls", "median_calls", 0), ("Median side calls", "median_side_calls", 0),
        ("Median prompt tokens (total)", "median_prompt_tokens", 0),
        ("First-call prompt (tokens)", "median_first_prompt_tokens", 0),
        ("Median output tokens", "median_output", 0), ("Median reasoning tokens", "median_reasoning", 0),
        ("Median cache hit", "median_cache_hit", 3), ("Est. cost total ($)", "total_est_cost", 2),
        ("Est. cost per solved ($)", "cost_per_solved", 3), ("Median tool calls", "median_tool_calls", 0),
        ("Median tool errors", "median_tool_errors", 0), ("Timeouts", "timeouts", 0),
        ("Harness errors", "harness_errors", 0), ("Runs flagged for contamination", "contaminated", 0),
    ]
    p.append("<h2>Per arm</h2><table><tr><th>metric</th>" + "".join(
        f'<th><span class="sw" style="background:{ARM_COLOR[a]}"></span>{ARM_LABEL[a]}</th>' for a in arms) + "</tr>")
    for label, key, d in rows:
        p.append(f"<tr><td>{html.escape(label)}</td>" + "".join(
            f"<td>{_fmt(summary['arms'][a].get(key), d)}</td>" for a in arms) + "</tr>")
    p.append("</table>")

    p.append(f"<h2>Median wall time per task (s)</h2><div class='legend'>{legend}</div>")
    p.append(_bars(summary["tasks"], arms, "median_wall_s", "s"))
    p.append(f"<h2>Median harness overhead per task (s)</h2><p class='muted'>Wall time not spent in a model "
             f"call or a tool.</p><div class='legend'>{legend}</div>")
    p.append(_bars(summary["tasks"], arms, "median_overhead_s", "s"))

    p.append("<h2>Per task</h2><table><tr><th>task</th>" + "".join(
        f"<th>{ARM_LABEL[a]}<br>solved · wall · calls · prompt tok</th>" for a in arms) + "</tr>")
    for task in sorted(summary["tasks"]):
        per = summary["tasks"][task]
        cells = []
        for a in arms:
            s = per.get(a)
            if not s:
                cells.append("<td>–</td>")
                continue
            cls = "pass" if s["passed"] == s["runs"] else ("fail" if s["passed"] == 0 else "")
            cells.append(f'<td><span class="{cls}">{s["passed"]}/{s["runs"]}</span> · {_fmt(s["median_wall_s"], 0)}s · '
                         f'{_fmt(s["median_calls"], 0)} · {_fmt(s["median_prompt_tokens"], 0)}</td>')
        p.append(f"<tr><td>{html.escape(task)}</td>{''.join(cells)}</tr>")
    p.append("</table>")

    p.append("<h2>Every run</h2><details><summary>Show table</summary><table><tr><th>task</th><th>arm</th>"
             "<th>trial</th><th>pass</th><th>wall</th><th>model</th><th>tool</th><th>overhead</th><th>calls</th>"
             "<th>prompt tok</th><th>cache</th><th>out tok</th><th>$ est</th><th>tools</th><th>tool err</th>"
             "<th>note</th></tr>")
    for m in sorted(metrics, key=lambda m: (m["task"], m["arm"], m["trial"])):
        note = m["harness_error"] or ("timeout" if m["timed_out"] else "") or (
            "contamination: " + "; ".join(m["contamination"])[:120] if m["contamination"] else "")
        p.append(f"<tr><td>{html.escape(m['task'])}</td><td>{ARM_LABEL.get(m['arm'], m['arm'])}</td><td>{m['trial']}</td>"
                 f"<td class='{'pass' if m['passed'] else 'fail'}'>{'yes' if m['passed'] else 'no'}</td>"
                 f"<td>{_fmt(m['wall_s'], 0)}</td><td>{_fmt(m['model_s'], 0)}</td><td>{_fmt(m['tool_s'], 0)}</td>"
                 f"<td>{_fmt(m['overhead_s'], 0)}</td><td>{m['calls']}</td><td>{_fmt(m['prompt_tokens'])}</td>"
                 f"<td>{_fmt(m['cache_hit'], 2)}</td><td>{_fmt(m['output'])}</td><td>{_fmt(m['est_cost'], 3)}</td>"
                 f"<td>{m['tool_calls']}</td><td>{m['tool_errors']}</td><td style='text-align:left'>{html.escape(note)}</td></tr>")
    p.append("</table></details>")
    p.append(f"<p class='muted'>Cost estimate: {html.escape(PRICE['source'])}. The subscription bills none of "
             f"it. App-arm call timings are rebuilt from its event log; Claude Code and bare Fluid calls are "
             f"timed by the same metered client.</p>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>Harness head-to-head</title>"
            f"<style>{_CSS}</style></head><body class='viz-root'>{''.join(p)}</body></html>")


def write_report(out: Path) -> Path:
    runs = load_runs(out)
    metrics = [run_metrics(r) for r in runs]
    summary = aggregate(metrics)
    config = {}
    if (out / "config.json").exists():
        config = json.loads((out / "config.json").read_text())
    (out / "summary.json").write_text(json.dumps({"summary": summary, "runs": metrics}, indent=2))
    path = out / "report.html"
    path.write_text(render_html(summary, metrics, config), encoding="utf-8")
    return path
