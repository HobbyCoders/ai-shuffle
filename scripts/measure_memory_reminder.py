"""Measure what the # Memory snapshot costs across a scripted 20-turn session.

Run: PYTHONPATH=fluid python scripts/measure_memory_reminder.py [--json] [--legacy-only]

No model, network or database. Both legs run the real HarnessAdapter:

* native: a real NativeLoop over a recording provider. Every model request is
  captured as sent (system, tool schemas, messages);
* delegated: a real DelegatedLoop driving the fake ``claude`` CLI from
  fluid/tests/fixtures (scenario ``x``). The CLI owns its request, so the
  request is MODELLED from what the loop handed it: the user messages as
  written to its stdin (prompt + turn note), the replies, and, after its
  compaction, a summary plus the SessionStart hook's put-back note.

The script: a memory store seeded to the real snapshot size (~8.4k chars, the
mean of 2,768 snapshots in 20 long sessions on the audit worker), one
Remember at turn 8, 20 user turns of about 300 characters with replies of
about 1,200, and compaction: native compacts AUTOMATICALLY, inside turns,
whenever history passes AUTO_COMPACT_HISTORY_TOKENS above the fixed prefix
(the in-turn case: a digest folds the turn that carried the snapshot right
before a model call); delegated has the CLI compact mid-turn at turn 14.

Each leg runs twice: ``legacy`` (the full snapshot on every user message, the
behavior before this change, reproduced by routing the memory slot through
``_memory_snapshot``) and ``current``. Tokens are chars/4, not provider
billing. The prompt-cache figure models a provider cache that keeps the
previous request's prompt: a request's cached part is its longest whole-
message prefix shared with the request before it. The fixed prefix (system
prompt and tool schemas) stands in at the size measure_prompt_catalog.py
reports for the owner code fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]

TURNS = 20
REMEMBER_AT = 8
COMPACT_BEFORE = 14  # delegated: the CLI compacts during this turn
# Native: the compactor's trigger, in estimated tokens above the fixed prefix.
# Low on purpose, so the deduplicated run compacts too and the in-turn case
# (a digest folding the turn that carried the snapshot) is exercised.
AUTO_COMPACT_HISTORY_TOKENS = 500
USER_CHARS = 300
REPLY_CHARS = 1_200
# The delegated leg's CLI summary after compaction (modelled, not measured).
SUMMARY_CHARS = 6_000
MEMORY_TARGET_CHARS = 8_400


def _prefix_chars() -> tuple[int, int]:
    """The fixed prefix each leg sends, from measure_prompt_catalog.py's owner
    code fixture: native compiled eager prompt (tool sections included);
    delegated host append + loaded bridge schemas + deferred names (the CLI's
    own preset is not ours and not counted)."""
    from scripts.measure_prompt_catalog import measure as catalog

    native = catalog(native=True)["totals"]["compiled_eager"]
    delegated = catalog(native=False)["totals"]
    return native, (delegated["system"] + delegated["delegated_loaded_tools"]
                    + delegated["delegated_deferred_names"])


def _tokens(chars: int) -> int:
    return (chars + 3) // 4


def _memory_store(root: Path):
    """A real fluid memory store whose snapshot is about the audited size."""
    from fluid.codemode.skills import SkillStore

    store = SkillStore(root)
    i = 0
    while True:
        section = store.render_memory_section(None) or ""
        if len(section) >= MEMORY_TARGET_CHARS or "omitted for space" in section:
            break
        i += 1
        store.save_memory(
            f"Fixture fact {i}: the build host keeps service {i} on port {8000 + i}; "
            f"run its checks with the project's interpreter and keep its data under "
            f"/srv/fixture/{i} (owner note, kept for later sessions).",
            provenance="user",
        )
    return store


def _memory_chars(text: str) -> int:
    from app.core.memory_reminder import memory_blocks, wrap

    return sum(len(wrap(body)) for _, body in memory_blocks(text))


def _cache_stats(requests: list[list[str]], texts: list[list[str]] | None = None) -> list[dict]:
    """Per request: total tokens, the cached part (longest whole-element prefix
    shared with the previous request), and the memory tokens it carries
    (counted on ``texts``, the elements' plain text, when serialized elements
    escape newlines)."""
    out = []
    prev: list[str] = []
    for index, elements in enumerate(requests):
        plain = texts[index] if texts is not None else elements
        shared = 0
        for a, b in zip(prev, elements):
            if a != b:
                break
            shared += 1
        total = sum(len(e) for e in elements)
        cached = sum(len(e) for e in elements[:shared])
        out.append({
            "tokens": _tokens(total), "cached_tokens": _tokens(cached),
            "memory_tokens": _tokens(sum(_memory_chars(e) for e in plain)),
        })
        prev = elements
    return out


def _summary(per_request: list[dict]) -> dict:
    total = sum(r["tokens"] for r in per_request)
    cached = sum(r["cached_tokens"] for r in per_request)
    return {
        "requests": len(per_request),
        "tokens_total": total,
        "tokens_per_request_mean": round(total / max(len(per_request), 1)),
        "tokens_last_request": per_request[-1]["tokens"] if per_request else 0,
        "memory_tokens_total": sum(r["memory_tokens"] for r in per_request),
        "uncached_tokens_total": total - cached,
        "cache_hit_rate": round(cached / total, 4) if total else 0.0,
    }


def _patches(stack, store, legacy: bool):
    from app.core.provider import harness_adapter as ha

    stack.enter_context(patch("app.core.fluid_memory.session_memory",
                              return_value=(store, None)))
    stack.enter_context(patch.object(ha.HarnessAdapter, "_resolve_app_session_id",
                                     lambda self: "measure-session"))
    stack.enter_context(patch.object(ha.HarnessAdapter, "_away_note", lambda self: None))
    stack.enter_context(patch.object(ha.HarnessAdapter, "_scratch_note", lambda self: ""))
    if legacy:
        # The behavior before this change: the full snapshot on every message.
        stack.enter_context(patch.object(ha.HarnessAdapter, "_memory_turn_note",
                                         lambda self: self._memory_snapshot()))


def _user_text(turn: int) -> str:
    head = f"Turn {turn}: please continue with the next step of the task. "
    return head + "x" * (USER_CHARS - len(head))


def _remember(store) -> None:
    store.save_memory("Saved mid-session with Remember: the staging database listens on 5433.",
                      provenance="user")


async def _native(store, legacy: bool, prefix_chars: int) -> tuple[list[dict], int]:
    from contextlib import ExitStack

    from fluid.context.compaction import CompactionConfig
    from fluid.events import ContextCompaction
    from fluid.store.base import iter_events

    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions
    from fluid.engine.native import NativeLoop
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.store.base import MemoryStore

    reply = "r" * REPLY_CHARS
    chunks = [
        StreamChunk("message_start", {"usage": {"input_tokens": 5}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": reply}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"}),
    ]

    class Recording:
        name = "fake"
        caps = ProviderCaps()

        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            for chunk in chunks:
                yield chunk

    provider = Recording()
    with ExitStack() as stack:
        _patches(stack, store, legacy)
        loop = NativeLoop(
            provider=provider, store=MemoryStore(), model="fake", system="s" * prefix_chars,
            compaction=CompactionConfig(
                trigger_tokens=prefix_chars // 4 + AUTO_COMPACT_HISTORY_TOKENS),
        )
        adapter = HarnessAdapter()
        adapter._engine = loop
        adapter._options = SessionOptions(model="fake")
        adapter._connected = True

        async def drain():
            async for _ in adapter.receive_events():
                pass

        for turn in range(1, TURNS + 1):
            if turn == REMEMBER_AT:
                _remember(store)
            await adapter.send_query(_user_text(turn))
            await drain()
        folds = len([
            e async for e in iter_events(loop.store, loop.session_id, 0)
            if isinstance(e, ContextCompaction) and e.tier >= 2
        ])
    requests, texts = [], []
    for request in provider.requests:
        elements = [request.system or ""]
        elements += [json.dumps(t, sort_keys=True) for t in request.tools]
        elements += [json.dumps(m, sort_keys=True, default=lambda o: getattr(o, "__dict__", str(o)))
                     for m in request.messages]
        requests.append(elements)
        texts.append([_message_text(m) for m in request.messages])
    stats = _cache_stats(requests, texts)
    for row, plain in zip(stats, texts):
        # The snapshot reached this request through a fold (a digest, epoch or
        # summary carrying it), not through the user message that sent it.
        row["memory_via_fold"] = any(
            "<system-reminder>\n# Memory\n" in t and not t.startswith("Turn ") for t in plain
        )
    return stats, folds


def _message_text(message) -> str:
    """Plain text of a provider request message (dict or fluid Message)."""
    if hasattr(message, "text") and callable(message.text):
        return message.text() or ""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "") for block in content or ()
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


async def _delegated(store, legacy: bool, cwd: str, prefix_chars: int) -> list[dict]:
    from contextlib import ExitStack

    from app.core.provider import harness_adapter as ha
    from app.core.provider.events import ResultMessage
    from app.core.provider.types import SessionOptions
    from fluid.tools.registry import ToolRegistry

    fake = ROOT / "fluid" / "tests" / "fixtures" / "fake_claude.py"
    hook_notes: list[str] = []
    with ExitStack() as stack:
        _patches(stack, store, legacy)
        stack.enter_context(patch.object(ha, "_resolve_claude_cmd",
                                         return_value=[sys.executable, str(fake)]))
        stack.enter_context(patch.dict(os.environ, {"FAKE_CLAUDE_SCENARIO": "x"}))
        # Offline: an in-memory session store and no app database.
        from app.core.config import settings
        from fluid.store.base import MemoryStore

        stack.enter_context(patch.object(type(settings), "managed_database_available",
                                         return_value=False))
        stack.enter_context(patch.object(ha.HarnessAdapter, "_test_memory_store_factory",
                                         MemoryStore))
        adapter = ha.HarnessAdapter()
        await adapter.connect(SessionOptions(
            model="fable", cwd=cwd,
            system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
            extra={"fluid_registry": ToolRegistry()},
        ))
        try:
            loop = adapter._engine
            original = loop._cli_session_start

            async def session_start(data):
                # Record the put-back context the CLI receives after compacting.
                answer = await original(data)
                context = (answer.get("hookSpecificOutput") or {}).get("additionalContext")
                if context:
                    hook_notes.append(context)
                return answer

            loop._cli_session_start = session_start
            sent: list[str] = []
            compacted_at = None
            for turn in range(1, TURNS + 1):
                if turn == REMEMBER_AT:
                    _remember(store)
                text = _user_text(turn) + (" [compact-now]" if turn == COMPACT_BEFORE else "")
                await adapter.send_query(text)
                async for message in adapter.receive_events():
                    if isinstance(message, ResultMessage):
                        break
                nodes = await loop.store.load_path(loop.session_id)
                users = [n for n in nodes if n.message.role == "user"]
                sent.append(users[-1].message.text())
                if turn == COMPACT_BEFORE:
                    compacted_at = turn
                    # The fake CLI does not call the hook itself; the real one
                    # does on every main-conversation compaction.
                    loop._track_main_compaction({"type": "system", "subtype": "status",
                                                 "status": "compacting"})
                    await session_start({"source": "compact", "transcript_path": ""})
        finally:
            await adapter.disconnect()
    # Model the CLI's request for each turn's first model call.
    requests = []
    prefix = "p" * prefix_chars
    history: list[str] = []
    for turn, user in enumerate(sent, start=1):
        requests.append([prefix, *history, user])
        history += [user, "r" * REPLY_CHARS]
        if turn == compacted_at:
            history = ["s" * SUMMARY_CHARS, *hook_notes]
    return _cache_stats(requests)


def measure(legacy: bool, prefixes: tuple[int, int]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        native_store = _memory_store(Path(tmp) / "native")
        native, folds = asyncio.run(_native(native_store, legacy, prefixes[0]))
        delegated_store = _memory_store(Path(tmp) / "delegated")
        delegated = asyncio.run(_delegated(delegated_store, legacy, tmp, prefixes[1]))
    return {
        "native": {"summary": {**_summary(native), "compactions_tier2plus": folds,
                               "requests_with_memory_via_fold": sum(
                                   r["memory_via_fold"] for r in native)},
                   "per_request": native},
        "delegated": {"summary": _summary(delegated), "per_request": delegated},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    prefixes = _prefix_chars()
    result = {"prefix_chars": {"native": prefixes[0], "delegated": prefixes[1]},
              "legacy": measure(True, prefixes), "current": measure(False, prefixes)}
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Scripted session: {TURNS} turns, Remember at turn {REMEMBER_AT}, "
          f"native compacts automatically, delegated CLI compacts in turn {COMPACT_BEFORE}; "
          f"tokens are chars/4; fixed prefix "
          f"{prefixes[0]:,} chars native, {prefixes[1]:,} delegated")
    keys = ("tokens_total", "tokens_per_request_mean", "tokens_last_request",
            "memory_tokens_total", "uncached_tokens_total", "cache_hit_rate")
    for leg in ("native", "delegated"):
        print(f"\n{leg}")
        print(f"{'':28}{'legacy':>12}{'current':>12}")
        for key in keys:
            before = result["legacy"][leg]["summary"][key]
            after = result["current"][leg]["summary"][key]
            print(f"{key:28}{before:>12,}{after:>12,}")
        if leg == "native":
            for key in ("compactions_tier2plus", "requests_with_memory_via_fold"):
                print(f"{key:28}{result['legacy'][leg]['summary'][key]:>12,}"
                      f"{result['current'][leg]['summary'][key]:>12,}")
        series = [r["memory_tokens"] for r in result["current"][leg]["per_request"]]
        print("memory tokens per request (current):", series)


if __name__ == "__main__":
    main()
