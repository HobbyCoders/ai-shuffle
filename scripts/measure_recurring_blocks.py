"""Measure what harness-injected reminder blocks add to chats over their life.

Run on a worker, with the app's own Python (it needs psycopg2 and the app's
settings to find the managed database the way the app does):

    PYTHONPATH=.:fluid python -B scripts/measure_recurring_blocks.py [--days 14] [--json]
    PYTHONPATH=.:fluid python -B scripts/measure_recurring_blocks.py --cells --cutoff 2026-11-14T14:45Z

Read-only (server-enforced ``default_transaction_read_only``). It prints only
sizes and counts: never transcript text, titles, paths or memory contents.

Default mode groups every ``<system-reminder>`` block found in user turns and
tool results by its first line (``# Memory``, ``Current environment (`` ...)
and reports, per chat, how many times each block was injected and how many
characters it added. Tokens are chars/4 estimates, not billed tokens; a block
also rides every later call until compaction, which this does not multiply in.

``--cells`` reports the run_python result size distribution for native
code-mode chats before and after ``--cutoff`` (median, p90, share truncated).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import re
import statistics

REMINDER = re.compile(r"<system-reminder>\n(.*?)\n</system-reminder>", re.S)
TRUNCATED = "[stdout truncated:"


def connect():
    """Open the worker's managed database read-only, as the app resolves it."""
    import psycopg2

    from app.core.config import settings

    params = dict(settings.get_database_connection())
    params["options"] = "-c default_transaction_read_only=on -c statement_timeout=300000"
    return psycopg2.connect(**params)


def opener(body: str) -> str:
    """A content-free label for a reminder: its first line up to any '('."""
    head = body.split("\n", 1)[0]
    return re.sub(r"\s*\(.*", " (", head)[:48]


def pct(values, q):
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def chats(cur, days):
    """Recent chats with a Fluid transcript: (fluid id, leg, role)."""
    cur.execute(
        """select s.sdk_session_id, s.provider_id is null, coalesce(s.is_worker, false)
             from sessions s
            where s.created_at > now() - make_interval(days => %s)
              and s.sdk_session_id is not null
              and exists (select 1 from fluid_session_nodes n where n.session_id = s.sdk_session_id)""",
        (days,),
    )
    out = {}
    for sid, claude_cli, worker in cur.fetchall():
        # provider_id NULL is the Claude Code CLI (delegated leg); every
        # configured API provider runs on the native code-mode leg.
        leg = "delegated" if claude_cli else "native"
        out[sid] = f"{leg}/{'worker' if worker else 'owner'}"
    return out


def reminder_stats(cur, sessions):
    per_chat = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    turns = Counter()
    cur.execute(
        """select n.session_id,
                  b->>'kind',
                  coalesce(b->>'text', (select string_agg(c->>'text', '') from jsonb_array_elements(b->'content') c))
             from fluid_session_nodes n, jsonb_array_elements(n.message->'content') b
            where n.session_id = any(%s) and n.message->>'role' = 'user'""",
        (list(sessions),),
    )
    for sid, kind, text in cur:
        if kind == "text":
            turns[sid] += 1
        if not text or "<system-reminder>" not in text:
            continue
        for match in REMINDER.finditer(text):
            slot = per_chat[sid][opener(match.group(1))]
            slot[0] += 1
            slot[1] += len(match.group(0))
    return per_chat, turns


