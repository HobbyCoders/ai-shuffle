"""Consent replay must be readback of the exact StartSession execution receipt."""
import asyncio
import json
import os

import pytest

from app.core import session_orchestration as orch
from app.db.base import get_db
from tests.core import test_model_override as model_fixtures
from tests.core.test_model_override import ARGS, _start, _turn

chats = model_fixtures.chats
world = model_fixtures.world


@pytest.fixture(scope="module", autouse=True)
def replay_schema():
    """The same producer regressions can opt into an explicitly disposable PG."""
    if os.environ.get("ALLOW_REAL_PG") != "1":
        yield
        return
    from app.db import base, mobile_tool_identity
    from app.db.schema_postgres import create_postgres_schema
    base.init_pool(min_conn=1, max_conn=8)
    try:
        with base.get_db() as conn:
            create_postgres_schema(conn.cursor())
        mobile_tool_identity.init_schema()
        yield
    finally:
        base.close_pool()


@pytest.fixture(autouse=True)
def clean_replay_pg(replay_schema):
    if os.environ.get("ALLOW_REAL_PG") == "1":
        with get_db() as conn:
            conn.cursor().execute("TRUNCATE turns, sessions, agents, projects, model_providers, api_users, "
                                  "pending_interactions, system_settings CASCADE")


@pytest.mark.parametrize("remote", [False, True])
async def test_repeated_id_in_later_owner_root_requires_consent(chats, monkeypatch, remote):
    if remote:
        monkeypatch.setattr(orch, "_remote_worker", lambda worker: {
            "device_id": "other-worker", "device_name": "Other",
            "capabilities": {"start_session_permissions": 1}})
    args = {**ARGS, "model": "opus", **({"worker": "Other"} if remote else {})}
    first_owner = _turn("caller", "user", "go with opus", profile="agent")
    plan = await orch._confirm_start_model("caller", args, None)
    first = orch._queue("caller", args, "start", model_plan=plan)
    if remote:
        assert first["reserved"] is True
    else:
        assert first["created"] is True
    _turn("caller", "user", "Can you use Opus model: 4?", profile="agent")
    try:
        plan = await orch._confirm_start_model("caller", args, None)
        second = orch._queue("caller", args, "start", model_plan=plan)
    except PermissionError:
        pass
    else:
        pytest.fail(f"Later owner root bypassed consent: first={first!r}, second={second!r}")
    assert len(chats[2]) == 1
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT parent_turn_id FROM turns WHERE command_id LIKE 'orchestration:start:%%'")
        assert [row["parent_turn_id"] for row in cur.fetchall()] == [first_owner["id"]]


async def test_same_root_replay_reads_original_child_without_prompt(chats):
    _turn("caller", "user", "go with opus", profile="agent")
    args = {**ARGS, "model": "opus"}
    first = json.loads((await _start(args)).text)
    # Process-local approval state is not authority for replay readback.
    from app.core import model_override
    model_override._APPROVED.clear()
    replay = json.loads((await _start(args)).text)
    assert replay["created"] is False
    assert (replay["session_id"], replay["turn_id"]) == (first["session_id"], first["turn_id"])
    assert not chats[2]


async def test_same_root_argument_mismatch_refuses_without_prompt(chats):
    _turn("caller", "user", "go with opus", profile="agent")
    args = {**ARGS, "model": "opus"}
    assert not (await _start(args)).is_error
    result = await _start({**args, "prompt": "different job"})
    assert result.is_error and "different arguments" in result.text
    assert not chats[2]


