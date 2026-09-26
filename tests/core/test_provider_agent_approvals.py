"""P3-A3 — a ProviderAgent worker's approvals surface on the PARENT session.

The chunk's contract (plan §2 C.3, user decision 2026-07-25):

* a worker's tool-approval prompt is raised as a ``pending_interactions`` row on
  the PARENT's session id, never the worker's;
* the row (and the WS frame) carry the asking worker's label AND session id so
  the parent's existing permission UI can tag it — no new interaction ``kind``,
  no new frontend component;
* approve, deny, TTL expiry and a vanished parent ALL resolve the worker's tool
  call — it may never hang;
* several workers prompting ONE parent must resolve independently. This is the
  sharp edge: the answer path scans ``(session_id, kind, payload.request_id)``
  and ``notify_changed(session_id)`` wakes every waiter on that session, so the
  request ids have to be unambiguous;
* an ordinary, non-worker session's permission flow is UNCHANGED.

Harness mirrors ``tests/core/test_interaction_placement.py``: real pg-shim DB
(``mock_db``), durable placement always on (#603), gateway LISTEN thread and
mobile push patched off.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock

from app.core.agent_types import PermissionResultAllow, PermissionResultDeny

from app.core import provider_agent_approvals as approvals
from app.core.permission_handler import PermissionHandler
import app.core.permission_handler as ph
from app.db import interactions
from tests.core.interaction_harness import (  # noqa: F401  (autouse fixtures)
    no_connector_delivery,
    no_gateway_listener,
    no_push,
    reset_backfill_memo_on_setup,
    reset_interaction_wait_state,
)


PARENT = "parent-session"
WORKER_A = "worker-aaaa-1111"
WORKER_B = "worker-bbbb-2222"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_routes():
    approvals.reset_routes()
    yield
    approvals.reset_routes()


@pytest.fixture
def unpooled_parent(monkeypatch):
    """No pooled parent ⇒ the delivery legs are inert (durable row still works)."""
    monkeypatch.setattr(approvals, "_parent_refs", lambda sid: {})


@pytest.fixture
def serial_interaction_reads(monkeypatch):
    """Serialize the waiters' interaction reads — a HARNESS fix, not a product one.

    ``await_interaction`` re-reads its row via ``asyncio.to_thread``. The pg-shim
    shares ONE sqlite connection across threads and ``get_db`` commits on exit,
    which resets another thread's in-flight SELECT — so two concurrent waiters
    make one of them read ``None`` and give up. That shape does not exist in
    production: real Postgres hands every caller its own pooled connection.

    Serializing the read removes the shim artifact while leaving the thing under
    test fully intact: two waiters registered on ONE parent, one
    ``notify_changed`` waking both, each resolving only on its own row.
    """
    import threading

    lock = threading.Lock()
    real = interactions.get_interaction

    def _serial(interaction_id):
        with lock:
            return real(interaction_id)

    monkeypatch.setattr(interactions, "get_interaction", _serial)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _wait_pending(session_id, timeout=2.0, count=1):
    """Poll until ``count`` pending permission rows exist on ``session_id``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        rows = [
            r for r in interactions.get_pending_interactions(session_id)
            if r.get("kind") == "permission"
        ]
        if len(rows) >= count:
            return rows
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"expected {count} pending permission row(s) on {session_id}"
    )


def _route(worker_session_id=WORKER_A, label="reviewer"):
    return approvals.register_route(
        worker_session_id=worker_session_id,
        parent_session_id=PARENT,
        label=label,
        provider_id="prov-openai",
        model_id="gpt-5.5",
    )


def _route_perm(kwargs):
    """Production shape: the caller hands in its own request_permission."""
    return approvals.route_permission(kwargs, ph.permission_handler.request_permission)


def _kwargs(session_id=WORKER_A, request_id=None, **over):
    out = dict(
        request_id=request_id or f"perm-{session_id}-abc123",
        session_id=session_id,
        profile_id="default",
        tool_name="Bash",
        tool_input={"command": "ls"},
        broadcast_func=AsyncMock(),
    )
    out.update(over)
    return out


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

