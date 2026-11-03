"""Which request may run a chat in this process right now.

Every route that runs a turn takes the chat's lease before it does anything
else, keyed on the app session id, which exists before any engine does. So one
lease covers settings, connect, pool entry and the turn, whichever engine or
fluid id they end up with. A second turn is refused with the same busy error a
pooled client gives. ``acquire`` has no await on its uncontended path, so two
requests cannot both pass it.

A lease has one lifetime rule: the route's outermost scope closes it,
unconditionally, and nothing else frees the chat. Nothing below that scope can leak
it (a connect that fails or is cancelled, a cancelled teardown) because nothing
below it owns it, and nothing the route does can reach a successor, which does not
exist yet. The client may send its next message as soon as it hears the turn's end,
a moment before that scope closes, so :func:`turn_ended` makes that message wait for
the close rather than be refused.

A turn outlives whoever is reading it. The engine runs on without a reader, so a
chat must stay held until the turn has really finished, and what it produced must
still be saved. :func:`outlive_reader` gives an HTTP stream its own task, because a
client leaving cancels whatever the response is waiting on. A pump is an owned task
already, so if it leaves early :func:`stream_turn` hands the turn to one.

A rewind on a running reply stops the reply, so :func:`take_for_rewind` takes the
chat from the current holder rather than being refused. The holder's late
release no longer frees anything. From :func:`begin_rewind` on, turns wait for the
rewind instead of being refused: a message sent right after it must land on the
rewound chat. Rewinds run one at a time and always finish
(:func:`finish_unattended`), so none can act on a chat that a later rewind or turn
has moved on from.

Another install on the same database never passes through here, so a turn can
still be lost that way. ``HarnessAdapter.receive_events`` raises
:class:`SessionReplacedError` for that, so the turn cannot end as a success.

Every function runs on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Optional, Set, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How long a next message waits for a turn that has announced its end to close.
_ENDING_WAIT_SECONDS = 15.0
# How long a stopping app gives unfinished turns to save what they have.
_SHUTDOWN_WAIT_SECONDS = 5.0


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
    # A rewind took the chat: a turn still connecting must not start.
    stopped: bool = False
    # The turn has announced its end: a next message waits rather than being refused.
    ending: bool = False
    # Set when the route that held this lease has finished, saving included.
    exited: asyncio.Event = field(default_factory=asyncio.Event)


_holders: Dict[str, Lease] = {}
_rewinds: Dict[str, Lease] = {}
# Turns and rewinds nobody is waiting on any more. Tasks need a strong reference.
_unattended: Set[asyncio.Task] = set()


async def acquire(session_id: str) -> Lease:
    """Hold the chat for one turn, or raise :class:`SessionBusyError`."""
    while True:
        if (rewind := _rewinds.get(session_id)) is not None:
            await rewind.exited.wait()
        elif (holder := _holders.get(session_id)) is None:
            break
        elif not holder.ending:
            raise SessionBusyError()
        else:
            try:
                await asyncio.wait_for(holder.exited.wait(), _ENDING_WAIT_SECONDS)
            except asyncio.TimeoutError:
                raise SessionBusyError() from None
    lease = Lease(session_id)
    _holders[session_id] = lease
    return lease


async def begin_rewind(session_id: str) -> Lease:
    """Wait for any other rewind of this chat, then hold new turns back from it."""
    while (other := _rewinds.get(session_id)) is not None:
        await other.exited.wait()
    lease = Lease(session_id)
    _rewinds[session_id] = lease
    return lease


def take_for_rewind(lease: Lease) -> Optional[Lease]:
    """Take the chat from whoever holds it; returns that holder, told to stop."""
    previous = _holders.get(lease.session_id)
    if previous is not None:
        previous.stopped = True
    _holders[lease.session_id] = lease
    return previous


def turn_ended(lease: Lease) -> None:
    """The turn is about to announce its end, so a next message may come at once."""
    lease.ending = True


def holds_chat(lease: Lease) -> bool:
    """Whether session-wide cleanup is still this turn's to do.

    Only a rewind takes a chat from its turn. It allows the cleanup only while it
    is running: after that, a new turn may own what the cleanup would remove.
    """
    return not lease.stopped or lease.session_id in _rewinds


def close(lease: Lease) -> None:
    """The route is done. Safe to repeat, and for a lease that was taken over."""
    if _holders.get(lease.session_id) is lease:
        del _holders[lease.session_id]
    if _rewinds.get(lease.session_id) is lease:
        del _rewinds[lease.session_id]
    lease.exited.set()


def _own(work: Awaitable[T]) -> "asyncio.Task[T]":
    task = asyncio.ensure_future(work)
    _unattended.add(task)
    task.add_done_callback(_disown)
    return task


def _disown(task: asyncio.Task) -> None:
    _unattended.discard(task)
    if not task.cancelled():
        task.exception()  # whoever is still waiting has it; nobody else needs it


async def stream_turn(
    session_id: str, turn: Callable[[Lease], AsyncGenerator[Dict[str, Any], None]],
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run a streaming route under the chat's lease; a refusal is its error frame."""
    try:
        lease = await acquire(session_id)
    except SessionBusyError as busy:
        yield {"type": "error", "message": str(busy)}
        return
    events = turn(lease)
    handed_over = False
    try:
        async for event in events:
            yield event
    except GeneratorExit:
        # The reader left with the turn parked at a frame, so it is still alive.
        handed_over = True
        _own(_finish_turn(events, lease))
        raise
    finally:
        if not handed_over:
            close(lease)


async def _finish_turn(events: AsyncGenerator[Dict[str, Any], None], lease: Lease) -> None:
    try:
        async for _ in events:
            pass
    except Exception:
        logger.exception("A turn nobody is reading failed for %s", lease.session_id)
    finally:
        close(lease)


async def outlive_reader(
    frames: AsyncGenerator[Dict[str, Any], None],
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run ``frames`` in its own task, so the reader leaving does not stop it.

    A client leaving cancels whatever the response is waiting on. Waiting here means
    that is this queue, and the turn runs on to its end and saves, as it would have
    with the client still there.
    """
    relayed: asyncio.Queue = asyncio.Queue()
    finished = object()

    async def run() -> None:
        try:
            async for frame in frames:
                relayed.put_nowait(frame)
        except Exception:
            logger.exception("A turn failed with its reader's stream open or gone")
            raise
        finally:
            relayed.put_nowait(finished)

    running = _own(run())
    while (frame := await relayed.get()) is not finished:
        yield frame
    await running


def finish_unattended(work: Awaitable[T]) -> Awaitable[T]:
    """Wait for ``work``; if the caller is cancelled, ``work`` finishes anyway."""
    return asyncio.shield(_own(work))


async def stop_unattended() -> None:
    """The app is stopping: end what is left while it can still save."""
    tasks = list(_unattended)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=_SHUTDOWN_WAIT_SECONDS)


def clear() -> None:
    """Forget every holder. Tests only: each test starts with an empty process."""
    _holders.clear()
    _rewinds.clear()
