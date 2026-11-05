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
    replay_plan = await orch._confirm_start_model("caller", args, None)
    replay = orch._queue("caller", args, "start", model_plan=replay_plan)
    assert replay["body"] == first["body"]
    assert replay["body"]["model_choice"]["model"] == "prov:lane:astra"
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


@pytest.fixture
def remote_wire(chats, monkeypatch):
    """Real caller/receiver producers and SQL, with an offline transport fixture.

    The member fixture supplies authenticated worker identity; no permission
    or consent check is patched. This is not a live network/capability probe.
    """
    from types import SimpleNamespace

    from app.network import catalog, control, engine, membership

    state = {"offline": True, "calls": [], "after_delivery": False, "old_peer": False}
    monkeypatch.setattr(orch, "_remote_worker", lambda worker: {
        "device_id": "other-worker", "device_name": "Other",
        "capabilities": {"start_session_permissions": 1}})
    member = SimpleNamespace(kind=membership.KIND_WORKER, status=membership.STATUS_ACTIVE,
                             name="Caller worker")
    context = SimpleNamespace(device_id="other-worker",
                              member=lambda device: member if device == "caller-worker" else None)

    async def wire(device, action, body, **kwargs):
        state["calls"].append((action, body))
        if state["offline"]:
            raise control.RunnerUnreachable("undelivered fixture request")
        if state["old_peer"] and action != orch.REMOTE_ACTION:
            raise control.ControlRefused("Unknown control action")
        with monkeypatch.context() as receiver:
            receiver.setattr(engine, "context", lambda: context)
            receiver.setattr(catalog, "publish_owned", lambda: None)
            try:
                answer = await control.handle(action, body, from_device="caller-worker")
            except (ValueError, PermissionError, control.UnknownControlAction) as exc:
                raise control.ControlRefused(str(exc)) from exc
        if state["after_delivery"]:
            raise control.MaybeApplied("delivered fixture request lost its response")
        return answer

    monkeypatch.setattr(control, "call", wire)
    return state


def _remote_reservation():
    from app.db import turns

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM turns WHERE session_id LIKE %s",
                    (orch.REMOTE_RECEIPT_PREFIX + "%",))
        [receipt] = cur.fetchall()
        return turns._row(receipt)


@pytest.mark.parametrize("metadata", ["missing", "old_override", "old_noop", "partial_body"])
async def test_legacy_undelivered_receipt_cannot_create_new_catalog_choice(chats, remote_wire, metadata):
    from app.core.agent_types import PermissionResultAllow
    from app.db import sessions

    db = chats[0]
    db.update_model_provider("lane", models_json=[{"id": "astra", "label": "Review Lane"}, {"id": "sol"}])
    _turn("caller", "user", "do the work", profile="agent")
    chats[3]["result"] = PermissionResultAllow(updated_input={})
    args = {**ARGS, "model": "Review Lane", "worker": "Other"}
    first = await _start(args)
    assert first.is_error and "could not be reached" in first.text
    receipt = _remote_reservation()
    # Explicit upgrade fixture: old reservations had no canonical metadata.
    # This is not a claim to have executed an old binary.
    legacy = {key: value for key, value in receipt["payload"].items()
              if key not in ("model_override", "model_unchanged", "remote_start_body")}
    if metadata == "old_override":
        legacy["model_override"] = receipt["payload"]["model_override"]
    elif metadata == "old_noop":
        legacy["model_unchanged"] = True
    elif metadata == "partial_body":
        partial = dict(remote_wire["calls"][0][1])
        partial.pop("model_choice")
        legacy["remote_start_body"] = partial
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET payload=%s WHERE id=%s",
                              (json.dumps(legacy), receipt["id"]))
    db.update_model_provider("lane", models_json=[{"id": "astra"}, {"id": "sol", "label": "Review Lane"}])
    remote_wire["offline"] = False
    retry = await _start(args)
    child = sessions.get_session(receipt["payload"]["remote_session_id"])
    assert retry.is_error and child is None, f"legacy retry created an unapproved child: {retry.text}"
    assert len(chats[2]) == 1  # first explicit approval only, no fabricated consent
    assert remote_wire["calls"][-1][0] == orch.REMOTE_READBACK_ACTION