class TestRouteRegistry:

    def test_register_and_lookup(self):
        route = _route()
        assert approvals.get_route(WORKER_A) is route
        assert route.parent_session_id == PARENT
        assert route.display_label == "reviewer"
        assert route.binding == "prov-openai · gpt-5.5"

    def test_unknown_session_has_no_route(self):
        _route()
        assert approvals.get_route("some-ordinary-session") is None
        assert approvals.get_route(None) is None
        assert approvals.get_route("") is None

    def test_missing_ids_do_not_register(self):
        assert approvals.register_route(
            worker_session_id="", parent_session_id=PARENT
        ) is None
        assert approvals.register_route(
            worker_session_id=WORKER_A, parent_session_id=""
        ) is None
        assert approvals.get_route(WORKER_A) is None

    def test_registry_is_bounded(self, monkeypatch):
        monkeypatch.setattr(approvals, "_ROUTE_LIMIT", 3)
        for i in range(5):
            approvals.register_route(
                worker_session_id=f"w{i}", parent_session_id=PARENT
            )
        assert approvals.get_route("w0") is None
        assert approvals.get_route("w1") is None
        assert approvals.get_route("w4") is not None

    def test_unregister_drops_the_route(self):
        _route()
        approvals.unregister_route(WORKER_A)
        assert approvals.get_route(WORKER_A) is None

    def test_default_label_when_none_given(self):
        route = approvals.register_route(
            worker_session_id=WORKER_A, parent_session_id=PARENT
        )
        assert route.display_label == "ProviderAgent worker"
        assert route.binding == ""


# ---------------------------------------------------------------------------
# Correlation — the sharp edge
# ---------------------------------------------------------------------------

class TestScopedRequestId:

    def test_production_id_is_untouched(self):
        """``query_engine`` mints ``perm-<worker_session_id>-<hex>`` already."""
        route = _route()
        rid = f"perm-{WORKER_A}-deadbeef"
        assert approvals.scoped_request_id(route, rid) == rid

    def test_foreign_id_is_namespaced_by_the_worker(self):
        route = _route()
        out = approvals.scoped_request_id(route, "perm-shared-1")
        assert out.endswith(WORKER_A)
        assert approvals.SCOPE_SEPARATOR in out

    def test_two_workers_never_collide_on_the_same_id(self):
        a = _route(WORKER_A)
        b = _route(WORKER_B, label="tester")
        assert (
            approvals.scoped_request_id(a, "perm-shared-1")
            != approvals.scoped_request_id(b, "perm-shared-1")
        )

    def test_is_idempotent(self):
        route = _route()
        once = approvals.scoped_request_id(route, "perm-shared-1")
        assert approvals.scoped_request_id(route, once) == once

    def test_empty_id_still_yields_a_scoped_id(self):
        route = _route()
        out = approvals.scoped_request_id(route, None)
        assert out and out.endswith(WORKER_A)


# ---------------------------------------------------------------------------
# Tagging (rides existing payload fields)
# ---------------------------------------------------------------------------

class TestTagging:

    def test_title_is_the_label_when_the_sdk_gave_none(self):
        assert approvals.tagged_title(_route(), None) == "reviewer"

    def test_sdk_title_is_kept_and_prefixed(self):
        assert approvals.tagged_title(_route(), "Run command") == "reviewer — Run command"

    def test_description_names_the_worker_and_its_binding(self):
        desc = approvals.tagged_description(_route(), None)
        assert "reviewer" in desc
        assert "prov-openai · gpt-5.5" in desc
        # Short enough to render without the frontend's 120-char "Show more".
        assert len(desc) <= 120

    def test_sdk_description_is_preserved(self):
        desc = approvals.tagged_description(_route(), "Deletes files")
        assert desc.endswith("Deletes files")
        assert "reviewer" in desc


# ---------------------------------------------------------------------------
# The normal (non-worker) path is unchanged
# ---------------------------------------------------------------------------

