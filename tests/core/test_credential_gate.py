"""The one credential gate (plan item C10).

Unit cases for ``agent_credentials_ready()`` (the Claude leg), worker
readiness across every provider (``status_payload`` / ``record_startup``), and
an integration test for a fresh install: with no CLI sign-in and no stored key,
every Claude entry point (a turn of any source through the real SessionRuntime
and the real pump, and the REST ``/query`` paths) is refused with the Claude
setup message, and no CLI process is ever built.
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import claude_auth_mode, claude_cli_status, credential_gate, license as license_mod
from app.core import session_runtime
from app.db import database
from app.db import turns as turns_db

pytestmark = pytest.mark.real_agent_gate


def _cli(installed=True, signed_in=False, error=None):
    return claude_cli_status.ClaudeCliStatus(installed=installed, signed_in=signed_in, error=error)


# ---------------------------------------------------------------------------
# agent_credentials_ready()
# ---------------------------------------------------------------------------

def test_fresh_install_without_cli_is_blocked(mock_db):
    ready, reason = credential_gate.agent_credentials_ready()
    assert ready is False
    assert reason == credential_gate.NO_CLI_MESSAGE


def test_subscription_mode_needs_a_cli_sign_in(mock_db):
    claude_auth_mode.set_mode("subscription")
    assert credential_gate.agent_credentials_ready(_cli(signed_in=False)) == (
        False, credential_gate.NOT_SIGNED_IN_MESSAGE)
    assert credential_gate.agent_credentials_ready(_cli(signed_in=True)) == (True, None)


def test_subscription_mode_ignores_a_stored_key(mock_db):
    """Both present or only the key: the explicit mode decides, no fallback."""
    claude_auth_mode.set_mode("subscription")
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
    ready, reason = credential_gate.agent_credentials_ready(_cli(signed_in=False))
    assert not ready and reason == credential_gate.NOT_SIGNED_IN_MESSAGE


def test_api_key_mode_needs_a_stored_key_not_a_cli_login(mock_db):
    claude_auth_mode.set_mode("api_key")
    assert credential_gate.agent_credentials_ready(_cli(signed_in=True)) == (
        False, credential_gate.NO_KEY_MESSAGE)
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
    assert credential_gate.agent_credentials_ready(_cli(signed_in=False)) == (True, None)


def test_off_mode_refuses_claude_whatever_credentials_exist(mock_db):
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
    claude_auth_mode.set_mode("off")
    assert credential_gate.claude_credential_ready(_cli(signed_in=True)) == (
        False, credential_gate.OFF_MESSAGE)
    assert credential_gate.agent_credentials_ready(_cli(signed_in=True)) == (
        False, credential_gate.OFF_MESSAGE)
    assert credential_gate.OFF_MESSAGE == (
        "Claude is off on this worker. Turn it on in Settings > Providers > Claude, "
        "or pick a model from another provider.")


def test_off_mode_refuses_the_claude_leg_but_not_a_provider_leg(mock_db):
    claude_auth_mode.set_mode("off")
    assert credential_gate.turn_refusal("delegated") == credential_gate.OFF_MESSAGE
    assert credential_gate.turn_refusal(None) == credential_gate.OFF_MESSAGE
    assert credential_gate.turn_refusal("openai_responses") is None


def _provider_row(row_id, provider_type, **fields):
    checked = fields.pop("checked", False)
    database.create_model_provider(
        row_id, row_id, provider_type, "https://example.invalid/v1",
        auth_token=fields.pop("auth_token", None),
        models=fields.pop("models", None),
        default_model=fields.pop("default_model", None),
        enabled=fields.pop("enabled", True),
    )
    if fields:
        database.update_model_provider(row_id, **fields)
    if checked:  # the catalog fetch accepted the key
        database.record_credential_check(row_id, True)


def test_off_mode_status_payload(mock_db):
    claude_auth_mode.set_mode("off")
    payload = credential_gate.status_payload(_cli(signed_in=True))
    assert payload["mode"] == "off"
    assert payload["claude_credential_ready"] is False
    assert payload["reason"] == credential_gate.NO_PROVIDER_MESSAGE
    assert payload["ready"] is False and payload["license_allows_runs"] is True
    assert payload["any_provider_ready"] is False and payload["ready_providers"] == []


def test_nothing_configured_is_not_ready_with_a_neutral_reason(mock_db):
    payload = credential_gate.status_payload(_cli(installed=False, signed_in=None))
    assert payload["ready"] is False
    assert payload["any_provider_ready"] is False and payload["ready_providers"] == []
    assert payload["reason"] == credential_gate.NO_PROVIDER_MESSAGE
    assert "Claude" not in credential_gate.NO_PROVIDER_MESSAGE


def test_another_provider_makes_the_worker_ready_without_claude(mock_db):
    _provider_row("oa", "openai", auth_token="enc:key", checked=True)
    payload = credential_gate.status_payload(_cli(installed=False, signed_in=None))
    assert payload["ready"] is True and payload["reason"] is None
    assert payload["claude_credential_ready"] is False
    assert payload["any_provider_ready"] is True
    assert payload["ready_providers"] == ["openai"]


def test_ready_providers_lists_each_usable_provider_once(mock_db):
    claude_auth_mode.set_mode("api_key")
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
    _provider_row("codex", "chatgpt", auth_mode="chatgpt", codex_access_token="enc:tok")
    _provider_row("oa", "openai", auth_token="enc:key", checked=True)
    _provider_row("sg", "grok", auth_mode="grok", codex_access_token="enc:tok")
    _provider_row("or", "openrouter", auth_token="enc:key", checked=True)
    _provider_row("gw1", "custom")  # a keyless gateway still counts
    _provider_row("off", "xai", auth_token="enc:key", enabled=False)
    _provider_row("nokey", "openrouter")
    _provider_row("stale", "grok", auth_mode="grok", codex_access_token="enc:tok", needs_reauth=True)
    _provider_row("half", "chatgpt", auth_mode="chatgpt")  # sign-in never finished
    providers = credential_gate.status_payload(_cli(signed_in=False))["ready_providers"]
    assert providers[0] == "claude"
    assert sorted(providers[1:]) == sorted(["openai", "grok", "openrouter", "gw:gw1"])


def test_a_provider_row_read_failure_never_breaks_the_payload(mock_db, monkeypatch):
    monkeypatch.setattr(database, "list_usable_model_providers",
                        lambda: (_ for _ in ()).throw(RuntimeError("pool down")))
    payload = credential_gate.status_payload(_cli(signed_in=True))
    assert payload["ready"] is True and payload["ready_providers"] == ["claude"]


def test_license_block_wins_over_ready_providers(mock_db, monkeypatch):
    _provider_row("oa", "openai", auth_token="enc:key")
    monkeypatch.setattr(license_mod, "license_allows_agent_runs", lambda: (False, "No license is active."))
    payload = credential_gate.status_payload(_cli(signed_in=True))
    assert payload["ready"] is False and payload["reason"] == "No license is active."
    assert payload["any_provider_ready"] is True
    ready, reason = credential_gate.record_startup(_cli(signed_in=True))
    assert (ready, reason) == (False, "No license is active.")


def test_claude_leg_still_refused_when_another_provider_is_ready(mock_db):
    _provider_row("oa", "openai", auth_token="enc:key")
    assert credential_gate.turn_refusal("delegated") == credential_gate.NO_CLI_MESSAGE
    assert credential_gate.NO_CLI_MESSAGE.startswith("Claude cannot run on this worker")
    assert credential_gate.turn_refusal("openai_responses") is None


def _build(tmp_path, overrides=None):
    from app.core.query_options import build_options_from_profile

    if database.get_profile("floor") is None:
        database.create_profile("floor", "Floor", None, {"cwd": str(tmp_path)})
    options, _ = build_options_from_profile(database.get_profile("floor"), overrides=overrides)
    return options


def _openai_with_default():
    _provider_row("oa", "openai", auth_token="enc:key",
                  models=[{"id": "gpt-5", "label": "GPT-5"}], default_model="gpt-5")


def test_no_model_named_starts_on_a_ready_provider_when_claude_is_not_set_up(mock_db, tmp_path):
    _openai_with_default()
    options = _build(tmp_path)
    assert options.model == "gpt-5"
    leg = credential_gate.leg_for_options(options)
    assert leg != "delegated" and credential_gate.turn_refusal(leg) is None


def test_an_explicit_claude_model_is_not_rerouted(mock_db, tmp_path):
    _openai_with_default()
    options = _build(tmp_path, {"model": "sonnet"})
    assert options.model == "sonnet"
    leg = credential_gate.leg_for_options(options)
    assert leg == "delegated"
    assert credential_gate.turn_refusal(leg) == credential_gate.NO_CLI_MESSAGE


def _build_turn(tmp_path, session_id, source="user", **session_fields):
    """Build a turn's options for ``session_id`` (created on first use) the way
    the session runtime does: the turn's source rides in the overrides."""
    from app.core.query_options import build_options_from_profile

    if database.get_profile("floor") is None:
        database.create_profile("floor", "Floor", None, {"cwd": str(tmp_path)})
    if database.get_session(session_id) is None:
        database.create_session(session_id, "floor", **session_fields)
    options, _ = build_options_from_profile(
        database.get_profile("floor"), overrides={"turn_source": source},
        session_id=session_id,
    )
    return options


