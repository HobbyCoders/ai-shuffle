"""The fal catalog must not touch Postgres from the loop.

Each synchronous psycopg2 call made directly from one of its coroutines
stalls every other request on the worker for the duration of the query.

These tests assert by identity: the DB callable must run on a thread that is
not the loop thread, while the loop-bound half of each operation (task
creation, APScheduler job registration) stays where it is.
"""

import threading
from unittest.mock import patch

import pytest

from app.services.fal_catalog import CatalogModel, FalCatalogService


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
