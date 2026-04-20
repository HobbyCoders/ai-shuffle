"""
Claude Agent SDK one-shot latency benchmark.

Purpose
-------
Measure every stage of a Claude Agent SDK one-shot `query()` call so we have
a clean-environment baseline to compare against AI Shuffle's generation path
(commit messages, PR descriptions, branch names).

What it measures
----------------
Per-call timing (ms), for each iteration:

    import_sdk           module import cost (first iter only)
    options_build        ClaudeAgentOptions construction
    query_enter          time until first `async for` await suspends
    find_cli             SDK resolves bundled claude.exe path
    version_check        `claude.exe -v` subprocess (cold)
    subprocess_spawn     anyio.open_process for main claude.exe
    initialize_rpc       time from process spawn until SystemMessage arrives
    first_system         first SystemMessage (CLI init done)
    first_assistant      first AssistantMessage
    first_text           first TextBlock inside AssistantMessage
    last_event           final ResultMessage
    total                import → response complete

Usage
-----
From repo root:

    # 1x baseline
    python tools/benchmark_sdk_latency.py

    # matrix
    python tools/benchmark_sdk_latency.py --runs 5 --model claude-haiku-4-5
    python tools/benchmark_sdk_latency.py --runs 5 --skip-version-check
    python tools/benchmark_sdk_latency.py --runs 5 --model claude-sonnet-4-5

The benchmark monkey-patches SDK internals (`_find_cli`, `_check_claude_version`,
`anyio.open_process`) to capture subprocess stage boundaries. Patches are
idempotent and scoped to the process — safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Stage timestamps captured via monkey-patches. Reset per iteration.
# ---------------------------------------------------------------------------


@dataclass
class StageTimings:
    t_start: float = 0.0
    t_options_built: float = 0.0
    t_query_enter: float = 0.0
    t_find_cli: float = 0.0
    t_version_check_start: float = 0.0
    t_version_check_end: float = 0.0
    t_subprocess_spawn_start: float = 0.0
    t_subprocess_spawn_end: float = 0.0
    t_first_system: float = 0.0
    t_first_assistant: float = 0.0
    t_first_text: float = 0.0
    t_last_event: float = 0.0

    # Metadata
    msg_types: List[str] = field(default_factory=list)
    output_chars: int = 0
    cli_argv_len: int = 0
    cli_argv_preview: List[str] = field(default_factory=list)

    def durations(self) -> Dict[str, float]:
        """Return stage durations in ms."""
        def ms(end: float, start: float) -> float:
            return max(0.0, (end - start) * 1000) if (end and start) else 0.0

        return {
            "options_build": ms(self.t_options_built, self.t_start),
            "query_enter": ms(self.t_query_enter, self.t_options_built),
            "find_cli": ms(self.t_find_cli, self.t_query_enter),
            "version_check": ms(self.t_version_check_end, self.t_version_check_start),
            "subprocess_spawn": ms(self.t_subprocess_spawn_end, self.t_subprocess_spawn_start),
            "initialize_rpc": ms(self.t_first_system, self.t_subprocess_spawn_end),
            "first_system_abs": ms(self.t_first_system, self.t_start),
            "first_assistant_abs": ms(self.t_first_assistant, self.t_start),
            "first_text_abs": ms(self.t_first_text, self.t_start),
            "inference": ms(self.t_first_text, self.t_first_system),
            "streaming_tail": ms(self.t_last_event, self.t_first_text),
            "total": ms(self.t_last_event, self.t_start),
        }


# Shared state used by monkey-patches. `current` is reset per iter.
_TIMINGS_HOLDER: Dict[str, Optional[StageTimings]] = {"current": None}


# ---------------------------------------------------------------------------
# Monkey-patch installation — idempotent.
# ---------------------------------------------------------------------------


def _install_sdk_patches() -> None:
    """Wrap SDK internals to stamp stage boundaries into _TIMINGS_HOLDER."""
    from claude_agent_sdk._internal.transport import subprocess_cli as sc

    if getattr(sc, "_benchmark_patched", False):
        return

    # _find_cli (the sync method SDK runs in a thread). Wrap by patching
    # both _find_cli and _find_bundled_cli — whichever SDK resolves first.
    orig_find_cli = sc.SubprocessCLITransport._find_cli

    def patched_find_cli(self):
        t = _TIMINGS_HOLDER["current"]
        result = orig_find_cli(self)
        if t is not None:
            t.t_find_cli = time.perf_counter()
        return result

    sc.SubprocessCLITransport._find_cli = patched_find_cli

    # _check_claude_version (async method)
    orig_version = sc.SubprocessCLITransport._check_claude_version

    async def patched_version(self):
        t = _TIMINGS_HOLDER["current"]
        if t is not None:
            t.t_version_check_start = time.perf_counter()
        try:
            return await orig_version(self)
        finally:
            if t is not None:
                t.t_version_check_end = time.perf_counter()

    sc.SubprocessCLITransport._check_claude_version = patched_version

    # _build_command — capture argv length/preview
    orig_build = sc.SubprocessCLITransport._build_command

    def patched_build(self):
        cmd = orig_build(self)
        t = _TIMINGS_HOLDER["current"]
        if t is not None:
            t.cli_argv_len = len(cmd)
            # Keep flag names + short values only
            preview: List[str] = []
            skip_next = False
            for tok in cmd:
                if skip_next:
                    if isinstance(tok, str) and len(tok) > 80:
                        preview.append(f"<{len(tok)}c>")
                    else:
                        preview.append(str(tok))
                    skip_next = False
                    continue
                preview.append(str(tok))
                if tok in (
                    "--system-prompt",
                    "--append-system-prompt",
                    "--settings",
                    "--mcp-config",
                    "--json-schema",
                ):
                    skip_next = True
            t.cli_argv_preview = preview
        return cmd

    sc.SubprocessCLITransport._build_command = patched_build

    # anyio.open_process — stamp spawn boundaries
    import anyio

    orig_open_process = anyio.open_process

    async def patched_open_process(*args, **kwargs):
        t = _TIMINGS_HOLDER["current"]
        # Only stamp the MAIN spawn (the one with many argv tokens).
        # Version check spawn has short argv (claude.exe -v).
        is_main = False
        try:
            first_arg = args[0] if args else kwargs.get("command")
            if isinstance(first_arg, (list, tuple)) and len(first_arg) > 5:
                is_main = True
        except Exception:
            pass

        if t is not None and is_main:
            t.t_subprocess_spawn_start = time.perf_counter()
        try:
            result = await orig_open_process(*args, **kwargs)
        finally:
            if t is not None and is_main:
                t.t_subprocess_spawn_end = time.perf_counter()
        return result

    anyio.open_process = patched_open_process

    sc._benchmark_patched = True


# ---------------------------------------------------------------------------
# One benchmark iteration.
# ---------------------------------------------------------------------------


async def run_once(model: str, prompt: str, cwd: Optional[str]) -> StageTimings:
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        AssistantMessage,
        SystemMessage,
        TextBlock,
    )

    timings = StageTimings()
    _TIMINGS_HOLDER["current"] = timings
    timings.t_start = time.perf_counter()

    options_kwargs: Dict[str, Any] = {
        "model": model,
        "permission_mode": "bypassPermissions",
        "tools": [],
        "max_turns": 1,
        "include_partial_messages": False,
        "continue_conversation": False,
        "fork_session": False,
        "setting_sources": ["local"],
        # Same override used in AI Shuffle generation path
        "settings": json.dumps({"enabledPlugins": {}}),
        "env": {"CLAUDECODE": ""},
    }
    if cwd:
        options_kwargs["cwd"] = cwd

    options = ClaudeAgentOptions(**options_kwargs)
    timings.t_options_built = time.perf_counter()

    gen = query(prompt=prompt, options=options)
    timings.t_query_enter = time.perf_counter()

    last_event_time = timings.t_start

    async for message in gen:
        now = time.perf_counter()
        last_event_time = now
        tname = type(message).__name__
        timings.msg_types.append(tname)

        if isinstance(message, SystemMessage) and not timings.t_first_system:
            timings.t_first_system = now
        if isinstance(message, AssistantMessage):
            if not timings.t_first_assistant:
                timings.t_first_assistant = now
            for block in message.content:
                if isinstance(block, TextBlock):
                    if not timings.t_first_text:
                        timings.t_first_text = now
                    timings.output_chars += len(block.text)

    timings.t_last_event = last_event_time
    _TIMINGS_HOLDER["current"] = None
    return timings


# ---------------------------------------------------------------------------
# Pretty printing.
# ---------------------------------------------------------------------------


STAGE_ORDER = [
    "options_build",
    "query_enter",
    "find_cli",
    "version_check",
    "subprocess_spawn",
    "initialize_rpc",
    "first_system_abs",
    "first_assistant_abs",
    "first_text_abs",
    "inference",
    "streaming_tail",
    "total",
]


def print_single_run(i: int, t: StageTimings) -> None:
    d = t.durations()
    print(f"\n--- run {i} ---")
    print(f"  msg_types: {t.msg_types}")
    print(f"  output_chars: {t.output_chars}")
    print(f"  cli_argv_len: {t.cli_argv_len}")
    if t.cli_argv_preview:
        print(f"  cli_argv: {t.cli_argv_preview}")
    for stage in STAGE_ORDER:
        print(f"  {stage:<22} {d[stage]:>8.0f} ms")


def print_summary(results: List[StageTimings]) -> None:
    if not results:
        return
    print("\n=== summary (median / p95 / mean over {} runs) ===".format(len(results)))
    stats: Dict[str, Dict[str, float]] = {}
    for stage in STAGE_ORDER:
        vals = [r.durations()[stage] for r in results]
        stats[stage] = {
            "min": min(vals),
            "median": statistics.median(vals),
            "p95": sorted(vals)[max(0, int(len(vals) * 0.95) - 1)],
            "mean": statistics.mean(vals),
            "max": max(vals),
        }
    header = f"{'stage':<22} {'min':>8} {'median':>8} {'p95':>8} {'mean':>8} {'max':>8}"
    print(header)
    print("-" * len(header))
    for stage in STAGE_ORDER:
        s = stats[stage]
        print(
            f"{stage:<22} "
            f"{s['min']:>8.0f} {s['median']:>8.0f} {s['p95']:>8.0f} "
            f"{s['mean']:>8.0f} {s['max']:>8.0f}"
        )


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    t_import_start = time.perf_counter()
    _install_sdk_patches()
    t_import_end = time.perf_counter()
    print(f"SDK import + patch install: {(t_import_end - t_import_start) * 1000:.0f} ms")

    if args.skip_version_check:
        os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] = "1"
        print("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK=1 (version subprocess skipped)")

    results: List[StageTimings] = []
    for i in range(1, args.runs + 1):
        print(f"\n>>> run {i}/{args.runs} — model={args.model}")
        try:
            t = await run_once(args.model, args.prompt, args.cwd)
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            continue
        print_single_run(i, t)
        results.append(t)

    if len(results) > 1:
        print_summary(results)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "iter": i + 1,
                        "msg_types": r.msg_types,
                        "output_chars": r.output_chars,
                        "cli_argv_len": r.cli_argv_len,
                        "cli_argv": r.cli_argv_preview,
                        "durations_ms": r.durations(),
                    }
                    for i, r in enumerate(results)
                ],
                f,
                indent=2,
            )
        print(f"\nwrote JSON: {args.json_out}")

    return 0 if results else 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--model", default="claude-haiku-4-5")
    p.add_argument(
        "--prompt",
        default='Reply with exactly the word "ok" and nothing else.',
    )
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--cwd", default=None, help="Working directory for subprocess")
    p.add_argument(
        "--skip-version-check",
        action="store_true",
        help="Set CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK=1 for the run",
    )
    p.add_argument("--json-out", default=None, help="Write raw results as JSON")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
