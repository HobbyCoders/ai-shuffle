"""The mobile services and the fal catalog must not touch Postgres from the loop.

``action_queue`` is live: it initialises lazily from every public method and
``app/mcp/phone_tools.py`` drives it, so its loads and saves run inside a turn.
``mobile_device_manager`` and ``proactive_scheduler`` persist the same way.
Each synchronous psycopg2 call made directly from one of their coroutines
stalls every other request on the worker for the duration of the query.

These tests assert by identity: the DB callable must run on a thread that is
not the loop thread, while the loop-bound half of each operation (task
creation, APScheduler job registration) stays where it is.
"""

import threading
from unittest.mock import patch

import pytest

from app.services.action_queue import ActionQueue, SecurityLevel
from app.services.fal_catalog import CatalogModel, FalCatalogService
from app.services.mobile_device_manager import MobileDeviceManager
from app.services.proactive_scheduler import ProactiveScheduler


class _ThreadSpy:
    """Stand in for a DB function, recording the thread each call ran on."""

    def __init__(self, return_value=None):
        self._return_value = return_value
        self.threads: list[int] = []
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.threads.append(threading.get_ident())
        self.calls.append((args, kwargs))
        return self._return_value


@pytest.fixture
def loop_thread():
    return threading.get_ident()


# ═══ action_queue — live, reached through the phone MCP tools ═══════════════


class TestActionQueueOffload:
    async def test_initialize_loads_actions_off_the_loop(self, loop_thread):
        """The load is offloaded; the cleanup task is still created ON the loop
        (``asyncio.create_task`` from a worker thread has no running loop)."""
        spy = _ThreadSpy(return_value=[])
        queue = ActionQueue()
        with patch("app.services.action_queue.database.get_mobile_actions", spy):
            await queue.initialize()
        try:
            assert spy.threads and loop_thread not in spy.threads
            assert queue._cleanup_task is not None
        finally:
            await queue.shutdown()

    async def test_queue_action_saves_off_the_loop(self, loop_thread):
        save = _ThreadSpy()
        queue = ActionQueue()
        queue._initialized = True  # skip initialize's cleanup task
        with patch("app.services.action_queue.database.save_mobile_action", save):
            action_id = await queue.queue_action(
                device_id="dev-1",
                action_type="open_app",
                params={"app": "notes"},
                security_level=SecurityLevel.MEDIUM,
            )

        assert action_id in queue._actions
        assert save.threads and loop_thread not in save.threads

    async def test_a_db_failure_is_still_swallowed_by_the_same_warning(self):
        """Error handling must not drift: a save failure degrades to a warning
        and the in-memory action survives, exactly as before."""
        def _boom(**kwargs):
            raise RuntimeError("db down")

        queue = ActionQueue()
        queue._initialized = True
        with patch("app.services.action_queue.database.save_mobile_action", _boom):
            action_id = await queue.queue_action(
                device_id="dev-1", action_type="open_app", params={}
            )
        assert action_id in queue._actions


# ═══ mobile_device_manager ══════════════════════════════════════════════════


class TestMobileDeviceManagerOffload:
    async def test_initialize_loads_devices_off_the_loop(self, loop_thread):
        spy = _ThreadSpy(return_value=[])
        manager = MobileDeviceManager()
        with patch(
            "app.services.mobile_device_manager.database.get_mobile_devices", spy
        ):
            await manager.initialize()

        assert spy.threads and loop_thread not in spy.threads
        assert manager._initialized is True

    async def test_register_device_saves_off_the_loop(self, loop_thread):
        save = _ThreadSpy()
        manager = MobileDeviceManager()
        manager._initialized = True
        with patch(
            "app.services.mobile_device_manager.database.save_mobile_device", save
        ):
            result = await manager.register_device(
                {"name": "Phone", "platform": "ios"}
            )

        assert result["name"] == "Phone"
        assert save.threads and loop_thread not in save.threads


# ═══ proactive_scheduler — dead wiring today, but it would block if wired ═══


class TestProactiveSchedulerOffload:
    async def test_load_schedules_query_runs_off_the_loop(self, loop_thread):
        """Only the query moves: the decode loop registers APScheduler jobs
        against the scheduler bound to THIS loop, so it stays on the loop."""
        spy = _ThreadSpy(return_value=[])
        scheduler = ProactiveScheduler()
        with patch(
            "app.services.proactive_scheduler.database.get_mobile_schedules", spy
        ):
            await scheduler._load_schedules_from_db()

        assert spy.threads and loop_thread not in spy.threads


# ═══ fal_catalog — a get_db() write the blocking-DB scanner cannot see ══════


class TestFalCatalogOffload:
    async def test_refresh_persists_off_the_loop(self, loop_thread):
        """``_persist_to_db`` is a DELETE plus one INSERT per model (~600 rows)
        in one transaction. It reaches the loop through ``get_db()`` rather than
        a ``database.*`` call, so ``scan_blocking_db.py`` does not report it —
        but ``refresh()`` is awaited from an MCP tool mid-turn, so running it
        inline stalled every other request for the whole write.
        """
        models = [
            CatalogModel(
                endpoint_id="fal-ai/x",
                display_name="X",
                category="text-to-image",
                description="",
            )
        ]
        service = FalCatalogService()
        persist = _ThreadSpy()

        async def _fetch(api_key=None):
            return models

        with patch.object(service, "_fetch_all_models", _fetch), \
             patch.object(service, "_persist_to_db", persist):
            count = await service.refresh()

        assert count == 1
        assert persist.threads and loop_thread not in persist.threads
