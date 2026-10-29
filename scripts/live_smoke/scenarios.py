"""Load and validate the smoke scenarios, and render the human-readable table.

``scripts/live_smoke/scenarios.json`` is the one source of truth;
``docs/internal/live-smoke-scenarios.md`` is generated from it (``python -m
scripts.live_smoke render``) and a unit test keeps the two in sync.
New row IDs use stable ``<group>.<slug>`` names; legacy numeric IDs stay valid.

Kinds: ``script`` rows are driven by the runner on a real worker; ``journey``
rows are covered by UI journeys (``frontend/tests/journeys/<name>.journey.mjs``)
whose pass in CI at the candidate is the evidence; ``browser`` and ``owner``
rows are checked by hand and say why with ``needs`` (see :data:`NEEDS`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_JSON = Path(__file__).with_name("scenarios.json")
SCENARIOS_MD = ROOT / "docs" / "internal" / "live-smoke-scenarios.md"
JOURNEYS_DIR = ROOT / "frontend" / "tests" / "journeys"

ROW_ID = re.compile(
    r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\."
    r"(?:[0-9]+|[a-z][a-z0-9]*(?:-[a-z0-9]+)*)"
)
KINDS = ("script", "journey", "browser", "owner")
#: Why a hand-checked (browser/owner) row is not automated. A row names one or more.
NEEDS = {
    "device": "a real phone, desktop app, Windows/macOS install or other hardware",
    "provider": "a real model provider, paid or subscription account, or outside service",
    "install": "a disposable candidate install or worker pair (restarts, upgrades, faults, containers)",
    "owner": "the owner's own data, history or decision",
    "dev-instance": "could be a UI journey on the dev instance with the fake AI; not written yet",
}
OPS = ("workers", "start", "message", "await", "compact", "tool_catalog", "peer_delivery")
EXPECT_KEYS = ("expect_reply", "expect_tool", "expect_owner", "expect_files", "user_messages", "replies")
STEP_KEYS = {
    "workers": set(),
    "tool_catalog": {"tools"},
    "peer_delivery": {"sender", "session", "message", "request_id", "within_s"},
    "start": {"as", "on", "prompt", "wait", "within_s", *EXPECT_KEYS},
    "message": {"session", "prompt", "when", "wait", "within_s", *EXPECT_KEYS},
    "await": {"session", "within_s", *EXPECT_KEYS},
    "compact": {"session", "within_s"},
}


class ScenarioError(ValueError):
    pass


def journey_names(directory: Path | None = None) -> set:
    """Names of the UI journeys in the checkout (``<name>.journey.mjs``)."""
    folder = Path(directory or JOURNEYS_DIR)
    return {p.name.removesuffix(".journey.mjs") for p in folder.glob("*.journey.mjs")}


def load(path: Path | None = None) -> dict:
    data = json.loads(Path(path or SCENARIOS_JSON).read_text(encoding="utf-8"))
    validate(data)
    return data


def _validate_coverage(rid: str, row: dict, journeys: set) -> None:
    """``journeys`` on journey rows, ``needs`` on hand-checked rows, neither elsewhere."""
    names, needs = row.get("journeys"), row.get("needs")
    if row["kind"] == "journey":
        if not isinstance(names, list) or not names or len(set(names)) != len(names):
            raise ScenarioError(f"{rid}: journey rows name their journeys (a list without repeats)")
        unknown = sorted(n for n in names if n not in journeys)
        if unknown:
            raise ScenarioError(f"{rid}: no UI journey named {', '.join(map(repr, unknown))} "
                                f"in {JOURNEYS_DIR.relative_to(ROOT).as_posix()}")
    elif names is not None:
        raise ScenarioError(f"{rid}: only journey rows name journeys")
    if row["kind"] in ("browser", "owner"):
        if not isinstance(needs, list) or not needs or len(set(needs)) != len(needs):
            raise ScenarioError(f"{rid}: {row['kind']} rows say why with needs (one or more of {sorted(NEEDS)})")
        unknown = sorted(n for n in needs if n not in NEEDS)
        if unknown:
            raise ScenarioError(f"{rid}: unknown needs {unknown}; use {sorted(NEEDS)}")
    elif needs is not None:
        raise ScenarioError(f"{rid}: only browser and owner rows take needs")


def validate(data: dict, journeys: set | None = None) -> None:
    journeys = journey_names() if journeys is None else journeys
    seen = set()
    for suite in data.get("suites") or []:
        for row in suite.get("rows") or []:
            rid = row.get("id")
            if not isinstance(rid, str) or not rid or rid in seen:
                raise ScenarioError(f"row id missing or duplicated: {rid!r}")
            if not ROW_ID.fullmatch(rid):
                raise ScenarioError(f"{rid!r}: row id must be <group>.<number-or-slug>")
            seen.add(rid)
            if row.get("kind") not in KINDS:
                raise ScenarioError(f"{rid}: kind must be one of {KINDS}")
            steps = row.get("steps") or []
            if row["kind"] == "script" and not steps:
                raise ScenarioError(f"{rid}: script rows need steps")
            if row["kind"] != "script" and steps:
                raise ScenarioError(f"{rid}: only script rows have steps")
            _validate_coverage(rid, row, journeys)
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
                elif op == "tool_catalog":
                    if not step.get("tools"):
                        raise ScenarioError(f"{rid}: tool_catalog needs tools")
                elif op == "peer_delivery":
                    if step.get("sender") not in aliases or step.get("session") not in aliases:
                        raise ScenarioError(f"{rid}: peer_delivery needs earlier sender and receiver sessions")
                    if not step.get("message") or not step.get("request_id"):
                        raise ScenarioError(f"{rid}: peer_delivery needs message and request_id")
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
        "runner; `journey` rows are covered by the named UI journeys",
        "(`frontend/tests/journeys/`), whose pass in CI at the candidate is their",
        "evidence; `browser` rows need the browser tools and `owner` rows a person;",
        "record those with `python -m scripts.live_smoke record`. Hand-checked rows",
        "say why they are not automated (`needs`): "
        + "; ".join(f"`{k}`: {v}" for k, v in NEEDS.items()) + ".",
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
            kind += f": {', '.join(row['journeys'])}" if row.get("journeys") else ""
            kind += f"; needs {' + '.join(row['needs'])}" if row.get("needs") else ""
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
