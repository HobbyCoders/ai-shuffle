#!/usr/bin/env python3
"""Score the learner model's conflict review on labelled sets of owner rules.

    PYTHONPATH=fluid python scripts/measure_memory_questions.py
    PYTHONPATH=fluid python scripts/measure_memory_questions.py --claude-cli sonnet
    PYTHONPATH=fluid python scripts/measure_memory_questions.py --cases private.json

Each case saves its rules as owner-kept facts in a disposable store and runs
the shipped review (``fluid.codemode.conflict_review.run``) with the learner
model fluid is configured with, so this SPENDS model calls: one per case, two
when the model claims a conflict. No worker memory is read.

``--claude-cli MODEL`` answers the same prompts through a signed-in Claude
Code CLI (``claude -p``) instead, for a machine with no API key configured
for fluid. Everything but the transport is the shipped code.

A case is ``{"name": ..., "rules": [text, ...], "conflicts": [[i, j], ...]}``
where ``conflicts`` lists the index pairs that really contradict (usually
none). The built-in cases are invented. ``--cases`` adds a JSON list of your
own, for example rules a previous detector asked about by mistake; only case
names and counts are printed, never rule text.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from fluid.codemode import conflict_review
from fluid.codemode.continual import Synthesizer
from fluid.codemode.memory_backfill import _synthesizer
from fluid.codemode.skills import SkillStore
from fluid.providers.base import StreamChunk

MAX_PASSES = 4


class ClaudeCli:
    """One-shot text completion through ``claude -p``: no tools, no project
    files, the request's own system prompt."""
    name = "claude-cli"

    def __init__(self, model: str) -> None:
        self.model = model

    async def stream(self, request):
        with tempfile.TemporaryDirectory(prefix="question-measure-cli-") as cwd:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", "--model", self.model, "--system-prompt", request.system,
                "--output-format", "json", "--tools", "", "--strict-mcp-config",
                "--setting-sources", "", "--no-session-persistence",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=cwd)
            out, _ = await proc.communicate(request.messages[0].content[0].text.encode())
        answer = json.loads(out)
        if proc.returncode or answer.get("is_error"):
            raise RuntimeError("claude -p failed")
        usage = answer.get("usage") or {}
        yield StreamChunk(kind="text_delta", payload={"text": str(answer.get("result") or "")})
        yield StreamChunk(kind="usage", payload={"stop_reason": "end_turn", "usage": {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0)}})

