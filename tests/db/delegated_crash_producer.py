"""Child process for the delegated crash matrix (not collected by pytest).

Runs one real ``DelegatedLoop`` turn against the fake stream-json CLI, with
``PgFluidStore`` on the disposable PostgreSQL named by ``TEST_POSTGRES_DSN``,
and calls ``os._exit`` at the requested boundary: no ``finally``, no flush, no
pool shutdown — the same as the worker process being killed there.

    python delegated_crash_producer.py <session_id> buffered|batch|terminal <deltas>

``buffered`` and ``batch`` exit right after the store accepts delta number
``<deltas>``; ``terminal`` exits right after the TurnComplete write returns.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

CRASH_EXIT = 17


def _connect() -> None:
    from app.core.config import settings
    from app.db import base

    connection = base.parse_database_url(os.environ["TEST_POSTGRES_DSN"])
    if connection["host"] not in {"127.0.0.1", "localhost"}:
        raise SystemExit("TEST_POSTGRES_DSN must identify a disposable loopback database")
    connection.update(hostaddr="127.0.0.1", options="", connect_timeout=5, sslmode="disable")
    object.__setattr__(settings, "get_database_connection", lambda: dict(connection))
    base.init_pool(min_conn=1, max_conn=4)


async def _main(session_id: str, boundary: str, exit_after: int) -> None:
    from app.core.provider.fluid_pg_store import PgFluidStore
    from fluid.engine.delegated import DelegatedLoop
    from fluid.events import UserTurn
    from fluid.events.model import TurnComplete

    store = PgFluidStore()
    store.STREAM_BATCH_SECONDS = 60  # Count boundaries, independent of host speed.
    accept_delta, accept_event = store.append_stream_event, store.append_event
    accepted = 0

    async def append_stream_event(sid, event):
        nonlocal accepted
        await accept_delta(sid, event)
        accepted += 1
        if boundary in ("buffered", "batch") and accepted == exit_after:
            os._exit(CRASH_EXIT)

    async def append_event(sid, event):
        await accept_event(sid, event)
        if boundary == "terminal" and isinstance(event, TurnComplete):
            os._exit(CRASH_EXIT)

    store.append_stream_event = append_stream_event
    store.append_event = append_event
    fake = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures" / "fake_claude.py"
    loop = DelegatedLoop(
        store=store, model="claude-fake", claude_cmd=[sys.executable, str(fake)],
        session_id=session_id, partial=True,
        env={"FAKE_CLAUDE_SCENARIO": "s", "FAKE_CLAUDE_DELTAS": "40,8"},
    )
    await loop.submit(UserTurn(content="stream then crash"))
    await asyncio.wait_for(loop.wait_turn(), 60)
    raise SystemExit("the producer reached the end of its turn without crashing")


if __name__ == "__main__":
    _connect()
    asyncio.run(_main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