class TestNonWorkerPathUnchanged:

    async def test_route_permission_is_a_pass_through(self):
        """No route ⇒ the caller's OWN request_fn, with UNCHANGED kwargs."""
        seen = {}

        async def _fake(**kwargs):
            seen.update(kwargs)
            return PermissionResultAllow(updated_input=kwargs["tool_input"])

        kwargs = _kwargs(session_id="ordinary-session")
        out = await approvals.route_permission(kwargs, _fake)

        assert isinstance(out, PermissionResultAllow)
        assert seen == kwargs  # nothing added, nothing rewritten

    async def test_ordinary_prompt_payload_and_event_carry_no_worker_tag(self, mock_db):
        h = PermissionHandler()
        broadcast = AsyncMock()
        task = asyncio.create_task(h.request_permission(**_kwargs(
            session_id="ordinary-session",
            request_id="r-plain",
            broadcast_func=broadcast,
        )))
        rows = await _wait_pending("ordinary-session")

        payload = rows[0]["payload"]
        assert "provider_agent" not in payload
        assert payload["request_id"] == "r-plain"
        # Legacy payload shape, exactly.
        assert set(payload) == {
            "request_id", "profile_id", "tool_name", "tool_input",
        }

        await h.respond("r-plain", "ordinary-session", "allow", broadcast_func=broadcast)
        assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)

        events = [
            c.args[0] for c in broadcast.call_args_list
            if c.args and c.args[0].get("type") == "permission_request"
        ]
        assert len(events) == 1
        assert "provider_agent" not in events[0]
        # Legacy WS shape, exactly.
        assert set(events[0]) == {
            "type", "session_id", "request_id", "tool_name", "tool_input",
            "queue_position", "queue_total",
        }

    async def test_ordinary_prompt_is_unaffected_by_a_live_worker_route(self, mock_db):
        """A registered worker route must not leak into any other session."""
        _route()
        broadcast = AsyncMock()
        task = asyncio.create_task(_route_perm(_kwargs(
            session_id="ordinary-session",
            request_id="r-plain",
            broadcast_func=broadcast,
        )))
        rows = await _wait_pending("ordinary-session")
        assert "provider_agent" not in rows[0]["payload"]
        assert rows[0]["payload"]["request_id"] == "r-plain"
        assert interactions.get_pending_interactions(PARENT) == []

        await ph.permission_handler.respond(
            "r-plain", "ordinary-session", "allow", broadcast_func=broadcast
        )
        assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)
        # The caller's OWN broadcast was used, not a parent-directed one.
        assert any(
            c.args and c.args[0].get("type") == "permission_request"
            and c.args[0].get("session_id") == "ordinary-session"
            for c in broadcast.call_args_list
        )


# ---------------------------------------------------------------------------
# The redirect
# ---------------------------------------------------------------------------

class TestWorkerPromptLandsOnParent:

    async def test_row_is_created_on_the_parent_not_the_worker(
        self, mock_db, unpooled_parent
    ):
        _route()
        task = asyncio.create_task(_route_perm(_kwargs()))
        rows = await _wait_pending(PARENT)

        assert interactions.get_pending_interactions(WORKER_A) == []
        payload = rows[0]["payload"]
        assert rows[0]["session_id"] == PARENT
        assert rows[0]["kind"] == "permission"  # no new interaction kind

        tag = payload["provider_agent"]
        assert tag["worker_session_id"] == WORKER_A
        assert tag["parent_session_id"] == PARENT
        assert tag["label"] == "reviewer"
        assert tag["provider_id"] == "prov-openai"
        assert tag["model_id"] == "gpt-5.5"
        # The label also rides the fields the existing UI already renders.
        assert "reviewer" in payload["title"]
        assert "reviewer" in payload["description"]

        await ph.permission_handler.respond(
            payload["request_id"], PARENT, "deny", broadcast_func=AsyncMock()
        )
        assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)

    async def test_allow_on_the_parent_resolves_the_worker_call(
        self, mock_db, unpooled_parent
    ):
        _route()
        task = asyncio.create_task(_route_perm(_kwargs()))
        rows = await _wait_pending(PARENT)
        rid = rows[0]["payload"]["request_id"]

        result = await ph.permission_handler.respond(
            rid, PARENT, "allow", broadcast_func=AsyncMock(),
            answered_by_device="dev-parent",
        )
        assert result["resolved"] is True

        out = await asyncio.wait_for(task, 12.0)
        assert isinstance(out, PermissionResultAllow)
        assert out.updated_input == {"command": "ls"}

    async def test_deny_on_the_parent_resolves_the_worker_call(
        self, mock_db, unpooled_parent
    ):
        _route()
        task = asyncio.create_task(_route_perm(_kwargs()))
        rows = await _wait_pending(PARENT)

        await ph.permission_handler.respond(
            rows[0]["payload"]["request_id"], PARENT, "deny",
            broadcast_func=AsyncMock(),
        )
        assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)

    async def test_ttl_expiry_resolves_the_worker_call_as_deny(
        self, mock_db, unpooled_parent, monkeypatch
    ):
        """Unanswered ⇒ the existing 5-minute TTL denies; the worker never hangs."""
        monkeypatch.setattr(ph, "PERMISSION_TTL_SECONDS", 0)
        _route()
        out = await asyncio.wait_for(
            _route_perm(_kwargs()), 12.0
        )
        assert isinstance(out, PermissionResultDeny)
        assert "timed out" in (out.message or "")

    async def test_a_vanished_parent_denies_instead_of_raising(self):
        """Any failure raising the row on the parent must still resolve."""
        async def _boom(**kwargs):
            raise RuntimeError("parent session is gone")

        _route()
        out = await asyncio.wait_for(
            approvals.route_permission(_kwargs(), _boom), 5.0
        )
        assert isinstance(out, PermissionResultDeny)
        assert out.message == approvals.ROUTE_FAILURE_MESSAGE

    async def test_cancellation_still_propagates(self):
        """The failure containment must not swallow a real cancellation."""
        async def _cancelled(**kwargs):
            raise asyncio.CancelledError()

        _route()
        with pytest.raises(asyncio.CancelledError):
            await approvals.route_permission(_kwargs(), _cancelled)


