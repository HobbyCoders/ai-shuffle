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

Only the turn saves, so a reader can lose frames and never saved data. One that
leaves is forgotten at once. One that stays connected but falls behind is cut off
past a limit, with a last frame saying the reply goes on: dropping frames from the
middle would look like a whole reply, and waiting for it would let a stuck client
stall the turn.

A Stop for the turn holding the chat, even one still setting up, stays on its
lease as well as the pooled engine. The adapter reapplies it after submit resets
an idle Interrupt, before the newly scheduled engine task can start model work.
If the holder is out of a Stop's reach (a rewind, a turn that has ended, a
turn off the pool), :func:`stop_waiting_turn` marks the lease of each turn waiting then.
So the Stop leaves with those turns, however their wait ends, and a later arrival has a
lease of its own. The turn that gets the chat puts it on the engine it runs on
(:func:`apply_stop`), since a rewind can replace the engine or there may be none yet.
Anything else on the flag was for a turn that has left, and :func:`acquire` drops it.

A rewind on a running reply stops the reply, so :func:`take_for_rewind` takes the
chat from the current holder rather than being refused. The holder's late
release no longer frees anything. From :func:`begin_rewind` on, turns wait for the
rewind instead of being refused: a message sent right after it must land on the
rewound chat. Rewinds run one at a time and always finish
(:func:`finish_unattended`), so none can act on a chat that a later rewind or turn
has moved on from. A rewind whose database write hangs therefore holds its chat, so
those waits have a limit and end in the busy error. The chat is not freed at that
limit, because that rewind can still act once the write returns. A waiter looks again
at what is in its way whenever a holder leaves or a rewind begins, and has one limit,
counted from when it arrived.

Another install on the same database never passes through here, so a turn can
still be lost that way. ``HarnessAdapter.receive_events`` raises
:class:`SessionReplacedError` for that, so the turn cannot end as a success.

Every function runs on the event loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional, Set, TypeVar

from app.core.session_pool import get_session_pool
from app.db.async_database import TurnWork, bind_turn_work, unbind_turn_work

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How long a next message waits, from arriving, when only turns that have announced
# their end are in its way.
_ENDING_WAIT_SECONDS = 15.0
# How long a turn or a later rewind waits, from arriving, once a rewind is in its way.
# It covers a healthy rewind's worst case: its wait for the stopped turn and its claim's
# lock budget, 15 s each, plus closing the engine and re-seeding.
_REWIND_WAIT_SECONDS = 60.0
# How long a stopping app gives unfinished turns to save what they have.
_SHUTDOWN_WAIT_SECONDS = 5.0
# A reader exit must not turn a wedged engine into an immortal chat holder.
_ABANDONED_DRAIN_SECONDS = 15.0
_ABANDONED_CANCEL_SECONDS = 1.0
# How far behind a stream's reader may fall. Far past anything a live connection
# gets to, as a turn writes kilobytes a second apart from the odd large tool result.
_READER_BUFFER_BYTES = 8 * 1024 * 1024
# What holding one frame costs beyond its text, so tiny frames count too.
_FRAME_OVERHEAD_BYTES = 512
_FELL_BEHIND_MESSAGE = (
    "This connection fell too far behind to keep streaming. The reply is still "
    "running and will be saved. Reload the chat to see it."
)


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
    # A Stop can reach this turn. False for a rewind and for a turn off the pool.
    stoppable: bool = True
    # The rewind that took the chat from this turn.
    taken_by: Optional["Lease"] = None
    # The turn has announced its end: a next message waits rather than being refused.
    ending: bool = False
    # A Stop pressed while this turn waited for the chat or was setting up/running.
    stop_requested: bool = False
    # Set when the route that held this lease has finished, saving included.
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    # While it waits for the chat: what is in its way may have changed.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    # Captures the originating adapter/context, never a fresh session-id lookup.
    abandon: Optional[Callable[[], Awaitable[None]]] = None
    abandoned: bool = False
    # The deadline applies only to engine reads, never app-session writes.
    engine_waiting: asyncio.Event = field(default_factory=asyncio.Event)
    engine_finished: asyncio.Event = field(default_factory=asyncio.Event)
    work: TurnWork = field(default_factory=TurnWork)
    closing: bool = False

    @property
    def stopped(self) -> bool:
        """A rewind took the chat: a turn still connecting must not start."""
        return self.taken_by is not None


_current_lease: contextvars.ContextVar[Optional[Lease]] = contextvars.ContextVar("admission_lease", default=None)