def test_no_model_named_follows_the_most_used_model_with_claude_set_up(mock_db, tmp_path):
    claude_auth_mode.set_mode("api_key")
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
    _openai_with_default()
    # No usage history: Claude is just another candidate, the row's default leads.
    assert _build_turn(tmp_path, "c1").model == "gpt-5"
    _book(_chat("s1"), "claude-opus-5-5", turns=3)
    credential_gate.invalidate_most_used_cache()
    options = _build_turn(tmp_path, "c2")
    assert options.model == "opus"
    assert credential_gate.leg_for_options(options) == "delegated"


@pytest.mark.parametrize("row_type,model", [("xai", "grok-4"), ("openrouter", "meta-llama/llama-3.3-70b")])
def test_a_provider_default_is_bound_to_the_session_like_an_explicit_pick(
    mock_db, tmp_path, row_type, model,
):
    """B1: turn 2 (auto-continue, failover, wake) stays on turn 1's provider."""
    # A plain token: the gateway leg reads it into the env at build time.
    _provider_row("gw", row_type, auth_token="sk-plain-test",
                  models=[{"id": model, "label": model}], default_model=model)
    first = _build_turn(tmp_path, "b1")
    assert (first.aih_provider_id, first.model) == ("gw", model)
    row = database.get_session("b1")
    assert (row["provider_id"], row["model"]) == ("gw", f"prov:gw:{model}")
    # The runtime records the bare id the turn ran on; the most used ranking
    # then moves to Claude. Neither may reroute the session.
    database.update_session("b1", launched_model=model)
    _claude_api_key_ready()
    _book(_chat("other"), "opus", turns=5)
    credential_gate.invalidate_most_used_cache()
    for source in ("auto_continue", "failover", "wakeup", "user"):
        later = _build_turn(tmp_path, "b1", source)
        assert (later.aih_provider_id, later.model) == ("gw", model), source
        leg = credential_gate.leg_for_options(later)
        assert leg != "delegated" and credential_gate.turn_refusal(leg) is None


def test_unattended_runs_keep_the_claude_default_over_the_most_used_model(mock_db, tmp_path):
    """S1: only a typed owner turn follows the most used chat model."""
    from app.core.automation_service import with_model_resolution
    from app.core.model_registry import DEFAULT_MODEL, registry_payload

    _claude_and_openai()
    _book(_chat("s1"), "gpt-5-mini", "oa", turns=5)
    assert _build_turn(tmp_path, "chat").model == "gpt-5-mini"
    assert registry_payload()["default"] == "prov:oa:gpt-5-mini"  # composer preselect
    unattended = [
        ("auto", "automation", {"automation_id": "a1"}),
        ("wake", "wakeup", {}),
        ("conn", "connector", {}),
        ("api", "api", {}),
        ("worker", "user", {"is_worker": True}),
        ("voice", "user", {"mode": "voice"}),
    ]
    for sid, source, fields in unattended:
        options = _build_turn(tmp_path, sid, source, **fields)
        assert (options.model, options.aih_provider_id) == (DEFAULT_MODEL, None), sid
        assert database.get_session(sid)["provider_id"] is None, sid
    reported = with_model_resolution({"id": "a1"})
    assert (reported["effective_model"], reported["effective_provider_id"]) == (DEFAULT_MODEL, None)


def test_unattended_runs_without_claude_use_and_bind_the_provider_default(mock_db, tmp_path):
    from app.core.automation_service import with_model_resolution

    _provider_row("oa", "openai", auth_token="enc:key",
                  models=[{"id": "gpt-5", "label": "GPT-5"}, {"id": "gpt-5-mini", "label": "Mini"}],
                  default_model="gpt-5")
    claude_cli_status.get_status()  # the CLI is missing
    _book(_chat("s1"), "gpt-5-mini", "oa", turns=5)
    options = _build_turn(tmp_path, "auto", "automation", automation_id="a1")
    assert (options.aih_provider_id, options.model) == ("oa", "gpt-5")
    row = database.get_session("auto")
    assert (row["provider_id"], row["model"]) == ("oa", "prov:oa:gpt-5")
    reported = with_model_resolution({"id": "a1"})
    assert (reported["effective_model"], reported["effective_provider_id"]) == ("gpt-5", "oa")


def test_a_row_without_a_default_model_uses_its_first_curated_model(mock_db, tmp_path):
    """F2: the server matches the composer's ``default_model || models[0]``."""
    from app.core.model_registry import registry_payload

    _provider_row("oa", "openai", auth_token="enc:key",
                  models=[{"id": "gpt-5.5", "label": "GPT-5.5"}, {"id": "gpt-5", "label": "GPT-5"}])
    claude_cli_status.get_status()  # the CLI is missing
    assert credential_gate.default_run_selection() == "prov:oa:gpt-5.5"
    assert credential_gate.default_run_selection(prefer_most_used=False) == "prov:oa:gpt-5.5"
    assert registry_payload()["default"] == "prov:oa:gpt-5.5"
    options = _build_turn(tmp_path, "f2")
    assert (options.aih_provider_id, options.model) == ("oa", "gpt-5.5")
    assert database.get_session("f2")["model"] == "prov:oa:gpt-5.5"
    # A usable row that lists no model is still skipped.
    database.delete_model_provider("oa")
    _provider_row("gw1", "custom")
    assert credential_gate.default_provider_model() is None


def test_a_subscription_row_leads_an_api_row_of_the_same_provider(mock_db):
    _provider_row("api", "openai", auth_token="enc:key", checked=True,
                  models=[{"id": "gpt-5", "label": "GPT-5"}])
    _provider_row("sub", "openai", auth_mode="chatgpt",
                  models=[{"id": "gpt-5-codex", "label": "Codex"}])
    database.update_model_provider("sub", codex_access_token="enc:access")
    with database.get_db() as conn:  # the API-key row is the newer one
        conn.cursor().execute(
            "UPDATE model_providers SET created_at = '2099-01-01T00:00:00+00:00' WHERE id = 'api'")
    assert credential_gate.default_provider_model() == ("sub", "gpt-5-codex")