@pytest.mark.parametrize("model", ["opus", "prov:lane:astra", "sonnet"])
@pytest.mark.parametrize("remote", [False, True])
async def test_replay_is_exact_readback_even_without_approval_cache(chats, monkeypatch, model, remote):
    from app.core import model_override as mo
    if remote:
        monkeypatch.setattr(orch, "_remote_worker", lambda worker: {
            "device_id": "other-worker", "device_name": "Other",
            "capabilities": {"start_session_permissions": 1}})
    _turn("caller", "user", "use opus and GPT-6 Astra", profile="agent")
    # Whitespace is normalized identically by planning and queueing.
    args = {**ARGS, "model": model, "request_id": " same-id ",
            **({"worker": "Other"} if remote else {})}
    plan = await orch._confirm_start_model("caller", args, None)
    first = orch._queue("caller", args, "start", model_plan=plan)
    mo._APPROVED.clear()

    async def unexpected_accept(*args, **kwargs):
        pytest.fail("An exact execution replay must not ask for new authority")

    monkeypatch.setattr(mo, "accept", unexpected_accept)
    replay_plan = await orch._confirm_start_model("caller", args, None)
    replay = orch._queue("caller", args, "start", model_plan=replay_plan)
    if remote:
        assert replay["reserved"] is False
        assert replay["receipt_id"] == first["receipt_id"]
        assert replay["body"] == first["body"]
    else:
        assert replay["created"] is False
        assert (replay["session_id"], replay["turn_id"]) == (first["session_id"], first["turn_id"])
    assert not chats[2]


@pytest.mark.parametrize("remote", [False, True])
async def test_deleted_replay_receipt_cannot_become_new_execution(chats, monkeypatch, remote):
    if remote:
        monkeypatch.setattr(orch, "_remote_worker", lambda worker: {
            "device_id": "other-worker", "device_name": "Other",
            "capabilities": {"start_session_permissions": 1}})
    _turn("caller", "user", "use opus", profile="agent")
    args = {**ARGS, "model": "opus"}
    plan = await orch._confirm_start_model("caller", args, None)
    first = orch._queue("caller", args, "start", model_plan=plan)
    replay_plan = await orch._confirm_start_model("caller", args, None)
    receipt_id = first["receipt_id"] if remote else first["turn_id"]
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM turns WHERE id=%s", (receipt_id,))
    with pytest.raises(ValueError, match="replay receipt no longer exists"):
        orch._queue("caller", args, "start", model_plan=replay_plan)
    # A fresh attempt is rechecked, not silently executed from the stale token.
    fresh_plan = await orch._confirm_start_model("caller", args, None)
    assert not fresh_plan["receipt_id"] and fresh_plan["source"] == "named_by_user"


@pytest.mark.parametrize("when", ["before_queue", "before_lock"])
@pytest.mark.parametrize("change", ["new_root", "in_place", "owner", "overrides"])
async def test_changed_consent_context_fails_closed(chats, monkeypatch, when, change):
    owner = _turn("caller", "user", "use opus", profile="agent")
    args = {**ARGS, "model": "opus"}
    plan = await orch._confirm_start_model("caller", args, None)

    def mutate():
        if change == "new_root":
            _turn("caller", "user", "Can you use Opus version 4?", profile="agent")
        else:
            with get_db() as conn:
                cur = conn.cursor()
                if change == "in_place":
                    cur.execute("UPDATE turns SET prompt=%s WHERE id=%s",
                                ("Can you use Opus version 4?", owner["id"]))
                elif change == "owner":
                    cur.execute("INSERT INTO api_users (id, name, api_key_hash) "
                                "VALUES ('different-owner', 'Different owner', 'fixture-hash')")
                    cur.execute("UPDATE sessions SET api_user_id='different-owner' WHERE id='caller'")
                else:
                    cur.execute("UPDATE turns SET overrides=%s WHERE id=%s",
                                (json.dumps({"model": "haiku"}), owner["id"]))

    if when == "before_queue":
        mutate()
    else:
        original = orch._start_checkout

        def checkout(*args, **kwargs):
            value = original(*args, **kwargs)
            mutate()
            return value

        monkeypatch.setattr(orch, "_start_checkout", checkout)
    with pytest.raises(ValueError, match="context changed|scope or permissions changed"):
        orch._queue("caller", args, "start", model_plan=plan)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM turns WHERE command_id LIKE 'orchestration:start:%%'")
        assert not cur.fetchall()


