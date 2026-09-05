"""What ``_persist_turn_results`` hands ``update_session`` for one turn's usage.

Two properties, both learned the hard way.

None-safety: the turn metadata keys (``total_cost_usd``/``tokens_in``/
``tokens_out``/``num_turns``) can be PRESENT with a None value — the fluid
harness path does this whenever a provider reports no cost/usage — so
``metadata.get(key, 0)``'s default never fires. The None then reached
``update_session``, whose additive SQL turned into ``col = col + NULL`` and
NULLed the session row's counters, breaking GET /api/v1/sessions/cursor
(pydantic float validation).

Correct routing: usage is normalized Anthropic-style, so ``tokens_in`` is FRESH
input and the cache numbers are separate — they must reach the cumulative
``total_cache_*`` columns, not just the overwrite snapshots. And ``num_turns``
on the fluid path is the MODEL-CALL count, so feeding it to ``turn_count`` made
a one-turn child read as sixteen turns.
"""

import pytest

from app.core import query_persistence as qp
from app.db import database


@pytest.fixture
def captured_updates(monkeypatch):
    """Replace run_sync so no DB is touched; record update_session kwargs."""
    calls: list[dict] = []

    async def fake_run_sync(fn, *args, **kwargs):
        if fn is database.update_session:
            calls.append(kwargs)
            return None
        if fn is database.get_session:
            return None
        return [] if args or kwargs else None

    monkeypatch.setattr(qp, "run_sync", fake_run_sync)
    return calls


async def _persist(metadata: dict) -> None:
    await qp._persist_turn_results(
        session_id="s1",
        sdk_session_id="sdk-1",
        tool_messages=[],
        response_text=[],
        metadata=metadata,
        profile_id="p1",
    )


@pytest.mark.asyncio
async def test_none_usage_values_become_zero(captured_updates):
    await _persist({
        "total_cost_usd": None,
        "tokens_in": None,
        "tokens_out": None,
        "num_turns": None,
    })

    assert captured_updates, "update_session was never called"
    kwargs = captured_updates[0]
    assert kwargs["cost_increment"] == 0
    assert kwargs["tokens_in_increment"] == 0
    assert kwargs["tokens_out_increment"] == 0
    assert kwargs["cache_read_increment"] == 0
    assert kwargs["cache_creation_increment"] == 0
    assert kwargs["model_calls_increment"] == 0
    # The turn still completed — one turn is one turn even when the provider
    # reported no usage at all.
    assert kwargs["turn_increment"] == 1


@pytest.mark.asyncio
async def test_real_usage_values_pass_through(captured_updates):
    await _persist({
        "total_cost_usd": 0.42,
        "tokens_in": 100,
        "tokens_out": 200,
        "cache_read_tokens": 50_000,
        "cache_creation_tokens": 7_000,
        "num_turns": 3,
    })

    kwargs = captured_updates[0]
    assert kwargs["cost_increment"] == 0.42
    assert kwargs["tokens_in_increment"] == 100
    assert kwargs["tokens_out_increment"] == 200
    assert kwargs["cache_read_increment"] == 50_000
    assert kwargs["cache_creation_increment"] == 7_000
    # ``num_turns`` is the MODEL-CALL count on the fluid path — it belongs to
    # total_model_calls. One persisted turn is one turn.
    assert kwargs["model_calls_increment"] == 3
    assert kwargs["turn_increment"] == 1


@pytest.mark.asyncio
async def test_a_failed_turn_books_its_model_calls_but_not_a_turn(captured_updates):
    """The invariant an audit mistook for a double count.

    ``total_model_calls`` counts the API hops of EVERY terminal, including the
    interrupted and errored ones. A TurnComplete-only sum over the fluid event
    log therefore comes in BELOW this column — that is the log undercounting,
    not the column double counting. The calls a stopped or failed turn made
    were really made and really billed; booking only completed turns is the bug
    this column was added to fix (a session with 167 calls read as 11).
    """
    await _persist({"num_turns": 9, "error": "provider connection reset"})

    kwargs = captured_updates[0]
    assert kwargs["model_calls_increment"] == 9
    # …and it is NOT a user-visible turn.
    assert kwargs["turn_increment"] == 0


@pytest.mark.asyncio
async def test_one_turn_books_exactly_one_increment(captured_updates):
    """There is one runtime writer per turn. ``settle_killed_session`` is the
    only other writer and books INSTEAD of this one, never as well: the atomic
    ``settle_turn`` claim lets exactly one of them win."""
    await _persist({"num_turns": 4})

    assert len([c for c in captured_updates if "model_calls_increment" in c]) == 1
