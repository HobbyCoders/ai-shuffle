"""The automations / connectors / import-export / hooks routers must not do
blocking DB work on the event loop.

Every synchronous ``psycopg2`` call made straight from an ``async def`` handler
freezes EVERY other request, WebSocket frame and SSE stream on that worker for
its whole duration (perf review 2026-08). These tests pin the fix by identity,
not by wire shape: each blocking callable records the thread it actually ran on
and must not have run on the loop thread — and the handlers whose DB work is
contiguous must spend exactly ONE thread hop, so the stall is not simply traded
for a hop (and a pool round-trip) per query.

Companion behavioural coverage lives in ``tests/test_api_automations.py``,
``tests/test_api_connectors.py`` and ``tests/api/test_import_export.py``; the
handlers are driven here as plain coroutines, so the test owns the event loop
and can name its thread.
"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import automations as automations_api
from app.api import connectors as connectors_api
from app.api import hooks as hooks_api
from app.api import import_export as ie_api
from app.api.import_export import ExportData, ExportedProfile, ExportedSubagent
from app.core.models import HookUpdate


# ═══ harness ════════════════════════════════════════════════════════════════


class _SpyDatabase:
    """Stand-in for the sync ``database`` facade.

    Every attribute is a callable that records the thread it was called on and
    returns whatever ``returns`` maps its name to (a MagicMock otherwise).
    """

    def __init__(self, **returns):
        self._returns = returns
        self.threads = []
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args, **kwargs):
            self.threads.append(threading.get_ident())
            self.calls.append(name)
            value = self._returns.get(name, _MISSING)
            if value is _MISSING:
                return MagicMock(name=f"database.{name}()")
            return value(*args, **kwargs) if callable(value) else value

        return _call


_MISSING = object()


class _HopCounter:
    """Wrap a module's ``run_sync`` so offloads can be counted exactly.

    Thread identity alone cannot count hops: two sequential ``to_thread`` calls
    usually land on the SAME pooled worker, so "one thread" does not mean "one
    hop". Counting the offloads does.
    """

    def __init__(self, module):
        self._module = module
        self._real = module.run_sync
        self.count = 0

    def __enter__(self):
        async def _counting(fn, *args, **kwargs):
            self.count += 1
            return await self._real(fn, *args, **kwargs)

        self._patch = patch.object(self._module, "run_sync", _counting)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


@pytest.fixture
def loop_thread():
    """The thread the test's event loop runs on — every DB call must land
    somewhere else."""
    return threading.get_ident()


# ═══ automations ════════════════════════════════════════════════════════════


async def test_list_automation_runs_reads_off_the_loop_in_one_hop(loop_thread):
    """Existence check + run listing are contiguous: two queries, one hop."""
    db = _SpyDatabase()
    with patch.object(automations_api, "database", db), _HopCounter(automations_api) as hops:
        await automations_api.list_automation_runs(
            automation_id="auto-1", limit=50, offset=0, token="t"
        )

    assert db.calls == ["get_automation", "get_automation_runs"]
    assert db.threads and loop_thread not in db.threads
    assert hops.count == 1


async def test_approve_run_keeps_read_decide_write_in_one_hop(loop_thread):
    """The pending check and both writes must share ONE offload — split across
    hops, two concurrent approvals could both see "pending" and both resume the
    same run."""
    db = _SpyDatabase(get_automation_approval={"status": "pending", "run_id": "run-1"})
    manager = SimpleNamespace(execute_approved_run=AsyncMock())

    with patch.object(automations_api, "database", db), \
         patch("app.core.automation_manager.automation_manager", manager), \
         _HopCounter(automations_api) as hops:
        await automations_api.approve_run_endpoint(approval_id="ap-1", token="t")

    # read -> decide -> write -> write, then the re-read after the resume await
    assert db.calls == [
        "get_automation_approval",
        "update_automation_approval",
        "update_automation_run",
        "get_automation_approval",
    ]
    assert loop_thread not in db.threads
    # One hop for the claim, one for the post-resume re-read; the resume is a
    # real await and cannot be folded in.
    assert hops.count == 2
    assert manager.execute_approved_run.await_count == 1


async def test_approve_run_rejects_a_settled_approval_from_inside_the_offload(loop_thread):
    """HTTPException raised inside the closure must propagate unchanged — the
    400 must not turn into a 500 just because it crossed a thread."""
    from fastapi import HTTPException

    db = _SpyDatabase(get_automation_approval={"status": "denied", "run_id": "run-1"})
    with patch.object(automations_api, "database", db):
        with pytest.raises(HTTPException) as exc:
            await automations_api.approve_run_endpoint(approval_id="ap-1", token="t")

    assert exc.value.status_code == 400
    assert db.calls == ["get_automation_approval"]  # nothing was written


async def test_enable_automation_writes_off_the_loop_in_one_hop(loop_thread):
    """With no scheduled trigger there is no await at all, so the check, the
    write and the response read are a single offload."""
    db = _SpyDatabase(get_automation={"id": "auto-1", "triggers": [{"type": "webhook"}]})
    with patch.object(automations_api, "database", db), \
         patch("app.core.automation_manager.automation_manager", SimpleNamespace()), \
         _HopCounter(automations_api) as hops:
        result = await automations_api.enable_automation(automation_id="auto-1", token="t")

    assert db.calls == ["get_automation", "update_automation", "get_automation"]
    assert loop_thread not in db.threads
    assert hops.count == 1
    assert result["id"] == "auto-1"


# ═══ connectors ═════════════════════════════════════════════════════════════


async def test_list_connectors_masks_off_the_loop_in_one_hop(loop_thread):
    """Masking decrypts each row, so it belongs in the hop with the query, not
    on the loop after it."""
    rows = [{"id": "c1", "type": "telegram", "config": {"token": "secret"}}]
    db = _SpyDatabase(get_all_connectors=rows)
    mask_threads = []

    def _mask(config, ctype):
        mask_threads.append(threading.get_ident())
        return {"token": "****"}

    with patch.object(connectors_api, "database", db), \
         patch("app.core.connector_service.mask_config", _mask), \
         _HopCounter(connectors_api) as hops:
        result = await connectors_api.list_connectors(token="t")

    assert result[0]["config"] == {"token": "****"}
    assert loop_thread not in db.threads
    assert mask_threads and loop_thread not in mask_threads
    assert hops.count == 1


async def test_activate_connector_claims_the_runner_in_one_hop(loop_thread):
    """The existence check, the is_active write and the runner claim are a
    read-decide-write: they must not be split around the start await."""
    db = _SpyDatabase(
        get_connector={"id": "c1", "type": "telegram", "config": {}, "runner_id": None},
        claim_connector_runner=True,
    )
    manager = SimpleNamespace(start_connector=AsyncMock(return_value=True))

    with patch.object(connectors_api, "database", db), \
         patch("app.core.connectors.manager.connector_manager", manager), \
         patch("app.core.connector_service.mask_config", lambda c, t: c), \
         _HopCounter(connectors_api) as hops:
        await connectors_api.activate_connector(connector_id="c1", token="t")

    assert db.calls == [
        "get_connector",
        "update_connector",
        "claim_connector_runner",  # inside _owns_local_runner, same hop
        "get_connector",           # after the start await
    ]
    assert loop_thread not in db.threads
    assert hops.count == 2
    assert manager.start_connector.await_count == 1


async def test_whatsapp_webhook_reads_every_connector_row_in_one_hop(loop_thread):
    """The per-connector row read used to sit inside the dispatch loop: N
    connectors meant N blocking reads interleaved with the handler awaits."""
    handled = []

    def _running(_type):
        return [
            (f"c{i}", SimpleNamespace(
                handle_webhook_payload=AsyncMock(side_effect=lambda p, i=i: handled.append(i))
            ))
            for i in range(4)
        ]

    db = _SpyDatabase(get_connector={"config": {}})
    manager = SimpleNamespace(get_running_connectors=_running)
    request = MagicMock()
    request.body = AsyncMock(return_value=b"{}")
    request.json = AsyncMock(return_value={"entry": [{}]})
    request.headers = {}

    with patch.object(connectors_api, "database", db), \
         patch("app.core.connectors.manager.connector_manager", manager), \
         patch("app.core.connector_service.decrypt_config", lambda c, t: {}), \
         _HopCounter(connectors_api) as hops:
        result = await connectors_api.whatsapp_webhook_receive(request)

    assert result == {"status": "ok"}
    assert handled == [0, 1, 2, 3], "every running connector still gets the payload"
    assert db.calls == ["get_connector"] * 4
    assert loop_thread not in db.threads
    assert hops.count == 1, "four row reads, one hop"


# ═══ import / export ════════════════════════════════════════════════════════


def _export_payload(n):
    return ExportData(
        export_type="all",
        exported_at="2026-08-28T00:00:00Z",
        subagents=[
            ExportedSubagent(id=f"a{i}", name=f"a{i}", description="d", prompt="p")
            for i in range(n)
        ],
        profiles=[ExportedProfile(id=f"p{i}", name=f"p{i}") for i in range(n)],
    )


async def test_import_json_runs_the_whole_bulk_loop_in_one_hop(loop_thread):
    """The worst case in this module: every item costs a read plus a write, so
    on the loop an import of N items stalled the worker for 2N queries. One
    offload for the whole loop — not one per item."""
    db = _SpyDatabase(
        get_subagent=None, get_profile=None, get_all_subagents=[]
    )

    with patch.object(ie_api, "database", db), _HopCounter(ie_api) as hops:
        result = await ie_api.import_data_json(
            data=_export_payload(5), overwrite_existing=False, token="t"
        )

    assert result.subagents_imported == 5
    assert result.profiles_imported == 5
    assert result.success is True
    assert db.calls.count("create_subagent") == 5
    assert db.calls.count("create_profile") == 5
    assert len(db.threads) == 21  # 5*(get+create) x2 + one get_all_subagents
    assert loop_thread not in db.threads
    assert hops.count == 1


async def test_import_json_per_item_errors_still_land_in_the_result(loop_thread):
    """A failing item is recorded, not raised — the offload must not change
    that."""
    def _boom(*a, **k):
        raise RuntimeError("nope")

    db = _SpyDatabase(get_subagent=None, get_profile=None, get_all_subagents=[],
                      create_subagent=_boom)

    with patch.object(ie_api, "database", db):
        result = await ie_api.import_data_json(
            data=_export_payload(2), overwrite_existing=False, token="t"
        )

    assert result.success is False
    assert len(result.errors) == 2
    assert result.subagents_imported == 0
    assert result.profiles_imported == 2


async def test_import_offload_runs_concurrently_with_the_loop():
    """The point of the offload: while a bulk import is running, other
    coroutines keep running. A ~150 ms import must not stop a 10 ms ticker."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    def _slow_create(*a, **k):
        time.sleep(0.03)

    db = _SpyDatabase(get_subagent=None, get_profile=None, get_all_subagents=[],
                      create_subagent=_slow_create, create_profile=_slow_create)

    with patch.object(ie_api, "database", db):
        task = asyncio.create_task(ticker())
        await ie_api.import_data_json(
            data=_export_payload(3), overwrite_existing=False, token="t"
        )
        task.cancel()

    assert ticks >= 5, f"the event loop was stalled during the import ({ticks} ticks)"


