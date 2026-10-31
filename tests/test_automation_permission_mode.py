"""
An automation's own permission mode.

Nobody watches an automation run, so a run in Ask can't get its tools
approved. Each automation stores a ``permission_mode`` (the composer's modes:
``default`` = Ask, ``acceptEdits``, ``auto``, ``bypassPermissions``). New
automations default to Bypass on every creation path; an explicit choice wins.
Rows older than the column are NULL and keep running exactly as before: the
agent's saved mode, else Ask. The run session is minted with the mode as its
binding, so every successor turn resolves it from the row.
"""

import json

import pytest
from unittest.mock import AsyncMock, patch

from app.core.automation_manager import AutomationManager
from app.core.automation_service import (
    DEFAULT_AUTOMATION_PERMISSION_MODE,
    automation_run_permission_mode,
    normalize_permission_mode_choice,
    resolve_automation_permission_mode,
)
from app.mcp.config_automations import _create_automation_tools

from tests.test_automation_manager import (  # noqa: F401 - pytest fixtures
    _WEBHOOK_NO_SECRET,
    _make_automation,
    _session_run_env,
    manager,
    runner,
)


def _automation(db, automation_id="auto-p", **kwargs):
    return db.create_automation(
        automation_id=automation_id,
        name="Permission test",
        trigger_type="webhook",
        trigger_config={},
        profile_id=kwargs.pop("profile_id", "default"),
        prompt="hello",
        **kwargs,
    )


def _bare_agent(db, profile_id="bare", **config):
    return db.create_profile(profile_id=profile_id, name=profile_id.title(), description=None, config=config)


# =============================================================================
# Storage
# =============================================================================


class TestStorage:
    def test_column_round_trips_and_stays_null_at_the_db_layer(self, mock_db):
        """The DB layer stores what it is given; the Bypass default lives in
        the service so a raw insert (sync, old code) never invents a mode."""
        from app.db import database

        assert _automation(database, "auto-null")["permission_mode"] is None
        chosen = _automation(database, "auto-edits", permission_mode="acceptEdits")
        assert chosen["permission_mode"] == "acceptEdits"
        updated = database.update_automation("auto-edits", permission_mode="bypassPermissions")
        assert updated["permission_mode"] == "bypassPermissions"

    def test_migration_adds_the_column_to_an_existing_table(self, mock_db):
        """An install whose automations table predates the column gains it, and
        its existing rows read NULL (no backfill)."""
        from app.db import database
        from app.db.base import column_exists, get_db

        _automation(database, "auto-old")
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("ALTER TABLE automations DROP COLUMN permission_mode")
            assert not column_exists(cursor, "automations", "permission_mode")
            database._run_column_migrations(cursor)
            assert column_exists(cursor, "automations", "permission_mode")
            # Running again is a no-op, not an error.
            database._run_column_migrations(cursor)
        assert database.get_automation("auto-old")["permission_mode"] is None

    def test_synced_collection_carries_the_mode(self):
        from app.network.policy import COLLECTIONS

        assert "permission_mode" in COLLECTIONS["automations"].fields


# =============================================================================
# Validation and resolution
# =============================================================================


class TestNormalize:
    def test_no_choice_takes_the_default(self):
        assert normalize_permission_mode_choice(None) is None
        assert normalize_permission_mode_choice("") is None
        assert normalize_permission_mode_choice(
            None, default=DEFAULT_AUTOMATION_PERMISSION_MODE) == "bypassPermissions"

    @pytest.mark.parametrize("mode", ["default", "acceptEdits", "auto", "bypassPermissions"])
    def test_composer_modes_are_accepted(self, mode):
        assert normalize_permission_mode_choice(mode, default="bypassPermissions") == mode

    def test_plan_is_stored_as_ask(self):
        """Plan is a turn-scoped composer toggle, not a binding; an unattended
        run in plan mode would never act, so it is stored as Ask."""
        assert normalize_permission_mode_choice("plan") == "default"

    @pytest.mark.parametrize("bad", ["yolo", "bypass", "DEFAULT", 3])
    def test_unknown_modes_are_refused(self, bad):
        with pytest.raises(ValueError, match="permission_mode"):
            normalize_permission_mode_choice(bad)


