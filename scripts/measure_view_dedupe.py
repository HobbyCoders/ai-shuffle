"""Measure what an in-view pointer saves on repeat Read, Grep, Glob and Skill calls.

Run: PYTHONPATH=fluid python scripts/measure_view_dedupe.py [--json]

No model, network or database. A real NativeLoop with the real builtin tools
runs one scripted turn over a scratch project: it loads a skill whose body is
the measured mean (~7.6k characters), reads a 400-line file, greps and globs,
then makes each of those calls again while the first results are still in the
request. It runs twice:

* ``before``: the view check is off, so every tool takes the path it had
  before this change (Read, Glob and Skill deliver in full; Grep already
  swapped an identical repeat for a pointer, without checking the view);
* ``after``: the current loop, where a repeat whose earlier result is still
  verbatim in the request gets a pointer.

Reported per tool: characters of the repeat's result as the next request
carries it, before and after. Tokens are chars/4, not provider billing. The
weekly projection multiplies by the counts the coordinator measured on the
desktop worker (Nov 7-12 2026): 150 mid-chat skill reloads and 57 groups of
repeated identical calls. Only the share of those repeats whose earlier copy
was still in view is saved; that share was not measured, so the projection
is an upper bound.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from fluid.engine import native
from fluid.events import UserTurn
from fluid.messages import ToolResultBlock
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools import view
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry

SKILL_BODY_CHARS = 7_600
WEEKLY_SKILL_RELOADS = 150
WEEKLY_REPEAT_GROUPS = 57


def _call(tuid: str, name: str, args: dict) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                    "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps(args)}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _done() -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": "done"}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


class _Recording:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.requests: list = []

    async def stream(self, request):
        self.requests.append(request)
        for chunk in self.scripts[min(len(self.requests) - 1, len(self.scripts) - 1)]:
            yield chunk


def _results(request) -> dict[str, str]:
    out: dict[str, str] = {}
    for message in request.messages:
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                out[block.tool_use_id] = "".join(getattr(p, "text", "") for p in block.content)
    return out


def _project(root: Path) -> Path:
    proj = root / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / ".git").mkdir()
    (proj / "src" / "engine.py").write_text(
        "".join(f"def step_{i}(state):\n    return state + {i}  # advance\n" for i in range(200)),
        encoding="utf-8",
    )
    for i in range(30):
        (proj / "src" / f"mod_{i}.py").write_text(f"VALUE = {i}  # advance\n", encoding="utf-8")
    skill = proj / ".fluid" / "skills" / "deploy"
    skill.mkdir(parents=True)
    line = "Check the staging build, then follow the release checklist step by step.\n"
    body = (line * (SKILL_BODY_CHARS // len(line) + 1))[:SKILL_BODY_CHARS]
    (skill / "SKILL.md").write_text(
        f"---\nname: deploy\ndescription: deploy steps\n---\n{body}", encoding="utf-8",
    )
    return proj


CALLS = [
    ("Skill", {"name": "deploy"}),
    ("Read", {"file_path": "src/engine.py"}),
    ("Grep", {"pattern": "advance"}),
    ("Glob", {"pattern": "src/**/*.py"}),
]


async def _run(proj: Path, dedupe: bool) -> dict[str, tuple[int, int]]:
    scripts = []
    for round_ in ("a", "b"):
        for i, (name, args) in enumerate(CALLS):
            if name == "Read":
                args = {"file_path": str(proj / args["file_path"])}
            scripts.append(_call(f"{round_}{i}", name, args))
    scripts.append(_done())
    provider = _Recording(scripts)
    registry = ToolRegistry()
    register_builtins(registry)
    loop = native.NativeLoop(
        provider=provider, store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("never_ask"), ctx=ToolCtx(cwd=str(proj)),
    )
    original = view._call_id
    if not dedupe:
        # No surface can check the view: every tool takes its old path.
        view._call_id = lambda ctx: None
    try:
        await loop.submit(UserTurn(content="work"))
        await loop.wait_turn()
    finally:
        view._call_id = original
    final = _results(provider.requests[-1])
    return {name: (len(final[f"a{i}"]), len(final[f"b{i}"])) for i, (name, _) in enumerate(CALLS)}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    opts = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        proj = _project(Path(tmp))
        before = await _run(proj, dedupe=False)
        after = await _run(proj, dedupe=True)
    rows = []
    for name, _ in CALLS:
        first, repeat_before = before[name]
        _, repeat_after = after[name]
        rows.append({
            "tool": name, "first_chars": first,
            "repeat_before_chars": repeat_before, "repeat_after_chars": repeat_after,
            "saved_chars": repeat_before - repeat_after,
        })
    skill_saved = next(r["saved_chars"] for r in rows if r["tool"] == "Skill")
    other = [r["saved_chars"] for r in rows if r["tool"] != "Skill"]
    projection = {
        "skill_reloads_chars_upper_bound": skill_saved * WEEKLY_SKILL_RELOADS,
        "repeat_groups_chars_upper_bound": round(sum(other) / len(other)) * WEEKLY_REPEAT_GROUPS,
    }
    if opts.json:
        print(json.dumps({"rows": rows, "weekly": projection}, indent=2))
        return 0
    print(f"{'tool':<6} {'first':>7} {'repeat before':>14} {'repeat after':>13} {'saved':>7}")
    for r in rows:
        print(f"{r['tool']:<6} {r['first_chars']:>7} {r['repeat_before_chars']:>14} "
              f"{r['repeat_after_chars']:>13} {r['saved_chars']:>7}")
    print()
    # Each character kept out of history is also kept out of every later
    # request of that context, mostly at cache-read price.
    for key, chars in projection.items():
        print(f"weekly {key}: {chars:,} chars (~{chars // 4:,} tokens) kept out of history")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