# ---------------------------------------------------------------------------
# Concurrency — many workers, ONE parent (ruling P12: fan-out is uncapped)
# ---------------------------------------------------------------------------

class TestConcurrentWorkers:

    async def test_two_workers_on_one_parent_resolve_independently(
        self, mock_db, unpooled_parent, serial_interaction_reads
    ):
        _route(WORKER_A, label="reviewer")
        _route(WORKER_B, label="tester")

        task_a = asyncio.create_task(_route_perm(
            _kwargs(session_id=WORKER_A, tool_input={"command": "ls"})
        ))
        task_b = asyncio.create_task(_route_perm(
            _kwargs(session_id=WORKER_B, tool_input={"command": "rm -rf /"})
        ))
        rows = await _wait_pending(PARENT, count=2)

        by_worker = {
            r["payload"]["provider_agent"]["worker_session_id"]: r["payload"]
            for r in rows
        }
        assert set(by_worker) == {WORKER_A, WORKER_B}
        # The whole point: no two live prompts on this parent share a request_id.
        assert (
            by_worker[WORKER_A]["request_id"] != by_worker[WORKER_B]["request_id"]
        )
        assert by_worker[WORKER_A]["title"].startswith("reviewer")
        assert by_worker[WORKER_B]["title"].startswith("tester")

        # Answer ONE. notify_changed wakes EVERY waiter on the parent, so this is
        # exactly where a cross-resolve would show up.
        await ph.permission_handler.respond(
            by_worker[WORKER_A]["request_id"], PARENT, "allow",
            broadcast_func=AsyncMock(),
        )
        out_a = await asyncio.wait_for(task_a, 12.0)
        assert isinstance(out_a, PermissionResultAllow)
        assert out_a.updated_input == {"command": "ls"}
        assert not task_b.done()  # B is still waiting on its OWN row

        await ph.permission_handler.respond(
            by_worker[WORKER_B]["request_id"], PARENT, "deny",
            broadcast_func=AsyncMock(),
        )
        assert isinstance(await asyncio.wait_for(task_b, 12.0), PermissionResultDeny)

    async def test_workers_sharing_a_request_id_are_still_disambiguated(
        self, mock_db, unpooled_parent
    ):
        """Belt: even with an identical incoming request_id, ids stay unique."""
        _route(WORKER_A, label="reviewer")
        _route(WORKER_B, label="tester")

        task_a = asyncio.create_task(_route_perm(
            _kwargs(session_id=WORKER_A, request_id="perm-collide")
        ))
        task_b = asyncio.create_task(_route_perm(
            _kwargs(session_id=WORKER_B, request_id="perm-collide")
        ))
        rows = await _wait_pending(PARENT, count=2)

        ids = {r["payload"]["request_id"] for r in rows}
        assert len(ids) == 2

        for r in rows:
            await ph.permission_handler.respond(
                r["payload"]["request_id"], PARENT, "deny",
                broadcast_func=AsyncMock(),
            )
        assert isinstance(await asyncio.wait_for(task_a, 12.0), PermissionResultDeny)
        assert isinstance(await asyncio.wait_for(task_b, 12.0), PermissionResultDeny)