def summarize(sessions, per_chat, turns):
    groups = defaultdict(list)
    for sid, group in sessions.items():
        groups[group].append(sid)
    report = {}
    for group, sids in sorted(groups.items()):
        long_cut = pct([turns[s] for s in sids], 0.75)
        labels = sorted({label for s in sids for label in per_chat[s]})
        rows = {}
        for label in labels:
            counts = [per_chat[s][label][0] for s in sids if label in per_chat[s]]
            chars = [per_chat[s][label][1] for s in sids if label in per_chat[s]]
            long_chars = [per_chat[s][label][1] for s in sids
                          if label in per_chat[s] and turns[s] >= long_cut]
            rows[label] = {
                "chats": len(counts),
                "injections_median": statistics.median(counts),
                "injections_p90": pct(counts, 0.9),
                "chars_per_injection": sum(chars) // max(1, sum(counts)),
                "tokens_per_chat_median": statistics.median(chars) // 4,
                "tokens_per_chat_p90": pct(chars, 0.9) // 4,
                "tokens_per_long_chat_median": (statistics.median(long_chars) // 4) if long_chars else 0,
            }
        totals = [sum(v[1] for v in per_chat[s].values()) for s in sids]
        long_totals = [t for s, t in zip(sids, totals) if turns[s] >= long_cut]
        report[group] = {
            "chats": len(sids),
            "user_text_blocks_median": statistics.median([turns[s] for s in sids]),
            "long_chat_cut_user_text_blocks": long_cut,
            "all_blocks_tokens_per_chat_median": statistics.median(totals) // 4,
            "all_blocks_tokens_per_chat_p90": pct(totals, 0.9) // 4,
            "all_blocks_tokens_per_long_chat_median": (statistics.median(long_totals) // 4) if long_totals else 0,
            "blocks": rows,
        }
    return report


def cell_stats(cur, cutoff: datetime, days: int):
    """run_python result sizes on native chats, before and after ``cutoff``."""
    start = cutoff.timestamp() - days * 86400
    cur.execute(
        """with uses as (
               select n.session_id, b->>'id' as id
                 from fluid_session_nodes n, jsonb_array_elements(n.message->'content') b
                where n.ts > %s and n.message->>'role' = 'assistant' and b->>'name' = 'run_python')
           select n.ts,
                  (select coalesce(sum(length(c->>'text')), 0) from jsonb_array_elements(b->'content') c),
                  (select bool_or(position(%s in coalesce(c->>'text', '')) > 0) from jsonb_array_elements(b->'content') c)
             from fluid_session_nodes n
             join lateral jsonb_array_elements(n.message->'content') b on b ? 'tool_use_id'
             join uses u on u.session_id = n.session_id and u.id = b->>'tool_use_id'
            where n.ts > %s and n.message->>'role' = 'user'""",
        (start, TRUNCATED, start),
    )
    windows = {"before": [], "after": []}
    for ts, size, cut in cur.fetchall():
        windows["before" if ts < cutoff.timestamp() else "after"].append((size, bool(cut)))
    out = {}
    for name, rows in windows.items():
        sizes = [s for s, _ in rows]
        out[name] = {
            "cells": len(rows),
            "median_chars": statistics.median(sizes) if sizes else 0,
            "p90_chars": pct(sizes, 0.9),
            "p99_chars": pct(sizes, 0.99),
            "share_truncated": round(sum(c for _, c in rows) / max(1, len(rows)), 4),
            "share_over_8000": round(sum(s > 8000 for s in sizes) / max(1, len(sizes)), 4),
            "share_over_16384": round(sum(s > 16384 for s in sizes) / max(1, len(sizes)), 4),
        }
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--cells", action="store_true", help="run_python result sizes around --cutoff")
    parser.add_argument("--cutoff", default=None, help="ISO time, e.g. 2026-11-14T14:45Z")
    args = parser.parse_args()
    with connect() as conn, conn.cursor() as cur:
        if args.cells:
            if not args.cutoff:
                parser.error("--cells needs --cutoff")
            cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00")).astimezone(timezone.utc)
            result = cell_stats(cur, cutoff, args.days)
        else:
            sessions = chats(cur, args.days)
            per_chat, turns = reminder_stats(cur, sessions)
            result = summarize(sessions, per_chat, turns)
    if args.json or args.cells:
        print(json.dumps(result, indent=2))
        return
    for group, data in result.items():
        print(f"\n{group}: {data['chats']} chats, median {data['user_text_blocks_median']} user text blocks; "
              f"long = top quartile (>= {data['long_chat_cut_user_text_blocks']})")
        print(f"  all blocks, tokens/chat: median {data['all_blocks_tokens_per_chat_median']:,}, "
              f"p90 {data['all_blocks_tokens_per_chat_p90']:,}, long-chat median "
              f"{data['all_blocks_tokens_per_long_chat_median']:,}")
        for label, row in sorted(data["blocks"].items(), key=lambda kv: -kv[1]["tokens_per_chat_p90"]):
            print(f"  {label!r}: {row['chats']} chats, injections median {row['injections_median']} "
                  f"p90 {row['injections_p90']}, {row['chars_per_injection']:,} chars each, tokens/chat "
                  f"median {row['tokens_per_chat_median']:,} p90 {row['tokens_per_chat_p90']:,} "
                  f"long {row['tokens_per_long_chat_median']:,}")


if __name__ == "__main__":
    main()
