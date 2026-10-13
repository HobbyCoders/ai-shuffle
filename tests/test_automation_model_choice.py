"""
An automation's own model choice.

Agents do not pin a model; a session gets one when it is created. An
automation mints its run sessions itself, so it stores the choice (``model`` /
``provider_id``) and its runs stamp it on the session row exactly like the
composer's pick on a new session. Without a choice, runs use the app default,
like a new chat with no pick; an agent row that still carries a legacy saved
model keeps it, and the API reports that as ``agent``. A choice that is no
longer available fails the run with a clear reason.
"""

import json

import pytest
from unittest.mock import patch

from app.core.automation_manager import AutomationManager
from app.core import automation_service
from app.core.automation_service import (
    describe_automation_model,
    model_choice_errors_for_run,
    normalize_model_choice,
    resolve_automation_model,
)
from app.mcp.config_automations import _create_automation_tools

from tests.test_automation_manager import _session_run_env


def _provider(db, provider_id="p-open", models=("gpt-x", "gpt-y"), enabled=True):
    return db.create_model_provider(
        provider_id, "Open Provider", "openai", "https://example.invalid",
        models=[{"id": m, "label": m} for m in models],
        enabled=enabled,
    )


def _automation(db, automation_id="auto-m", **kwargs):
    return db.create_automation(
        automation_id=automation_id,
        name="Model test",
        trigger_type="webhook",
        trigger_config={},
        profile_id="default",
        prompt="hello",
        **kwargs,
    )


# =============================================================================
# Storage
# =============================================================================


class TestStorage:
    def test_columns_round_trip_and_default_null(self, mock_db):
        from app.db import database

        plain = _automation(database, "auto-plain")
        assert plain["model"] is None
        assert plain["provider_id"] is None

        chosen = _automation(database, "auto-chosen", model="prov:p-open:gpt-x", provider_id="p-open")
        assert chosen["model"] == "prov:p-open:gpt-x"
        assert chosen["provider_id"] == "p-open"

        updated = database.update_automation("auto-chosen", model="opus", provider_id=None)
        assert updated["model"] == "opus"
        assert updated["provider_id"] is None

    def test_migration_adds_columns_to_old_table(self, mock_db):
        """An install whose automations table predates v92 gains both columns."""
        from app.db import database
        from app.db.base import column_exists, get_db

        with get_db() as conn:
            cursor = conn.cursor()
            for column in ("model", "provider_id"):
                cursor.execute(f"ALTER TABLE automations DROP COLUMN {column}")
            assert not column_exists(cursor, "automations", "model")
            database._run_column_migrations(cursor)
            assert column_exists(cursor, "automations", "model")
            assert column_exists(cursor, "automations", "provider_id")

    def test_synced_collection_carries_the_choice(self):
        from app.network.policy import COLLECTIONS

        fields = COLLECTIONS["automations"].fields
        assert "model" in fields and "provider_id" in fields


# =============================================================================
# Validation
# =============================================================================


class TestNormalize:
    def test_no_choice(self, mock_db):
        assert normalize_model_choice(None, None) == (None, None)
        assert normalize_model_choice("  ", "") == (None, None)

    def test_provider_without_model_is_refused(self, mock_db):
        with pytest.raises(ValueError, match="Pick a model"):
            normalize_model_choice("", "p-open")

    def test_native_claude_pick(self, mock_db):
        assert normalize_model_choice("opus") == ("opus", None)

    def test_bare_non_claude_id_needs_a_provider(self, mock_db):
        with pytest.raises(ValueError, match="not a Claude model"):
            normalize_model_choice("gpt-x")

    def test_provider_pick_as_composite_or_pair(self, mock_db):
        from app.db import database

        _provider(database)
        assert normalize_model_choice("prov:p-open:gpt-x") == ("prov:p-open:gpt-x", "p-open")
        assert normalize_model_choice("gpt-y", "p-open") == ("prov:p-open:gpt-y", "p-open")

    def test_provider_mismatch_is_refused(self, mock_db):
        from app.db import database

        _provider(database)
        with pytest.raises(ValueError, match="belongs to provider"):
            normalize_model_choice("prov:p-open:gpt-x", "p-other")

    def test_unknown_provider_or_model_is_refused(self, mock_db):
        from app.db import database

        _provider(database)
        with pytest.raises(ValueError, match="has been removed"):
            normalize_model_choice("prov:p-gone:gpt-x")
        with pytest.raises(ValueError):
            normalize_model_choice("prov:p-open:not-curated")

    def test_resending_the_stored_choice_skips_the_availability_check(self, mock_db):
        """The edit form sends every field; a gone model must not block a rename."""
        assert normalize_model_choice(
            "prov:p-gone:gpt-x", stored=("prov:p-gone:gpt-x",)
        ) == ("prov:p-gone:gpt-x", "p-gone")
        with pytest.raises(ValueError):
            normalize_model_choice("prov:p-gone:gpt-y", stored=("prov:p-gone:gpt-x",))