class TestResolve:
    def test_own_mode_wins_over_the_agent(self):
        auto = {"permission_mode": "acceptEdits"}
        assert automation_run_permission_mode(auto, {"permission_mode": "bypassPermissions"}) == "acceptEdits"

    def test_null_row_keeps_the_agent_mode(self):
        assert automation_run_permission_mode({"permission_mode": None}, {"permission_mode": "acceptEdits"}) == "acceptEdits"

    def test_null_row_on_a_plain_agent_leaves_the_session_unbound(self):
        # None = no binding: turn_permission_mode then reads the agent, else Ask.
        assert automation_run_permission_mode({"permission_mode": None}, {}) is None
        assert automation_run_permission_mode({}, None) is None

    def test_a_corrupt_stored_mode_fails_closed_to_ask(self):
        assert automation_run_permission_mode({"permission_mode": "yolo"}, {"permission_mode": "bypassPermissions"}) == "default"

    def test_reports_source_and_effective_mode(self, mock_db):
        from app.db import database

        _bare_agent(database, "bare")
        _bare_agent(database, "edits", permission_mode="acceptEdits")
        assert resolve_automation_permission_mode({"permission_mode": "bypassPermissions", "profile_id": "bare"}) == {
            "effective_permission_mode": "bypassPermissions", "permission_mode_source": "automation"}
        assert resolve_automation_permission_mode({"permission_mode": None, "profile_id": "edits"}) == {
            "effective_permission_mode": "acceptEdits", "permission_mode_source": "agent"}
        assert resolve_automation_permission_mode({"permission_mode": None, "profile_id": "bare"}) == {
            "effective_permission_mode": "default", "permission_mode_source": "default"}


# =============================================================================
# Run path
# =============================================================================


class TestRunPath:
    def _automation(self, **overrides):
        auto = {
            "id": "auto-run", "name": "Digest", "profile_id": "default",
            "project_id": None, "action_steps": [], "run_count": 0,
            "failure_count": 0, "total_cost_used": 0, "max_turns": 3,
            "timeout_seconds": 60, "output_config": {"type": "none"},
        }
        auto.update(overrides)
        return auto

    @pytest.mark.asyncio
    async def test_run_session_is_minted_with_the_automations_mode(self, mock_db):
        stack, db, submit, _wait, _int = _session_run_env()
        db.get_profile.return_value = {"config": {"permission_mode": "acceptEdits"}}
        with stack:
            await AutomationManager()._execute_run(
                self._automation(permission_mode="bypassPermissions"), "run-p1", "do it", {},
            )
        assert db.create_session.call_args.kwargs["permission_mode"] == "bypassPermissions"
        # The turn carries no mode override: it resolves from the session row.
        assert "permission_mode" not in submit.call_args.kwargs["overrides"]

    @pytest.mark.asyncio
    async def test_null_row_run_keeps_the_agent_mode(self, mock_db):
        stack, db, _submit, _wait, _int = _session_run_env()
        db.get_profile.return_value = {"config": {"permission_mode": "acceptEdits"}}
        with stack:
            await AutomationManager()._execute_run(self._automation(), "run-p2", "do it", {})
        assert db.create_session.call_args.kwargs["permission_mode"] == "acceptEdits"

    @pytest.mark.asyncio
    async def test_null_row_run_on_a_plain_agent_runs_in_ask(self, mock_db):
        stack, db, _submit, _wait, _int = _session_run_env()
        db.get_profile.return_value = {"config": {}}
        with stack:
            await AutomationManager()._execute_run(self._automation(), "run-p3", "do it", {})
        assert db.create_session.call_args.kwargs["permission_mode"] is None

    def test_minted_session_resolves_the_mode_every_turn(self, mock_db):
        """The real session row decides: a successor turn (no override, a
        resume) runs in the automation's mode, not the agent's."""
        from app.core.session_modes import turn_permission_mode
        from app.db import database

        _bare_agent(database, "askagent", permission_mode="default")
        database.create_session(
            session_id="s-auto-p", profile_id="askagent", automation_id=None,
            permission_mode="bypassPermissions",
        )
        row = database.get_session("s-auto-p")
        config = database.get_profile("askagent")["config"]
        assert turn_permission_mode({}, row, config) == "bypassPermissions"
        assert turn_permission_mode({}, row, config, resume=True) == "bypassPermissions"

        database.create_session(session_id="s-legacy-p", profile_id="askagent", automation_id=None)
        assert turn_permission_mode({}, database.get_session("s-legacy-p"), config) == "default"

    @pytest.mark.asyncio
    async def test_send_prompt_step_session_gets_the_mode(self, manager, mock_db, runner, monkeypatch):  # noqa: F811
        failover = AsyncMock(return_value={"response": "ok"})
        monkeypatch.setattr("app.core.account_failover.execute_query_with_failover", failover)
        _make_automation(
            "auto-step-p", triggers=_WEBHOOK_NO_SECRET, prompt="Review",
            permission_mode="acceptEdits",
            action_steps=[{"type": "send_prompt", "prompt": "Polish: {{response}}"}],
        )
        _make_automation(
            "auto-step-null", triggers=_WEBHOOK_NO_SECRET, prompt="Review",
            action_steps=[{"type": "send_prompt", "prompt": "Polish: {{response}}"}],
        )
        await manager._execute("auto-step-p", {"trigger": "manual"})
        await manager._execute("auto-step-null", {"trigger": "manual"})
        chosen, legacy = failover.await_args_list
        assert chosen.kwargs["overrides"]["permission_mode"] == "acceptEdits"
        # A NULL row sends no mode: the step session keeps the old resolution.
        assert "permission_mode" not in legacy.kwargs["overrides"]

    @pytest.mark.asyncio
    async def test_execute_query_binds_the_step_mode_on_the_new_session(self, mock_db):
        """The step's new session stores the mode as its binding, so a
        failover retry on it resolves the same mode."""
        from app.core import query_engine

        created = {}

        def fake_create_session(**kwargs):
            created.update(kwargs)
            raise RuntimeError("stop after create")

        with patch.object(query_engine.database, "create_session", side_effect=fake_create_session), \
                patch.object(query_engine.database, "get_profile", return_value={"id": "default", "config": {}}):
            with pytest.raises(RuntimeError, match="stop after create"):
                await query_engine.execute_query(
                    prompt="x", profile_id="default", automation_id="auto-1",
                    overrides={"permission_mode": "bypassPermissions", "max_turns": 1},
                )
        assert created["permission_mode"] == "bypassPermissions"