@pytest.mark.parametrize("provider", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
async def test_pending_noop_or_inherited_retry_keeps_original_binding(chats, remote_wire, provider, explicit):
    from app.db import sessions

    if provider:
        sessions.set_session_run_binding("caller", model="prov:lane:astra")
    _turn("caller", "user", "start a review", profile="agent")
    original = "prov:lane:astra" if provider else "sonnet"
    args = {**ARGS, "worker": "Other", **({"model": original} if explicit else {})}
    first = await _start(args)
    assert first.is_error and "could not be reached" in first.text
    if provider:
        sessions.set_session_run_binding("caller", model="prov:grok:grok-4.5")
    else:
        sessions.set_session_run_binding("caller", model="opus")
    remote_wire["offline"] = False
    retry = await _start(args)
    assert not retry.is_error, retry.text
    result = json.loads(retry.text)
    child = sessions.get_session(result["session_id"])
    assert child["model"] == original, f"pending retry drifted: {retry.text}"
    assert child["provider_id"] == ("lane" if provider else None)
    assert not chats[2]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("model", [None, "sonnet", "Review Lane"])
async def test_delivered_retry_reads_original_child_despite_drift(chats, remote_wire, monkeypatch, legacy, model):
    from app.core import model_override as mo
    from app.core.agent_types import PermissionResultAllow
    from app.db import sessions

    db = chats[0]
    db.update_model_provider("lane", models_json=[{"id": "astra", "label": "Review Lane"}, {"id": "sol"}])
    _turn("caller", "user", "do the work", profile="agent")
    chats[3]["result"] = PermissionResultAllow(updated_input={})
    remote_wire.update(offline=False, after_delivery=True)
    args = {**ARGS, "worker": "Other", **({"model": model} if model else {})}
    first = await _start(args)
    assert first.is_error and "could not be reached" in first.text
    receipt = _remote_reservation()
    original = sessions.get_session(receipt["payload"]["remote_session_id"])
    assert original is not None  # real receiver creation committed before response loss
    if legacy:
        payload = {key: value for key, value in receipt["payload"].items()
                   if key not in ("remote_start_body", "model_override", "model_unchanged")}
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET payload=%s WHERE id=%s",
                                  (json.dumps(payload), receipt["id"]))
    sessions.set_session_run_binding("caller", model="opus")
    db.update_model_provider("lane", models_json=[{"id": "astra"}, {"id": "sol", "label": "Review Lane"}])
    mo._APPROVED.clear()

    async def unexpected_accept(*args, **kwargs):
        pytest.fail("Proven delivered readback must not prompt or authorize fresh work")

    monkeypatch.setattr(mo, "accept", unexpected_accept)
    remote_wire["after_delivery"] = False
    replay = await _start(args)
    assert not replay.is_error, replay.text
    result = json.loads(replay.text)
    assert result["created"] is False and result["session_id"] == original["id"]
    assert result["model"] == original["model"]
    assert result["provider"]["id"] == original["provider_id"]
    assert remote_wire["calls"][-1][0] == (orch.REMOTE_READBACK_ACTION if legacy else orch.REMOTE_ACTION)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM turns WHERE session_id=%s", (original["id"],))
        assert len(cur.fetchall()) == 1


async def test_legacy_readback_on_old_peer_never_falls_back_to_create(chats, remote_wire):
    from app.db import sessions

    _turn("caller", "user", "do the work", profile="agent")
    args = {**ARGS, "worker": "Other"}
    assert (await _start(args)).is_error
    receipt = _remote_reservation()
    payload = {key: value for key, value in receipt["payload"].items() if key != "remote_start_body"}
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET payload=%s WHERE id=%s", (json.dumps(payload), receipt["id"]))
    remote_wire.update(offline=False, old_peer=True)
    replay = await _start(args)
    assert replay.is_error and "older release" in replay.text
    assert [action for action, _ in remote_wire["calls"]] == [orch.REMOTE_ACTION, orch.REMOTE_READBACK_ACTION]
    assert sessions.get_session(payload["remote_session_id"]) is None
    assert not chats[2]


