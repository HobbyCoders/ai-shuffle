"""Effort follows the dispatching turn through queuing and model routing.

No provider or CLI is started: real worker setup records its persisted binding
and submitted turn, then real serializers/argv builders verify the final leg.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core import provider_agent as pa, task_dispatch as td
from tests.core.test_provider_agent import dispatch_env
from tests.core.test_task_dispatch import agents, report_dir


@pytest.fixture(autouse=True)
def no_live_turn(monkeypatch):
    from app.db import turns

    monkeypatch.setattr(turns, "get_live_turn", lambda _: None)


@pytest.mark.parametrize("turn,session,legacy,expected", [
    ("xhigh", "high", "low", "xhigh"),
    (None, "high", "low", "high"),
    ("", "high", "low", "high"),
    (None, None, "low", "low"),
    (None, None, None, None),
    ("none", "high", "low", "none"),
])
def test_caller_effort_precedence(monkeypatch, turn, session, legacy, expected):
    from app.db import database, turns

    monkeypatch.setattr(turns, "get_latest_turn", lambda _: {"overrides": {"effort": turn}})
    monkeypatch.setattr(database, "get_profile", lambda _: {"config": {"effort": legacy}})
    assert pa._caller_effort({"id": "caller", "profile_id": "p", "effort": session}) == expected


def test_voice_ignores_historical_typed_effort(monkeypatch):
    from app.db import turns

    monkeypatch.setattr(turns, "get_latest_turn", lambda _: pytest.fail("voice read a typed turn"))
    assert pa._caller_effort({"id": "voice", "mode": "voice", "effort": "high"}) == "high"


def test_active_turn_effort_wins_over_a_newer_queued_message(monkeypatch):
    from app.db import turns

    monkeypatch.setattr(turns, "get_live_turn", lambda _: {"overrides": {"effort": "xhigh"}})
    monkeypatch.setattr(turns, "get_latest_turn", lambda _: pytest.fail("read a newer queued turn"))
    assert pa._caller_effort({"id": "caller", "effort": "low"}) == "xhigh"


def test_queued_override_does_not_configure_the_calling_session(monkeypatch):
    from app.db import turns

    monkeypatch.setattr(turns, "get_latest_turn", lambda _: {"state": "queued", "overrides": {"effort": "low"}})
    assert pa._caller_effort({"id": "caller", "effort": "high"}) == "high"


def test_worker_without_effort_does_not_inherit_its_role_default(monkeypatch):
    from app.db import database

    monkeypatch.setattr(database, "get_profile", lambda _: pytest.fail("worker role effort is not its binding"))
    assert pa._caller_effort({"is_worker": True, "profile_id": "role"}) is None


@pytest.mark.parametrize("spawn,session,expected", [
    (None, None, None), (None, "xhigh", "xhigh"), ("low", "xhigh", "low"),
])
def test_worker_options_use_model_default_when_no_spawn_or_caller_effort(mock_db, spawn, session, expected):
    from app.core.query_options import build_options_from_profile
    from app.db import database

    database.create_profile("role", "Role", None, {"model": "sonnet", "effort": "high"})
    database.create_session(session_id="effort-child", profile_id="role", is_worker=True, effort=session)
    options, _ = build_options_from_profile(
        database.get_profile("role"), session_id="effort-child", overrides={"effort": spawn},
    )
    assert options.effort == expected


@pytest.mark.parametrize("explicit,expected", [(None, "xhigh"), ("low", "low"), ("none", "low")])
def test_child_spawn_effort_overrides_inheritance(dispatch_env, monkeypatch, explicit, expected):
    from app.db import turns

    monkeypatch.setattr(turns, "get_latest_turn", lambda _: {"overrides": {"effort": "xhigh"}})
    result = asyncio.run(pa.dispatch_worker(
        parent_session_id="root", context_session_id="caller", profile_id="child",
        prompt="offline", model_id="sonnet", effort=explicit,
    ))
    assert result.ok
    assert dispatch_env.create_kwargs["effort"] == expected
    assert dispatch_env.tm.submitted[0]["overrides"]["effort"] == expected


def test_captured_absence_cannot_read_a_later_parent_setting(dispatch_env, monkeypatch):
    monkeypatch.setattr(pa, "_caller_effort", lambda _: pytest.fail("re-read caller after admission"))
    result = asyncio.run(pa.dispatch_worker(
        parent_session_id="root", profile_id="child", prompt="offline", inherited_effort=None,
    ))
    assert result.ok
    assert dispatch_env.create_kwargs["effort"] is None
    assert "effort" not in dispatch_env.tm.submitted[0]["overrides"]


@pytest.mark.parametrize("route,model,expected", [
    ("openai_responses", "test-model", "xhigh"),
    ("delegated", "sonnet", "xhigh"),
    ("delegated", "fable", "xhigh"),
    ("delegated", "opus", "xhigh"),
    ("delegated", "haiku", None),
])
def test_worker_effort_reaches_native_wire_and_cli_argv(
    dispatch_env, monkeypatch, route, model, expected,
):
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions
    from app.core.query_options import _build_thinking_config
    from fluid.engine.delegated import DelegatedLoop
    from fluid.providers.base import ProviderRequest
    from fluid.providers.openai_responses.serialize import build_request_body
    from app.db import turns

    from app.core import preference_identity
    from app.db import database, model_providers
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    database.set_user_preference("account", "owner", "composer.modelEfforts", {model: "xhigh"})
    monkeypatch.setattr(turns, "get_latest_turn", lambda _: {"overrides": {"effort": "low"}})
    provider = "p" if route == "openai_responses" else None
    row = {"provider_type": "openai", "models_json": [{"id": model,
        "reasoning_capabilities": {"version": 1, "effort_levels": ["low", "xhigh"]}}]}
    monkeypatch.setattr(ha, "_lookup_provider_row", lambda _: row)
    monkeypatch.setattr(model_providers, "get_model_provider", lambda _: row)
    result = asyncio.run(pa.dispatch_worker(
        parent_session_id="root", context_session_id="caller", profile_id="child",
        prompt="offline", provider_id=provider, model_id=model,
    ))
    assert result.ok
    # An unrelated legacy profile default must not silently replace the caller.
    options = _build_thinking_config({"effort": "low"}, model,
        dispatch_env.tm.submitted[0]["overrides"], dispatch_env.create_kwargs["effort"],
        allow_profile_effort=False)
    requested = options.get("effort")
    assert requested == expected
    effective = ha._effective_effort(SessionOptions(model=model, extra={"provider_id": provider}), requested, route)
    assert effective == expected
    if route == "openai_responses":
        body = build_request_body(ProviderRequest(model=model, messages=[], effort=effective))
        assert body["reasoning"]["effort"] == expected
    else:
        argv = DelegatedLoop(store=None, model=model, effort=effective)._build_argv()
        if expected:
            assert argv[argv.index("--effort") + 1] == expected
        else:
            assert "--effort" not in argv


@pytest.mark.parametrize("native_context", [False, True])
def test_effort_is_captured_before_admission_and_survives_retry(
    agents, report_dir, monkeypatch, native_context,
):
    from app.db import turns

    agents.create_session(session_id="effort-parent", profile_id="root-agent", title="offline", effort="high")
    latest = {"overrides": {"effort": "xhigh"}}
    monkeypatch.setattr(turns, "get_latest_turn", lambda _: latest)
    monkeypatch.setattr(td, "_credential_gap", lambda _: None)
    monkeypatch.setattr(td, "_retry_backoff", lambda *args: 0)
    monkeypatch.setattr(pa, "announce_abandoned_retry", _noop)
    admissions = []

    async def reserve(*args, **kwargs):
        admissions.append(args[0])
        latest["overrides"]["effort"] = "low"
        await asyncio.sleep(0)
        return None, 0

    monkeypatch.setattr(td, "_reserve_admission", reserve)
    workers = []

    async def worker(**kwargs):
        workers.append(kwargs)
        if len(workers) == 1:
            return pa.WorkerOutcome(ok=False, session_id=kwargs["worker_session_id"],
                error="429 Too Many Requests",
                frames_forwarded=0)
        return pa.WorkerOutcome(ok=True, session_id=kwargs["worker_session_id"], report="done")

    monkeypatch.setattr(pa, "dispatch_worker", worker)
    args = {"agent": "helper", "prompt": "offline", "mode": "sync"}
    if native_context:
        # The connected engine's binding wins over a subsequently queued turn.
        latest["overrides"]["effort"] = "low"
        ctx = SimpleNamespace(extra={"app_caller_effort": "xhigh"})
        outcome = asyncio.run(td.make_fluid_dispatcher("effort-parent")(args, ctx))
        assert not outcome.is_error
    else:
        assert asyncio.run(td.dispatch_task_tool(args, caller_session_id="effort-parent"))["ok"]
    assert len(admissions) == len(workers) == 2
    assert [worker["inherited_effort"] for worker in workers] == ["xhigh", "xhigh"]


async def _noop(*args, **kwargs):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "xhigh"])
async def test_native_context_carries_the_connected_engine_effort(monkeypatch, tmp_path, effort):
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions
    from tests.core.test_harness_adapter_session_ctx import _CtxLoop, _fake_provider_client

    monkeypatch.setattr(ha, "NativeLoop", _CtxLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    adapter = ha.HarnessAdapter()
    await adapter.connect(SessionOptions(model="unknown-test-model", effort=effort,
        cwd=str(tmp_path), extra={"fluid_provider": "openai_responses"}))
    try:
        assert _CtxLoop.captured["ctx"].extra["app_caller_effort"] == _CtxLoop.captured["effort"] == effort
    finally:
        await adapter.disconnect()
