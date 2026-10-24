"""Retained owner identity/remote-spend storage (the research inbox is retired)."""

from __future__ import annotations
from typing import Any
from pytest import MonkeyPatch
import asyncio
import uuid

import pytest

from app.core import assistant_service as service
from app.core.config import settings
from app.db import assistant as store, database
from app.db.base import get_db, table_exists


def owner_chat(title: Any = "Owner chat") -> Any:
    return database.create_session(str(uuid.uuid4()), "assistant-agent", title=title, is_active=True,
                                   mode="chat", target_instance_id=service.assistant_id())


@pytest.fixture
def assistant_fixture(monkeypatch: MonkeyPatch) -> Any:
    """Ordinary owner identity plus a legacy agent row for compatibility tests."""
    monkeypatch.setattr(settings, "instance_id", "assistant-test")
    database.create_profile("assistant-agent", "Helper", "custom", {"prompt": "Keep custom prompt", "model": "custom-model", "allowed_tools": ["Read"], "permission_mode": "bypassPermissions"}, mcp_tools=["keep-me"])
    # Loaded from an older install: never auto-created by the app any more.
    database.create_profile(
        service.AGENT_ID,
        "Legacy agent",
        "Retired",
        {},
        is_builtin=True,
    )
    with get_db() as conn:
        store.write_account(conn, {"agent_id": "assistant-agent"})
    service.reconcile()
    home = owner_chat()
    from app.core import connector_service, connector_packages
    tokens = iter(("mail", "mail-user"))
    with monkeypatch.context() as package_setup:
        package_setup.setattr(connector_service.secrets, "token_hex", lambda _: next(tokens))
        package_setup.setattr(connector_packages, "require_installed", lambda _: "test-runtime")
        connector = asyncio.run(connector_service.create_connector("Owner mail", "gmail", {}, []))
    database.update_connector(connector["id"], is_active=True)
    database.update_connector_status(connector["id"], "connected")
    return home


def test_schema_keeps_identity_and_remote_spend_but_not_research() -> None:
    with get_db() as conn:
        c = conn.cursor()
        store.create_schema(c)
        store.create_schema(c)
        for table in (
            "assistant_state",
            "assistant_account",
            "assistant_remote_jobs",
            "assistant_remote_costs",
        ):
            assert table_exists(c, table)
        for table in ("assistant_sources", "assistant_runs", "assistant_events"):
            assert not table_exists(c, table)
    assert store.get_state(service.assistant_id()) is None


def test_synced_legacy_sources_cannot_create_background_work(assistant_fixture: Any) -> None:
    with get_db() as conn:
        store.write_account(conn, {"sources": {"conn-mail": True}})
    service.reconcile()
    with get_db() as conn:
        assert not table_exists(conn.cursor(), "assistant_sources")
    from app.db.watches import list_watches

    assert list_watches() == []