@pytest.mark.parametrize("token", ["sk-plain-test", None], ids=["keyed", "keyless"])
def test_a_gateway_only_owner_runs_on_the_gateway_by_default(mock_db, tmp_path, token):
    """B-1: with only a custom gateway connected, a run that names no model
    runs on it; it never floors to a Claude model the gate refuses."""
    from app.core.model_registry import registry_payload

    _provider_row("gw1", "custom", auth_token=token,
                  models=[{"id": "llama-3", "label": "Llama 3"}], default_model="llama-3")
    claude_cli_status.get_status()  # the CLI is missing
    status = credential_gate.status_payload()
    assert status["any_provider_ready"] and status["ready_providers"] == ["gw:gw1"]
    assert credential_gate.default_run_selection() == "prov:gw1:llama-3"
    assert registry_payload()["default"] == "prov:gw1:llama-3"
    for interactive in (True, False):
        assert credential_gate.default_run_model(prefer_most_used=interactive) == ("gw1", "llama-3")
    options = _build_turn(tmp_path, "gw-chat")
    assert (options.aih_provider_id, options.model) == ("gw1", "llama-3")
    assert database.get_session("gw-chat")["model"] == "prov:gw1:llama-3"
    leg = credential_gate.leg_for_options(options)
    assert leg != "delegated" and credential_gate.turn_refusal(leg) is None
    # The most used gateway model leads too.
    _book(_chat("s1"), "llama-3", "gw1", turns=3)
    credential_gate.invalidate_most_used_cache()
    assert credential_gate.most_used_model() == ("gw1", "llama-3")
    # A caller that validates with the servable rule (voice) never gets it.
    assert credential_gate.default_provider_model(servable_only=True) is None
    assert credential_gate.most_used_model(servable_only=True) is None


@pytest.mark.parametrize("rows", [
    ["gw"], ["oa", "gw"], ["gw", "oa"], ["gw", "claude"], ["claude"], ["oa"],
])
def test_an_interactive_default_always_runs_on_a_ready_provider(mock_db, tmp_path, rows):
    """Invariant: while any provider is ready, an interactive run that names
    no model resolves to a provider in ``ready_providers``."""
    claude_cli_status.get_status()  # the CLI is missing
    for row in rows:  # created oldest first
        if row == "claude":
            _claude_api_key_ready()
        elif row == "oa":
            _openai_with_default()
        else:
            _provider_row("gw", "custom", models=[{"id": "m1", "label": "M1"}])
    status = credential_gate.status_payload()
    assert status["any_provider_ready"]
    ready = status["ready_providers"]
    provider_id, _model = credential_gate.default_run_model(prefer_most_used=True)
    if provider_id is None:
        assert "claude" in ready
    else:
        row = database.get_model_provider(provider_id, include_token=False)
        assert credential_gate.provider_id_for_row(row) in ready
    options = _build_turn(tmp_path, "inv")
    assert credential_gate.turn_refusal(credential_gate.leg_for_options(options)) is None


def test_startup_logs_off_as_information_not_a_warning(mock_db, caplog):
    import logging

    claude_auth_mode.set_mode("off")
    _provider_row("oa", "openai", auth_token="enc:key", checked=True)
    with caplog.at_level(logging.INFO, logger=credential_gate.logger.name):
        ready, reason = credential_gate.record_startup(_cli(signed_in=True))
    assert ready and reason is None
    assert credential_gate.startup_state == "ready"
    records = [r for r in caplog.records if r.name == credential_gate.logger.name]
    assert records and all(r.levelno == logging.INFO for r in records)
    assert any("Claude is off" in r.getMessage() for r in records)


def test_startup_without_claude_but_another_provider_is_ready_and_quiet(mock_db, caplog):
    import logging

    _provider_row("sg", "grok", auth_mode="grok", codex_access_token="enc:tok")
    with caplog.at_level(logging.INFO, logger=credential_gate.logger.name):
        ready, reason = credential_gate.record_startup(_cli(installed=False, signed_in=None))
    assert ready and reason is None
    records = [r for r in caplog.records if r.name == credential_gate.logger.name]
    assert all(r.levelno == logging.INFO for r in records)


def test_startup_with_no_provider_warns_with_the_neutral_reason(mock_db, caplog):
    import logging

    claude_auth_mode.set_mode("off")
    with caplog.at_level(logging.INFO, logger=credential_gate.logger.name):
        ready, reason = credential_gate.record_startup(_cli(signed_in=True))
    assert not ready and reason == credential_gate.NO_PROVIDER_MESSAGE
    assert credential_gate.startup_state == "setup_required"
    warnings = [r for r in caplog.records
                if r.name == credential_gate.logger.name and r.levelno == logging.WARNING]
    assert len(warnings) == 1 and credential_gate.NO_PROVIDER_MESSAGE in warnings[0].getMessage()


def test_an_unlicensed_worker_is_blocked_with_the_license_reason(mock_db, monkeypatch):
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
    claude_auth_mode.set_mode("api_key")
    monkeypatch.setattr(license_mod, "license_allows_agent_runs", lambda: (False, "No license is active."))
    assert credential_gate.agent_credentials_ready() == (False, "No license is active.")
    # A provider-row turn needs no Claude credential, but the license still applies.
    assert credential_gate.turn_refusal("openai_responses") == "No license is active."


def test_provider_leg_turn_needs_no_claude_credential(mock_db):
    assert credential_gate.turn_refusal("openrouter") is None
    assert credential_gate.turn_refusal("delegated") == credential_gate.NO_CLI_MESSAGE


def test_startup_records_a_blocking_setup_state(mock_db):
    ready, reason = credential_gate.record_startup(_cli(installed=False, signed_in=None))
    assert not ready
    assert credential_gate.startup_state == "setup_required"
    assert credential_gate.status_payload(_cli(installed=False, signed_in=None))["reason"] == reason


def test_check_prerequisites_records_the_mode_and_the_gate(mock_db, monkeypatch):
    from app.core import startup

    monkeypatch.setattr(claude_cli_status, "get_status", lambda force=False, resolver=None: _cli(signed_in=False))
    startup.check_prerequisites()
    assert claude_auth_mode.stored_mode() == "subscription"
    assert credential_gate.startup_state == "setup_required"
    assert credential_gate.startup_reason == credential_gate.NO_PROVIDER_MESSAGE


# ---------------------------------------------------------------------------
# /auth/status
# ---------------------------------------------------------------------------

async def test_auth_status_reports_the_gate(mock_db):
    from app.api import auth as auth_api

    request = MagicMock()
    request.cookies = {}
    request.headers = {}
    auth_api.reset_github_auth_cache()
    cli = {"installed": True, "signed_in": False, "email": None, "error": None}
    with patch.object(auth_api, "auth_service") as mock_auth, \
         patch.object(auth_api.owner_auth, "local_access", return_value=True), \
         patch.object(auth_api, "claude_cli_summary", AsyncMock(return_value=cli)):
        mock_auth.is_setup_required.return_value = False
        mock_auth.is_github_authenticated.return_value = False
        mock_auth.is_onboarding_complete.return_value = True
        status = await auth_api.get_auth_status(request)
    gate = status["agent_gate"]
    assert gate["ready"] is False
    assert gate["reason"] == credential_gate.NO_PROVIDER_MESSAGE
    assert gate["mode"] == "subscription"
    assert gate["any_provider_ready"] is False and gate["ready_providers"] == []
    assert status["has_any_credential"] is False


async def test_auth_status_is_ready_with_only_another_provider(mock_db):
    from app.api import auth as auth_api

    _provider_row("oa", "openai", auth_token="enc:key", checked=True)
    request = MagicMock()
    request.cookies = {}
    request.headers = {}
    auth_api.reset_github_auth_cache()
    cli = {"installed": True, "signed_in": False, "email": None, "error": None}
    with patch.object(auth_api, "auth_service") as mock_auth, \
         patch.object(auth_api.owner_auth, "local_access", return_value=True), \
         patch.object(auth_api, "claude_cli_summary", AsyncMock(return_value=cli)):
        mock_auth.is_setup_required.return_value = False
        mock_auth.is_github_authenticated.return_value = False
        mock_auth.is_onboarding_complete.return_value = True
        status = await auth_api.get_auth_status(request)
    gate = status["agent_gate"]
    assert gate["ready"] is True and gate["reason"] is None
    assert gate["claude_credential_ready"] is False
    assert gate["any_provider_ready"] is True and gate["ready_providers"] == ["openai"]
    assert status["has_any_credential"] is True