# =============================================================================
# Effective model reporting
# =============================================================================


class TestResolve:
    def test_explicit_choice(self, mock_db):
        from app.db import database

        _provider(database)
        resolved = resolve_automation_model({"model": "prov:p-open:gpt-x", "profile_id": "default"})
        assert resolved == {
            "selection": "prov:p-open:gpt-x", "model": "gpt-x", "provider_id": "p-open",
            "source": "automation", "error": None,
        }

    def test_step_choice_beats_automation(self, mock_db):
        resolved = resolve_automation_model({"model": "sonnet"}, {"model": "opus"})
        assert resolved["selection"] == "opus"
        assert resolved["source"] == "step"

    def test_step_without_choice_inherits_automation(self, mock_db):
        resolved = resolve_automation_model({"model": "sonnet"}, {"profile_id": "other"})
        assert resolved["selection"] == "sonnet"
        assert resolved["source"] == "automation"

    def test_fallback_to_agent_saved_model(self, mock_db):
        from app.db import database

        database.create_profile(
            profile_id="legacy", name="Legacy", description=None, config={"model": "opus"},
        )
        resolved = resolve_automation_model({"profile_id": "legacy"})
        assert resolved["selection"] is None
        assert resolved["model"] == "opus"
        assert resolved["source"] == "agent"

    def test_fallback_to_agent_provider_default(self, mock_db):
        from app.db import database

        _provider(database)
        database.update_model_provider("p-open", default_model="gpt-y")
        database.create_profile(
            profile_id="prov-agent", name="Prov", description=None, config={"provider": "p-open"},
        )
        resolved = resolve_automation_model({"profile_id": "prov-agent"})
        assert (resolved["model"], resolved["provider_id"], resolved["source"]) == ("gpt-y", "p-open", "agent")

    def test_fallback_to_app_default(self, mock_db):
        from app.core.model_registry import DEFAULT_MODEL
        from app.db import database

        database.create_profile(profile_id="bare", name="Bare", description=None, config={})
        resolved = resolve_automation_model({"profile_id": "bare"})
        assert resolved["model"] == DEFAULT_MODEL
        assert resolved["provider_id"] is None
        assert resolved["source"] == "default"

    def test_no_agent_or_missing_agent_is_the_app_default(self, mock_db):
        from app.core.model_registry import DEFAULT_MODEL

        for automation in ({}, {"profile_id": "deleted-agent"}):
            resolved = resolve_automation_model(automation)
            assert (resolved["model"], resolved["provider_id"], resolved["source"]) == (
                DEFAULT_MODEL, None, "default")

    def test_description_names_the_model_and_why(self, mock_db):
        from app.core.model_registry import DEFAULT_MODEL
        from app.db import database

        database.create_profile(profile_id="bare", name="Bare", description=None, config={})
        database.create_profile(
            profile_id="legacy", name="Legacy", description=None, config={"model": "opus"},
        )
        assert describe_automation_model({"profile_id": "bare"}) == f"{DEFAULT_MODEL}, the app default"
        assert describe_automation_model({"profile_id": "legacy"}) == "opus, saved on this agent (legacy)"
        assert describe_automation_model(
            {"profile_id": "legacy", "model": "haiku"}) == "haiku, chosen for this automation"
        assert "no longer available" in describe_automation_model({"model": "prov:p-gone:gpt-x"})

    def test_removed_provider_reports_error(self, mock_db):
        resolved = resolve_automation_model({"model": "prov:p-gone:gpt-x"})
        assert resolved["selection"] == "prov:p-gone:gpt-x"
        assert "no longer available" in resolved["error"]
        assert "has been removed" in resolved["error"] and "p-gone" not in resolved["error"]

    def test_model_dropped_from_list_reports_error(self, mock_db):
        from app.db import database

        _provider(database, models=("gpt-y",))
        assert "no longer available" in resolve_automation_model({"model": "prov:p-open:gpt-x"})["error"]

    def test_disabled_provider_reports_error(self, mock_db):
        from app.db import database

        _provider(database, enabled=False)
        assert "disabled" in resolve_automation_model({"model": "prov:p-open:gpt-x"})["error"]

    def test_run_preflight_names_the_failing_step(self, mock_db):
        automation = {
            "prompt": "",
            "action_steps": [
                {"type": "delay", "seconds": 1},
                {"type": "send_prompt", "prompt": "x", "model": "prov:p-gone:gpt-x"},
            ],
        }
        error = model_choice_errors_for_run(automation)
        assert error.startswith("Action step 2:")
        assert model_choice_errors_for_run({"prompt": "x", "model": "opus"}) is None


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
    async def test_explicit_choice_is_stamped_on_the_run_session(self, mock_db):
        from app.db import database

        _provider(database)
        stack, db, submit, _wait, _int = _session_run_env()
        with stack:
            await AutomationManager()._execute_run(
                self._automation(model="prov:p-open:gpt-x", provider_id="p-open"),
                "run-m1", "do it", {},
            )
        kwargs = db.create_session.call_args.kwargs
        assert kwargs["model"] == "prov:p-open:gpt-x"
        assert kwargs["derive_provider_from_profile"] is False
        # The turn carries no model override: every turn resolves from the row.
        assert "model" not in submit.call_args.kwargs["overrides"]

    @pytest.mark.asyncio
    async def test_native_choice_stays_native(self, mock_db):
        stack, db, _submit, _wait, _int = _session_run_env()
        with stack:
            await AutomationManager()._execute_run(
                self._automation(model="opus"), "run-m2", "do it", {},
            )
        kwargs = db.create_session.call_args.kwargs
        assert kwargs["model"] == "opus"
        assert kwargs["derive_provider_from_profile"] is False

    @pytest.mark.asyncio
    async def test_no_choice_keeps_the_old_unbound_session(self, mock_db):
        stack, db, _submit, _wait, _int = _session_run_env()
        with stack:
            await AutomationManager()._execute_run(self._automation(), "run-m3", "do it", {})
        kwargs = db.create_session.call_args.kwargs
        assert kwargs["model"] is None
        assert kwargs["derive_provider_from_profile"] is True

    @pytest.mark.asyncio
    async def test_stamped_session_resolves_to_the_choice_every_turn(self, mock_db):
        """The real create_session + query resolution: the row decides the model."""
        from app.core.query_options import build_options_from_profile
        from app.db import database

        _provider(database)
        database.create_profile(
            profile_id="legacy", name="Legacy", description=None, config={"model": "opus"},
        )
        database.create_session(
            session_id="s-auto", profile_id="legacy", automation_id=None,
            model="prov:p-open:gpt-x", derive_provider_from_profile=False,
        )
        row = database.get_session("s-auto")
        assert row["provider_id"] == "p-open"
        options, _ = build_options_from_profile(
            database.get_profile("legacy"), overrides={"max_turns": 2}, session_id="s-auto",
        )
        # The agent's legacy "opus" does not leak in: the row's pick wins.
        assert options.model == "gpt-x"

    @pytest.mark.asyncio
    async def test_unbound_run_on_a_plain_agent_runs_the_app_default(self, mock_db):
        """The real create_session + query resolution, as a new chat with no pick."""
        from app.core.model_registry import DEFAULT_MODEL
        from app.core.query_options import build_options_from_profile
        from app.db import database

        database.create_profile(profile_id="bare", name="Bare", description=None, config={})
        database.create_session(session_id="s-plain", profile_id="bare", automation_id=None)
        assert database.get_session("s-plain")["provider_id"] is None
        options, _ = build_options_from_profile(
            database.get_profile("bare"), overrides={"max_turns": 2}, session_id="s-plain",
        )
        assert options.model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_unbound_run_keeps_a_legacy_agent_model(self, mock_db):
        """No silent change: an agent row that still carries a saved model and
        provider keeps binding unbound runs, and the report says so."""
        from app.core.query_options import build_options_from_profile
        from app.db import database

        _provider(database)
        database.create_profile(
            profile_id="legacy", name="Legacy", description=None,
            config={"model": "gpt-y", "provider": "p-open"},
        )
        database.create_session(session_id="s-legacy", profile_id="legacy", automation_id=None)
        assert database.get_session("s-legacy")["provider_id"] == "p-open"
        options, _ = build_options_from_profile(
            database.get_profile("legacy"), overrides={"max_turns": 2}, session_id="s-legacy",
        )
        assert options.model == "gpt-y"
        resolved = resolve_automation_model({"profile_id": "legacy"})
        assert (resolved["model"], resolved["provider_id"], resolved["source"]) == (
            "gpt-y", "p-open", "agent")

    @pytest.mark.asyncio
    async def test_unavailable_choice_fails_the_run_with_a_clear_error(self, mock_db, mock_encryption):
        from app.db import database

        _automation(database, "auto-gone", model="prov:p-gone:gpt-x", provider_id="p-gone")
        database.update_automation("auto-gone", is_enabled=True)
        with patch("app.core.turn_manager.submit_async") as submit, \
                patch("app.db.sessions.create_session") as create:
            await AutomationManager()._execute("auto-gone", {"trigger": "manual"})
        submit.assert_not_called()
        create.assert_not_called()
        runs = database.get_automation_runs("auto-gone")
        assert runs and runs[0]["status"] == "failed"
        assert "no longer available" in runs[0]["error"]
        assert "no longer available" in database.get_automation("auto-gone")["last_error"]


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

    def test_create_with_model_reports_it(self, authenticated_client, mock_encryption):
        from app.db import database

        _provider(database)
        response = authenticated_client.post(
            "/api/v1/automations", json=self._body(model="gpt-x", provider_id="p-open"),
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["model"] == "prov:p-open:gpt-x"
        assert data["provider_id"] == "p-open"
        assert data["effective_model"] == "gpt-x"
        assert data["effective_provider_id"] == "p-open"
        assert data["model_source"] == "automation"
        assert data["model_error"] is None

    def test_create_rejects_unavailable_model(self, authenticated_client, mock_encryption):
        response = authenticated_client.post(
            "/api/v1/automations", json=self._body(model="prov:p-gone:gpt-x"),
        )
        assert response.status_code == 400
        assert "has been removed" in response.json()["detail"]

    def test_list_reports_fallback_for_old_automations(self, authenticated_client, mock_encryption):
        from app.core.model_registry import DEFAULT_MODEL
        from app.db import database

        database.create_profile(profile_id="bare", name="Bare", description=None, config={})
        database.create_automation(
            automation_id="auto-old", name="Old", trigger_type="webhook",
            trigger_config={}, profile_id="bare", prompt="hi",
        )
        rows = authenticated_client.get("/api/v1/automations").json()
        old = next(r for r in rows if r["id"] == "auto-old")
        assert old["model"] is None
        assert old["model_source"] == "default"
        assert old["effective_model"] == DEFAULT_MODEL

    def test_update_sets_clears_and_reports_gone_model(self, authenticated_client, mock_encryption):
        from app.db import database

        _provider(database)
        database.create_profile(profile_id="bare", name="Bare", description=None, config={})
        created = authenticated_client.post(
            "/api/v1/automations", json=self._body(model="prov:p-open:gpt-x", profile_id="bare"),
        ).json()
        auto_id = created["id"]

        # Provider removed: the read says so, and an unrelated edit that resends
        # the stored choice still saves.
        database.delete_model_provider("p-open")
        detail = authenticated_client.get(f"/api/v1/automations/{auto_id}").json()
        assert "no longer available" in detail["model_error"]
        renamed = authenticated_client.patch(
            f"/api/v1/automations/{auto_id}",
            json={"name": "Renamed", "model": "prov:p-open:gpt-x"},
        )
        assert renamed.status_code == 200, renamed.text

        switched = authenticated_client.patch(f"/api/v1/automations/{auto_id}", json={"model": "opus"})
        assert switched.status_code == 200
        assert switched.json()["model"] == "opus"
        assert switched.json()["provider_id"] is None

        cleared = authenticated_client.patch(f"/api/v1/automations/{auto_id}", json={"model": None})
        assert cleared.status_code == 200
        assert cleared.json()["model"] is None
        assert cleared.json()["model_source"] == "default"

    def test_step_model_is_validated(self, authenticated_client, mock_encryption):
        response = authenticated_client.post(
            "/api/v1/automations",
            json=self._body(action_steps=[{"type": "send_prompt", "prompt": "x", "model": "gpt-x"}]),
        )
        assert response.status_code == 400
        assert "Action step 1" in response.json()["detail"]


# =============================================================================
# App-config tools
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
    async def test_create_and_update_automation_take_a_model(self, mock_db, mock_encryption):
        from app.db import database

        _provider(database)
        database.create_profile(profile_id="bare", name="Bare", description=None, config={})
        tools = _create_automation_tools(is_admin=True, project_id=None)
        create = _get_tool(tools, "createAutomation")
        with patch.object(automation_service, "logger"):
            result = await create.handler({
                "name": "T", "trigger_type": "webhook", "prompt": "hi",
                "profile_id": "bare", "model": "gpt-x", "provider_id": "p-open",
                "enabled": False,
            })
        payload = json.loads(_text(result))
        assert payload["model"] == "prov:p-open:gpt-x"
        assert payload["runs_on"] == "gpt-x on provider 'p-open', chosen for this automation"
        auto_id = payload["automation_id"]

        update = _get_tool(tools, "updateAutomation")
        bad = await update.handler({"automation_id": auto_id, "model": "gpt-x"})
        assert "not a Claude model" in _text(bad)
        cleared = json.loads(_text(await update.handler({"automation_id": auto_id, "model": ""})))
        assert database.get_automation(auto_id)["model"] is None
        assert cleared["runs_on"].endswith(", the app default")

    @pytest.mark.asyncio
    async def test_create_automation_without_a_model_reports_the_app_default(self, mock_db, mock_encryption):
        from app.core.model_registry import DEFAULT_MODEL
        from app.db import database

        database.create_profile(profile_id="bare", name="Bare", description=None, config={})
        tools = _create_automation_tools(is_admin=True, project_id=None)
        create = _get_tool(tools, "createAutomation")
        result = await create.handler({
            "name": "T", "trigger_type": "webhook", "prompt": "hi",
            "profile_id": "bare", "enabled": False,
        })
        payload = json.loads(_text(result))
        assert payload["model"] is None
        assert payload["runs_on"] == f"{DEFAULT_MODEL}, the app default"

        # The test fixture's "default" agent still carries a legacy saved model:
        # the result names it instead of calling it the app default.
        legacy = json.loads(_text(await create.handler({
            "name": "L", "trigger_type": "webhook", "prompt": "hi",
            "profile_id": "default", "enabled": False,
        })))
        assert legacy["runs_on"].endswith(", saved on this agent (legacy)")

    @pytest.mark.asyncio
    async def test_cron_create_takes_a_model(self, mock_db):
        import app.mcp.inproc as sdk
        from app.core.automation_manager import automation_manager
        from app.db import automations as auto_db
        from app.mcp import builtin_tools

        captured = {}
        real = sdk.create_tool_server

        def spy(name, version, tools):
            captured["tools"] = tools
            return real(name=name, version=version, tools=tools)

        from app.db import database

        database.create_profile(profile_id="bare", name="Bare", description=None, config={})
        with patch.object(sdk, "create_tool_server", side_effect=spy):
            builtin_tools.create_builtin_tools_server(
                session_id="s-cron", profile_id="bare", is_admin=True, cwd="/tmp",
            )
        cron_create = {t.name: t for t in captured["tools"]}["CronCreate"]

        with patch.object(automation_manager, "schedule_automation"):
            bad = await cron_create.handler(
                {"name": "c", "expression": "0 9 * * *", "prompt": "p", "model": "gpt-x"})
            assert bad.get("isError") is True
            ok = await cron_create.handler(
                {"name": "c", "expression": "0 9 * * *", "prompt": "p", "model": "opus"})
            plain = await cron_create.handler(
                {"name": "d", "expression": "0 9 * * *", "prompt": "p"})
        assert not ok.get("isError")
        assert json.loads(_text(ok))["runs_on"] == "opus, chosen for this automation"
        assert json.loads(_text(plain))["runs_on"].endswith(", the app default")
        rows = auto_db.get_all_automations()
        assert sorted(r["model"] or "" for r in rows) == ["", "opus"]