async def test_remote_inherited_native_choice_does_not_rebind_to_receiver_profile(chats, remote_wire):
    from app.db import sessions

    _turn("caller", "user", "do the work", profile="agent")
    args = {**ARGS, "worker": "Other"}
    assert (await _start(args)).is_error
    # Receiver agent configuration changes after reservation; the saved native
    # binding must win rather than inheriting that agent's new provider.
    chats[0].update_profile("agent", config={"provider_id": "lane", "model": "astra"})
    remote_wire["offline"] = False
    retry = await _start(args)
    assert not retry.is_error, retry.text
    result = json.loads(retry.text)
    child = sessions.get_session(result["session_id"])
    assert child["provider_id"] is None and child["model"] == "sonnet"
    assert "model_override" not in result  # inheritance is not fabricated consent


@pytest.mark.parametrize("invalid", ["fingerprint", "worker"])
async def test_legacy_readback_receiver_rechecks_identity(chats, remote_wire, monkeypatch, invalid):
    from types import SimpleNamespace

    from app.network import control, engine, membership

    _turn("caller", "user", "do the work", profile="agent")
    remote_wire.update(offline=False, after_delivery=True)
    args = {**ARGS, "worker": "Other"}
    assert (await _start(args)).is_error
    receipt = _remote_reservation()
    body = {"session_id": receipt["payload"]["remote_session_id"], "command_id": receipt["command_id"],
            "fingerprint": receipt["payload"]["orchestration_fingerprint"]}
    member = SimpleNamespace(kind=membership.KIND_WORKER, status=membership.STATUS_ACTIVE)
    context = SimpleNamespace(device_id="other-worker", member=lambda device: member if device == "caller-worker" else None)
    monkeypatch.setattr(engine, "context", lambda: context)
    if invalid == "fingerprint":
        body["fingerprint"] = "0" * 64
    with pytest.raises((ValueError, PermissionError), match="different arguments|active workers"):
        await control.handle(orch.REMOTE_READBACK_ACTION, body,
                             from_device="caller-worker" if invalid == "fingerprint" else "unpaired-worker")


async def test_unbound_remote_inheritance_requires_an_explicit_choice(chats, remote_wire):
    _turn("caller", "user", "do the work", profile="agent")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET model=NULL WHERE id='caller'")
    result = await _start({**ARGS, "worker": "Other"})
    assert result.is_error and "inherited remote model is unknown" in result.text
    assert not remote_wire["calls"] and not chats[2]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM turns WHERE session_id LIKE %s", (orch.REMOTE_RECEIPT_PREFIX + "%",))
        assert not cur.fetchall()


@pytest.mark.parametrize("model", [None, "sonnet", "prov:lane:astra"])
@pytest.mark.parametrize("delivered", [False, True])
async def test_remote_retry_honors_current_permission_tightening(chats, remote_wire, model, delivered):
    from app.core.agent_types import PermissionResultAllow
    from app.db import sessions

    _turn("caller", "user", "do the work", profile="agent")
    # Controlled binding fixture, not a UI-picker claim. Use the normal session
    # binding writer; the reservation and receiver policy are real producers.
    sessions.set_session_run_binding("caller", permission_mode="bypassPermissions")
    chats[3]["result"] = PermissionResultAllow(updated_input={})
    args = {**ARGS, "worker": "Other", **({"model": model} if model else {})}
    remote_wire.update(offline=not delivered, after_delivery=delivered)
    first = await _start(args)
    assert first.is_error and "could not be reached" in first.text
    receipt = _remote_reservation()
    original_body = receipt["payload"]["remote_start_body"]
    assert original_body["permission_mode"] == "bypassPermissions"
    child_id = original_body["session_id"]
    assert (sessions.get_session(child_id) is not None) is delivered
    original_prompt_count = len(chats[2])

    sessions.set_session_run_binding("caller", permission_mode="default")
    remote_wire.update(offline=False, after_delivery=False)
    retry = await _start(args)
    child = sessions.get_session(child_id)
    if delivered:
        assert not retry.is_error, retry.text
        result = json.loads(retry.text)
        assert result["created"] is False and result["session_id"] == child_id
        # Readback is not fresh authority and must not rewrite an existing child.
        assert child["permission_mode"] == "bypassPermissions"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM turns WHERE session_id=%s", (child_id,))
            assert len(cur.fetchall()) == 1
    else:
        assert retry.is_error and child is None, (
            f"tightened retry created work with permission_mode={child and child['permission_mode']}: {retry.text}")
        assert "new request_id" in retry.text
    assert len(chats[2]) == original_prompt_count
    assert _remote_reservation()["payload"]["remote_start_body"] == original_body
    if not delivered:
        assert remote_wire["calls"][-1][0] == orch.REMOTE_READBACK_ACTION