def _bind(lease: Lease) -> None:
    _current_lease.set(lease)
    bind_turn_work(lease.work)


async def raw_engine_events(events: Any, *, on_shutdown=None) -> Any:
    """Mark only the raw engine advance, never adapter-side app work."""
    lease = _current_lease.get()
    while lease is None or not lease.abandoned:
        if lease is not None:
            lease.engine_waiting.set()
        try:
            from app.core.query_shutdown import engine_event
            event = await engine_event(events, on_shutdown)
        except StopAsyncIteration:
            return
        finally:
            if lease is not None:
                lease.engine_waiting.clear()
        if lease is not None and lease.abandoned:
            return
        yield event


_holders: Dict[str, Lease] = {}
_rewinds: Dict[str, Lease] = {}
# Requests waiting for the chat, oldest first.
_waiting: Dict[str, List[Lease]] = {}
# Turns and rewinds nobody is waiting on any more. Tasks need a strong reference.
_unattended: Set[asyncio.Task] = set()


def _stop_is_for_holder(session_id: str) -> bool:
    holder = _holders.get(session_id)
    return holder is not None and holder.stoppable and not holder.ending


def stop_waiting_turn(session_id: str) -> bool:
    """Give this Stop to the turns waiting for the chat; False if it is not theirs.

    The engine is left alone, because nothing using it now is what this Stop is for.
    """
    if _stop_is_for_holder(session_id):
        # The engine may not exist yet, or may clear its idle Interrupt at
        # submit. Keep the Stop on the turn through every setup boundary.
        _holders[session_id].stop_requested = True
        return False
    waiters = [lease for lease in _waiting.get(session_id, ()) if lease.stoppable]
    for lease in waiters:
        lease.stop_requested = True
    return bool(waiters)


def stop_requested(session_id: Optional[str]) -> bool:
    """Whether the turn holding this chat received a Stop, including during setup."""
    holder = _holders.get(session_id) if session_id else None
    return bool(holder is not None and holder.stoppable and holder.stop_requested)


def apply_stop(lease: Lease, state: Any) -> None:
    """Put a Stop this turn was given while waiting on the engine it runs on."""
    if lease.stop_requested:
        state.interrupt_requested = True


def _wake_waiters(session_id: str) -> None:
    for lease in _waiting.get(session_id, ()):
        lease.wake.set()


async def _wait_for_chat(lease: Lease, in_the_way: Callable[[], bool]) -> None:
    """Return when nothing is in the way, or raise :class:`SessionBusyError`.

    The limit is counted from here and does not restart when the holder changes. It
    is the short one until a rewind gets in the way, then the long one.
    """
    session_id = lease.session_id
    loop = asyncio.get_running_loop()
    arrived = loop.time()
    limit = _ENDING_WAIT_SECONDS
    waiting = _waiting.setdefault(session_id, [])
    waiting.append(lease)
    try:
        while in_the_way():
            if session_id in _rewinds:
                limit = _REWIND_WAIT_SECONDS
            lease.wake.clear()
            try:
                await asyncio.wait_for(lease.wake.wait(), arrived + limit - loop.time())
            except asyncio.TimeoutError:
                raise SessionBusyError() from None
    finally:
        waiting.remove(lease)
        if not waiting:
            _waiting.pop(session_id, None)


def _turn_must_wait(session_id: str) -> bool:
    """Whether a turn waits for the chat. A running turn refuses it instead."""
    if session_id in _rewinds:
        return True
    holder = _holders.get(session_id)
    if holder is None:
        return False
    if holder.ending:
        return True
    raise SessionBusyError()


async def acquire(session_id: str, *, stoppable: bool = True) -> Lease:
    """Hold the chat for one turn, or raise :class:`SessionBusyError`.

    ``stoppable=False`` is for a turn that runs off the pool, so no Stop can reach it:
    a Stop must not report that it will, and goes on to whatever it can reach.
    """
    lease = Lease(session_id, stoppable=stoppable)
    await _wait_for_chat(lease, lambda: _turn_must_wait(session_id))
    # A Stop already on the engine was for a turn that has left: this one's is on its lease.
    state = get_session_pool().peek(session_id)
    if state is not None and not state.is_streaming:
        state.interrupt_requested = False
    _holders[session_id] = lease
    _bind(lease)
    return lease


