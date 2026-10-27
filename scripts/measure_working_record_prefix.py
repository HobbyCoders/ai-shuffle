"""Measure what a working-record change does to an owner session's cached prefix.

Run: PYTHONPATH=fluid python scripts/measure_working_record_prefix.py [--json]

Issue #680: a background report landing between two turns of an owner chat
changed the working-record snapshot. While that snapshot lived in the system
prompt, the resumed turn's request differed in its first segment (fluid's cache
probe: ``prefix_changed``/``changed=system`` -> ``harness_mutation``) and the
pool replaced the client (``reconnect_option_names`` -> ``system_prompt``).

The scenario uses the production producers on a disposable SQLite database
(tests/pg_shim.py): ``build_options_from_profile`` for an owner chat, the real
``WorkingRecord`` store and, when present, ``working_record.turn_note``. Turn 1
is built, a pending item is added and a child report is recorded (the ordinary
"background task finished" path), then turn 2 is built. The two requests are
fingerprinted and diagnosed with fluid's own probe (``cache_probe`` and
``cache_miss_classes``) as if the provider reported zero cache reads.

The prior conversation is SYNTHETIC: ``--history-messages`` messages of
``--message-chars`` characters, sized to resemble the incident (~94k fresh
tokens). Token figures are the probe's chars/4 estimates, not billed tokens.
The script also runs unchanged on the pre-fix revision (it detects the
missing ``turn_note``), which is how the "before" column is produced.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]


def _system_text(system) -> str:
    if isinstance(system, dict):
        return json.dumps(system, sort_keys=True)
    return str(system or "")


def measure(history_messages: int = 30, message_chars: int = 12_000) -> dict:
    import tempfile

    from fluid.context.cache_miss_classes import classify_cache_miss
    from fluid.context.cache_probe import diagnose_cache_miss, fingerprint_prefix
    from fluid.messages import Message, Text
    from tests.pg_shim import new_sqlite_connection, route_db_to

    from app.core import config, query_options, working_record
    from app.core import session_orchestration as orch
    from app.core.session_pool import reconnect_option_names
    from app.db import database, sessions, turns
    from app.db import working_record as store

    conn = new_sqlite_connection()
    with route_db_to(conn), tempfile.TemporaryDirectory() as workspace, \
            patch.object(orch, "_principal", lambda sid: None), \
            patch.dict(config._runtime_overrides, {"workspace_dir": Path(workspace)}), \
            patch.object(query_options, "get_security_instructions", lambda *a, **k: "SECURITY"):
        database.create_profile("agent", "Agent", None, {"model": "sonnet"})
        sessions.create_session("owner", "agent", title="owner", is_active=True, model="sonnet",
                                permission_mode="default", mode="chat")
        turns.enqueue_turn("owner", "user", prompt="work", profile_id="agent")
        store.add("decision", "Ship only after review.", "owner")

        turn_note = getattr(working_record, "turn_note", None)
        history = [Message(role="user" if i % 2 == 0 else "assistant",
                           content=[Text(f"{i}:" + "x" * message_chars)])
                   for i in range(history_messages)]

        def request(prompt: str):
            options, _ = query_options.build_options_from_profile(
                database.get_profile("agent"), session_id="owner")
            note = turn_note("owner") if turn_note else None
            text = f"{prompt}\n\n{note}" if note else prompt
            return options, Message(role="user", content=[Text(text)]), note

        first, first_msg, first_note = request("Start the review.")
        # Background work finishes while the chat is parked on a Monitor.
        store.add("pending", "Collect performance numbers.", "owner")
        working_record.record_report("owner", {
            "task_id": "child", "agent_message_turn_id": "t1", "status": "completed",
            "summary": "Found the cache invalidation bug; patch ready.", "title": "Investigate"})
        second, second_msg, second_note = request("<task-notification>child finished</task-notification>")

        msgs1 = [*history, first_msg]
        msgs2 = [*msgs1, Message(role="assistant", content=[Text("Monitoring.")]), second_msg]
        fp1 = fingerprint_prefix(system=_system_text(first.system_prompt), tools=[], messages=msgs1)
        fp2 = fingerprint_prefix(system=_system_text(second.system_prompt), tools=[], messages=msgs2,
                                 extra_checkpoints=[len(msgs1)])
        fresh = int(fp2[f"_tokens_m{len(msgs2)}"])
        diagnosis = diagnose_cache_miss(previous=fp1, current=fp2, input_tokens=fresh,
                                        cache_read_tokens=0) or {}
        reusable = int(diagnosis.get("shared_prefix_tokens_estimated", 0))
        with_reads = diagnose_cache_miss(previous=fp1, current=fp2, input_tokens=fresh,
                                         cache_read_tokens=reusable)
        state = SimpleNamespace(connect_options=first)
        return {
            "record_in_system_prompt": "Owner working record" in _system_text(second.system_prompt),
            "system_prompt_changed": _system_text(first.system_prompt) != _system_text(second.system_prompt),
            "system_prompt_chars": [len(_system_text(first.system_prompt)), len(_system_text(second.system_prompt))],
            "reconnect_option_names": reconnect_option_names(state, second),
            "turn_note_chars": [len(first_note or ""), len(second_note or "")],
            "request_tokens_estimated": fresh,
            "probe_reason": diagnosis.get("reason"),
            "probe_changed": diagnosis.get("changed"),
            "miss_class_if_no_cache_read": classify_cache_miss(diagnosis) if diagnosis else None,
            "reusable_prefix_tokens_estimated": reusable,
            "fresh_tokens_if_prefix_reused": fresh - reusable,
            "miss_reported_when_prefix_reused": with_reads is not None,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history-messages", type=int, default=30)
    parser.add_argument("--message-chars", type=int, default=12_000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    import logging

    # The shim lacks a few optional tables; their best-effort readers log and continue.
    logging.disable(logging.CRITICAL)
    result = measure(args.history_messages, args.message_chars)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    for key, value in result.items():
        print(f"{key:36} {value}")


if __name__ == "__main__":
    main()
