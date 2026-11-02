"""One engine at a time in this process, and no silent success after a takeover.

The database fence stops a replaced engine's writes; these cases stop the
replacement from happening to a live turn in the first place, and fail a turn
that another install took over.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.provider import session_admission as admission
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.session_admission import SessionBusyError, SessionReplacedError
from app.core.session_pool import SessionPool, SessionState

SID = "ses_admission"


def _options(**fields):
    return SimpleNamespace(**{"session_id": None, "resume_session_id": SID, **fields})


class _Adapter(HarnessAdapter):
    """Runs the admission around ``connect``; the engine build is stubbed."""

    def __init__(self, connect=None):
        super().__init__()
        self.connect_body = connect or AsyncMock()

    async def _connect_engine(self, options):
        await self.connect_body(options)


async def _start_turn(adapter, options=None) -> SessionState:
    await adapter.connect(options or _options())
    state = SessionState(adapter=adapter, is_connected=True, is_streaming=True)
    admission.bind_state(adapter, state)
    return state


# ─── who may connect ─────────────────────────────────────────────────────────


async def test_second_connect_is_refused_before_its_claim_while_the_first_connects():
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def slow(_options):
        entered.set()
        await proceed.wait()

    first, second = _Adapter(slow), _Adapter()
    connecting = asyncio.ensure_future(first.connect(_options()))
    await entered.wait()

    with pytest.raises(SessionBusyError):
        await second.connect(_options())
    second.connect_body.assert_not_awaited()

    proceed.set()
    await connecting


async def test_admission_uses_either_id_the_claim_would():
    first, second = _Adapter(), _Adapter()
    await _start_turn(first, _options(session_id="ses_new", resume_session_id=None))
    with pytest.raises(SessionBusyError):
        await second.connect(_options(session_id=None, resume_session_id="ses_new"))


async def test_connect_without_a_session_needs_no_admission():
    """One-shot generations and watches share nothing."""
    for adapter in (_Adapter(), _Adapter()):
        await adapter.connect(_options(session_id=None, resume_session_id=None))
    assert admission.holds(adapter) is False


async def test_mid_turn_engine_refuses_a_newcomer_and_an_idle_one_does_not():
    running, idle_after = _Adapter(), _Adapter()
    state = await _start_turn(running)
    with pytest.raises(SessionBusyError):
        await idle_after.connect(_options())

    state.is_streaming = False
    await idle_after.connect(_options())  # the claim displaces an idle engine
    assert admission.holds(running) and admission.holds(idle_after)


@pytest.mark.parametrize("failure", [RuntimeError("no"), asyncio.TimeoutError(), asyncio.CancelledError()])
async def test_a_connect_that_fails_lets_the_retry_in(failure):
    failing = _Adapter(AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await failing.connect(_options())

    retry = _Adapter()
    await retry.connect(_options())
    assert admission.holds(failing) is False


async def test_a_refused_connect_keeps_the_runner_it_lost_to():
    running = _Adapter()
    await _start_turn(running)
    with pytest.raises(SessionBusyError):
        await _Adapter().connect(_options())
    assert admission.holds(running) is True


async def test_a_failed_newcomer_leaves_the_idle_engine_it_displaced_in_place():
    idle = _Adapter()
    await idle.connect(_options())
    admission.bind_state(idle, SessionState(adapter=idle, is_connected=True))

    with pytest.raises(RuntimeError):
        await _Adapter(AsyncMock(side_effect=RuntimeError("no"))).connect(_options())

    assert admission.holds(idle) is True
    assert admission.held_by_another(idle) is False


async def test_reconnecting_the_same_adapter_is_not_a_competitor():
    adapter = _Adapter()
    await adapter.connect(_options())
    await adapter.connect(_options())
    await adapter.connect(_options(resume_session_id="ses_other"))
    assert admission.held_by_another(adapter) is False
    await _Adapter().connect(_options())  # it gave the first session up


# ─── when the session is free again ──────────────────────────────────────────


async def test_disconnect_frees_the_session():
    adapter = _Adapter()
    await _start_turn(adapter)
    await adapter.disconnect_transport()
    await _Adapter().connect(_options())


async def test_a_turn_outside_the_pool_holds_the_session_until_its_disconnect():
    """A headless turn: nothing in the pool can say it is running."""
    adapter = _Adapter()
    await adapter.connect(_options())
    admission.hold_for_unpooled_turn(adapter)

    with pytest.raises(SessionBusyError):
        await _Adapter().connect(_options())

    await adapter.disconnect_transport()
    await _Adapter().connect(_options())


@pytest.mark.parametrize("remove", ["remove", "remove_dead", "remove_broken"])
async def test_every_way_out_of_the_pool_frees_the_session_at_once(remove, monkeypatch):
    """Before its teardown, which can take seconds: the fence covers that gap."""
    from app.core import session_pool as module

    monkeypatch.setattr(module, "graceful_disconnect", AsyncMock())
    monkeypatch.setattr(module, "release_dead_adapter", AsyncMock())
    adapter = _Adapter()
    await adapter.connect(_options())
    pool = SessionPool()
    state = SessionState(adapter=adapter, is_connected=True, is_streaming=True)
    pool.put("app-id", state)
    with pytest.raises(SessionBusyError):
        await _Adapter().connect(_options())

    await getattr(pool, remove)("app-id")

    assert admission.holds(adapter) is False
    await _Adapter().connect(_options())


def test_things_that_never_held_a_session_are_ignored():
    double = MagicMock()
    admission.bind_state(double, object())
    admission.release(double)
    assert admission.held_by_another(double) is False
    assert admission.holds(double) is False


# ─── a turn another install takes over ───────────────────────────────────────


def _result_event():
    from fluid.events.model import TurnComplete

    return TurnComplete(session_id=SID, turn_id="t1", seq=3, stop_reason="end_turn", num_model_calls=1)


def _delta_event():
    from fluid.events.model import TextDelta

    return TextDelta(session_id=SID, turn_id="t1", seq=2, index=0, text="partial")


class _Store:
    """The ownership surface of ``PgFluidStore``."""

    def __init__(self, *, lost=False, at_end=False):
        self.lost, self.at_end = lost, at_end

    def lost_ownership(self):
        return self.lost

    async def check_ownership(self):
        return self.lost or self.at_end

    async def release_resume_memory(self):
        return None


async def _adapter_streaming(store, events, *, admitted=True):
    adapter = _Adapter()
    if admitted:
        await adapter.connect(_options())

    async def stream(since_seq=0):
        for event in events:
            yield event

    adapter._engine = SimpleNamespace(events=stream, submit=AsyncMock())
    adapter._store, adapter._connected = store, True
    adapter._sdk_session_id = SID
    adapter._turn_floor = 0
    return adapter


async def _drain(adapter) -> list:
    return [message async for message in adapter.receive_events()]


async def test_write_refused_mid_turn_fails_the_turn_and_stops_it():
    adapter = await _adapter_streaming(_Store(lost=True), [_delta_event(), _result_event()])

    with pytest.raises(SessionReplacedError):
        await _drain(adapter)

    adapter._engine.submit.assert_awaited_once()


async def test_turn_stops_at_the_first_event_after_a_refused_write():
    """It does not run on to its end, spending tools and tokens on unsaved work."""

    def more():
        raise AssertionError("the turn ran on after losing the session")

    class Events(list):
        def __iter__(self):
            yield _delta_event()
            more()

    adapter = await _adapter_streaming(_Store(lost=True), Events())
    with pytest.raises(SessionReplacedError):
        await _drain(adapter)


async def test_takeover_seen_only_at_the_end_fails_the_turn_instead_of_success():
    """The last writes may not have been refused yet: this asks the database."""
    from app.core.provider.events import ResultMessage

    adapter = await _adapter_streaming(_Store(at_end=True), [_result_event()])
    seen: list = []
    with pytest.raises(SessionReplacedError):
        async for message in adapter.receive_events():
            seen.append(message)
    assert not any(isinstance(message, ResultMessage) for message in seen)


async def test_the_failure_tells_the_user_what_happened():
    assert "another running copy" in str(SessionReplacedError())
    assert "not saved" in str(SessionReplacedError())


async def test_a_stop_that_does_not_get_through_still_fails_the_turn():
    adapter = await _adapter_streaming(_Store(lost=True), [_result_event()])
    adapter._engine.submit = AsyncMock(side_effect=RuntimeError("closed"))
    with pytest.raises(SessionReplacedError):
        await _drain(adapter)


async def test_owner_finishes_normally():
    from app.core.provider.events import ResultMessage

    adapter = await _adapter_streaming(_Store(), [_result_event()])
    assert isinstance((await _drain(adapter))[-1], ResultMessage)
    adapter._engine.submit.assert_not_awaited()


async def test_a_client_this_process_evicted_has_no_takeover_to_report():
    """A rewind or a forced stop ended it on purpose; its stream just finishes."""
    from app.core.provider.events import ResultMessage

    adapter = await _adapter_streaming(_Store(lost=True), [_result_event()], admitted=False)
    assert isinstance((await _drain(adapter))[-1], ResultMessage)


async def test_a_failed_ownership_check_does_not_fail_a_finished_turn():
    from app.core.provider.events import ResultMessage

    store = _Store()
    store.check_ownership = AsyncMock(side_effect=RuntimeError("database down"))
    adapter = await _adapter_streaming(store, [_result_event()])
    assert isinstance((await _drain(adapter))[-1], ResultMessage)


# ─── rewind ──────────────────────────────────────────────────────────────────


async def _rewind(monkeypatch, *, rewind=None):
    from app.api import commands
    from app.api.commands import RewindRequestV2
    from app.core.jsonl_rewind import RewindResult
    from app.core.provider import fluid_transcript

    pool = MagicMock(contains=MagicMock(return_value=False))
    monkeypatch.setattr(commands, "get_session_pool", lambda: pool)
    monkeypatch.setattr(commands, "database", MagicMock(get_session=MagicMock(return_value={
        "id": "app-id", "sdk_session_id": SID, "project_id": None, "turn_count": 0,
    })))
    monkeypatch.setattr(commands, "get_working_dir_for_project", lambda _id: "/tmp")
    monkeypatch.setattr(commands, "invalidate_parse_cache", lambda _id: None, raising=False)
    monkeypatch.setattr(fluid_transcript, "is_fluid_session", lambda _id: True)
    monkeypatch.setattr(
        fluid_transcript, "rewind_fluid_session",
        rewind or (lambda *a: RewindResult(
            success=True, message="moved", writer_generation=7, messages_removed=1,
        )),
    )
    reseed = MagicMock(return_value=fluid_transcript.ReseedResult(status="skipped"))
    monkeypatch.setattr(fluid_transcript, "reseed_delegated_cli_session", reseed)
    with patch("app.api.rewind_owner.forward_rewind", AsyncMock(return_value=None)):
        response = await commands.execute_rewind(
            "app-id", RewindRequestV2(target_uuid="n1", restore_chat=True),
        )
    return response, reseed, pool


async def test_rewind_is_refused_while_an_engine_connects_and_touches_nothing(monkeypatch):
    connecting = object()
    admission.admit(SID, connecting)
    rewind = MagicMock()

    response, reseed, pool = await _rewind(monkeypatch, rewind=rewind)

    assert response.success is False and response.message == "Rewind refused"
    assert response.error == str(SessionBusyError())
    rewind.assert_not_called()
    reseed.assert_not_called()
    pool.remove.assert_not_called()
    assert admission.holds(connecting) is True


async def test_rewind_is_refused_mid_turn_and_leaves_the_turn_running(monkeypatch):
    running = object()
    admission.admit(SID, running)
    state = SimpleNamespace(is_streaming=True)
    admission.bind_state(running, state)

    response, _, pool = await _rewind(monkeypatch)

    assert response.success is False
    pool.remove.assert_not_called()


async def test_rewind_holds_the_session_until_its_reseed_is_done_and_passes_its_claim(monkeypatch):
    seen: dict = {}
    from app.core.jsonl_rewind import RewindResult

    def rewind(*_args):
        # A connect at this point would resume the history being rewound away.
        try:
            admission.admit(SID, object())
        except SessionBusyError:
            seen["connect_refused"] = True
        return RewindResult(success=True, message="moved", writer_generation=7)

    response, reseed, _ = await _rewind(monkeypatch, rewind=rewind)

    assert response.success is True
    assert seen == {"connect_refused": True}
    assert reseed.call_args.kwargs["generation"] == 7
    admission.admit(SID, object())  # released


async def test_a_failing_rewind_still_lets_the_session_go(monkeypatch):
    def rewind(*_args):
        raise RuntimeError("database down")

    with pytest.raises(RuntimeError):
        await _rewind(monkeypatch, rewind=rewind)
    admission.admit(SID, object())


# ─── a headless turn ─────────────────────────────────────────────────────────


async def test_headless_resume_is_refused_before_it_stores_the_prompt(monkeypatch):
    from app.core import query_engine as qe

    running = object()
    admission.admit(SID, running)
    database = MagicMock(
        get_session=MagicMock(return_value={"id": "app-id", "sdk_session_id": SID}),
    )
    monkeypatch.setattr(qe, "database", database)
    monkeypatch.setattr(qe, "get_profile", lambda _id: {"id": "p", "name": "P"})
    make_adapter = MagicMock(side_effect=AssertionError("must not get this far"))

    with patch("app.core.provider.factory.make_adapter", make_adapter), pytest.raises(SessionBusyError):
        await qe.execute_query("hello", "p", session_id="app-id")

    database.add_session_message.assert_not_called()
    make_adapter.assert_not_called()


async def test_headless_turn_holds_the_session_until_it_is_over(monkeypatch):
    """Nothing in the pool can say it is running, so its lease has to."""
    from app.core import query_engine as qe

    outcome: dict = {}

    class Headless(_Adapter):
        async def send_query(self, prompt):
            try:
                await _Adapter().connect(_options())
            except SessionBusyError:
                outcome["refused"] = True

        async def receive_events(self):
            return
            yield  # pragma: no cover

    database = MagicMock(
        get_session=MagicMock(return_value={"id": "app-id", "sdk_session_id": SID}),
    )
    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(qe, "database", database)
    monkeypatch.setattr(qe, "get_profile", lambda _id: {"id": "p", "name": "P"})
    monkeypatch.setattr(qe, "reconcile_memory", _noop)
    monkeypatch.setattr(qe, "push_memory", _noop)
    monkeypatch.setattr(qe, "_persist_turn_results", MagicMock(side_effect=_noop))
    adapter = Headless()
    adapter.connect_with_sdk_options = adapter.connect
    options = _options(cwd=None)

    with patch("app.core.provider.factory.make_adapter", return_value=adapter), patch(
        "app.core.sdk_options.build_options_from_profile", return_value=(options, None),
    ):
        await qe.execute_query("hello", "p", session_id="app-id")

    assert outcome == {"refused": True}
    await _Adapter().connect(_options())  # released with its disconnect