# =============================================================================
# REST API
# =============================================================================


class TestApi:
    def _body(self, **extra):
        body = {
            "name": "Auto", "trigger_type": "webhook", "trigger_config": {},
            "prompt": "hi", "profile_id": "default",
        }
        body.update(extra)
        return body

    def test_create_defaults_to_bypass(self, authenticated_client, mock_encryption):
        response = authenticated_client.post("/api/v1/automations", json=self._body())
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["permission_mode"] == "bypassPermissions"
        assert data["effective_permission_mode"] == "bypassPermissions"
        assert data["permission_mode_source"] == "automation"

    @pytest.mark.parametrize("mode", ["default", "acceptEdits", "auto"])
    def test_an_explicit_mode_wins(self, authenticated_client, mock_encryption, mode):
        response = authenticated_client.post("/api/v1/automations", json=self._body(permission_mode=mode))
        assert response.status_code == 201, response.text
        assert response.json()["permission_mode"] == mode

    def test_an_invalid_mode_is_refused(self, authenticated_client, mock_encryption):
        response = authenticated_client.post("/api/v1/automations", json=self._body(permission_mode="yolo"))
        assert response.status_code == 400
        assert "permission_mode" in response.json()["detail"]

    def test_old_null_rows_report_the_legacy_resolution(self, authenticated_client, mock_encryption):
        from app.db import database

        _bare_agent(database, "bare")
        _bare_agent(database, "edits", permission_mode="acceptEdits")
        _automation(database, "auto-ask", profile_id="bare")
        _automation(database, "auto-agent", profile_id="edits")
        rows = {r["id"]: r for r in authenticated_client.get("/api/v1/automations").json()}
        assert rows["auto-ask"]["permission_mode"] is None
        assert rows["auto-ask"]["effective_permission_mode"] == "default"
        assert rows["auto-ask"]["permission_mode_source"] == "default"
        assert rows["auto-agent"]["permission_mode"] is None
        assert rows["auto-agent"]["effective_permission_mode"] == "acceptEdits"
        assert rows["auto-agent"]["permission_mode_source"] == "agent"
        # A read never writes the default back.
        assert database.get_automation("auto-ask")["permission_mode"] is None

    def test_update_sets_keeps_and_refuses(self, authenticated_client, mock_encryption):
        from app.db import database

        _bare_agent(database, "bare")
        _automation(database, "auto-up", profile_id="bare")

        # An unrelated edit leaves a NULL row NULL (no silent change).
        renamed = authenticated_client.patch("/api/v1/automations/auto-up", json={"name": "Renamed"})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["permission_mode"] is None

        picked = authenticated_client.patch("/api/v1/automations/auto-up", json={"permission_mode": "bypassPermissions"})
        assert picked.status_code == 200
        assert picked.json()["permission_mode"] == "bypassPermissions"

        # null means unchanged, not "reset to Ask".
        kept = authenticated_client.patch("/api/v1/automations/auto-up", json={"permission_mode": None})
        assert kept.status_code == 200
        assert kept.json()["permission_mode"] == "bypassPermissions"

        planned = authenticated_client.patch("/api/v1/automations/auto-up", json={"permission_mode": "plan"})
        assert planned.json()["permission_mode"] == "default"

        bad = authenticated_client.patch("/api/v1/automations/auto-up", json={"permission_mode": "yolo"})
        assert bad.status_code == 400
        assert database.get_automation("auto-up")["permission_mode"] == "default"