# ---------------------------------------------------------------------------
# Delivery to the parent's UI
# ---------------------------------------------------------------------------

class TestDeliveryToParent:

    async def test_frame_goes_to_the_parents_socket_and_devices(self, monkeypatch):
        parent_socket = AsyncMock()
        fanout = AsyncMock()
        monkeypatch.setattr(
            approvals, "_parent_refs",
            lambda sid: {"broadcast_func": parent_socket, "user_id": "user-1"},
        )
        from app.core import sync_engine as se

        monkeypatch.setattr(se.sync_engine, "broadcast_user_permission", fanout)

        send = approvals.parent_broadcast(_route())
        await send({"type": "permission_request", "session_id": PARENT})

        parent_socket.assert_awaited_once()
        assert parent_socket.await_args.args[0]["session_id"] == PARENT
        fanout.assert_awaited_once()
        assert fanout.await_args.args[0] == "user-1"          # user id
        assert fanout.await_args.args[1]["type"] == "permission_request"

    async def test_delivery_failures_are_swallowed(self, monkeypatch):
        monkeypatch.setattr(
            approvals, "_parent_refs",
            lambda sid: {
                "broadcast_func": AsyncMock(side_effect=RuntimeError("socket gone")),
                "user_id": "user-1",
            },
        )
        from app.core import sync_engine as se

        monkeypatch.setattr(
            se.sync_engine, "broadcast_user_permission",
            AsyncMock(side_effect=RuntimeError("fanout gone")),
        )
        # Must not raise — delivery is best-effort; the durable row is the truth.
        await approvals.parent_broadcast(_route())({"type": "permission_request"})

    async def test_no_pooled_parent_is_inert(self, monkeypatch):
        monkeypatch.setattr(approvals, "_parent_refs", lambda sid: {})
        await approvals.parent_broadcast(_route())({"type": "permission_request"})

    async def test_worker_broadcast_func_is_replaced(self, mock_db, unpooled_parent):
        """The worker's own broadcast reaches nobody; it must not be used."""
        worker_broadcast = AsyncMock()
        _route()
        task = asyncio.create_task(_route_perm(
            _kwargs(broadcast_func=worker_broadcast)
        ))
        rows = await _wait_pending(PARENT)
        await ph.permission_handler.respond(
            rows[0]["payload"]["request_id"], PARENT, "deny",
            broadcast_func=AsyncMock(),
        )
        await asyncio.wait_for(task, 12.0)
        worker_broadcast.assert_not_awaited()


# ---------------------------------------------------------------------------
# Killing a worker sweeps ITS prompts off the parent (and nothing else)
# ---------------------------------------------------------------------------

def _raise_worker_prompt(worker_session_id, request_id):
    """A pending permission row on the PARENT, tagged with a worker — the exact
    shape ``redirect_kwargs`` + ``_build_permission_payload`` persist."""
    return interactions.create_interaction(
        PARENT,
        "permission",
        {
            "request_id": request_id,
            "profile_id": "default",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "provider_agent": {
                "worker_session_id": worker_session_id,
                "parent_session_id": PARENT,
                "label": "reviewer",
            },
        },
        300,
    )


def _raise_plain_prompt(request_id="r-plain"):
    """An ordinary (untagged) pending permission row on the parent."""
    return interactions.create_interaction(
        PARENT,
        "permission",
        {
            "request_id": request_id,
            "profile_id": "default",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        },
        300,
    )


def _state(interaction_id):
    return interactions.get_interaction(interaction_id)["state"]