async def begin_rewind(session_id: str) -> Lease:
    """Wait for any other rewind of this chat, then hold new turns back from it.

    Raises :class:`SessionBusyError` if the other rewinds do not finish in time.
    """
    lease = Lease(session_id, stoppable=False)
    await _wait_for_chat(lease, lambda: session_id in _rewinds)
    _rewinds[session_id] = lease
    _bind(lease)
    _wake_waiters(session_id)
    return lease


def take_for_rewind(lease: Lease) -> Optional[Lease]:
    """Take the chat from whoever holds it; returns that holder, told to stop."""
    previous = _holders.get(lease.session_id)
    if previous is not None:
        previous.taken_by = lease
    _holders[lease.session_id] = lease
    return previous


def turn_ended(lease: Lease) -> None:
    """The turn is about to announce its end, so a next message may come at once."""
    lease.ending = True


def is_current(lease: Lease) -> bool:
    """Only this admission generation may publish a terminal or change Stop state."""
    return _holders.get(lease.session_id) is lease


def holds_chat(lease: Lease) -> bool:
    """Whether session-wide cleanup is still this turn's to do.

    Only a rewind takes a chat from its turn. That rewind allows the cleanup only
    while it is running: after that, a new turn may own what the cleanup would
    remove, whatever a later rewind is doing.
    """
    return lease.taken_by is None or not lease.taken_by.exited.is_set()


def close(lease: Lease) -> None:
    """Release only after every issued app thread has actually returned.

    The API stays synchronous for callers, but a cancelled caller's outstanding
    work retains its exact admission generation until the owned drain finishes.
    """
    if _current_lease.get() is lease:
        _current_lease.set(None)
    unbind_turn_work(lease.work)
    if lease.exited.is_set() or lease.closing:
        return
    # Preserve synchronous release when only scheduled discard callbacks remain.
    lease.work.reap_done()
    if lease.work.pending:
        lease.closing = True
        closing = _own(_close_after_writes(lease))

        def completed(task: asyncio.Task) -> None:
            # Cancellation before the coroutine's first instruction cannot run
            # cleanup. Retry cancellation, never a failed drain: its proof is
            # broken, so admission stays held until explicit recovery/restart.
            if not lease.exited.is_set() and task.cancelled():
                lease.closing = False
                close(lease)

        closing.add_done_callback(completed)
        return
    _close_now(lease)


async def close_and_wait(lease: Lease) -> None:
    """An owning route is not complete until its issued threads have settled."""
    close(lease)
    await finish_saving(lease.exited.wait())


async def _close_after_writes(lease: Lease) -> None:
    try:
        await finish_saving(lease.work.drain())
    except asyncio.CancelledError:
        # finish_saving propagates cancellation only after its drain settles.
        if not lease.work.pending:
            _close_now(lease)
        raise
    except Exception:
        logger.exception("Failed to drain turn writes for %s; keeping admission held", lease.session_id)
        raise
    else:
        _close_now(lease)


def _close_now(lease: Lease) -> None:
    if _holders.get(lease.session_id) is lease:
        del _holders[lease.session_id]
    if _rewinds.get(lease.session_id) is lease:
        del _rewinds[lease.session_id]
    lease.exited.set()
    _wake_waiters(lease.session_id)


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
    *, lease: Optional[Lease] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Drain abandoned readers, retaining a runtime's lease through persistence."""
    owns_lease = lease is None
    if owns_lease:
        try:
            lease = await acquire(session_id)
        except SessionBusyError as busy:
            yield {"type": "error", "message": str(busy)}
            return
    _bind(lease)
    events = turn(lease)
    handed_over = False
    try:
        while True:
            # Async generators may be advanced by a fresh consumer task for
            # every frame (e.g. wait_for(anext(...))); bind the exact generation
            # at each advance, not only when the generator was first entered.
            _bind(lease)
            try:
                event = await anext(events)
            except StopAsyncIteration:
                break
            if isinstance(event, dict) and event.get("type") in {"done", "interrupted", "error"}:
                # The reader may send its next message before release finishes.
                # Queue it behind saving rather than refusing an idle-looking chat.
                turn_ended(lease)
            yield event
    except GeneratorExit:
        # The reader left with the turn parked at a frame, so it is still alive.
        _bind(lease)
        handed_over = True
        finishing = _own(_finish_turn(events, lease, release=owns_lease))
        if not owns_lease:
            # The runtime cannot finalize before the engine and persistence
            # tail finish. Repeated cancellation must not detach that work.
            cancelled = False
            while not finishing.done():
                try:
                    await asyncio.shield(finishing)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
        raise
    finally:
        if not handed_over and owns_lease:
            await close_and_wait(lease)