async def test_export_all_reads_both_tables_in_one_hop(loop_thread):
    db = _SpyDatabase(get_all_profiles=[], get_all_subagents=[])
    with patch.object(ie_api, "database", db), _HopCounter(ie_api) as hops:
        await ie_api.export_all(token="t")

    assert db.calls == ["get_all_profiles", "get_all_subagents"]
    assert loop_thread not in db.threads
    assert hops.count == 1


# ═══ hooks ══════════════════════════════════════════════════════════════════


def _request(api_user=None):
    return SimpleNamespace(state=SimpleNamespace(api_user=api_user))


async def test_update_hook_keeps_read_authorise_write_in_one_hop(loop_thread):
    db = _SpyDatabase(get_hook={"id": "h1", "project_id": None})
    with patch.object(hooks_api, "database", db), _HopCounter(hooks_api) as hops:
        await hooks_api.update_hook(
            hook_id="h1",
            hook_data=HookUpdate(enabled=False),
            request=_request(),
            token="t",
        )

    assert db.calls == ["get_hook", "update_hook"]
    assert loop_thread not in db.threads
    assert hops.count == 1


async def test_delete_hook_keeps_read_authorise_delete_in_one_hop(loop_thread):
    db = _SpyDatabase(get_hook={"id": "h1", "project_id": None}, delete_hook=True)
    with patch.object(hooks_api, "database", db), _HopCounter(hooks_api) as hops:
        await hooks_api.delete_hook(hook_id="h1", request=_request(), token="t")

    assert db.calls == ["get_hook", "delete_hook"]
    assert loop_thread not in db.threads
    assert hops.count == 1


async def test_delete_hook_still_403s_for_a_foreign_project(loop_thread):
    """The access check moved inside the closure; it must still deny, and it
    must deny BEFORE the delete."""
    from fastapi import HTTPException

    db = _SpyDatabase(get_hook={"id": "h1", "project_id": "proj-a"}, delete_hook=True)
    with patch.object(hooks_api, "database", db):
        with pytest.raises(HTTPException) as exc:
            await hooks_api.delete_hook(
                hook_id="h1",
                request=_request({"project_id": "proj-b"}),
                token="t",
            )

    assert exc.value.status_code == 403
    assert db.calls == ["get_hook"]


async def test_create_project_hook_checks_the_project_in_the_write_hop(loop_thread):
    db = _SpyDatabase(get_project={"id": "proj-a"})
    from app.core.models import HookCreate

    with patch.object(hooks_api, "database", db), _HopCounter(hooks_api) as hops:
        await hooks_api.create_project_hook(
            project_id="proj-a",
            hook_data=HookCreate(
                name="h", event="PreToolUse", hook_type="bash", command="echo hi"
            ),
            request=_request(),
            token="t",
        )

    assert db.calls == ["get_project", "create_hook"]
    assert loop_thread not in db.threads
    assert hops.count == 1