# =============================================================================
# App-config tools and CronCreate
# =============================================================================


def _get_tool(tools, name):
    for t in tools:
        if getattr(t, "name", None) == name:
            return t
    raise AssertionError(f"tool {name!r} not found")


def _text(result):
    return result["content"][0]["text"]


class TestConfigTools:
    @pytest.mark.asyncio
    async def test_create_defaults_to_bypass_and_explicit_wins(self, mock_db, mock_encryption):
        from app.db import database

        _bare_agent(database, "bare")
        tools = _create_automation_tools(is_admin=True, project_id=None)
        create = _get_tool(tools, "createAutomation")
        base = {"trigger_type": "webhook", "prompt": "hi", "profile_id": "bare", "enabled": False}

        plain = json.loads(_text(await create.handler({"name": "A", **base})))
        assert plain["permission_mode"] == "bypassPermissions"
        assert database.get_automation(plain["automation_id"])["permission_mode"] == "bypassPermissions"

        ask = json.loads(_text(await create.handler({"name": "B", "permission_mode": "default", **base})))
        assert database.get_automation(ask["automation_id"])["permission_mode"] == "default"

        bad = await create.handler({"name": "C", "permission_mode": "yolo", **base})
        assert "permission_mode" in _text(bad)

    @pytest.mark.asyncio
    async def test_update_sets_the_mode(self, mock_db, mock_encryption):
        from app.db import database

        _bare_agent(database, "bare")
        _automation(database, "auto-mcp", profile_id="bare")
        tools = _create_automation_tools(is_admin=True, project_id=None)
        update = _get_tool(tools, "updateAutomation")

        await update.handler({"automation_id": "auto-mcp", "name": "Renamed"})
        assert database.get_automation("auto-mcp")["permission_mode"] is None

        await update.handler({"automation_id": "auto-mcp", "permission_mode": "acceptEdits"})
        assert database.get_automation("auto-mcp")["permission_mode"] == "acceptEdits"

        bad = await update.handler({"automation_id": "auto-mcp", "permission_mode": "yolo"})
        assert "permission_mode" in _text(bad)
        assert database.get_automation("auto-mcp")["permission_mode"] == "acceptEdits"

    def test_schemas_offer_the_composer_modes(self):
        tools = _create_automation_tools(is_admin=True, project_id=None)
        for name in ("createAutomation", "updateAutomation"):
            schema = _get_tool(tools, name).input_schema
            props = schema.get("properties", schema)
            assert props["permission_mode"]["enum"] == [
                "default", "acceptEdits", "auto", "bypassPermissions"]

    @pytest.mark.asyncio
    async def test_cron_create_defaults_to_bypass(self, mock_db):
        import app.mcp.inproc as sdk
        from app.core.automation_manager import automation_manager
        from app.db import automations as auto_db
        from app.db import database
        from app.mcp import builtin_tools

        captured = {}
        real = sdk.create_tool_server

        def spy(name, version, tools):
            captured["tools"] = tools
            return real(name=name, version=version, tools=tools)

        _bare_agent(database, "bare")
        with patch.object(sdk, "create_tool_server", side_effect=spy):
            builtin_tools.create_builtin_tools_server(
                session_id="s-cron-p", profile_id="bare", is_admin=True, cwd="/tmp",
            )
        cron_create = {t.name: t for t in captured["tools"]}["CronCreate"]

        with patch.object(automation_manager, "schedule_automation"):
            plain = await cron_create.handler({"name": "c", "expression": "0 9 * * *", "prompt": "p"})
            edits = await cron_create.handler(
                {"name": "d", "expression": "0 9 * * *", "prompt": "p", "permission_mode": "acceptEdits"})
            bad = await cron_create.handler(
                {"name": "e", "expression": "0 9 * * *", "prompt": "p", "permission_mode": "yolo"})
        assert json.loads(_text(plain))["permission_mode"] == "bypassPermissions"
        assert json.loads(_text(edits))["permission_mode"] == "acceptEdits"
        assert bad.get("isError") is True
        modes = sorted(r["permission_mode"] for r in auto_db.get_all_automations())
        assert modes == ["acceptEdits", "bypassPermissions"]