async def test_remote_recovery_cannot_substitute_a_new_catalog_choice(chats, monkeypatch):
    from app.core import model_override as mo
    monkeypatch.setattr(orch, "_remote_worker", lambda worker: {
        "device_id": "other-worker", "device_name": "Other",
        "capabilities": {"start_session_permissions": 1}})
    _turn("caller", "user", "use GPT-6 Astra", profile="agent")
    args = {**ARGS, "model": "GPT-6 Astra", "worker": "Other"}
    plan = await orch._confirm_start_model("caller", args, None)
    first = orch._queue("caller", args, "start", model_plan=plan)
    assert first["reserved"] is True
    # The reserved request may not yet have reached the destination. Its
    # receipt must not authorize a newly-resolved model after a catalog edit.
    monkeypatch.setattr(mo, "resolve", lambda *args, **kwargs: mo.Choice(
        model="prov:lane:sol", provider_id="lane", model_id="sol"))
    with pytest.raises(ValueError, match="replay choice changed"):
        await orch._confirm_start_model("caller", args, None)
    assert not chats[2]


async def test_model_plan_cannot_authorize_different_arguments(chats):
    _turn("caller", "user", "use opus", profile="agent")
    args = {**ARGS, "model": "opus"}
    plan = await orch._confirm_start_model("caller", args, None)
    with pytest.raises(ValueError, match="context changed"):
        orch._queue("caller", {**args, "prompt": "another job"}, "start", model_plan=plan)


@pytest.mark.parametrize("missing", ["context", "origin"])
async def test_missing_origin_never_uses_global_receipt(chats, missing):
    _turn("caller", "user", "use opus", profile="agent")
    args = {**ARGS, "model": "opus"}
    assert not (await _start(args)).is_error
    with get_db() as conn:
        if missing == "context":
            conn.cursor().execute("DELETE FROM turns WHERE session_id='caller'")
        else:
            conn.cursor().execute("UPDATE turns SET payload=%s WHERE session_id='caller'",
                                  (json.dumps({"orchestration_root": "missing-origin"}),))
    with pytest.raises(ValueError, match="origin no longer exists|no saved turn context"):
        await orch._confirm_start_model("caller", args, None)
    assert not chats[2]


async def test_deleted_origin_after_planning_fails_closed(chats):
    origin = _turn("caller", "user", "use opus", profile="agent")
    _turn("caller", "unpark", "background continuation", profile="agent",
          payload={"orchestration_root": origin["id"]})
    args = {**ARGS, "model": "opus"}
    plan = await orch._confirm_start_model("caller", args, None)
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM turns WHERE id=%s", (origin["id"],))
    with pytest.raises(ValueError, match="origin no longer exists"):
        orch._queue("caller", args, "start", model_plan=plan)


async def test_automated_continuation_preserves_same_root_readback(chats, monkeypatch):
    from app.core import model_override as mo
    origin = _turn("caller", "user", "use opus", profile="agent")
    args = {**ARGS, "model": "opus"}
    first = json.loads((await _start(args)).text)
    _turn("caller", "unpark", "background continuation", profile="agent",
          payload={"orchestration_root": origin["id"]})

    async def unexpected_accept(*args, **kwargs):
        pytest.fail("A continuation reading the same receipt must not ask again")

    monkeypatch.setattr(mo, "accept", unexpected_accept)
    replay = json.loads((await _start(args)).text)
    assert replay["created"] is False and replay["turn_id"] == first["turn_id"]
    assert not chats[2]


async def test_concurrent_same_root_starts_share_one_prompt_and_child(chats, monkeypatch):
    if os.environ.get("ALLOW_REAL_PG") != "1":
        pytest.skip("row-lock concurrency requires disposable PostgreSQL")
    from threading import Barrier

    from app.core.agent_types import PermissionResultAllow
    _turn("caller", "user", "Can you use Opus model: 4?", profile="agent")
    chats[3]["result"] = PermissionResultAllow(updated_input={})
    args = {**ARGS, "model": "opus"}
    plans = await asyncio.gather(*(orch._confirm_start_model("caller", args, None) for _ in range(2)))
    assert len(chats[2]) == 1
    barrier = Barrier(2, timeout=5)
    original = orch._start_checkout

    def checkout(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait()
        return result

    monkeypatch.setattr(orch, "_start_checkout", checkout)
    results = await asyncio.gather(*(asyncio.to_thread(
        orch._queue, "caller", args, "start", model_plan=plan) for plan in plans))
    assert sorted(result["created"] for result in results) == [False, True]
    assert len({(result["session_id"], result["turn_id"]) for result in results}) == 1
