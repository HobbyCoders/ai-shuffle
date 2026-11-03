"""Which request may run a chat in this process right now.

Every route that runs a turn takes the chat's lease before it does anything
else, keyed on the app session id, which exists before any engine does. So one
lease covers settings, connect, pool entry and the turn, whichever engine or
fluid id they end up with. A second turn is refused with the same busy error a
pooled client gives. ``acquire`` has no await on its uncontended path, so two
requests cannot both pass it.

A lease has one lifetime rule: the route's outermost scope closes it,
unconditionally. Nothing below that scope can leak it (a connect that fails or is
cancelled, a cancelled teardown) because nothing below it owns it. The one early
step is :func:`turn_ended`: when a turn's end is announced, the client may send
its next message while the route is still saving, so the chat is free from then
on. Closing afterwards is harmless, it only frees a lease that is still current.

A rewind on a running reply stops the reply, so :func:`take_for_rewind` takes the
chat from the current holder rather than being refused. The holder's late
release no longer frees anything, and turns wait for the rewind instead of being
refused: a message sent right after it must land on the rewound chat.

Another install on the same database never passes through here, so a turn can
still be lost that way. ``HarnessAdapter.receive_events`` raises
:class:`SessionReplacedError` for that, so the turn cannot end as a success.

Every function runs on the event loop.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, Optional


class SessionBusyError(RuntimeError):
    """Another turn in this process is running the session."""

    MESSAGE = (
        "Session is busy with another response. "
        "Wait for it to finish, then send your message again."
    )

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class SessionReplacedError(RuntimeError):
    """Another writer took the session over while this engine's turn was running."""

    ANOTHER_COPY = (
        "This chat was continued from another running copy of AI Shuffle, so "
        "this reply stopped and the rest of it was not saved. Reload the chat "
        "to see its latest state."
    )
    REWOUND = "This chat was rewound, so this reply stopped and the rest of it was not saved."

    def __init__(self, message: str = ANOTHER_COPY) -> None:
        super().__init__(message)


def is_conflict_message(message: object) -> bool:
    """A turn's recorded failure that is the caller's next step, not a server fault."""
    return message in (
        SessionBusyError.MESSAGE, SessionReplacedError.ANOTHER_COPY, SessionReplacedError.REWOUND,
    )


@dataclass(eq=False)
class Lease:
    session_id: str
    is_rewind: bool = False
    # A rewind took the chat: a turn still connecting must not start.
    stopped: bool = False
    # Set when the route that held this lease has finished, saving included.
    exited: asyncio.Event = field(default_factory=asyncio.Event)


_holders: Dict[str, Lease] = {}


async def acquire(session_id: str) -> Lease:
    """Hold the chat for one turn, or raise :class:`SessionBusyError`."""
    while (holder := _holders.get(session_id)) is not None:
        if not holder.is_rewind:
            raise SessionBusyError()
        await holder.exited.wait()
    lease = Lease(session_id)
    _holders[session_id] = lease
    return lease


def take_for_rewind(lease: Lease) -> Optional[Lease]:
    """Take the chat from whoever holds it; returns that holder, told to stop."""
    previous = _holders.get(lease.session_id)
    if previous is not None:
        previous.stopped = True
    _holders[lease.session_id] = lease
    return previous


def turn_ended(lease: Lease) -> None:
    """The turn's end is announced, so the client's next message may start."""
    if _holders.get(lease.session_id) is lease:
        del _holders[lease.session_id]


def close(lease: Lease) -> None:
    """The route is done. Safe to repeat, and for a lease that was taken over."""
    turn_ended(lease)
    lease.exited.set()


async def stream_turn(
    session_id: str, turn: Callable[[Lease], AsyncGenerator[Dict[str, Any], None]],
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run a streaming route under the chat's lease; a refusal is its error frame."""
    try:
        lease = await acquire(session_id)
    except SessionBusyError as busy:
        yield {"type": "error", "message": str(busy)}
        return
    try:
        async with aclosing(turn(lease)) as events:
            async for event in events:
                yield event
    finally:
        close(lease)


def clear() -> None:
    """Forget every holder. Tests only: each test starts with an empty process."""
    _holders.clear()
