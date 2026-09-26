"""
Tests for automation service — prompt rendering, budget checking, CRUD operations.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.core.automation_service import (
    render_prompt,
    check_budget,
    encrypt_webhook_secret,
    decrypt_webhook_secret,
    get_automation_stats,
)


# =============================================================================
# Prompt Rendering
# =============================================================================


class TestRenderPrompt:
    def test_simple_variable(self):
        template = "Hello {{name}}"
        result = render_prompt(template, {"name": "World"})
        assert result == "Hello World"

    def test_dot_notation(self):
        template = "Session: {{event.session_id}}"
        result = render_prompt(template, {"event": {"session_id": "abc123"}})
        assert result == "Session: abc123"

    def test_missing_variable_left_as_is(self):
        template = "Hello {{missing}}"
        result = render_prompt(template, {"other": "value"})
        assert result == "Hello {{missing}}"

    def test_none_trigger_data(self):
        template = "Hello {{name}}"
        result = render_prompt(template, None)
        assert result == "Hello {{name}}"

    def test_empty_trigger_data(self):
        template = "Hello {{name}}"
        result = render_prompt(template, {})
        assert result == "Hello {{name}}"

    def test_multiple_variables(self):
        template = "{{greeting}} {{name}}!"
        result = render_prompt(template, {"greeting": "Hi", "name": "Alice"})
        assert result == "Hi Alice!"

    def test_nested_dot_notation(self):
        template = "{{a.b.c}}"
        result = render_prompt(template, {"a": {"b": {"c": "deep"}}})
        assert result == "deep"

    def test_partially_resolved(self):
        template = "{{known}} and {{unknown}}"
        result = render_prompt(template, {"known": "yes"})
        assert result == "yes and {{unknown}}"

    def test_whitespace_in_placeholder(self):
        template = "{{ name }}"
        result = render_prompt(template, {"name": "Bob"})
        assert result == "Bob"


# =============================================================================
# Budget Checking
# =============================================================================


class TestCheckBudget:
    def test_no_limits_ok(self, mock_db):
        automation = {"id": "auto-1", "total_cost_used": 0}
        # Mock daily stats
        from app.db import database
        database.create_automation(
            automation_id="auto-1",
            name="Test",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        ok, reason = check_budget(automation)
        assert ok is True
        assert reason == "OK"

    def test_lifetime_budget_exceeded(self, mock_db):
        automation = {
            "id": "auto-1",
            "total_cost_used": 10.0,
            "max_cost_total": 10.0,
        }
        from app.db import database
        database.create_automation(
            automation_id="auto-1",
            name="Test",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        ok, reason = check_budget(automation)
        assert ok is False
        assert "Lifetime budget" in reason

    def test_daily_run_limit(self, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-1",
            name="Test",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        # Create runs that happened today
        database.create_automation_run(
            run_id="run-1", automation_id="auto-1", status="completed",
        )
        automation = {
            "id": "auto-1",
            "total_cost_used": 0,
            "max_runs_per_day": 1,
        }
        ok, reason = check_budget(automation)
        assert ok is False
        assert "Daily run limit" in reason

    def test_budget_under_limit_ok(self, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-1",
            name="Test",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        automation = {
            "id": "auto-1",
            "total_cost_used": 5.0,
            "max_cost_total": 10.0,
        }
        ok, reason = check_budget(automation)
        assert ok is True


# =============================================================================
# Webhook Secret Encryption
# =============================================================================


class TestWebhookSecretEncryption:
    def test_encrypt_webhook_secret(self, mock_encryption):
        config = {"secret": "my-secret"}
        result = encrypt_webhook_secret(config)
        assert result["secret"] == "encrypted:my-secret"

    def test_encrypt_already_encrypted(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            config = {"secret": "encrypted:already"}
            result = encrypt_webhook_secret(config)
            assert result["secret"] == "encrypted:already"

    def test_encrypt_no_secret(self, mock_encryption):
        config = {"other": "value"}
        result = encrypt_webhook_secret(config)
        assert "secret" not in result

    def test_decrypt_webhook_secret(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            config = {"secret": "encrypted:my-secret"}
            result = decrypt_webhook_secret(config)
            assert result["secret"] == "my-secret"

    def test_decrypt_no_secret(self, mock_encryption):
        config = {"other": "value"}
        result = decrypt_webhook_secret(config)
        assert "secret" not in result


# =============================================================================
# CRUD Operations
# =============================================================================


class TestCreateAutomation:
    @pytest.mark.asyncio
    async def test_create_basic(self, mock_db, mock_encryption):
        from app.core.automation_service import create_automation
        from app.db import database

        # Create a profile first
        database.create_profile(
            profile_id="test-profile",
            name="Test Profile",
            description=None,
            config={"model": "claude-sonnet-4-20250514"},
        )

        result = await create_automation(
            name="Test Automation",
            trigger_type="webhook",
            trigger_config={},
            prompt="Say hello",
            profile_id="test-profile",
        )
        assert result is not None
        assert result["name"] == "Test Automation"
        assert result["trigger_type"] == "webhook"

    @pytest.mark.asyncio
    async def test_create_invalid_trigger_type(self, mock_db):
        from app.core.automation_service import create_automation

        with pytest.raises(ValueError, match="Invalid trigger type"):
            await create_automation(
                name="Test",
                trigger_type="invalid_type",
                trigger_config={},
                prompt="test",
                profile_id="test-profile",
            )

    @pytest.mark.asyncio
    async def test_create_invalid_profile(self, mock_db, mock_encryption):
        # mock_encryption: webhook triggers encrypt an auto-generated secret
        # before profile validation, so without it this test only passes when
        # an earlier test happens to initialize the process-global key.
        from app.core.automation_service import create_automation

        with pytest.raises(ValueError, match="Profile not found"):
            await create_automation(
                name="Test",
                trigger_type="webhook",
                trigger_config={},
                prompt="test",
                profile_id="nonexistent",
            )

    @pytest.mark.asyncio
    async def test_create_interval_too_short(self, mock_db):
        from app.core.automation_service import create_automation
        from app.db import database

        database.create_profile(
            profile_id="test-profile",
            name="Test Profile",
            description=None,
            config={"model": "claude-sonnet-4-20250514"},
        )

        with pytest.raises(ValueError, match="at least 60 seconds"):
            await create_automation(
                name="Test",
                trigger_type="interval",
                trigger_config={"seconds": 30},
                prompt="test",
                profile_id="test-profile",
            )

    @pytest.mark.asyncio
    async def test_create_webhook_auto_generates_secret(self, mock_db, mock_encryption):
        from app.core.automation_service import create_automation
        from app.db import database

        database.create_profile(
            profile_id="test-profile",
            name="Test Profile",
            description=None,
            config={"model": "claude-sonnet-4-20250514"},
        )

        result = await create_automation(
            name="Webhook Auto",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="test-profile",
        )
        # trigger_config should now have a secret
        assert result is not None


class TestDeleteAutomation:
    @pytest.mark.asyncio
    async def test_delete_existing(self, mock_db, mock_encryption):
        from app.core.automation_service import create_automation, delete_automation
        from app.db import database

        database.create_profile(
            profile_id="test-profile",
            name="Test Profile",
            description=None,
            config={"model": "claude-sonnet-4-20250514"},
        )

        auto = await create_automation(
            name="Delete Me",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="test-profile",
        )

        result = await delete_automation(auto["id"])
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, mock_db):
        from app.core.automation_service import delete_automation
        result = await delete_automation("nonexistent")
        assert result is False


class TestTestAutomation:
    @pytest.mark.asyncio
    async def test_dry_run(self, mock_db, mock_encryption):
        from app.core.automation_service import create_automation, test_automation
        from app.db import database

        database.create_profile(
            profile_id="test-profile",
            name="Test Profile",
            description=None,
            config={"model": "claude-sonnet-4-20250514"},
        )

        auto = await create_automation(
            name="Test Auto",
            trigger_type="webhook",
            trigger_config={},
            prompt="Hello {{name}}",
            profile_id="test-profile",
        )

        result = await test_automation(auto["id"])
        assert "prompt_rendered" in result
        assert "budget_ok" in result
        assert result["budget_ok"] is True

    @pytest.mark.asyncio
    async def test_dry_run_not_found(self, mock_db):
        from app.core.automation_service import test_automation
        result = await test_automation("nonexistent")
        assert result["budget_ok"] is False
        assert "not found" in result["message"]


# =============================================================================
# Stats
# =============================================================================


class TestGetAutomationStats:
    def test_stats_nonexistent(self, mock_db):
        result = get_automation_stats("nonexistent")
        assert result == {}

    def test_stats_basic(self, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-stats",
            name="Stats Test",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )

        result = get_automation_stats("auto-stats")
        assert result["automation_id"] == "auto-stats"
        assert result["total_runs"] == 0
        assert result["successful_runs"] == 0


# =============================================================================
# Scheduled-runner election (V2 placement — capable_runners)
# =============================================================================

ME = "install-me"
OTHER = "install-other"
EARLY = "install-aaa"  # sorts before both ME and OTHER


def _proj(pid, instance_id=None, path="/p"):
    return {
        "id": pid, "name": pid, "path": path, "settings": {},
        "instance_id": instance_id,
    }


def _installs(*specs):
    """specs: (install_id, online) tuples → {id: row} registry map."""
    return {iid: {"id": iid, "device_name": iid, "online": online} for iid, online in specs}


def _pv2(pid="proj-A"):
    return {"id": f"pv2-{pid}", "name": pid}


@pytest.fixture
def svc_env():
    """Patch the service's database facade and pin this install's id to ME.

    ``should_run_scheduled_here`` reads ``database``/``settings`` from
    ``automation_service`` (the install registry + this install's id) and lazily
    imports ``resolve_project_v2`` / ``placement.capable_runners`` /
    ``get_project_working_dir`` at call time — the tests patch those three at their
    definition modules.
    """
    with patch("app.core.automation_service.database") as db, \
         patch("app.core.automation_service.settings") as st:
        st.effective_instance_id = ME
        yield db


class TestShouldRunScheduledHere:
    def _auto(self, project_id=None):
        return {"id": "auto-1", "project_id": project_id}

    # ── projectless arm (byte-equivalent to the pre-cutover behavior) ──────────

    def test_empty_registry_runs_locally(self, svc_env):
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_installs_map.return_value = {}
        should_run, _ = should_run_scheduled_here(self._auto())
        assert should_run is True

    def test_registry_error_fails_open(self, svc_env):
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_installs_map.side_effect = RuntimeError("db down")
        should_run, _ = should_run_scheduled_here(self._auto())
        assert should_run is True

    def test_projectless_lowest_online_install_wins(self, svc_env):
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_installs_map.return_value = _installs((ME, True), (OTHER, True))
        should_run, _ = should_run_scheduled_here(self._auto())
        assert should_run is True  # "install-me" < "install-other"

        svc_env.get_installs_map.return_value = _installs((ME, True), (EARLY, True))
        should_run, reason = should_run_scheduled_here(self._auto())
        assert should_run is False
        assert EARLY in reason

    def test_projectless_counts_self_online_despite_stale_heartbeat(self, svc_env):
        """A stale own-heartbeat row must not make every install defer."""
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_installs_map.return_value = _installs((ME, False), (OTHER, True))
        should_run, _ = should_run_scheduled_here(self._auto())
        assert should_run is True  # ME counts itself online and sorts first

    # ── pv2 unresolvable → surface the error on exactly one runner ─────────────

    def test_pv2_unresolvable_elects_single_runner(self, svc_env):
        """A legacy project row missing / unresolvable to a project_v2 elects the
        lowest-id online install to surface the error, not all of them."""
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_installs_map.return_value = _installs((ME, True), (OTHER, True))
        with patch("app.core.project_resolver.resolve_project_v2", return_value=None):
            should_run, _ = should_run_scheduled_here(self._auto("proj-gone"))
        assert should_run is True  # "install-me" sorts first

        svc_env.get_installs_map.return_value = _installs((ME, True), (EARLY, True))
        with patch("app.core.project_resolver.resolve_project_v2", return_value=None):
            should_run, reason = should_run_scheduled_here(self._auto("proj-gone"))
        assert should_run is False
        assert EARLY in reason

    # ── capable_runners election ───────────────────────────────────────────────

    def test_owner_online_preferred_over_lowest(self, svc_env):
        """When the legacy owner holds a checkout it wins even if ME sorts first."""
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_project.return_value = _proj("proj-A", instance_id=OTHER)
        svc_env.get_installs_map.return_value = _installs((ME, True), (OTHER, True))
        with patch("app.core.project_resolver.resolve_project_v2", return_value=_pv2()), \
             patch("app.core.placement.capable_runners", return_value=[ME, OTHER]):
            should_run, reason = should_run_scheduled_here(self._auto("proj-A"))
        assert should_run is False
        assert OTHER in reason

    def test_owner_online_is_me_runs(self, svc_env):
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_project.return_value = _proj("proj-A", instance_id=ME)
        svc_env.get_installs_map.return_value = _installs((ME, True), (OTHER, True))
        with patch("app.core.project_resolver.resolve_project_v2", return_value=_pv2()), \
             patch("app.core.placement.capable_runners", return_value=[ME, OTHER]):
            should_run, _ = should_run_scheduled_here(self._auto("proj-A"))
        assert should_run is True

    def test_owner_offline_lowest_candidate_wins(self, svc_env):
        """Owner holds no active checkout (offline) → the lowest-id capable runner
        is elected."""
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_project.return_value = _proj("proj-A", instance_id=OTHER)
        svc_env.get_installs_map.return_value = _installs((ME, True), (EARLY, True), (OTHER, False))
        with patch("app.core.project_resolver.resolve_project_v2", return_value=_pv2()), \
             patch("app.core.placement.capable_runners", return_value=[EARLY, ME]):
            should_run, reason = should_run_scheduled_here(self._auto("proj-A"))
        assert should_run is False
        assert EARLY in reason

    def test_owner_offline_me_is_lowest_runs(self, svc_env):
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_project.return_value = _proj("proj-A", instance_id=OTHER)
        svc_env.get_installs_map.return_value = _installs((ME, True), (OTHER, False))
        with patch("app.core.project_resolver.resolve_project_v2", return_value=_pv2()), \
             patch("app.core.placement.capable_runners", return_value=[ME]):
            should_run, _ = should_run_scheduled_here(self._auto("proj-A"))
        assert should_run is True

    # ── zero capable runners → local-path fail-open + visibility ───────────────

    def test_no_candidates_local_path_exists_runs(self, svc_env, tmp_path):
        from app.core.automation_service import should_run_scheduled_here
        svc_env.get_project.return_value = _proj("proj-A", path=str(tmp_path))
        svc_env.get_installs_map.return_value = _installs((ME, True), (OTHER, True))
        with patch("app.core.project_resolver.resolve_project_v2", return_value=_pv2()), \
             patch("app.core.placement.capable_runners", return_value=[]), \
             patch("app.core.project_utils.get_project_working_dir", return_value=str(tmp_path)):
            should_run, _ = should_run_scheduled_here(self._auto("proj-A"))
        assert should_run is True
        svc_env.update_automation.assert_not_called()

    def test_no_candidates_no_local_path_skips_and_records(self, svc_env, tmp_path):
        from app.core.automation_service import should_run_scheduled_here
        missing = str(tmp_path / "nope")
        svc_env.get_project.return_value = _proj("proj-A", path=missing)
        svc_env.get_installs_map.return_value = _installs((ME, True), (OTHER, True))

        auto = self._auto("proj-A")
        with patch("app.core.project_resolver.resolve_project_v2", return_value=_pv2()), \
             patch("app.core.placement.capable_runners", return_value=[]), \
             patch("app.core.project_utils.get_project_working_dir", return_value=missing):
            should_run, _ = should_run_scheduled_here(auto)
        assert should_run is False
        svc_env.update_automation.assert_called_once()
        _, kwargs = svc_env.update_automation.call_args
        assert "No online install" in kwargs["last_error"]

        # Already recorded → no repeat write on subsequent fires.
        svc_env.update_automation.reset_mock()
        auto["last_error"] = kwargs["last_error"]
        with patch("app.core.project_resolver.resolve_project_v2", return_value=_pv2()), \
             patch("app.core.placement.capable_runners", return_value=[]), \
             patch("app.core.project_utils.get_project_working_dir", return_value=missing):
            should_run, _ = should_run_scheduled_here(auto)
        assert should_run is False
        svc_env.update_automation.assert_not_called()


# =============================================================================
# Rendering edge cases
# =============================================================================


class TestRenderEdgeCases:
    def test_dot_path_through_a_scalar_stays_literal(self):
        assert render_prompt("{{payload.repo.name}}", {"payload": {"repo": "shuffle"}}) == "{{payload.repo.name}}"

    def test_falsy_values_render(self):
        assert render_prompt("{{n}}/{{flag}}/{{s}}", {"n": 0, "flag": False, "s": ""}) == "0/False/"

    def test_action_params_render_strings_and_pass_other_types(self):
        from app.core.automation_service import render_action_params

        params = {"text": "{{payload.ref}}: {{response}}", "count": 3, "tags": ["{{x}}"], "on": True}
        rendered = render_action_params(params, {"payload": {"ref": "main"}}, "shipped")
        assert rendered == {"text": "main: shipped", "count": 3, "tags": ["{{x}}"], "on": True}
        assert params["text"] == "{{payload.ref}}: {{response}}"  # input untouched


# =============================================================================
# Budget: daily cost limit
# =============================================================================


class TestDailyCostLimit:
    def _seed_spend(self, cost):
        from app.db import database
        database.create_automation(
            automation_id="auto-1", name="T", trigger_type="webhook", trigger_config={},
            prompt="t", profile_id="default",
        )
        database.create_automation_run(run_id="run-1", automation_id="auto-1", status="completed")
        database.update_automation_run("run-1", cost_usd=cost)

    def test_daily_cost_limit_reached(self, mock_db):
        self._seed_spend(0.6)
        ok, reason = check_budget({"id": "auto-1", "total_cost_used": 0.6, "max_cost_per_day": 0.5})
        assert ok is False
        assert "Daily cost limit" in reason

    def test_daily_cost_under_limit(self, mock_db):
        self._seed_spend(0.2)
        ok, _ = check_budget({"id": "auto-1", "total_cost_used": 0.2, "max_cost_per_day": 0.5})
        assert ok is True


# =============================================================================
# Runner election: network ownership and no-runner bookkeeping
# =============================================================================


class TestElectionOwnership:
    def test_network_ownership_decision_wins(self, svc_env):
        from app.core.automation_service import should_run_scheduled_here

        with patch("app.network.ownership.decide_automation", return_value=(False, "owned by dev-2")):
            assert should_run_scheduled_here({"id": "auto-1"}) == (False, "owned by dev-2")
        svc_env.get_installs_map.assert_not_called()

    def test_network_ownership_error_fails_closed(self, svc_env):
        from app.core.automation_service import should_run_scheduled_here

        with patch("app.network.ownership.decide_automation", side_effect=RuntimeError("no network state")):
            assert should_run_scheduled_here({"id": "auto-1"}) == (False, "network ownership unavailable")
        svc_env.get_installs_map.assert_not_called()

    def test_record_no_runner_write_failure_is_swallowed(self, svc_env):
        from app.core.automation_service import _record_no_runner

        svc_env.update_automation.side_effect = RuntimeError("db down")
        _record_no_runner({"id": "auto-1"}, {"id": "proj-A", "name": "Repo"})  # must not raise
        svc_env.update_automation.assert_called_once()


# =============================================================================
# Webhook secret helpers: failure paths and reveal
# =============================================================================


@pytest.fixture
def enc_prefix():
    with patch("app.core.encryption.encrypt_value", side_effect=lambda v: f"enc:{v[::-1]}"), \
         patch("app.core.encryption.decrypt_value", side_effect=lambda v: v[len("enc:"):][::-1]), \
         patch("app.core.encryption.is_encryption_ready", return_value=True) as ready:
        yield ready


class TestWebhookSecretFailures:
    def test_encrypt_failure_refuses_to_store_plaintext(self):
        with patch("app.core.encryption.encrypt_value", side_effect=RuntimeError("no key")):
            with pytest.raises(ValueError, match="local recovery"):
                encrypt_webhook_secret({"secret": "plain"})

    def test_decrypt_failure_leaves_ciphertext(self, enc_prefix):
        with patch("app.core.encryption.decrypt_value", side_effect=RuntimeError("bad key")):
            assert decrypt_webhook_secret({"secret": "enc:abc"}) == {"secret": "enc:abc"}

    def test_decrypt_when_key_unavailable_leaves_ciphertext(self, enc_prefix):
        enc_prefix.return_value = False
        assert decrypt_webhook_secret({"secret": "enc:abc"}) == {"secret": "enc:abc"}

    def test_reveal_decrypts_webhook_triggers_only(self, enc_prefix):
        from app.core.automation_service import reveal_webhook_secrets

        automation = {
            "trigger_type": "webhook",
            "trigger_config": {"secret": "enc:terces"},
            "triggers": [
                {"type": "webhook", "config": {"secret": "enc:terces"}},
                {"type": "cron", "config": {"expression": "0 9 * * *", "secret": "enc:untouched"}},
                "not-a-dict",
            ],
        }
        revealed = reveal_webhook_secrets(automation)
        assert revealed["trigger_config"] == {"secret": "secret"}
        assert revealed["triggers"][0]["config"] == {"secret": "secret"}
        assert revealed["triggers"][1]["config"]["secret"] == "enc:untouched"
        assert revealed["triggers"][2] == "not-a-dict"
        assert automation["triggers"][0]["config"]["secret"] == "enc:terces"  # input untouched

    def test_reveal_passes_non_dicts_through(self):
        from app.core.automation_service import reveal_webhook_secrets
        assert reveal_webhook_secrets(None) is None


# =============================================================================
# create / update / delete: validation and scheduler hand-off
# =============================================================================


class TestCreateUpdateDeleteService:
    async def test_create_rejects_unknown_project(self, mock_db):
        from app.core.automation_service import create_automation

        with pytest.raises(ValueError, match="Project not found"):
            await create_automation(
                name="t", trigger_type="interval", trigger_config={"seconds": 60},
                profile_id="default", project_id="proj-missing",
            )

    async def test_create_raises_when_insert_returns_nothing(self, mock_db):
        from app.core.automation_service import create_automation

        with patch("app.db.database.create_automation", return_value=None):
            with pytest.raises(RuntimeError, match="Failed to create automation"):
                await create_automation(
                    name="t", trigger_type="interval", trigger_config={"seconds": 60},
                    profile_id="default",
                )

    async def test_create_enabled_survives_a_scheduling_failure(self, mock_db):
        from app.core.automation_manager import automation_manager
        from app.core.automation_service import create_automation

        with patch.object(automation_manager, "schedule_automation",
                          new=AsyncMock(side_effect=RuntimeError("scheduler down"))) as sched:
            auto = await create_automation(
                name="t", trigger_type="interval", trigger_config={"seconds": 60},
                profile_id="default", enabled=True,
            )
        sched.assert_awaited_once_with(auto["id"])
        assert auto["is_enabled"]

    async def test_create_enabled_event_trigger_is_not_scheduled(self, mock_db, enc_prefix):
        from app.core.automation_manager import automation_manager
        from app.core.automation_service import create_automation

        with patch.object(automation_manager, "schedule_automation", new=AsyncMock()) as sched:
            auto = await create_automation(
                name="t", trigger_type="webhook", trigger_config={},
                profile_id="default", enabled=True,
            )
        sched.assert_not_awaited()
        # A secret was generated and stored encrypted.
        assert auto["triggers"][0]["config"]["secret"].startswith("enc:")

    async def _seed(self, **kwargs):
        from app.db import database
        return database.create_automation(
            automation_id="auto-u", name="U", trigger_type="interval",
            trigger_config={"seconds": 600}, prompt="p", profile_id="default", **kwargs,
        )

    async def test_update_missing_returns_none(self, mock_db):
        from app.core.automation_service import update_automation
        assert await update_automation("auto-missing", name="x") is None

    @pytest.mark.parametrize("kwargs,match", [
        ({"profile_id": "nope"}, "Profile not found"),
        ({"project_id": "nope"}, "Project not found"),
        ({"triggers": [{"type": "sometimes", "config": {}}]}, "Invalid trigger type"),
        ({"triggers": [{"type": "interval", "config": {"seconds": 59}}]}, "at least 60 seconds"),
    ])
    async def test_update_validation(self, mock_db, kwargs, match):
        from app.core.automation_service import update_automation

        await self._seed()
        with pytest.raises(ValueError, match=match):
            await update_automation("auto-u", **kwargs)
        from app.db import database
        assert database.get_automation("auto-u")["triggers"] == [{"type": "interval", "config": {"seconds": 600}}]

    async def test_update_webhook_trigger_generates_and_encrypts_secret(self, mock_db, enc_prefix):
        from app.core.automation_service import update_automation

        await self._seed()
        updated = await update_automation("auto-u", triggers=[{"type": "webhook", "config": {}}])
        secret = updated["triggers"][0]["config"]["secret"]
        assert secret.startswith("enc:") and len(secret) > len("enc:") + 20

    async def test_update_legacy_webhook_trigger_config_is_encrypted(self, mock_db, enc_prefix):
        from app.core.automation_service import update_automation

        await self._seed()
        updated = await update_automation(
            "auto-u", trigger_type="webhook", trigger_config={"secret": "plain"},
        )
        assert updated["trigger_config"] == {"secret": "enc:nialp"}

    @pytest.mark.parametrize("kwargs,reschedules", [
        ({"triggers": [{"type": "interval", "config": {"seconds": 900}}]}, True),
        ({"trigger_config": {"seconds": 900}}, True),
        ({"trigger_type": "interval"}, True),
        ({"name": "renamed", "prompt": "new"}, False),
    ])
    async def test_update_reschedules_only_on_trigger_changes(self, mock_db, kwargs, reschedules):
        from app.core.automation_manager import automation_manager
        from app.core.automation_service import update_automation

        await self._seed(enabled=True)
        with patch.object(automation_manager, "schedule_automation", new=AsyncMock()) as sched:
            await update_automation("auto-u", **kwargs)
        assert sched.await_count == (1 if reschedules else 0)

    async def test_update_reschedule_failure_still_returns_the_row(self, mock_db):
        from app.core.automation_manager import automation_manager
        from app.core.automation_service import update_automation

        await self._seed(enabled=True)
        with patch.object(automation_manager, "schedule_automation",
                          new=AsyncMock(side_effect=RuntimeError("scheduler down"))):
            updated = await update_automation("auto-u", triggers=[{"type": "interval", "config": {"seconds": 900}}])
        assert updated["triggers"][0]["config"] == {"seconds": 900}

    @pytest.mark.parametrize("enabled,unschedules", [(True, 1), (False, 0)])
    async def test_delete_unschedules_enabled_automations(self, mock_db, enabled, unschedules):
        from app.core.automation_manager import automation_manager
        from app.core.automation_service import delete_automation
        from app.db import database

        await self._seed(enabled=enabled)
        with patch.object(automation_manager, "unschedule_automation", new=AsyncMock()) as unsched:
            assert await delete_automation("auto-u") is True
        assert unsched.await_count == unschedules
        assert database.get_automation("auto-u") is None

    async def test_delete_proceeds_when_unschedule_fails(self, mock_db):
        from app.core.automation_manager import automation_manager
        from app.core.automation_service import delete_automation
        from app.db import database

        await self._seed(enabled=True)
        with patch.object(automation_manager, "unschedule_automation",
                          new=AsyncMock(side_effect=RuntimeError("scheduler down"))):
            assert await delete_automation("auto-u") is True
        assert database.get_automation("auto-u") is None
