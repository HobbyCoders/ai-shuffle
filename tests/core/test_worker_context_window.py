"""Task workers run with the context window their caller chose.

Regression: a Claude worker started through Task or Swarm got no window
binding, so its claude CLI spawn carried no window pins and ran at the CLI's
1M default whatever the user had picked. No provider or CLI is started here:
real worker setup records the persisted binding and submitted turn, and the
real options builder and adapter projection show what the leg receives.
"""

import asyncio

import pytest

from app.core import provider_agent as pa
from tests.core.test_provider_agent import dispatch_env  # noqa: F401 — fixture

DISABLE_1M = "CLAUDE_CODE_DISABLE_1M_CONTEXT"
AUTO_COMPACT = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
MAX_CONTEXT = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


@pytest.fixture(autouse=True)
def no_live_turn(monkeypatch):
    from app.db import turns

    monkeypatch.setattr(turns, "get_live_turn", lambda _: None)
    monkeypatch.setattr(turns, "get_latest_turn", lambda _: {})


# --- the caller's choice ------------------------------------------------------

@pytest.mark.parametrize("live,latest,session,expected", [
    (200_000, 1_000_000, 1_000_000, 200_000),
    (None, 200_000, 1_000_000, 200_000),
    (None, None, 200_000, 200_000),
    (None, None, None, None),
    (None, "junk", 0, None),
])
def test_caller_window_precedence(monkeypatch, live, latest, session, expected):
    from app.db import turns

    monkeypatch.setattr(turns, "get_live_turn",
        lambda _: {"overrides": {"context_window": live}} if live else None)
    monkeypatch.setattr(turns, "get_latest_turn", lambda _: {"overrides": {"context_window": latest}})
    assert pa._caller_context_window({"id": "caller", "context_window": session}) == expected


def test_queued_turn_does_not_configure_the_caller(monkeypatch):
    from app.db import turns

    monkeypatch.setattr(turns, "get_latest_turn",
        lambda _: {"state": "queued", "overrides": {"context_window": 1_000_000}})
    assert pa._caller_context_window({"id": "caller", "context_window": 200_000}) == 200_000


def test_voice_reads_only_its_live_binding(monkeypatch):
    from app.db import turns

    monkeypatch.setattr(turns, "get_latest_turn", lambda _: pytest.fail("voice read a typed turn"))
    assert pa._caller_context_window({"id": "v", "mode": "voice", "context_window": 200_000}) == 200_000


# --- fitting the choice to the worker's model ----------------------------------

@pytest.mark.parametrize("window,model,expected", [
    (200_000, "opus", 200_000),
    (1_000_000, "opus", 1_000_000),
    (None, "opus", None),
    # The caller chose "standard" on GPT (272K): a Claude worker runs 200K.
    (272_000, "sonnet", 200_000),
    (256_000, "fable", 200_000),
    # Haiku 5.5 offers both windows; unknown choices pass through to query_options.
    (200_000, "haiku", 200_000),
    (1_000_000, "haiku", 1_000_000),
    (1_000_000, "claude-haiku-5-5", 1_000_000),
    (1_000_000, "claude-haiku-4-5", 1_000_000),
    # The model is inherited, so its choices are not known here.
    (200_000, None, 200_000),
])
def test_native_worker_window_fit(window, model, expected):
    assert pa.worker_context_window(window, None, model) == expected


def test_provider_worker_window_fit(monkeypatch):
    from app.db import database

    row = {"id": "chatgpt", "provider_type": "chatgpt",
           "models_json": [{"id": "gpt-6-luna", "context_window": 272_000}]}
    monkeypatch.setattr(database, "get_model_provider", lambda *_a, **_k: row)
    assert pa.worker_context_window(200_000, "chatgpt", "gpt-6-luna") == 272_000
    assert pa.worker_context_window(1_000_000, "chatgpt", "gpt-6-luna") == 1_000_000


# --- local dispatch: row and first turn ----------------------------------------

def _caller_window(monkeypatch, window):
    from app.db import database

    original = database.get_session
    monkeypatch.setattr(database, "get_session", lambda sid: {
        **original(sid), "context_window": window,
    })


