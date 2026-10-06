"""Load and validate the smoke scenarios, and render the human-readable table.

``.fluid/skills/live-test/scenarios.json`` is the one source of truth;
``scenarios.md`` next to it is generated from it (``python -m
scripts.live_smoke render``) and a unit test keeps the two in sync.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_JSON = ROOT / ".fluid" / "skills" / "live-test" / "scenarios.json"
SCENARIOS_MD = SCENARIOS_JSON.with_name("scenarios.md")

KINDS = ("script", "browser", "owner")
OPS = ("workers", "start", "message", "await", "compact")
EXPECT_KEYS = ("expect_reply", "expect_tool", "expect_owner", "expect_files", "user_messages", "replies")
STEP_KEYS = {
    "workers": set(),
    "start": {"as", "on", "prompt", "wait", "within_s", *EXPECT_KEYS},
    "message": {"session", "prompt", "when", "wait", "within_s", *EXPECT_KEYS},
    "await": {"session", "within_s", *EXPECT_KEYS},
    "compact": {"session", "within_s"},
}


class ScenarioError(ValueError):
    pass


def load(path: Path | None = None) -> dict:
    data = json.loads(Path(path or SCENARIOS_JSON).read_text(encoding="utf-8"))
    validate(data)
    return data


def validate(data: dict) -> None:
    seen = set()
    for suite in data.get("suites") or []:
        for row in suite.get("rows") or []:
            rid = row.get("id")
            if not rid or rid in seen:
                raise ScenarioError(f"row id missing or duplicated: {rid!r}")
            seen.add(rid)
            if row.get("kind") not in KINDS:
                raise ScenarioError(f"{rid}: kind must be one of {KINDS}")
            steps = row.get("steps") or []
            if row["kind"] == "script" and not steps:
                raise ScenarioError(f"{rid}: script rows need steps")
            if row["kind"] != "script" and steps:
                raise ScenarioError(f"{rid}: only script rows have steps")
            aliases = set()
            for step in steps:
                op = step.get("op")
                if op not in OPS:
                    raise ScenarioError(f"{rid}: unknown op {op!r}")
                extra = set(step) - {"op"} - STEP_KEYS[op]
                if extra:
                    raise ScenarioError(f"{rid}: {op} does not take {sorted(extra)}")
                if op == "start":
                    if not step.get("prompt"):
                        raise ScenarioError(f"{rid}: start needs a prompt")
                    if step.get("on", "self") not in ("self", "peer"):
                        raise ScenarioError(f"{rid}: on must be self or peer")
                    aliases.add(step.get("as", "a"))
                elif op in ("message", "await", "compact"):
                    if step.get("session", "a") not in aliases:
                        raise ScenarioError(f"{rid}: {op} names a session no earlier step started")
                    if op == "message" and not step.get("prompt"):
                        raise ScenarioError(f"{rid}: message needs a prompt")


def rows(data: dict) -> list:
    return [row for suite in data["suites"] for row in suite["rows"]]


def is_two_worker(row: dict) -> bool:
    return bool(row.get("two_worker")) or any(s.get("on") == "peer" for s in row.get("steps") or [])


def _cell(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def render_markdown(data: dict) -> str:
    out = [
        "<!-- Generated from scenarios.json by `python -m scripts.live_smoke render`. Edit the JSON, not this file. -->",
        "",
        "# Standing live-test scenarios",
        "",
        "Run all script rows with `python -m scripts.live_smoke run` (see",
        "`docs/internal/live-smoke.md`). Kinds: `script` rows are driven by the",
        "runner; `browser` rows need the browser tools and `owner` rows need the",
        "owner; record those with `python -m scripts.live_smoke record`.",
        f"Default token cap per run: {data.get('defaults', {}).get('token_cap')} tokens.",
    ]
    for suite in data["suites"]:
        out += ["", f"## {suite['name']}", ""]
        if suite.get("intro"):
            out += [suite["intro"], ""]
        out += ["| # | Check | Kind | How | Pass when | Past bug |", "|---|---|---|---|---|---|"]
        for row in suite["rows"]:
            kind = row["kind"] + (", two workers" if is_two_worker(row) else "")
            kind += ", optional" if row.get("optional") else ""
            out.append("| " + " | ".join(_cell(v) for v in (
                row["id"].rsplit(".", 1)[-1], row["check"], kind, row.get("how"),
                row.get("pass_when"), row.get("past_bug", ""))) + " |")
    out += [
        "",
        "Cleanup: the runner archives every session it created, also on failure",
        "or interruption. Rows that need the peer are reported \"not run: worker",
        "offline\" when it is not online.",
        "",
    ]
    return "\n".join(out)