CASES = [
    # Rules that share words (CI, merge, nightly, hook) without contradicting.
    {"name": "ci rules that share words", "conflicts": [], "rules": [
        "The nightly build must not run after a merge to main. It runs once a night at midnight, "
        "or when the owner starts it by hand, and ships the newest main commit whose CI run passed.",
        "Never put real machine names, addresses or hostnames into tracked files, commit messages "
        "or test fixtures. The pre-push hook refuses them; never push with --no-verify.",
        "Merge a pull request yourself once it is reviewed and CI is green. The CI run on main "
        "after the merge decides whether that commit may go into the nightly build.",
        "CI runs on hosted runners. Do not move the test suite to the owner's machines to save "
        "CI minutes.",
    ]},
    {"name": "a general rule and its exception", "conflicts": [], "rules": [
        "Run the changed test files before every push.",
        "For a change that only touches documentation, push without running tests.",
    ]},
    {"name": "the same rule in other words", "conflicts": [], "rules": [
        "Go files are indented with tabs.",
        "Use tabs, not spaces, to indent Go code; gofmt decides the layout.",
    ]},
    {"name": "different projects", "conflicts": [], "rules": [
        "In the website project, deploy with the deploy script right after a merge.",
        "In the game server project, never deploy without asking the owner first.",
    ]},
    {"name": "history told inside one rule", "conflicts": [], "rules": [
        "Reviews used to go to Opus; since November they go to Sol.",
        "Sol reviews every code change before a pull request is opened.",
    ]},
    {"name": "a model for each job", "conflicts": [], "rules": [
        "UI work goes to Opus.",
        "Backend reviews go to Sol.",
        "Quick lookups go to Haiku.",
        "Never pick a model the owner has not named for the job without asking.",
    ]},
    # Real contradictions.
    {"name": "who reviews code", "conflicts": [[0, 1]], "rules": [
        "Code reviews are done by Opus.",
        "From now on code reviews are done by Sol, never by Opus.",
    ]},
    {"name": "review before or after the pull request", "conflicts": [[0, 1]], "rules": [
        "Open a pull request first, then ask for a review of that pull request.",
        "Never open a pull request before the review has approved the branch.",
    ]},
    {"name": "when the nightly runs", "conflicts": [[0, 1]], "rules": [
        "The nightly build starts automatically after every merge to main.",
        "The nightly build must not run after a merge. It runs once a night at midnight.",
    ]},
    {"name": "a contradiction among unrelated rules", "conflicts": [[2, 7]], "rules": [
        "Commit messages are written in the imperative mood.",
        "Keep the owner's email address out of anything sent to an outside service.",
        "Always squash-merge pull requests.",
        "Screenshots for a UI change are taken at phone and desktop width.",
        "Database migrations are tested against a copy of the data first.",
        "Ask before spending money on a hosted service.",
        "Tests run on hosted CI, not on the owner's machines.",
        "Never squash. Merge pull requests with a merge commit so the history is kept.",
        "Use the project's theme colours, never a new hex value.",
    ]},
    {"name": "which language to answer in", "conflicts": [[0, 1]], "rules": [
        "Answer the owner in German.",
        "Always reply in English, whatever language the question is written in.",
    ]},
]


async def score(case: dict, cli_model: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="question-measure-") as home:
        store = SkillStore(Path(home))
        ids = [store.save_memory(text, protected=True).id for text in case["rules"]]
        synth = (Synthesizer(ClaudeCli(cli_model), cli_model, store, pass_timeout=300)
                 if cli_model else _synthesizer(store))
        calls = passes = 0
        while conflict_review.pending(store) and passes < MAX_PASSES:
            passes += 1
            result = await conflict_review.run(synth)
            calls += result.calls
            if not result.calls:
                break
        found = {frozenset(ids.index(e["id"]) for e in (c["a"], c["b"]))
                 for c in conflict_review.open_conflicts(
                     store._facts.snapshot()["entries"], store.root)}
        expected = {frozenset(pair) for pair in case.get("conflicts") or []}
        return {"case": case["name"], "rules": len(ids), "expected": len(expected),
                "asked": len(found), "false_questions": len(found - expected),
                "missed": len(expected - found), "calls": calls,
                "reviewed": conflict_review.pending(store) == 0, "model": synth._model}


async def measure(cases: list[dict], cli_model: str | None = None) -> dict:
    rows = [await score(case, cli_model) for case in cases]
    return {"model": rows[0]["model"] if rows else None, "cases": len(rows),
            "false_questions": sum(r["false_questions"] for r in rows),
            "missed": sum(r["missed"] for r in rows),
            "not_reviewed": sum(1 for r in rows if not r["reviewed"]),
            "calls": sum(r["calls"] for r in rows),
            "rows": [{k: v for k, v in r.items() if k != "model"} for r in rows]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, help="JSON list of extra cases (kept out of the repository)")
    parser.add_argument("--only-extra", action="store_true", help="skip the built-in cases")
    parser.add_argument("--claude-cli", metavar="MODEL",
                        help="answer through a signed-in Claude Code CLI with this model")
    args = parser.parse_args()
    extra = json.loads(args.cases.read_text(encoding="utf-8")) if args.cases else []
    print(json.dumps(asyncio.run(measure(([] if args.only_extra else CASES) + extra,
                                         args.claude_cli)), indent=2))