@pytest.mark.parametrize("model", [None, "sonnet", "prov:lane:astra"])
async def test_remote_retry_never_upgrades_saved_ask_to_current_bypass(chats, remote_wire, model):
    from app.core.agent_types import PermissionResultAllow
    from app.db import sessions

    _turn("caller", "user", "do the work", profile="agent")
    chats[3]["result"] = PermissionResultAllow(updated_input={})
    args = {**ARGS, "worker": "Other", **({"model": model} if model else {})}
    assert (await _start(args)).is_error
    receipt = _remote_reservation()
    original_body = receipt["payload"]["remote_start_body"]
    assert original_body["permission_mode"] == "default"
    original_prompt_count = len(chats[2])
    sessions.set_session_run_binding("caller", permission_mode="bypassPermissions", model="opus")
    remote_wire["offline"] = False
    retry = await _start(args)
    assert not retry.is_error, retry.text
    result = json.loads(retry.text)
    child = sessions.get_session(result["session_id"])
    assert result["created"] is True
    assert child["permission_mode"] == "default"
    assert child["model"] == original_body["model"]
    assert len(chats[2]) == original_prompt_count
    assert remote_wire["calls"][-1] == (orch.REMOTE_ACTION, original_body)


async def test_remote_retry_respects_current_connector_permission_ceiling(chats, remote_wire):
    from app.db import sessions, turns

    root = _turn("caller", "user", "do the work", profile="agent")
    sessions.set_session_run_binding("caller", permission_mode="bypassPermissions")
    args = {**ARGS, "worker": "Other"}
    assert (await _start(args)).is_error
    receipt = _remote_reservation()
    # A real automatic continuation producer with a connector ceiling keeps the
    # original root. No consent receipt or saved remote body is seeded here.
    continuation = turns.enqueue_turn(
        "caller", "agent_message", prompt="continue the same request", profile_id="agent",
        overrides={"_permission_ceiling": "default"}, payload={"orchestration_root": root["id"]})
    assert turns.start_turn(continuation["id"]) is not None
    current = orch._context("caller")
    assert current["id"] == continuation["id"]
    assert current["overrides"]["_permission_ceiling"] == "default"
    remote_wire["offline"] = False
    retry = await _start(args)
    child = sessions.get_session(receipt["payload"]["remote_session_id"])
    assert retry.is_error and child is None, f"connector ceiling was bypassed: {retry.text}"
    assert "new request_id" in retry.text
    assert not chats[2]


@pytest.mark.parametrize("saved,current", [(None, "bypassPermissions"), ("bypassPermissions", None),
                                           ("bypassPermissions", "retired-mode")])
async def test_remote_retry_with_unbound_or_unknown_permission_cannot_create(chats, remote_wire, saved, current):
    from app.db import sessions

    _turn("caller", "user", "do the work", profile="agent")
    # Explicit legacy/unbound caller-state controls, not UI or old-binary claims.
    # Only the caller input is controlled; every reservation is produced normally.
    if saved is None:
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET permission_mode=NULL WHERE id='caller'")
    else:
        sessions.set_session_run_binding("caller", permission_mode=saved)
    args = {**ARGS, "worker": "Other"}
    assert (await _start(args)).is_error
    receipt = _remote_reservation()
    assert receipt["payload"]["remote_start_body"]["permission_mode"] == saved
    if current is None:
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET permission_mode=NULL WHERE id='caller'")
    else:
        sessions.set_session_run_binding("caller", permission_mode=current)
    remote_wire["offline"] = False
    retry = await _start(args)
    child = sessions.get_session(receipt["payload"]["remote_session_id"])
    assert retry.is_error and child is None, f"unproven permission recovery created work: {retry.text}"
    assert "new request_id" in retry.text
    assert not chats[2]