class TestCancelWorkerPrompts:
    """A killed worker's prompts must not outlive it on the parent's screen —
    and a SIBLING worker's must not be collateral damage."""

    def test_only_the_target_workers_rows_are_cancelled(self, mock_db, monkeypatch):
        notified = []
        monkeypatch.setattr(
            approvals_wait_module(), "notify_changed", notified.append
        )

        mine_1 = _raise_worker_prompt(WORKER_A, "r-a1")
        mine_2 = _raise_worker_prompt(WORKER_A, "r-a2")
        sibling = _raise_worker_prompt(WORKER_B, "r-b1")
        plain = _raise_plain_prompt()

        count = approvals.cancel_worker_prompts(WORKER_A, PARENT)

        assert count == 2
        assert _state(mine_1["id"]) == "cancelled"
        assert _state(mine_2["id"]) == "cancelled"
        # The sibling worker and the parent's own prompt are untouched.
        assert _state(sibling["id"]) == "pending"
        assert _state(plain["id"]) == "pending"
        # ONE notify for the whole sweep — it wakes every waiter on the parent.
        assert notified == [PARENT]

    def test_nothing_to_cancel_is_a_silent_noop(self, mock_db, monkeypatch):
        notified = []
        monkeypatch.setattr(
            approvals_wait_module(), "notify_changed", notified.append
        )
        plain = _raise_plain_prompt()

        assert approvals.cancel_worker_prompts(WORKER_A, PARENT) == 0
        assert _state(plain["id"]) == "pending"
        assert notified == []

    def test_missing_ids_are_a_noop(self, mock_db):
        assert approvals.cancel_worker_prompts("", PARENT) == 0
        assert approvals.cancel_worker_prompts(WORKER_A, "") == 0

    def test_a_db_failure_never_raises(self, mock_db, monkeypatch):
        _raise_worker_prompt(WORKER_A, "r-a1")

        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(interactions, "get_pending_interactions", _boom)
        assert approvals.cancel_worker_prompts(WORKER_A, PARENT) == 0

    def test_a_route_registry_entry_is_not_required(self, mock_db, monkeypatch):
        """Routes are never unregistered, so they identify no ROWS — the sweep
        must work off the payload tag alone."""
        monkeypatch.setattr(approvals_wait_module(), "notify_changed", lambda sid: None)
        approvals.reset_routes()
        row = _raise_worker_prompt(WORKER_A, "r-a1")

        assert approvals.cancel_worker_prompts(WORKER_A, PARENT) == 1
        assert _state(row["id"]) == "cancelled"

    def test_worker_id_of_reads_the_tag(self):
        assert approvals.worker_id_of(
            {"payload": {"provider_agent": {"worker_session_id": WORKER_A}}}
        ) == WORKER_A
        assert approvals.worker_id_of({"payload": {}}) is None
        assert approvals.worker_id_of({"payload": None}) is None
        assert approvals.worker_id_of({}) is None
        assert approvals.worker_id_of(None) is None


def approvals_wait_module():
    """The ``interaction_wait`` module object ``cancel_worker_prompts`` imports."""
    from app.core import interaction_wait as iw

    return iw


# ---------------------------------------------------------------------------
# dispatch_worker registers the route
# ---------------------------------------------------------------------------

class TestDispatchWiring:

    def test_dispatch_worker_registers_the_route(self, monkeypatch):
        from app.core import provider_agent as pa
        from app.core import project_resolver
        from app.core import turn_manager as tm_mod
        from app.db import database as db

        created = {}

        def _submit(session_id, source, **kwargs):
            created["worker"] = session_id
            return "turn-1"

        async def _idle(session_id, timeout=300.0):
            return True

        monkeypatch.setattr(tm_mod, "submit", _submit)
        monkeypatch.setattr(tm_mod, "wait_session_idle", _idle)
        monkeypatch.setattr(tm_mod, "interrupt", lambda *a, **k: None)
        monkeypatch.setattr(db, "get_session", lambda sid: {"id": sid})
        monkeypatch.setattr(db, "create_session", lambda **kw: {"id": kw["session_id"]})
        monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: None)
        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {
                "usage": {}, "error": None, "interrupted": False,
                "terminal_found": True,
            },
        )
        monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "done")

        outcome = asyncio.run(pa.dispatch_worker(
            parent_session_id=PARENT,
            profile_id="default",
            provider_id="prov-openai",
            model_id="gpt-5.5",
            prompt="review this",
            label="reviewer",
        ))

        route = approvals.get_route(outcome.session_id)
        assert route is not None
        assert route.parent_session_id == PARENT
        assert route.display_label == "reviewer"
        assert route.provider_id == "prov-openai"
        assert route.model_id == "gpt-5.5"