# ---------------------------------------------------------------------------
# Fresh install: every entry point refused
# ---------------------------------------------------------------------------

UNATTENDED_AND_INTERACTIVE_SOURCES = [
    "user", "queue", "api", "worktree", "connector", "automation", "wakeup",
    "provider_agent", "unpark", "auto_continue", "resume",
]


@contextmanager
def _real_pump_env():
    """SessionRuntime with the REAL pump; only the recorder, the task registry
    and the provider seam are stubbed. ``make_adapter`` fails the test if any
    code path tries to build a CLI process."""
    registry = MagicMock(name="task_registry")
    registry.is_user_stopped.return_value = False
    registry.stop_generation.return_value = 0
    registry.stop_invalidated.return_value = False
    registry.has_unresolved_background_launches.return_value = False
    registry.has_active_background_work.return_value = False
    registry.get_active_background_work.return_value = []
    registry.get_background_launches.return_value = set()

    recorder = MagicMock(name="recorder")
    recorder.start = AsyncMock()
    recorder.on_event = AsyncMock()
    recorder.close = AsyncMock()

    def _no_spawn(*_a, **_k):
        raise AssertionError("a refused turn must never build a CLI adapter")

    session_runtime.register_broadcaster(AsyncMock(name="broadcaster"))
    session_runtime.register_scheduler(MagicMock(name="on_turn_settled"))
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            session_runtime.TurnRecorder, "create_for_run", new=AsyncMock(return_value=recorder)))
        stack.enter_context(patch("app.core.session_runtime.record_parked", new=AsyncMock()))
        stack.enter_context(patch("app.core.session_runtime.start_idle_reader", new=MagicMock()))
        stack.enter_context(patch("app.core.session_runtime.stop_idle_reader", new=AsyncMock()))
        stack.enter_context(patch("app.core.task_registry.task_registry", new=registry))
        stack.enter_context(patch("app.core.turn_finalize.persist_turn_end_state", new=AsyncMock()))
        stack.enter_context(patch("app.core.provider.factory.make_adapter", new=_no_spawn))
        yield recorder
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)


def _seed(tmp_path, sid):
    database.create_profile("gate-profile", "Gate", None, {"cwd": str(tmp_path)})
    database.create_session(session_id=sid, profile_id="gate-profile", title="t")


@pytest.mark.parametrize("source", UNATTENDED_AND_INTERACTIVE_SOURCES)
async def test_fresh_install_refuses_a_turn_from_every_source(mock_db, tmp_path, source):
    sid = f"gate-{source}"
    _seed(tmp_path, sid)
    turn = turns_db.enqueue_turn(sid, source, prompt="hello", profile_id="gate-profile")
    turn_id = turn["id"]
    with _real_pump_env() as recorder:
        outcome = await session_runtime.execute(turn)
    assert outcome == "aborted"
    frames = [c.args[0] for c in recorder.on_event.await_args_list]
    assert [(f["type"], f["code"], f["message"]) for f in frames] == [
        ("error", credential_gate.SETUP_CODE, credential_gate.NO_CLI_MESSAGE)]
    assert turns_db.get_turn(turn_id)["state"] == "aborted"


async def test_fresh_install_refuses_the_rest_query_paths(mock_db, tmp_path):
    from app.api.query import _refuse_unready_leg, require_claude_auth
    from app.core import query_engine
    from fastapi import HTTPException

    database.create_profile("gate-profile", "Gate", None, {"cwd": str(tmp_path)})
    # The route dependency checks only the license; the Claude credential is
    # checked per leg once the route knows the agent (a Claude one here).
    assert await require_claude_auth() is None
    with pytest.raises(HTTPException) as exc:
        await _refuse_unready_leg("gate-profile", None)
    assert exc.value.status_code == 503 and exc.value.detail == credential_gate.NO_CLI_MESSAGE
    assert exc.value.headers == {"X-Error-Code": credential_gate.SETUP_CODE}

    with patch("app.core.provider.factory.make_adapter", side_effect=AssertionError("no spawn")):
        frames = [f async for f in query_engine.stream_query("hello", "gate-profile")]
        assert frames[-1] == credential_gate.refusal_event(credential_gate.NO_CLI_MESSAGE)
        with pytest.raises(credential_gate.AgentSetupRequired, match="Claude cannot run on this worker"):
            await query_engine.execute_query("hello", "gate-profile")


# ---------------------------------------------------------------------------
# default_run_model(): the one default for a run that names no model
# ---------------------------------------------------------------------------

def _claude_api_key_ready():
    claude_auth_mode.set_mode("api_key")
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")


def test_default_run_model_follows_the_ready_providers(mock_db):
    from app.core.model_registry import DEFAULT_MODEL

    assert credential_gate.default_run_model() == (None, DEFAULT_MODEL)  # nothing set up
    _openai_with_default()
    assert credential_gate.default_run_model() == ("oa", "gpt-5")
    assert credential_gate.default_run_selection() == "prov:oa:gpt-5"
    # Claude is just another candidate: with no usage history the usable
    # provider's own default still wins over the registry default.
    _claude_api_key_ready()
    assert credential_gate.default_run_model() == ("oa", "gpt-5")
    # Only the text-generation order puts Claude's registry default first.
    assert credential_gate.default_run_model(prefer_most_used=False) == (None, DEFAULT_MODEL)
    database.delete_model_provider("oa")
    assert credential_gate.default_run_selection() == DEFAULT_MODEL  # Claude only