@pytest.mark.parametrize("default", ["native", "provider", "stale-launch"])
@pytest.mark.parametrize("worker", [None, "Other"])
@pytest.mark.parametrize("explicit_default", [False, True])
async def test_real_default_options_reach_startsession(
        chats, remote_wire, monkeypatch, tmp_path, default, worker, explicit_default):
    from fluid.tools.registry import ToolCtx

    from app.core import query_options, turn_manager
    from app.core.session_modes import CALLER_AUTHENTICATED
    from app.db import sessions, turns

    db = chats[0]
    db.update_profile("agent", config={"cwd": str(tmp_path)})
    sessions.create_session("default-caller", "agent", title="Default caller", is_active=True,
                            project_id="task-project", permission_mode="default")
    tid = turn_manager._submit_blocking("default-caller", "user", prompt="do the work", profile_id="agent",
                                       caller=CALLER_AUTHENTICATED)
    assert turns.start_turn(tid) is not None
    # Controlled availability boundary, not a real provider/CLI capability claim.
    monkeypatch.setattr(query_options, "_default_provider_model_without_claude",
                        lambda **kwargs: ("lane", "astra") if default == "provider" else None)
    options, _ = query_options.build_options_from_profile(
        profile=db.get_profile("agent"), session_id="default-caller", overrides={"cwd": str(tmp_path)})
    db.update_session("default-caller", launched_model=options.model)  # real engine init writer
    if default == "stale-launch":
        # A late old-generation stamp is not the current runtime model.
        db.update_session("default-caller", launched_model="haiku")
    else:
        assert db.get_session("default-caller")["launched_model"] == options.model
    if default != "provider":
        assert db.get_session("default-caller")["model"] is None
        assert turns.get_turn(tid)["overrides"].get("model") is None
    ctx = ToolCtx(extra={"session_runtime_model": lambda: (options.model, options.aih_provider_id)})
    remote_wire["offline"] = False
    args = {**ARGS, **({"worker": worker} if worker else {})}
    if explicit_default:
        args["model"] = options.model
    handler = orch.make_handler(lambda: "default-caller")
    if worker and default != "provider" and not explicit_default:
        unproven = await handler("start", args, ToolCtx())
        assert unproven.is_error and "inherited remote model is unknown" in unproven.text
    outcome = await handler("start", args, ctx)
    assert not outcome.is_error, outcome.text
    child = db.get_session(json.loads(outcome.text)["session_id"])
    assert child["model"] == ("prov:lane:astra" if default == "provider" else options.model)
    assert child["provider_id"] == options.aih_provider_id
    # A delivered retry is readback, even if this runtime's default changes.
    options.model = "haiku"
    replay = await handler("start", args, ctx)
    assert not replay.is_error, replay.text
    assert json.loads(replay.text)["session_id"] == child["id"]
    assert db.get_session(child["id"])["model"] == child["model"]
    assert not chats[2]


async def test_explicit_binding_beats_old_runtime_default(chats, remote_wire):
    from fluid.tools.registry import ToolCtx

    from app.db import sessions

    _turn("caller", "user", "do the work", profile="agent")
    sessions.set_session_run_binding("caller", model="opus")
    ctx = ToolCtx(extra={"session_runtime_model": lambda: ("sonnet", None)})
    remote_wire["offline"] = False
    outcome = await orch.make_handler(lambda: "caller")("start", {**ARGS, "worker": "Other"}, ctx)
    assert not outcome.is_error, outcome.text
    child = sessions.get_session(json.loads(outcome.text)["session_id"])
    assert child["model"] == "opus" and child["provider_id"] is None


async def test_runtime_default_change_while_consent_waits_invalidates_plan(chats, remote_wire, monkeypatch):
    from fluid.tools.registry import ToolCtx

    from app.core import model_override
    from app.core.agent_types import PermissionResultAllow

    _turn("caller", "user", "do the work", profile="agent")
    runtime = ["sonnet", None]
    ctx = ToolCtx(extra={"session_runtime_model": lambda: tuple(runtime)})

    async def answer(**kwargs):
        runtime[0] = "haiku"
        return PermissionResultAllow(updated_input={})

    monkeypatch.setattr("app.core.permission_handler.permission_handler.request_permission", answer)
    model_override._APPROVED.clear()
    remote_wire["offline"] = False
    outcome = await orch.make_handler(lambda: "caller")(
        "start", {**ARGS, "worker": "Other", "model": "opus"}, ctx)
    assert outcome.is_error and "context changed" in outcome.text
    assert not remote_wire["calls"]
