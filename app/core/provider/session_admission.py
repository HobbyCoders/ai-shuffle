"""Which engine in this process may run a session right now.

The database fence (``fluid_sessions.writer_generation``) keeps a replaced
engine's late writes off the session, but it cannot save that engine's turn: the
newest claim always wins, so a turn that is mid-flight when another connect
claims the session goes on running with nothing saved. Inside one process that
is avoidable, so a connect is admitted here first, before its claim. While
another engine is connecting or mid-turn the newcomer is refused with the same
busy error a pooled client gives. Admission is keyed on the fluid session id,
the one the claim uses, and is synchronous: two requests cannot both pass.

An idle engine does not hold a session back. It is displaced by the claim and
retired the next time a reuse gate looks at it (``retire_if_replaced``).

Another install on the same database never passes through here, so a turn can
still be lost that way. ``HarnessAdapter.receive_events`` raises
:class:`SessionReplacedError` for that, so the turn cannot end as a success.

Every function runs on the event loop, and any object that is not a holder is
ignored, so test doubles need no special handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


class SessionBusyError(RuntimeError):
    """Another engine in this process is connecting to or running the session."""

    def __init__(self) -> None:
        super().__init__(
            "Session is busy with another response. "
            "Wait for it to finish, then send your message again."
        )


class SessionReplacedError(RuntimeError):
    """Another install took the session over while this engine's turn was running."""

    def __init__(self) -> None:
        super().__init__(
            "This chat was continued from another running copy of AI Shuffle, so "
            "this reply stopped and the rest of it was not saved. Reload the chat "
            "to see its latest state."
        )


@dataclass(eq=False)
class _Lease:
    session_id: str
    owner: Any
    connecting: bool = True
    # What says whether the holder is mid-turn: the pool's SessionState once the
    # engine is pooled, which happens with no await after its connect returns.
    state: Any = None

    @property
    def busy(self) -> bool:
        return self.connecting or getattr(self.state, "is_streaming", False) is True


class _UnpooledTurn:
    """The whole life of an engine that never joins the pool is one turn."""

    is_streaming = True


# By ``id(owner)``: a holder need not be hashable, and its lease keeps it alive.
_by_owner: Dict[int, _Lease] = {}
_by_session: Dict[str, List[_Lease]] = {}


def admit(session_id: str, owner: Any) -> None:
    """Hold ``session_id`` for ``owner``, or raise :class:`SessionBusyError`."""
    current = _by_owner.get(id(owner))
    if any(lease.busy for lease in _by_session.get(session_id, ()) if lease.owner is not owner):
        raise SessionBusyError()
    if current is not None:
        if current.session_id == session_id:
            current.connecting = True
            return
        release(owner)
    lease = _Lease(session_id, owner)
    _by_owner[id(owner)] = lease
    _by_session.setdefault(session_id, []).append(lease)


def refuse_if_busy(session_id: str) -> None:
    """Raise :class:`SessionBusyError` if a holder is connecting or mid-turn."""
    if any(lease.busy for lease in _by_session.get(session_id, ())):
        raise SessionBusyError()


def connected(owner: Any) -> None:
    """The holder's connect finished, so only a turn makes it busy from here on."""
    lease = _by_owner.get(id(owner))
    if lease is not None:
        lease.connecting = False


def bind_state(owner: Any, state: Any) -> None:
    """The holder joined the pool: from now on ``state`` says whether it is busy."""
    lease = _by_owner.get(id(owner))
    if lease is not None:
        lease.state = state


def hold_for_unpooled_turn(owner: Any) -> None:
    """A headless turn: busy until it releases the session, at its disconnect."""
    bind_state(owner, _UnpooledTurn)


def release(owner: Any) -> None:
    """Give the session up. Safe to call repeatedly, or for a non-holder."""
    lease = _by_owner.pop(id(owner), None)
    if lease is None:
        return
    leases = _by_session[lease.session_id]
    leases.remove(lease)
    if not leases:
        del _by_session[lease.session_id]


def holds(owner: Any) -> bool:
    return id(owner) in _by_owner


def held_by_another(owner: Any) -> bool:
    """Whether another holder is busy on the session ``owner`` holds."""
    lease = _by_owner.get(id(owner))
    if lease is None:
        return False
    return any(
        other.busy for other in _by_session[lease.session_id] if other is not lease
    )


def clear() -> None:
    """Forget every holder. Tests only: each test starts with an empty process."""
    _by_owner.clear()
    _by_session.clear()
