"""Boundary patches for driving whole query-engine turns in unit tests.

Built on ``engine_harness`` (StubPool, stream_response, collect). Everything
outside ``app.core.query_engine`` that would reach a subprocess, the network or
another module's persistence is replaced; the engine itself runs for real, so a
test asserts the frames it yields, the rows it would write (``run_sync`` calls,
``_persist_turn_results`` kwargs) and the calls it makes on the adapter.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import app.core.query_engine as qe
from tests.core.engine_harness import StubPool, stream_response


class RecordingPool(StubPool):
    """StubPool that accepts the engine's keyword arguments and records calls."""

    def __init__(self, state=None, refusal=None):
        super().__init__(state)
        self.refusal = refusal
        self.removed: List[tuple] = []
        self.evict_lanes: List[str] = []

    def admission_check(self, lane="delegated"):
        return self.refusal

    async def evict_if_needed(self, incoming_lane="delegated"):
        self.evict_lanes.append(incoming_lane)

    async def remove(self, session_id, preserve_session=False):
        self.removed.append(("remove", session_id, preserve_session))
        self.state = None

    async def remove_dead(self, session_id):
        self.removed.append(("remove_dead", session_id))
        self.state = None


def fresh_adapter(messages=(), **attrs):
    """The adapter ``make_adapter()`` hands a turn that builds a new client."""
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=True)
    adapter.connect_with_sdk_options = AsyncMock()
    adapter.send_query = AsyncMock()
    adapter.send_resume = AsyncMock()
    adapter.disconnect = AsyncMock()
    adapter.set_model = AsyncMock()
    adapter.set_permission_mode = AsyncMock()
    adapter.receive_events = stream_response(list(messages))
    for name, value in attrs.items():
        setattr(adapter, name, value)
    return adapter


def scripted_events(steps):
    """A receive_events factory that yields messages and runs async callables.

    Each step is either an SDK message (yielded) or a zero-argument coroutine
    function (awaited in place, e.g. to invoke the permission callback the way
    the adapter does mid-stream, or to raise).
    """

    async def _gen():
        for step in steps:
            if callable(step):
                await step()
            else:
                yield step

    return MagicMock(side_effect=lambda: _gen())


@dataclass
class Boundaries:
    options: MagicMock
    pool: RecordingPool
    adapter: MagicMock
    persist: AsyncMock
    sync_engine: MagicMock
    make_adapter: MagicMock
    run_sync_calls: List[tuple] = field(default_factory=list)
    hooks: Dict[str, Any] = field(default_factory=dict)

    def run_sync_named(self, name):
        return [c for c in self.run_sync_calls if c[0] == name]


def fake_sync_engine():
    sync = MagicMock()
    for name in (
        "broadcast_message_added",
        "broadcast_session_attention",
        "broadcast_user_question",
        "broadcast_user_session_created",
        "broadcast_session_updated",
    ):
        setattr(sync, name, AsyncMock())
    return sync


def engine_patches(
    stack: ExitStack,
    *,
    pool: Optional[RecordingPool] = None,
    cwd: Optional[str] = None,
    session_row: Optional[Dict[str, Any]] = None,
    project_row: Optional[Dict[str, Any]] = None,
    profile: Optional[Dict[str, Any]] = ...,
    adapter=None,
    reconnect: bool = False,
    alive: bool = True,
    leg: str = "native",
) -> Boundaries:
    """Patch every boundary of stream_query / _stream_to_websocket_impl."""
    pool = pool if pool is not None else RecordingPool()
    adapter = adapter if adapter is not None else fresh_adapter()
    if profile is ...:
        profile = {"id": "claude-code", "config": {}}
    stack.enter_context(patch.object(qe, "get_profile", return_value=profile))

    calls: List[tuple] = []

    async def _run_sync(fn, *args, **kwargs):
        name = getattr(fn, "__name__", repr(fn))
        calls.append((name, args, kwargs))
        if name == "get_session":
            return session_row
        if name == "get_project":
            return project_row
        if name == "create_session":
            return {"id": kwargs.get("session_id")}
        if name == "add_session_message":
            return {"id": 7, "role": kwargs.get("role"), "content": kwargs.get("content")}
        return {"id": "row-1"}

    stack.enter_context(patch.object(qe, "run_sync", new=_run_sync))

    options = MagicMock()
    options.include_partial_messages = False
    options.cwd = cwd
    options.model = None
    options.aih_provider_id = None
    options.permission_mode = "default"
    options.can_use_tool = None
    stack.enter_context(patch(
        "app.core.sdk_options.build_options_from_profile", return_value=(options, {}),
    ))
    stack.enter_context(patch(
        "app.core.query_options.ensure_pending_checkout", new=AsyncMock(return_value=False),
    ))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=alive))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=reconnect))
    stack.enter_context(patch.object(
        qe, "reconnect_option_names", side_effect=lambda *a: ["model"] if reconnect else [],
    ))
    stack.enter_context(patch.object(qe, "mark_subprocess_oom_killable", new=MagicMock()))
    stack.enter_context(patch.object(qe, "resolve_note_references", side_effect=lambda p: p))
    persist = AsyncMock()
    stack.enter_context(patch.object(qe, "_persist_turn_results", new=persist))
    stack.enter_context(patch.object(qe, "push_memory", new=AsyncMock()))
    stack.enter_context(patch.object(qe, "reconcile_memory", new=AsyncMock()))
    stack.enter_context(patch.object(qe, "schedule_context_usage_capture", new=MagicMock()))
    stack.enter_context(patch.object(
        qe, "capture_usage_identity", new=AsyncMock(return_value="settle-key"),
    ))
    stack.enter_context(patch.object(qe, "memory_leg_for_options", return_value=leg))
    sync = fake_sync_engine()
    stack.enter_context(patch.object(qe, "sync_engine", new=sync))

    hooks: Dict[str, Any] = {}

    def _attach(opts, project_id=None, stderr_callback=None):
        hooks["project_id"] = project_id
        hooks["stderr_callback"] = stderr_callback

    stack.enter_context(patch("app.core.hook_service.attach_hooks_to_options", side_effect=_attach))
    make = MagicMock(return_value=adapter)
    stack.enter_context(patch("app.core.provider.factory.make_adapter", new=make))
    return Boundaries(
        options=options, pool=pool, adapter=adapter, persist=persist,
        sync_engine=sync, make_adapter=make, run_sync_calls=calls, hooks=hooks,
    )


def frames_of(events, kind):
    return [e for e in events if e.get("type") == kind]
