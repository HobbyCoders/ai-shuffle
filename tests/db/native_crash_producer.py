"""Child process for the native crash matrix (not collected by pytest).

Runs one real ``NativeLoop`` turn against a scripted streaming provider, with
``PgFluidStore`` on the disposable PostgreSQL named by ``TEST_POSTGRES_DSN``,
and calls ``os._exit`` at the requested boundary: no ``finally``, no flush, no
pool shutdown — the same as the worker process being killed there.

    python native_crash_producer.py <session_id> buffered|batch|terminal <deltas>

``buffered`` and ``batch`` exit right after the store accepts delta number
``<deltas>``; ``terminal`` exits right after the TurnComplete write returns.
"""

from __future__ import annotations

import asyncio
import os
import sys

CRASH_EXIT = 17
STREAMED_DELTAS = 48


def scripted_provider(deltas: int):
    """A deterministic native provider: one text block of ``deltas`` chunks."""
    from fluid.providers.base import ProviderCaps, StreamChunk

    class Provider:
        name = "anthropic"
        caps = ProviderCaps()

        async def stream(self, request):
            yield StreamChunk("message_start", {"usage": {"input_tokens": 1}})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            for i in range(deltas):
                yield StreamChunk("text_delta", {"index": 0, "text": f"w{i} "})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk(
                "message_stop",
                {"stop_reason": "end_turn", "usage": {"output_tokens": deltas}},
            )

    return Provider()


def _connect() -> None:
    from app.core.config import settings
    from app.db import base

    connection = base.parse_database_url(os.environ["TEST_POSTGRES_DSN"])
    if connection["host"] not in {"127.0.0.1", "localhost"}:
        raise SystemExit(
            "TEST_POSTGRES_DSN must identify a disposable loopback database"
        )
    connection.update(
        hostaddr="127.0.0.1", options="", connect_timeout=5, sslmode="disable"
    )
    object.__setattr__(settings, "get_database_connection", lambda: dict(connection))
    base.init_pool(min_conn=1, max_conn=4)


async def _main(session_id: str, boundary: str, exit_after: int) -> None:
    from fluid.engine.native import NativeLoop
    from fluid.events import UserTurn
    from fluid.events.model import TurnComplete

    from app.core.provider.fluid_pg_store import PgFluidStore

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
    loop = NativeLoop(
        provider=scripted_provider(STREAMED_DELTAS),
        store=store,
        model="scripted-native",
        session_id=session_id,
    )
    await loop.submit(UserTurn(content="stream then crash"))
    await asyncio.wait_for(loop.wait_turn(), 60)
    raise SystemExit("the producer reached the end of its turn without crashing")


if __name__ == "__main__":
    _connect()
    asyncio.run(_main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