async def finish_saving(work: Awaitable[T]) -> T:
    """Cancellation cannot detach an issued mutation's off-loop thread.

    Keep a strong reference and wait through cancellation, then propagate it.
    DB/pool timeouts, not an engine deadline, bound these writes.
    """
    saving = asyncio.ensure_future(work)
    cancelled = False
    while not saving.done():
        try:
            await asyncio.shield(saving)
        except asyncio.CancelledError:
            cancelled = True
    result = saving.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _finish_turn(
    events: AsyncGenerator[Dict[str, Any], None], lease: Lease, *, release: bool = True,
) -> None:
    async def finish() -> None:
        async def drain() -> None:
            async for _ in events:
                pass

        # Only this owner cancels its reader, and only while it awaits engine
        # events. App-session writes between frames and the saving tail settle
        # before admission can be released, even on shutdown cancellation.
        draining = asyncio.create_task(drain())
        engine_finished = asyncio.create_task(lease.engine_finished.wait())
        try:
            await asyncio.wait(
                {draining, engine_finished}, timeout=_ABANDONED_DRAIN_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            while not draining.done() and not lease.engine_finished.is_set():
                if lease.engine_waiting.is_set() or lease.abandon is None:
                    logger.warning(
                        "Abandoned engine drain timed out for %s; retiring its engine", lease.session_id,
                    )
                    lease.abandoned = True
                    if lease.abandon is not None:
                        await lease.abandon()
                    draining.cancel()
                    await asyncio.wait({draining}, timeout=_ABANDONED_CANCEL_SECONDS)
                    if not draining.done():
                        _own(draining)  # fenced engine-only reader; never an app write
                    return
                # A frame's app-session mutation is still in flight. Do not
                # cancel it; retire only when the next engine read is reached.
                waiting = asyncio.create_task(lease.engine_waiting.wait())
                try:
                    await asyncio.wait(
                        {draining, engine_finished, waiting}, return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    waiting.cancel()
            await draining
        finally:
            engine_finished.cancel()

    try:
        await finish_saving(finish())
    except Exception:
        logger.exception("A turn nobody is reading failed for %s", lease.session_id)
    finally:
        if release:
            await close_and_wait(lease)


def _frame_size(frame: Dict[str, Any]) -> int:
    try:
        return len(json.dumps(frame, default=str)) + _FRAME_OVERHEAD_BYTES
    except (TypeError, ValueError, RecursionError):
        return _FRAME_OVERHEAD_BYTES  # measuring must never be what stops a turn


class _Relay:
    """Frames on their way to one reader. Putting one never waits."""

    FINISHED = object()
    FELL_BEHIND = object()

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._size = 0
        self._detached = False

    def put(self, frame: Dict[str, Any]) -> None:
        if self._detached:
            return
        size = _frame_size(frame)
        # A reader with nothing to catch up on gets a frame of any size.
        if self._size and self._size + size > _READER_BUFFER_BYTES:
            logger.warning("A stream's reader fell behind and was cut off; its turn runs on")
            self.detach()
            self._queue.put_nowait((self.FELL_BEHIND, 0))
            return
        self._size += size
        self._queue.put_nowait((frame, size))

    def finish(self) -> None:
        if not self._detached:
            self._queue.put_nowait((self.FINISHED, 0))

    def detach(self) -> None:
        """Forget the reader: drop what it has not read, and everything to come."""
        self._detached = True
        self._size = 0
        while not self._queue.empty():
            self._queue.get_nowait()

    async def get(self) -> Any:
        item, size = await self._queue.get()
        self._size -= size
        return item


async def outlive_reader(
    frames: AsyncGenerator[Dict[str, Any], None],
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run ``frames`` in its own task, so the reader leaving does not stop it.

    A client leaving cancels whatever the response is waiting on. Waiting here means
    that is this relay, and the turn runs on to its end and saves, as it would have
    with the client still there.
    """
    relay = _Relay()

    async def run() -> None:
        try:
            async for frame in frames:
                relay.put(frame)
        except Exception:
            logger.exception("A turn failed with its reader's stream open or gone")
            raise
        finally:
            relay.finish()

    running = _own(run())
    try:
        while (frame := await relay.get()) is not relay.FINISHED:
            if frame is relay.FELL_BEHIND:
                yield {"type": "error", "message": _FELL_BEHIND_MESSAGE}
                return
            yield frame
    finally:
        relay.detach()
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
    _waiting.clear()
    _current_lease.set(None)
    bind_turn_work(None)