def test_default_run_model_without_probe_reads_only_the_cached_probe(mock_db, monkeypatch):
    from app.core.model_registry import DEFAULT_MODEL

    _openai_with_default()
    # Never probed: the CLI is never run; unknown reads as ready for the
    # text-generation order, and the provider default still leads otherwise.
    monkeypatch.setattr(claude_cli_status, "probe",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no probe")))
    assert credential_gate.default_run_model(probe=False) == ("oa", "gpt-5")
    assert credential_gate.default_run_model(probe=False, prefer_most_used=False) == (
        None, DEFAULT_MODEL)
    monkeypatch.setattr(claude_cli_status, "_cached", _cli(installed=False, signed_in=None))
    assert credential_gate.default_run_model(probe=False) == ("oa", "gpt-5")


def test_default_run_model_never_raises(mock_db, monkeypatch):
    from app.core.model_registry import DEFAULT_MODEL

    monkeypatch.setattr(credential_gate, "claude_credential_ready",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert credential_gate.default_run_model() == (None, DEFAULT_MODEL)


def test_models_registry_default_is_a_ready_providers_model(mock_db):
    from app.core.model_registry import DEFAULT_MODEL, registry_payload

    _openai_with_default()
    claude_cli_status.get_status()  # startup's probe: the CLI is missing
    assert registry_payload()["default"] == "prov:oa:gpt-5"
    _claude_api_key_ready()
    assert registry_payload()["default"] == "prov:oa:gpt-5"  # no usage history
    database.delete_model_provider("oa")
    assert registry_payload()["default"] == DEFAULT_MODEL


async def test_rest_query_with_no_model_runs_on_a_ready_provider(mock_db, tmp_path):
    from app.api.query import _refuse_unready_leg
    from fastapi import HTTPException

    _openai_with_default()
    database.create_profile("gate-profile", "Gate", None, {"cwd": str(tmp_path)})
    assert await _refuse_unready_leg("gate-profile", None) is None
    # An explicit Claude pick is never rerouted and keeps the Claude message.
    with pytest.raises(HTTPException) as exc:
        await _refuse_unready_leg("gate-profile", {"model": "sonnet"})
    assert exc.value.status_code == 503 and exc.value.detail == credential_gate.NO_CLI_MESSAGE


def test_command_leg_with_no_model_uses_a_ready_provider(mock_db, tmp_path):
    from app.api.command_context import command_provider_kind

    _openai_with_default()
    claude_cli_status.get_status()
    database.create_profile("cmd-profile", "Cmd", None, {"cwd": str(tmp_path)})
    assert command_provider_kind(None, None, None, "cmd-profile", None) != "delegated"
    assert command_provider_kind(None, None, "sonnet", "cmd-profile", None) == "delegated"


def test_automation_with_no_model_reports_the_model_it_runs_on(mock_db):
    from app.core.automation_service import MODEL_SOURCE_DEFAULT, with_model_resolution
    from app.core.model_registry import DEFAULT_MODEL

    _openai_with_default()
    claude_cli_status.get_status()
    reported = with_model_resolution({"id": "a1"})
    assert (reported["effective_model"], reported["effective_provider_id"]) == ("gpt-5", "oa")
    assert reported["model_source"] == MODEL_SOURCE_DEFAULT
    explicit = with_model_resolution({"id": "a2", "model": "sonnet"})
    assert (explicit["effective_model"], explicit["effective_provider_id"]) == ("sonnet", None)
    _claude_api_key_ready()
    database.delete_model_provider("oa")
    reported = with_model_resolution({"id": "a1"})
    assert (reported["effective_model"], reported["effective_provider_id"]) == (DEFAULT_MODEL, None)


def test_text_generation_with_no_setting_uses_a_ready_provider(mock_db):
    from app.core.generation_options import TEXT_GENERATION_MODEL_KEY, text_generation_config
    from app.core.model_registry import DEFAULT_MODEL

    _openai_with_default()
    claude_cli_status.get_status()
    config = text_generation_config()
    assert (config["model"], config["provider"]) == ("gpt-5", "oa")
    database.set_system_setting(TEXT_GENERATION_MODEL_KEY, "sonnet")  # explicit: kept
    config = text_generation_config()
    assert (config["model"], config["provider"]) == ("sonnet", None)
    database.set_system_setting(TEXT_GENERATION_MODEL_KEY, "")
    _claude_api_key_ready()
    config = text_generation_config()
    assert (config["model"], config["provider"]) == (DEFAULT_MODEL, None)


# ---------------------------------------------------------------------------
# default_run_model(): the owner's most used model leads
# ---------------------------------------------------------------------------

def _chat(session_id, *, profile="mu-profile", **session_fields):
    if database.get_profile(profile) is None:
        database.create_profile(profile, "Most used", None, {})
    database.create_session(session_id, profile, **session_fields)
    return session_id


def _book(session_id, model, provider_id=None, *, turns=1, source="user", **attribution):
    """Settle ``turns`` turns of ``source`` on ``model``, as turn end does."""
    from app.core.query_persistence import usage_settlement_key
    from app.core.usage_attribution import usage_attribution

    fields = attribution or dict(usage_attribution(provider_id))
    for _ in range(turns):
        turn = turns_db.enqueue_turn(session_id, source)
        assert database.settle_turn_usage(
            session_id, usage_settlement_key(session_id, turn), model=model,
            tokens_in=10, tokens_out=5, cost_usd=0.01, turn_increment=1, **fields,
        )


def _claude_and_openai():
    _claude_api_key_ready()
    _provider_row("oa", "openai", auth_token="enc:key",
                  models=[{"id": "gpt-5", "label": "GPT-5"}, {"id": "gpt-5-mini", "label": "Mini"}],
                  default_model="gpt-5")


def test_most_used_openai_model_is_the_default_with_claude_set_up(mock_db):
    _claude_and_openai()
    _book(_chat("s1"), "gpt-5-mini", "oa", turns=4)
    _book(_chat("s2"), "claude-opus-5-5", turns=2)
    assert credential_gate.default_run_model() == ("oa", "gpt-5-mini")
    assert credential_gate.default_run_selection() == "prov:oa:gpt-5-mini"


def test_most_used_claude_model_is_the_default(mock_db):
    _claude_and_openai()
    _book(_chat("s1"), "claude-opus-5-5", turns=3)  # a full id counts for its alias
    _book(_chat("s2"), "opus", turns=2)
    _book(_chat("s3"), "gpt-5-mini", "oa", turns=4)
    assert credential_gate.default_run_model() == (None, "opus")
    from app.core.model_registry import registry_payload

    assert registry_payload()["default"] == "opus"


def test_a_tie_goes_to_the_most_recent_model(mock_db):
    _claude_and_openai()
    _book(_chat("s1"), "opus", turns=3)
    _book(_chat("s2"), "gpt-5-mini", "oa", turns=3)
    with database.get_db() as conn:
        conn.cursor().execute(
            "UPDATE usage_log SET created_at = '2099-01-01 00:00:00' WHERE model = 'opus'")
    assert credential_gate.default_run_model() == (None, "opus")


def test_most_used_provider_no_longer_usable_falls_to_the_provider_default(mock_db):
    _claude_and_openai()
    _book(_chat("s1"), "opus", turns=5)
    claude_auth_mode.set_mode("off")
    assert credential_gate.default_run_model() == ("oa", "gpt-5")


def test_most_used_model_dropped_from_its_row_falls_to_the_next(mock_db):
    _claude_and_openai()
    _book(_chat("s1"), "gpt-4o", "oa", turns=6)  # no longer in the row's list
    _book(_chat("s2"), "opus", turns=3)
    assert credential_gate.default_run_model() == (None, "opus")


def test_most_used_row_deleted_falls_to_the_next(mock_db):
    _claude_and_openai()
    _book(_chat("s1"), "gpt-5-mini", "oa", turns=6)
    _book(_chat("s2"), "opus", turns=3)
    database.delete_model_provider("oa")
    assert credential_gate.default_run_model() == (None, "opus")


def test_fewer_than_the_minimum_turns_keeps_the_provider_default(mock_db):
    _claude_and_openai()
    _book(_chat("s1"), "opus", turns=credential_gate.MOST_USED_MIN_TURNS - 1)
    assert credential_gate.default_run_model() == ("oa", "gpt-5")


def test_unchosen_turns_are_not_counted(mock_db):
    _claude_and_openai()
    _book(_chat("worker", is_worker=True, hidden=True), "opus", turns=5)
    _book(_chat("auto", automation_id="a1"), "opus", turns=5)
    _book(_chat("voice", mode="voice"), "opus", turns=5)
    _book(_chat("cmd", hidden=True), "opus", turns=5)
    chat = _chat("chat")
    _book(chat, "opus", turns=5, source="wakeup")
    _book(chat, "opus", turns=5, source="automation")
    _book(chat, "opus", turns=5, source="provider_agent")
    # One-shot text generation (titles, suggestions) books with no session.
    for _ in range(5):
        database.log_usage(None, None, "haiku", 1, 1, 0.0, 1, provider_type="claude")
    assert credential_gate.default_run_model() == ("oa", "gpt-5")
    # Typed messages in the same chat do count, forks included.
    _book(chat, "opus", turns=2, source="queue")
    _book(_chat("fork", parent_session_id="chat"), "opus", turns=1, source="api")
    credential_gate.invalidate_most_used_cache()
    assert credential_gate.default_run_model() == (None, "opus")


def test_nothing_usable_is_the_registry_default(mock_db):
    from app.core.model_registry import DEFAULT_MODEL

    _provider_row("oa", "openai", auth_token="enc:key",
                  models=[{"id": "gpt-5", "label": "GPT-5"}], default_model="gpt-5")
    _book(_chat("s1"), "gpt-5", "oa", turns=4)
    _book(_chat("s2"), "opus", turns=4)
    database.delete_model_provider("oa")
    claude_cli_status.get_status()  # the CLI is missing
    assert credential_gate.default_run_model() == (None, DEFAULT_MODEL)


def test_a_usage_read_failure_never_raises(mock_db, monkeypatch):
    _claude_and_openai()
    monkeypatch.setattr(database, "get_most_used_models",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pool down")))
    assert credential_gate.default_run_model() == ("oa", "gpt-5")
    assert credential_gate.most_used_model() is None


def test_the_ranking_is_cached_but_usability_is_live(mock_db, monkeypatch):
    _claude_and_openai()
    _book(_chat("s1"), "gpt-5-mini", "oa", turns=3)
    assert credential_gate.default_run_model() == ("oa", "gpt-5-mini")
    calls = []
    monkeypatch.setattr(database, "get_most_used_models", lambda *a, **k: calls.append(1) or [])
    assert credential_gate.default_run_model() == ("oa", "gpt-5-mini")
    assert calls == []  # served from the cache
    database.update_model_provider("oa", enabled=False)
    assert credential_gate.default_run_model() == (None, "sonnet")


def test_text_generation_ignores_the_most_used_model(mock_db):
    from app.core.generation_options import text_generation_config
    from app.core.model_registry import DEFAULT_MODEL

    _claude_and_openai()
    _book(_chat("s1"), "gpt-5-mini", "oa", turns=5)
    config = text_generation_config()
    assert (config["model"], config["provider"]) == (DEFAULT_MODEL, None)


# ---------------------------------------------------------------------------
# A key-based provider counts only with evidence its key works
# ---------------------------------------------------------------------------

def _status():
    return credential_gate.status_payload(_cli(installed=False, signed_in=None))


def test_a_rejected_key_is_not_a_ready_provider(mock_db):
    _provider_row("oa", "openai", auth_token="enc:fake")
    payload = _status()
    assert payload["ready_providers"] == [] and payload["any_provider_ready"] is False
    database.record_credential_check("oa", False)  # the catalog fetch got a 401
    payload = _status()
    assert payload["ready_providers"] == [] and payload["any_provider_ready"] is False
    assert payload["ready"] is False
    # Rejected wins over a curated model list.
    database.update_model_provider("oa", models_json=[{"id": "gpt-5", "label": "GPT-5"}])
    assert _status()["any_provider_ready"] is False


def test_an_accepted_key_is_ready_and_stays_ready_until_the_key_changes(mock_db):
    _provider_row("oa", "openai", auth_token="enc:key")
    database.record_credential_check("oa", True)
    assert _status()["ready_providers"] == ["openai"]
    # Editing other fields keeps the verdict; a new key must be checked again.
    database.update_model_provider("oa", name="Renamed")
    assert _status()["ready_providers"] == ["openai"]
    # Resending the same endpoint or the same key is not a change.
    database.update_model_provider("oa", base_url="https://example.invalid/v1")
    assert _status()["ready_providers"] == ["openai"]
    database.update_model_provider("oa", auth_token="enc:key")
    assert _status()["ready_providers"] == ["openai"]
    database.update_model_provider("oa", auth_token="enc:other")
    assert _status()["ready_providers"] == []
    database.record_credential_check("oa", True)
    database.update_model_provider("oa", base_url="https://other.invalid/v1")
    assert _status()["ready_providers"] == []


async def test_a_fetch_for_a_replaced_key_records_nothing(mock_db, monkeypatch):
    """N2: an ``ok`` for the old key must not mark the new key working."""
    _provider_row("oa", "openai", auth_token="enc:old")
    mp_api = _fetch_with(monkeypatch, None)

    async def fetch_while_the_key_changes(*a, **k):
        database.update_model_provider("oa", auth_token="enc:new")
        return [{"id": "gpt-5", "label": "GPT-5"}]

    monkeypatch.setattr(mp_api, "fetch_upstream_models", fetch_while_the_key_changes)
    await mp_api.fetch_models("oa", token="")
    assert database.get_model_provider("oa").get("credential_status") is None
    assert _status()["ready_providers"] == []


def test_a_key_with_curated_models_counts_without_a_recorded_check(mock_db):
    _openai_with_default()  # an install that curated models before checks were recorded
    assert _status()["ready_providers"] == ["openai"]


def test_keyless_gateway_counts_unless_rejected(mock_db):
    _provider_row("gw1", "custom")
    assert _status()["ready_providers"] == ["gw:gw1"]
    database.record_credential_check("gw1", False)
    assert _status()["ready_providers"] == []


def _fetch_with(monkeypatch, outcome):
    from app.api import model_providers as mp_api

    async def fake_fetch(*a, **k):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(mp_api, "fetch_upstream_models", fake_fetch)
    monkeypatch.setattr(mp_api, "_generic_auth_token", lambda row: "sk-test")
    return mp_api


async def test_fetch_models_records_the_verdict(mock_db, monkeypatch):
    from fastapi import HTTPException

    _provider_row("oa", "openai", auth_token="enc:key")
    mp_api = _fetch_with(monkeypatch, [{"id": "gpt-5", "label": "GPT-5"}])
    await mp_api.fetch_models("oa", token="")
    assert _status()["ready_providers"] == ["openai"]
    # A previously good key through a network error or a 5xx stays ready.
    for code in ("timeout", "http_503", "http_429"):
        mp_api = _fetch_with(monkeypatch, mp_api.UpstreamModelsError(code))
        with pytest.raises(HTTPException):
            await mp_api.fetch_models("oa", token="")
        assert _status()["ready_providers"] == ["openai"], code
    # A 401 marks it rejected.
    mp_api = _fetch_with(monkeypatch, mp_api.UpstreamModelsError("http_401"))
    with pytest.raises(HTTPException):
        await mp_api.fetch_models("oa", token="")
    payload = _status()
    assert payload["ready_providers"] == [] and payload["any_provider_ready"] is False


def test_a_rejected_provider_is_never_the_default_model(mock_db):
    from app.core.model_registry import DEFAULT_MODEL

    _openai_with_default()
    _book(_chat("s1"), "gpt-5", "oa", turns=4)
    assert credential_gate.default_run_model() == ("oa", "gpt-5")
    database.record_credential_check("oa", False)
    assert credential_gate.default_run_model() == (None, DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# D4: subscriptions are the owner's own
#
# A run for anyone but the owner (an API user, or a connector message, which
# runs as its connector's API user) never uses a subscription sign-in: Claude
# on the CLI in subscription mode, a ChatGPT/Codex row or a SuperGrok row.
# API keys stay allowed, and the owner is allowed everything.
# ---------------------------------------------------------------------------

OWNER = None
API_USER = "u-d4"
CONNECTOR_USER = "connector:discord:d4"
REQUESTERS = [OWNER, API_USER, CONNECTOR_USER]


def _requesters():
    from app.api.auth import hash_api_key

    database.create_api_user(API_USER, "Friend", hash_api_key("aih_d4_friend"))
    database.create_api_user(CONNECTOR_USER, "Discord", hash_api_key("aih_d4_connector"))


def _claude(mode):
    def setup():
        claude_auth_mode.set_mode(mode)
        if mode == "api_key":
            database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
        return "sonnet"
    return setup


def _row(provider_type, model, **fields):
    def setup():
        claude_auth_mode.set_mode("subscription")  # never decides a provider row's run
        # A plain token: the Anthropic-compatible rows decrypt theirs at build time.
        _provider_row("p", provider_type, auth_token="sk-test-plain",
                      models=[{"id": model, "label": model}], default_model=model, **fields)
        return f"prov:p:{model}"
    return setup


SUBSCRIPTION_KINDS = {
    "claude_cli_subscription": _claude("subscription"),
    "chatgpt_row": _row("chatgpt", "gpt-5"),
    "openai_row_chatgpt_sign_in": _row("openai", "gpt-5", auth_mode="chatgpt"),
    "grok_row": _row("grok", "grok-4"),
    "xai_row_grok_sign_in": _row("xai", "grok-4", auth_mode="grok"),
    "anthropic_row_plan_login": _row("anthropic", "claude-sonnet-4-5", auth_mode="oauth"),
}
API_KEY_KINDS = {
    "claude_cli_api_key": _claude("api_key"),
    "claude_cli_cloud": _claude("cloud"),
    "anthropic_api_key_row": _row("anthropic", "claude-sonnet-4-5"),
    "openai_api_key_row": _row("openai", "gpt-5"),
    "xai_api_key_row": _row("xai", "grok-4"),
    "openrouter_row": _row("openrouter", "meta-llama/llama-3.3-70b"),
    "local_model_row": _row("custom", "llama-3"),
}


@pytest.mark.parametrize("requester", REQUESTERS, ids=["owner", "api_user", "connector"])
@pytest.mark.parametrize("kind", sorted(SUBSCRIPTION_KINDS) + sorted(API_KEY_KINDS))
def test_requester_against_every_provider_kind(mock_db, tmp_path, requester, kind):
    _requesters()
    setup = SUBSCRIPTION_KINDS.get(kind) or API_KEY_KINDS[kind]
    model = setup()
    options = _build(tmp_path, {"model": model})
    is_subscription = kind in SUBSCRIPTION_KINDS
    assert credential_gate.options_use_subscription(options) is is_subscription
    assert credential_gate.selection_uses_subscription(model) is is_subscription

    refusal = credential_gate.requester_refusal(options, requester)
    if requester is not OWNER and is_subscription:
        assert refusal == credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE
        # The run-start check every engine path calls refuses it first.
        assert credential_gate.refusal_for_options(options, requester) == refusal
    else:
        assert refusal is None
        # With the leg itself set up, nothing else refuses the run.
        with patch.object(credential_gate, "claude_credential_ready", return_value=(True, None)):
            assert credential_gate.refusal_for_options(options, requester) is None


def test_the_refusal_message_and_frame():
    assert credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE == (
        "This model uses the owner's personal subscription and can't run for other "
        "people. Pick a model on an API-key provider.")
    assert credential_gate.refusal_event(credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE) == {
        "type": "error", "message": credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE,
        "code": credential_gate.SUBSCRIPTION_OWNER_ONLY_CODE}
    assert credential_gate.refusal_event(credential_gate.NO_CLI_MESSAGE)["code"] == credential_gate.SETUP_CODE


def test_an_unreadable_provider_choice_is_refused_for_other_people(mock_db, tmp_path):
    _requesters()
    _row("openai", "gpt-5")()
    options = _build(tmp_path, {"model": "prov:p:gpt-5"})
    with patch.object(credential_gate, "options_use_subscription", side_effect=RuntimeError("db down")):
        assert credential_gate.requester_refusal(options, API_USER) == credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE
        assert credential_gate.requester_refusal(options, OWNER) is None


@pytest.mark.parametrize("source,payload", [
    ("api", {"api_user_id": API_USER}),
    ("connector", {"api_user_id": CONNECTOR_USER}),
    ("wakeup", None),  # no principal on the turn: the session's key decides
])
async def test_a_turn_for_someone_else_on_a_subscription_is_refused_end_to_end(
    mock_db, tmp_path, source, payload,
):
    """Through the real SessionRuntime and pump: refused before any CLI is built."""
    _requesters()
    claude_auth_mode.set_mode("subscription")
    owner_key = (payload or {}).get("api_user_id", API_USER)
    database.create_profile("gate-profile", "Gate", None, {"cwd": str(tmp_path), "model": "sonnet"})
    database.create_session(session_id="d4", profile_id="gate-profile", title="t", api_user_id=owner_key)
    turn = turns_db.enqueue_turn("d4", source, prompt="hello", profile_id="gate-profile", payload=payload)
    with _real_pump_env() as recorder, \
         patch.object(credential_gate, "claude_credential_ready", return_value=(True, None)):
        outcome = await session_runtime.execute(turn)
    assert outcome == "aborted"
    frames = [c.args[0] for c in recorder.on_event.await_args_list]
    assert [(f["type"], f["code"], f["message"]) for f in frames] == [
        ("error", credential_gate.SUBSCRIPTION_OWNER_ONLY_CODE, credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE)]


async def test_the_rest_query_paths_refuse_an_api_user_on_a_subscription(mock_db, tmp_path):
    from app.core import query_engine

    _requesters()
    claude_auth_mode.set_mode("subscription")
    database.create_profile("gate-profile", "Gate", None, {"cwd": str(tmp_path), "model": "sonnet"})
    with patch("app.core.provider.factory.make_adapter", side_effect=AssertionError("no spawn")), \
         patch.object(credential_gate, "claude_credential_ready", return_value=(True, None)):
        frames = [f async for f in query_engine.stream_query("hello", "gate-profile", api_user_id=API_USER)]
        assert frames[-1] == credential_gate.refusal_event(credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE)
        with pytest.raises(credential_gate.AgentSetupRequired, match="owner's personal subscription"):
            await query_engine.execute_query("hello", "gate-profile", api_user_id=API_USER)


# Existing setups: the settings warning and the save check.

def test_subscription_warning_for_a_saved_setup(mock_db):
    from app.core import api_user_scope

    _row("chatgpt", "gpt-5")()
    _provider_row("oa", "openai", auth_token="enc:key", models=[{"id": "gpt-5", "label": "GPT-5"}])
    assert api_user_scope.subscription_warning({"model": "prov:p:gpt-5"}) == api_user_scope.SUBSCRIPTION_MODEL_WARNING
    assert api_user_scope.subscription_warning({"model": "sonnet"}) == api_user_scope.SUBSCRIPTION_MODEL_WARNING
    assert api_user_scope.subscription_warning({"model": "prov:oa:gpt-5"}) is None
    claude_auth_mode.set_mode("api_key")
    assert api_user_scope.subscription_warning({"model": "sonnet"}) is None
    # Agent default: each assigned agent's saved model decides.
    database.create_profile("sub-agent", "Codex helper", None, {"model": "prov:p:gpt-5"})
    database.create_profile("key-agent", "Key helper", None, {"model": "gpt-5", "provider": "oa"})
    assert api_user_scope.subscription_warning({"model": None, "profile_ids": ["key-agent"]}) is None
    assert api_user_scope.subscription_warning({"model": None, "profile_ids": ["key-agent", "sub-agent"]}) == (
        api_user_scope.SUBSCRIPTION_AGENT_WARNING.format(agent="Codex helper"))
    # The key's own API-key model overrides a subscription agent.
    assert api_user_scope.subscription_warning({"model": "prov:oa:gpt-5", "profile_ids": ["sub-agent"]}) is None
    assert api_user_scope.subscription_warning(None) is None


def test_api_users_endpoint_refuses_a_subscription_model_and_warns_on_old_setups(authenticated_client):
    from app.core import api_user_scope

    client = authenticated_client
    _row("chatgpt", "gpt-5")()
    _provider_row("oa", "openai", auth_token="enc:key", models=[{"id": "gpt-5", "label": "GPT-5"}])

    refused = client.post("/api/v1/api-users", json={"name": "Friend", "model": "prov:p:gpt-5"})
    assert refused.status_code == 400
    assert refused.json()["detail"] == credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE

    created = client.post("/api/v1/api-users", json={"name": "Friend", "model": "prov:oa:gpt-5"})
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["subscription_warning"] is None
    patched = client.patch(f"/api/v1/api-users/{user['id']}", json={"model": "prov:p:gpt-5"})
    assert patched.status_code == 400

    # A key saved on a subscription before this check keeps its setting but
    # shows the warning (and its runs are refused at start).
    database.update_api_user(user["id"], model="prov:p:gpt-5")
    listed = {u["id"]: u for u in client.get("/api/v1/api-users").json()}
    assert listed[user["id"]]["subscription_warning"] == api_user_scope.SUBSCRIPTION_MODEL_WARNING
    assert client.get(f"/api/v1/api-users/{user['id']}").json()["subscription_warning"]
    # Moving it to an API-key model clears the warning.
    fixed = client.patch(f"/api/v1/api-users/{user['id']}", json={"model": "prov:oa:gpt-5"})
    assert fixed.status_code == 200 and fixed.json()["subscription_warning"] is None


# The owner's own account on a connector. A connector message runs as the
# connector's API user, so it is refused on a subscription unless its sender
# is one of the owner's own accounts saved on that connector
# (``owner_sender_ids``), matched by the platform's immutable user id only.

OWNER_TG_ID = "111"


def _owner_connector(owner_ids=(OWNER_TG_ID,), connector_type="telegram", api_user_id=CONNECTOR_USER,
                     connector_id="c-d4"):
    database.create_connector(connector_id, connector_type, "Owner bot",
                              {"owner_sender_ids": list(owner_ids)}, [], api_user_id=api_user_id)
    return connector_id


def _sender(platform_user_id, connector_id="c-d4"):
    return {"connector_id": connector_id, "platform_user_id": platform_user_id}


def test_only_the_owners_own_sender_id_counts_as_the_owner(mock_db):
    _requesters()
    _owner_connector(owner_ids=[OWNER_TG_ID, "@owner_handle"])
    is_owner = credential_gate.connector_sender_is_owner
    assert is_owner(_sender(OWNER_TG_ID), CONNECTOR_USER) is True
    assert is_owner(_sender(" 111 "), CONNECTOR_USER) is True
    # Anyone else on the platform.
    assert is_owner(_sender("222"), CONNECTOR_USER) is False
    # A turn for another API user, or one with no principal, never borrows it.
    assert is_owner(_sender(OWNER_TG_ID), API_USER) is False
    assert is_owner(_sender(OWNER_TG_ID), None) is False
    # A sender that names a different (or unknown) connector.
    assert is_owner(_sender(OWNER_TG_ID, "c-missing"), CONNECTOR_USER) is False
    assert is_owner({"platform_user_id": OWNER_TG_ID}, CONNECTOR_USER) is False
    assert is_owner(None, CONNECTOR_USER) is False
    assert is_owner("111", CONNECTOR_USER) is False


def test_a_spoofed_name_or_the_no_sender_fallback_is_not_the_owner(mock_db):
    _requesters()
    # Even if the owner typed a name or the fallback id into the list.
    _owner_connector(owner_ids=[OWNER_TG_ID, "Owner Name", "unknown"])
    is_owner = credential_gate.connector_sender_is_owner
    # Display names and usernames are never on the turn; only the id is matched.
    spoof = {**_sender("999"), "platform_user_name": "Owner Name", "username": OWNER_TG_ID}
    assert is_owner(spoof, CONNECTOR_USER) is False
    # Telegram's "unknown" (no sender on the update) and an empty id never match.
    assert is_owner(_sender("unknown"), CONNECTOR_USER) is False
    assert is_owner(_sender(""), CONNECTOR_USER) is False
    assert is_owner(_sender(None), CONNECTOR_USER) is False


def test_an_empty_list_or_a_connector_without_a_stable_sender_is_never_the_owner(mock_db):
    from app.core import connector_service

    _requesters()
    _owner_connector(owner_ids=[], connector_id="c-empty")
    assert credential_gate.connector_sender_is_owner(_sender(OWNER_TG_ID, "c-empty"), CONNECTOR_USER) is False
    for kind in ("bluebubbles", "line", "msteams", "webhook", "homeassistant"):
        assert kind not in connector_service.OWNER_SENDER_TYPES
        _owner_connector(connector_type=kind, connector_id=f"c-{kind}")
        assert credential_gate.connector_sender_is_owner(_sender(OWNER_TG_ID, f"c-{kind}"), CONNECTOR_USER) is False
    assert connector_service.OWNER_SENDER_TYPES == {"telegram", "discord", "slack", "whatsapp", "signal", "imessage"}


@pytest.mark.parametrize("case,sender,owner_ids,refused", [
    ("owner_sender", OWNER_TG_ID, [OWNER_TG_ID], False),
    ("other_sender", "222", [OWNER_TG_ID], True),
    ("empty_list", OWNER_TG_ID, [], True),
    ("no_sender_on_turn", None, [OWNER_TG_ID], True),
])
async def test_a_connector_turn_on_a_subscription_runs_only_for_the_owners_account(
    mock_db, tmp_path, case, sender, owner_ids, refused,
):
    """Through the real SessionRuntime and pump: the owner's own account gets
    past the subscription check; anyone else is refused before any CLI."""
    _requesters()
    claude_auth_mode.set_mode("subscription")
    _owner_connector(owner_ids=owner_ids)
    database.create_profile("gate-profile", "Gate", None, {"cwd": str(tmp_path), "model": "sonnet"})
    database.create_session(session_id="d4c", profile_id="gate-profile", title="t", api_user_id=CONNECTOR_USER)
    payload = {"api_user_id": CONNECTOR_USER}
    if sender is not None:
        payload["connector_sender"] = _sender(sender)
    turn = turns_db.enqueue_turn("d4c", "connector", prompt="hello", profile_id="gate-profile", payload=payload)
    built = []

    def _reached(*_a, **_k):
        built.append(True)
        raise RuntimeError("stop after the gate")

    with _real_pump_env() as recorder, \
         patch.object(credential_gate, "claude_credential_ready", return_value=(True, None)), \
         patch("app.core.provider.factory.make_adapter", new=_reached):
        try:
            await session_runtime.execute(turn)
        except RuntimeError:
            pass
    codes = [c.args[0].get("code") for c in recorder.on_event.await_args_list]
    if refused:
        assert codes == [credential_gate.SUBSCRIPTION_OWNER_ONLY_CODE]
        assert built == []
    else:
        assert credential_gate.SUBSCRIPTION_OWNER_ONLY_CODE not in codes
        assert built == [True]


async def test_the_sender_is_honoured_only_on_a_connector_turn(mock_db, tmp_path):
    """An API key's own turn can't claim to be the owner by carrying a sender."""
    _requesters()
    claude_auth_mode.set_mode("subscription")
    _owner_connector()
    database.create_profile("gate-profile", "Gate", None, {"cwd": str(tmp_path), "model": "sonnet"})
    database.create_session(session_id="d4a", profile_id="gate-profile", title="t", api_user_id=CONNECTOR_USER)
    payload = {"api_user_id": CONNECTOR_USER, "connector_sender": _sender(OWNER_TG_ID)}
    turn = turns_db.enqueue_turn("d4a", "api", prompt="hello", profile_id="gate-profile", payload=payload)
    with _real_pump_env() as recorder, \
         patch.object(credential_gate, "claude_credential_ready", return_value=(True, None)):
        assert await session_runtime.execute(turn) == "aborted"
    assert [c.args[0]["code"] for c in recorder.on_event.await_args_list] == [
        credential_gate.SUBSCRIPTION_OWNER_ONLY_CODE]


def test_connector_warning_and_save_check_follow_the_owner_account(authenticated_client):
    from app.core import api_user_scope

    client = authenticated_client
    _row("chatgpt", "gpt-5")()
    _requesters()
    # A plain API key still can't be set to a subscription model.
    assert client.patch(f"/api/v1/api-users/{API_USER}", json={"model": "prov:p:gpt-5"}).status_code == 400
    # The connector's key can: only the owner's own messages will use it.
    _owner_connector(owner_ids=[])
    saved = client.patch(f"/api/v1/api-users/{CONNECTOR_USER}", json={"model": "prov:p:gpt-5"})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["owner_sender_connector"] is True
    # No account saved yet: every message is refused, so it says how to fix it.
    assert body["subscription_warning"] == api_user_scope.CONNECTOR_NO_OWNER_WARNING.format(connector="Owner bot")
    database.update_connector("c-d4", config={"owner_sender_ids": [OWNER_TG_ID]})
    listed = {u["id"]: u for u in client.get("/api/v1/api-users").json()}
    assert listed[CONNECTOR_USER]["subscription_warning"] is None
    assert listed[API_USER]["owner_sender_connector"] is False
    # A connector that can't recognise a sender keeps the plain warning.
    database.update_api_user(API_USER, model="prov:p:gpt-5")
    _owner_connector(connector_type="msteams", api_user_id=API_USER, connector_id="c-teams")
    assert client.get(f"/api/v1/api-users/{API_USER}").json()["subscription_warning"] == (
        api_user_scope.SUBSCRIPTION_MODEL_WARNING)
