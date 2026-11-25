"""Replay Claude Code transcripts against derived memory triggers (Memory v3, phase 0).

Measures, offline and read-only, how often just-in-time delivery would fire
(fluid.codemode.triggers) on real sessions: deliveries per session, share of
facts delivered at least once, the trigger kinds that fire, characters a
full-text delivery would add, and whether a fact with a command trigger is
matched before the first matching command runs. Nothing is written except
the files named by --json and --out.

Replay:

    python tools/research/memory_trigger_replay.py \\
        --facts <fluid home>/skills/memories.json \\
        --transcripts ~/.claude/projects --days 30 \\
        --json <scratch>/replay.json --sample 80 --out <scratch>/sample.jsonl

``--transcripts`` holds Claude Code CLI JSONL transcripts, one directory per
working directory (``<dir>/*/*.jsonl``). Each transcript file is one session.
Facts are scoped like the snapshot: global facts plus the facts of the
session's project, found from the git remote of the transcript's working
directory. A session whose project cannot be mapped gets global facts only
(``--unscoped all`` gives it every fact) and is counted as unscoped.

The labelling sample (--sample N --out FILE) holds the fact id, its
when-line, the trigger kind and terms, and at most 200 characters of the
triggering snippet. It quotes transcript text: keep it out of the
repository. The aggregate JSON and table hold counts and fact ids only.

User turns skip tool results and harness text (system reminders,
compaction summaries and notes, task notifications, folded epochs); a
"[user interjection ...]" inside a tool result counts as a user turn.
Each fact is delivered at most once per session, at most --per-event facts
per turn or tool call (best score first).

Word deliveries are split by source: a short user turn (at most
SHORT_TURN_CHARS, usually typed by a person), a long one (usually a parent
agent's brief opening a subagent session), or a delegation tool's input.
``--word-turn-chars N`` ignores word matches on user turns longer than N.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import random
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "fluid"))

from fluid.codemode.triggers import (  # noqa: E402
    TriggerFact,
    command_text,
    match_tool,
    match_turn,
    prepare,
)

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_INTERJECTION = re.compile(r"\[user interjection[^\]]*\]\s*(.*?)\s*\[/user interjection\]", re.S)
_AGENT_MARK = re.compile(r"^\[agent.message from=[^\]]*\]\s*|\[/agent message\]\s*$", re.M)
_HARNESS_PREFIXES = (
    "Earlier context continues", "<task-notification>", "This session is being continued",
    "Tool loaded.", "[Fluid compaction note", "What you were told before", "[epoch node_",
    "<command-name>", "<local-command", "[Request interrupted", "Caveat:", "[harness notice",
)
SNIPPET_CHARS = 200
SHORT_TURN_CHARS = 1000


@dataclass
class Session:
    path: Path
    cwd: str = ""
    events: list[tuple[str, object]] = field(default_factory=list)  # ("turn", text) | ("tool", (name, input))


def _git_origin_scope(cwd: str, cache: dict[str, str | None]) -> str | None:
    """``project:pv2-…`` for a working directory, as AI Shuffle derives it
    (app/core/project_links.auto_link_group_id, app/db/projects_v2)."""
    if cwd in cache:
        return cache[cwd]
    scope = None
    path = Path(cwd)
    for candidate in (path, *path.parents):
        marker = candidate / ".git"
        try:
            if not marker.exists():
                continue
            gitdir = marker
            if marker.is_file():
                target = marker.read_text(encoding="utf-8").strip()
                gitdir = (candidate / target.removeprefix("gitdir:").strip()).resolve()
            common = gitdir / "commondir"
            if common.exists():
                gitdir = (gitdir / common.read_text(encoding="utf-8").strip()).resolve()
            config = configparser.RawConfigParser()
            config.read(gitdir / "config", encoding="utf-8")
            url = config.get('remote "origin"', "url", fallback="").strip().rstrip("/")
        except (OSError, configparser.Error):
            break
        if url:
            url = url[:-4] if url.lower().endswith(".git") else url
            group = "git:" + hashlib.sha256(url.lower().encode()).hexdigest()[:16]
            scope = "project:pv2-" + hashlib.sha256(group.encode()).hexdigest()[:24]
        break
    cache[cwd] = scope
    return scope


def _user_texts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_result":
            inner = block.get("content")
            texts = [inner] if isinstance(inner, str) else [
                str(b.get("text") or "") for b in inner or [] if isinstance(b, dict)]
            for text in texts:
                out.extend(_INTERJECTION.findall(text))
    return out


def clean_turn(text: str) -> str:
    """User-authored text of a turn, or "" for harness text."""
    text = _REMINDER.sub("", text).strip()
    if not text or text.startswith(_HARNESS_PREFIXES):
        return ""
    return _AGENT_MARK.sub("", text).strip()


def load_session(path: Path) -> Session:
    session = Session(path)
    seen_tools: set[str] = set()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("type")
            if kind not in ("user", "assistant"):
                continue
            if not session.cwd and record.get("cwd"):
                session.cwd = str(record["cwd"])
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            if kind == "user":
                for text in _user_texts(message.get("content")):
                    text = clean_turn(text)
                    if text:
                        session.events.append(("turn", text))
            else:
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id = str(block.get("id") or "")
                        if tool_id and tool_id in seen_tools:
                            continue
                        seen_tools.add(tool_id)
                        session.events.append(("tool", (str(block.get("name") or ""), block.get("input") or {})))
    return session


def _snippet(event: tuple[str, object], terms: tuple[str, ...]) -> str:
    """At most SNIPPET_CHARS of the event, around the first matched term."""
    if event[0] == "turn":
        prefix, text = "", str(event[1])
    else:
        name, tool_input = event[1]  # type: ignore[misc]
        prefix = name.rsplit("__", 1)[-1] + ": "
        tool_input = tool_input or {}
        # The text the matcher saw: a command without its heredocs and long
        # quoted strings, else every string field.
        text = command_text(tool_input) or " ".join(
            v for v in tool_input.values() if isinstance(v, str))
    text = " ".join(text.split())
    folded = text.casefold()
    positions = []
    for term in terms:
        if term.startswith("re:"):
            found = re.search(term[3:], folded)
            if found:
                positions.append(found.start())
            continue
        needle = term.strip("*/").split("/")[0] if "/" in term else term.split()[0]
        if needle and needle in folded:
            positions.append(folded.find(needle))
    start = max(0, min(positions) - 60) if positions else 0
    return (prefix + text[start:start + SNIPPET_CHARS])[:SNIPPET_CHARS]


def _percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(q * len(ordered)))])


def _word_source(event: tuple[str, object]) -> str:
    if event[0] == "tool":
        return "delegation"
    return "short_turn" if len(str(event[1])) <= SHORT_TURN_CHARS else "long_turn"


def replay(facts_path: Path, transcripts: Path, days: int, *, unscoped: str = "global",
           per_event: int = 3, cap_chars: int = 1500, sample: int = 0, seed: int = 7,
           sample_kinds: frozenset[str] = frozenset(),
           word_turn_chars: int = 0):
    document = json.loads(facts_path.read_text(encoding="utf-8"))
    entries = [e for e in document.get("entries", []) if isinstance(e, dict)]
    scope_of = {str(e.get("id")): str(e.get("scope") or "global") for e in entries}
    candidates = prepare(entries)
    by_id = {f.id: f for f in candidates}
    command_facts = {f.id for f in candidates if f.triggers.command}
    cutoff = time.time() - days * 86400
    files = sorted(p for p in transcripts.glob("*/*.jsonl") if p.stat().st_mtime >= cutoff)

    cache: dict[str, str | None] = {}
    per_session_deliveries: list[int] = []
    per_session_chars: list[int] = []
    by_kind: dict[str, int] = {}
    word_sources: dict[str, int] = {}
    by_fact: dict[str, int] = {}
    deliveries: list[dict] = []
    cap_hits = unscoped_sessions = empty_sessions = turns = tools = 0
    in_scope: set[str] = set()
    timing = {"command_fact_sessions": 0, "earlier_match": 0, "first_at_command": 0}
    for path in files:
        session = load_session(path)
        if not session.events:
            empty_sessions += 1
            continue
        project = _git_origin_scope(session.cwd, cache) if session.cwd else None
        if project is None:
            unscoped_sessions += 1
        allowed: list[TriggerFact] = [
            f for f in candidates
            if scope_of[f.id] == "global" or scope_of[f.id] == project
            or (project is None and unscoped == "all")]
        in_scope.update(f.id for f in allowed)
        delivered: set[str] = set()
        first_other: dict[str, int] = {}
        first_command: dict[str, int] = {}
        count = chars = 0
        for index, event in enumerate(session.events):
            if event[0] == "turn":
                turns += 1
                too_long = word_turn_chars and len(str(event[1])) > word_turn_chars
                matches = [] if too_long else match_turn(str(event[1]), allowed)
            else:
                tools += 1
                name, tool_input = event[1]  # type: ignore[misc]
                matches = match_tool(name, tool_input, allowed)
            for m in matches:
                if m.kind == "command":
                    first_command.setdefault(m.fact_id, index)
                else:
                    first_other.setdefault(m.fact_id, index)
            fresh = [m for m in matches if m.fact_id not in delivered]
            if len(fresh) > per_event:
                cap_hits += 1
            event_chars = 0
            for m in fresh[:per_event]:
                delivered.add(m.fact_id)
                count += 1
                event_chars += by_id[m.fact_id].chars
                by_kind[m.kind] = by_kind.get(m.kind, 0) + 1
                by_fact[m.fact_id] = by_fact.get(m.fact_id, 0) + 1
                source = _word_source(event) if m.kind == "words" else ""
                if source:
                    word_sources[source] = word_sources.get(source, 0) + 1
                deliveries.append({"fact_id": m.fact_id, "when": by_id[m.fact_id].when,
                                   "kind": m.kind, "source": source, "terms": list(m.terms),
                                   "snippet": _snippet(event, m.terms)})
            chars += min(event_chars, cap_chars)
        for fact_id, at in first_command.items():
            if fact_id not in command_facts:
                continue
            timing["command_fact_sessions"] += 1
            if first_other.get(fact_id, at) < at:
                timing["earlier_match"] += 1
            else:
                timing["first_at_command"] += 1
        per_session_deliveries.append(count)
        per_session_chars.append(chars)

    sessions = len(per_session_deliveries)
    reached = {d["fact_id"] for d in deliveries}
    shapes: dict[str, dict[str, int]] = {}
    for f in candidates:
        if f.id in in_scope:
            row = shapes.setdefault(_shape(f), {"facts": 0, "delivered": 0})
            row["facts"] += 1
            row["delivered"] += f.id in reached
    result = {
        "transcript_files": len(files),
        "sessions_replayed": sessions,
        "sessions_without_user_turns_or_tools": empty_sessions,
        "unscoped_sessions": unscoped_sessions,
        "user_turns": turns,
        "tool_calls": tools,
        "candidate_facts": len(candidates),
        "live_facts": len(entries),
        "facts_delivered_at_least_once": len(reached),
        "coverage": round(len(reached) / len(candidates), 3) if candidates else 0.0,
        # Facts some replayed session could receive (its scope matched).
        "facts_in_scope": len(in_scope),
        "coverage_in_scope": round(len(reached & in_scope) / len(in_scope), 3) if in_scope else 0.0,
        "coverage_by_trigger_shape": dict(sorted(shapes.items())),
        "deliveries": len(deliveries),
        "deliveries_per_session": {
            "mean": round(statistics.fmean(per_session_deliveries), 2) if sessions else 0.0,
            "p50": _percentile(per_session_deliveries, 0.5),
            "p90": _percentile(per_session_deliveries, 0.9)},
        "chars_per_session": {
            "mean": round(statistics.fmean(per_session_chars), 1) if sessions else 0.0,
            "p50": _percentile(per_session_chars, 0.5),
            "p90": _percentile(per_session_chars, 0.9),
            "cap_per_event": cap_chars},
        "events_over_per_event_cap": cap_hits,
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        "word_sources": dict(sorted(word_sources.items(), key=lambda kv: -kv[1])),
        "top_facts": sorted(by_fact.items(), key=lambda kv: -kv[1])[:15],
        "timing": timing,
    }
    pool = [d for d in deliveries if not sample_kinds or d["kind"] in sample_kinds]
    picked = random.Random(seed).sample(pool, min(sample, len(pool))) if sample else []
    return result, picked


def _shape(fact: TriggerFact) -> str:
    """The strongest trigger a fact has: command, path, tool, or words only."""
    t = fact.triggers
    return ("command" if t.command else "path" if t.paths else "tool" if t.tools
            else "words_only" if t.words else "none")


def _table(result: dict) -> str:
    dps, cps, t = result["deliveries_per_session"], result["chars_per_session"], result["timing"]
    rows = [
        ("sessions replayed", result["sessions_replayed"]),
        ("unscoped sessions (global facts only)", result["unscoped_sessions"]),
        ("user turns / tool calls", f'{result["user_turns"]} / {result["tool_calls"]}'),
        ("candidate facts", result["candidate_facts"]),
        ("coverage (delivered at least once)",
         f'{result["facts_delivered_at_least_once"]} ({result["coverage"]:.0%})'),
        ("coverage of facts in scope of a session",
         f'{result["coverage_in_scope"]:.0%} of {result["facts_in_scope"]}'),
        ("in scope: delivered / facts by shape", ", ".join(
            f'{k} {v["delivered"]}/{v["facts"]}' for k, v in result["coverage_by_trigger_shape"].items())),
        ("deliveries per session mean/p50/p90", f'{dps["mean"]} / {dps["p50"]:.0f} / {dps["p90"]:.0f}'),
        ("chars per session mean/p50/p90", f'{cps["mean"]:.0f} / {cps["p50"]:.0f} / {cps["p90"]:.0f}'),
        ("by kind", ", ".join(f"{k} {v}" for k, v in result["by_kind"].items())),
        ("word deliveries by source", ", ".join(f"{k} {v}" for k, v in result["word_sources"].items())),
        ("command facts: matched earlier / first at command",
         f'{t["earlier_match"]} / {t["first_at_command"]} of {t["command_fact_sessions"]}'),
    ]
    width = max(len(name) for name, _ in rows)
    return "\n".join(f"{name.ljust(width)}  {value}" for name, value in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--facts", type=Path, required=True, help="memories.json (read only)")
    parser.add_argument("--transcripts", type=Path, required=True, help="directory of <dir>/*.jsonl")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--unscoped", choices=("global", "all"), default="global")
    parser.add_argument("--per-event", type=int, default=3)
    parser.add_argument("--cap-chars", type=int, default=1500)
    parser.add_argument("--json", type=Path, help="write the aggregate result here")
    parser.add_argument("--sample", type=int, default=0, help="labelling sample size")
    parser.add_argument("--out", type=Path, help="labelling sample JSONL (keep out of the repo)")
    parser.add_argument("--word-threshold", type=int, default=0,
                        help="override triggers.WORD_THRESHOLD for a what-if run")
    parser.add_argument("--sample-kinds", default="",
                        help="comma-separated trigger kinds to sample (default: all)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--word-turn-chars", type=int, default=0,
                        help="ignore word matches on user turns longer than this (0: no limit)")
    args = parser.parse_args(argv)
    if args.sample and not args.out:
        parser.error("--sample needs --out")
    if args.word_threshold:
        import fluid.codemode.triggers as triggers
        triggers.WORD_THRESHOLD = args.word_threshold
    result, picked = replay(args.facts, args.transcripts.expanduser(), args.days,
                            unscoped=args.unscoped, per_event=args.per_event,
                            cap_chars=args.cap_chars, sample=args.sample, seed=args.seed,
                            sample_kinds=frozenset(filter(None, args.sample_kinds.split(","))),
                            word_turn_chars=args.word_turn_chars)
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.out:
        with args.out.open("w", encoding="utf-8") as handle:
            for row in picked:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(_table(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
