"""Pytest fixtures copied across several test files, kept in one place.

A plain module, not a conftest: a test file opts in by importing the fixtures
it wants, e.g. ``from tests.shared_fixtures import pin_runner  # noqa: F401``.
An imported ``autouse`` fixture applies to every test in the importing file.
"""
import pytest

from app.core.config import settings

# The install id ``pin_runner`` gives this process.
LOCAL_ME = "local-me"


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    """Pin ``settings.effective_instance_id`` to ``LOCAL_ME``."""
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: LOCAL_ME)
    )


@pytest.fixture(autouse=True)
def reset_backfill_memo():
    """Drop history_backfill's per-process memo before and after each test."""
    from app.core import history_backfill

    history_backfill._reset_memo()
    yield
    history_backfill._reset_memo()


@pytest.fixture(autouse=True)
def clean_task_registry():
    """Keep the global task_registry isolated per test."""
    from app.core.task_registry import task_registry

    task_registry.reset_all()
    yield
    task_registry.reset_all()


@pytest.fixture(autouse=True)
def foreign_keys_off(test_db):
    """Run on the shared conftest DB with foreign keys off: the importing
    tests seed rows without their FK parents."""
    test_db.execute("PRAGMA foreign_keys = OFF")