@pytest.mark.parametrize("window", [200_000, 1_000_000])
def test_native_claude_worker_inherits_the_callers_window(dispatch_env, monkeypatch, window):
    _caller_window(monkeypatch, window)
    result = asyncio.run(pa.dispatch_worker(
        parent_session_id="root", context_session_id="caller", profile_id="child",
        prompt="offline", model_id="opus",
    ))
    assert result.ok
    # On the row, so every later turn (message, restart, limit-reset resume)
    # runs with it too, and on the first turn.
    assert dispatch_env.create_kwargs["context_window"] == window
    assert dispatch_env.tm.submitted[0]["overrides"]["context_window"] == window


def test_worker_without_a_caller_choice_keeps_its_default(dispatch_env, monkeypatch):
    _caller_window(monkeypatch, None)
    result = asyncio.run(pa.dispatch_worker(
        parent_session_id="root", context_session_id="caller", profile_id="child",
        prompt="offline", model_id="opus",
    ))
    assert result.ok
    assert "context_window" not in dispatch_env.create_kwargs
    assert "context_window" not in dispatch_env.tm.submitted[0]["overrides"]


# --- what the delegated Claude leg receives ------------------------------------

def _leg_for_worker(mock_db, model, window, overrides=None):
    """Build the worker's options as its turn would, and project them the way
    the harness adapter does before choosing the delegated or native leg."""
    from unittest.mock import patch

    from app.core import query_options as qo
    from app.core.provider import harness_adapter as ha
    from app.db import database

    database.create_profile("role", "Role", None, {"model": model})
    database.create_session(session_id="window-child", profile_id="role", is_worker=True,
                            model=model, context_window=window)
    with patch.object(qo, "get_security_instructions", return_value=""):
        options, _ = qo.build_options_from_profile(
            database.get_profile("role"), session_id="window-child",
            overrides=overrides or {"model": model},
        )
    captured = {}
    adapter = ha.HarnessAdapter()

    async def connect(opts):
        captured["opts"] = opts

    adapter.connect = connect
    asyncio.run(adapter.connect_with_sdk_options(options))
    return options, captured["opts"]


@pytest.mark.parametrize("model", ["opus", "haiku", "claude-haiku-5-5"])
def test_standard_window_pins_the_delegated_cli_below_1m(mock_db, model):
    from app.core.provider import harness_adapter as ha
    from fluid.engine.delegated import DelegatedLoop

    options, opts = _leg_for_worker(mock_db, model, 200_000)
    assert options.aih_context_window == 200_000
    # What the delegated leg hands the claude CLI's process env.
    env = DelegatedLoop(store=None, model=opts.model, env=opts.env)._child_env()
    assert env[DISABLE_1M] == "1"
    assert env[AUTO_COMPACT] == env[MAX_CONTEXT] == "200000"
    # A native leg on the same options prices and compacts against 200K.
    assert ha._native_context_window("anthropic", opts) == 200_000


@pytest.mark.parametrize("model", ["opus", "haiku", "claude-haiku-5-5"])
def test_1m_window_selects_1m_on_the_delegated_cli(mock_db, model):
    from app.core.provider import harness_adapter as ha
    from fluid.engine.delegated import DelegatedLoop

    _, opts = _leg_for_worker(mock_db, model, 1_000_000)
    env = DelegatedLoop(store=None, model=opts.model, env=opts.env)._child_env()
    assert env[DISABLE_1M] == "0"
    assert env[AUTO_COMPACT] == env[MAX_CONTEXT] == "1000000"
    assert ha._native_context_window("anthropic", opts) == 1_000_000


@pytest.mark.parametrize("model,window", [("opus", None), ("haiku", None), ("claude-haiku-4-5", 200_000), ("claude-haiku-4-5", 1_000_000)])
def test_no_or_unsupported_choice_falls_back_like_a_session_turn(mock_db, model, window):
    from app.core.model_registry import get_context_window
    from app.core.provider import harness_adapter as ha

    options, opts = _leg_for_worker(mock_db, model, window)
    assert options.aih_context_window is None
    assert DISABLE_1M not in (opts.env or {}) and AUTO_COMPACT not in (opts.env or {})
    assert ha._native_context_window("anthropic", opts) == get_context_window(model)
