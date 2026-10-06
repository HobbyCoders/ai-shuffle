"""Per-session run binding (v79): model, effort and permission mode.

Agents used to pin a model, an effort level and a permission mode. They no
longer do — an agent is configuration (prompt, tools, memory), and the run
binding is chosen when a session is created and stored on the session row.

Covers:
- creation stamps ``sessions.model`` / ``effort`` / ``permission_mode``, and a
  ``prov:<pid>:<mid>`` model composite settles ``provider_id`` at the same time;
- ``set_session_run_binding`` writes a mid-session switch back (rebinding the
  provider with it) and never erases a leg the caller left out;
- ``build_options_from_profile`` resolves all three from the session row, a
  per-turn override still wins, and a pre-v79 session with no row binding falls
  back to ``launched_model`` and then the agent's legacy config;
- ``turn_manager.submit`` persists the binding for human-facing sources only,
  so an automation or a wakeup replay never re-binds a session behind the user.
"""

import asyncio
from unittest.mock import patch

import pytest

import app.core.query_options as qo
from app.core import encryption
from app.core import turn_manager
from app.db import database as db
from tests.core.helpers import make_profile


def _mk_provider(provider_id="op", *, token="gw-secret"):
    encryption.set_encryption_key("pw", db)
    return db.create_model_provider(
        provider_id=provider_id,
        name="OpenRouter",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api",
        auth_token=encryption.encrypt_value(token),
        models=[{
            "id": "openai/gpt-5.6",
            "label": "GPT-5.6",
            "context_window": 1_000_000,
        }],
        enabled=True,
    )


def _build_options(profile, *, session_id=None, overrides=None):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=profile, session_id=session_id, overrides=overrides
        )
    return opts


# ---------------------------------------------------------------------------
# Creation stamps the binding
# ---------------------------------------------------------------------------


class TestBindingStampedAtCreation:
    def test_stamps_model_effort_and_permission_mode(self, mock_db):
        make_profile("agent", {})
        row = db.create_session(
            "s-new",
            profile_id="agent",
            model="opus",
            effort="high",
            permission_mode="acceptEdits",
            context_window=200_000,
        )
        assert (
            row["model"], row["effort"], row["permission_mode"], row["context_window"]
        ) == (
            "opus",
            "high",
            "acceptEdits",
            200_000,
        )
        assert db.get_session("s-new")["model"] == "opus"

    def test_composite_model_binds_the_provider_too(self, mock_db):
        _mk_provider("op")
        make_profile("agent", {})
        row = db.create_session(
            "s-composite", profile_id="agent", model="prov:op:openai/gpt-5.6"
        )
        assert row["provider_id"] == "op"
        # The RAW pick is stored: the turn decodes it the same way an override is
        # decoded, so the model half survives ids that contain colons.
        assert row["model"] == "prov:op:openai/gpt-5.6"

    def test_no_pick_leaves_the_binding_null(self, mock_db):
        make_profile("agent", {})
        row = db.create_session("s-bare", profile_id="agent")
        assert row["model"] is None
        assert row["effort"] is None
        assert row["permission_mode"] is None
        assert row["context_window"] is None

    def test_explicit_provider_beats_a_composite(self, mock_db):
        """A Task worker names its provider outright; the model rides along."""
        _mk_provider("op")
        _mk_provider("other")
        make_profile("agent", {})
        row = db.create_session(
            "s-worker",
            profile_id="agent",
            provider_id="other",
            derive_provider_from_profile=False,
            model="prov:op:openai/gpt-5.6",
        )
        assert row["provider_id"] == "other"


# ---------------------------------------------------------------------------
# Mid-session switches are written back
# ---------------------------------------------------------------------------


class TestSetSessionRunBinding:
    def test_writes_all_three_legs(self, mock_db):
        make_profile("agent", {})
        db.create_session("s1", profile_id="agent", model="sonnet")
        db.set_session_run_binding(
            "s1",
            model="opus",
            effort="max",
            permission_mode="plan",
            context_window=1_000_000,
        )
        row = db.get_session("s1")
        assert (
            row["model"], row["effort"], row["permission_mode"], row["context_window"]
        ) == (
            "opus",
            "max",
            "plan",
            1_000_000,
        )

    def test_composite_rebinds_the_provider(self, mock_db):
        _mk_provider("op")
        make_profile("agent", {})
        db.create_session("s2", profile_id="agent", model="opus")
        assert db.get_session("s2")["provider_id"] is None

        db.set_session_run_binding("s2", model="prov:op:openai/gpt-5.6")
        assert db.get_session("s2")["provider_id"] == "op"

    def test_native_pick_clears_a_gateway_binding(self, mock_db):
        """Switching a provider session back to a Claude model must clear the
        row's provider too, or the stored binding would describe a leg the turn
        no longer runs on."""
        _mk_provider("op")
        make_profile("agent", {})
        db.create_session(
            "s-clear", profile_id="agent", model="prov:op:openai/gpt-5.6"
        )
        assert db.get_session("s-clear")["provider_id"] == "op"

        db.set_session_run_binding("s-clear", model="opus")
        row = db.get_session("s-clear")
        assert row["model"] == "opus"
        assert row["provider_id"] is None

    def test_bare_provider_model_keeps_the_binding(self, mock_db):
        _mk_provider("op")
        make_profile("agent", {})
        db.create_session(
            "s-keep", profile_id="agent", model="prov:op:openai/gpt-5.6"
        )
        db.set_session_run_binding("s-keep", model="openai/gpt-5.6")
        assert db.get_session("s-keep")["provider_id"] == "op"

    def test_omitted_legs_are_left_alone(self, mock_db):
        """A turn that carried no effort override must not erase the one the
        session is bound to."""
        make_profile("agent", {})
        db.create_session("s3", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s3", model="sonnet")
        row = db.get_session("s3")
        assert row["model"] == "sonnet"
        assert row["effort"] == "high"


# ---------------------------------------------------------------------------
# Turn resolution reads the session, not the agent
# ---------------------------------------------------------------------------


class TestTurnResolution:
    def test_session_binding_drives_the_turn(self, mock_db, tmp_path):
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session(
            "s-bound",
            profile_id="agent",
            model="opus",
            effort="high",
            permission_mode="acceptEdits",
            context_window=200_000,
        )
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="s-bound",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.model == "opus"
        assert opts.effort == "high"
        assert opts.permission_mode == "acceptEdits"
        assert opts.aih_context_window == 200_000
        assert opts.env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] == "1"
        assert opts.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "200000"

    def test_agent_config_never_overrides_the_session(self, mock_db, tmp_path):
        """A legacy agent that still carries a model does not reroute a session
        that has a binding of its own."""
        config = {
            "cwd": str(tmp_path),
            "model": "haiku",
            "effort": "low",
            "permission_mode": "plan",
        }
        make_profile("legacy", config)
        db.create_session(
            "s-wins",
            profile_id="legacy",
            model="opus",
            effort="max",
            permission_mode="bypassPermissions",
        )
        opts = _build_options(
            {"id": "legacy", "config": config},
            session_id="s-wins",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.model == "opus"
        assert opts.effort == "max"
        assert opts.permission_mode == "bypassPermissions"

    def test_per_turn_override_still_wins(self, mock_db, tmp_path):
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session(
            "s-ovr",
            profile_id="agent",
            model="opus",
            permission_mode="plan",
            context_window=200_000,
        )
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="s-ovr",
            overrides={
                "cwd": str(tmp_path),
                "model": "sonnet",
                "permission_mode": "acceptEdits",
                "context_window": 1_000_000,
            },
        )
        assert opts.model == "sonnet"
        assert opts.permission_mode == "acceptEdits"
        assert opts.aih_context_window == 1_000_000
        assert opts.env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] == "0"

    @pytest.mark.asyncio
    async def test_explicit_window_on_api_stream_wins_and_is_saved(self, mock_db, tmp_path):
        """The real producer of an explicit per-message window outside the turn
        queue: ``POST /conversation/stream`` on an existing chat. Its pick runs
        this turn and becomes the pin; a later server turn carrying the older
        200k copy cannot shrink it back, and a newer 200k pick still wins."""
        from app.api.sessions_permissions import RunSettingsUpdate, apply_run_settings
        from app.core import credential_gate, query_engine
        from app.db import turns

        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session("s-api", profile_id="agent", model="opus", context_window=200_000)
        built = []

        async def capture(options):
            built.append(options)
            return "stop before spawn"

        with patch.object(qo, "get_security_instructions", return_value=""), \
                patch.object(credential_gate, "refusal_for_options_async", capture):
            frames = [frame async for frame in query_engine.stream_query(
                "hello", "agent", session_id="s-api",
                overrides={"cwd": str(tmp_path), "context_window": 1_000_000})]
        assert frames[-1]["message"] == "stop before spawn"
        assert built[0].aih_context_window == 1_000_000
        assert db.get_session("s-api")["context_window"] == 1_000_000

        stale = turn_manager._submit_blocking(
            "s-api", "wakeup", prompt="later", profile_id="agent",
            overrides={"cwd": str(tmp_path), "context_window": 200_000})
        assert turns.get_turn(stale)["overrides"]["context_window"] == 1_000_000
        await apply_run_settings("s-api", RunSettingsUpdate(context_window=200_000))
        started = turns.start_turn(stale)
        assert started["overrides"]["context_window"] == 200_000
        opts = _build_options({"id": "agent", "config": {"cwd": str(tmp_path)}},
                              session_id="s-api", overrides=started["overrides"])
        assert opts.aih_context_window == 200_000

    def test_stale_window_is_ignored_for_an_ineligible_model(self, mock_db, tmp_path):
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session(
            "s-haiku", profile_id="agent", model="haiku", context_window=1_000_000
        )
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="s-haiku",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.aih_context_window is None
        assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in opts.env

    def test_pre_v79_session_falls_back_to_launched_model(self, mock_db, tmp_path):
        """No row binding (every session created before the migration): the model
        it actually last ran on wins over the agent's legacy config, so upgrading
        never reroutes an in-flight conversation."""
        config = {"cwd": str(tmp_path), "model": "haiku"}
        make_profile("legacy", config)
        db.create_session("s-old", profile_id="legacy")
        db.update_session("s-old", launched_model="opus")

        opts = _build_options(
            {"id": "legacy", "config": config},
            session_id="s-old",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.model == "opus"

    def test_pre_v79_session_falls_back_to_agent_config(self, mock_db, tmp_path):
        config = {
            "cwd": str(tmp_path),
            "model": "haiku",
            "effort": "low",
            "permission_mode": "plan",
        }
        make_profile("legacy", config)
        db.create_session("s-old2", profile_id="legacy")

        opts = _build_options(
            {"id": "legacy", "config": config},
            session_id="s-old2",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.model == "haiku"
        assert opts.effort == "low"
        assert opts.permission_mode == "default"

    def test_nothing_bound_anywhere_uses_the_registry_default(
        self, mock_db, tmp_path
    ):
        from app.core.model_registry import DEFAULT_MODEL

        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session("s-empty", profile_id="agent")
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="s-empty",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.model == DEFAULT_MODEL
        assert opts.permission_mode == "default"

    def test_session_composite_rebinds_the_turns_provider(self, mock_db, tmp_path):
        """The stored pick gets the same PA13 treatment an override does."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session(
            "s-gw", profile_id="agent", model="prov:op:openai/gpt-5.6"
        )
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="s-gw",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.model == "openai/gpt-5.6"
        assert opts.env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"

    def test_gpt_standard_context_uses_256k(self, mock_db, tmp_path):
        _mk_provider("op")
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session(
            "s-gpt-standard",
            profile_id="agent",
            model="prov:op:openai/gpt-5.6",
            context_window=256_000,
        )
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="s-gpt-standard",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.aih_context_window == 256_000
        assert opts.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "256000"

    @pytest.mark.parametrize("model_id,window", [
        ("gpt-6-astra", 256_000), ("gpt-6-astra", 1_000_000),
        ("gpt-6.1-sol", 272_000), ("gpt-6.1-sol", 1_000_000),
    ])
    def test_astra_context_choice_reaches_the_runtime(self, mock_db, tmp_path, model_id, window):
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="astra", name="OpenAI", provider_type="openai",
            base_url="https://api.openai.com/v1",
            auth_token=encryption.encrypt_value("test-token"),
            models=[{"id": model_id, "label": model_id, "context_window": 272000}],
            enabled=True,
        )
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session(
            "s-astra", profile_id="agent", model=f"prov:astra:{model_id}",
            context_window=window,
        )
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="s-astra", overrides={"cwd": str(tmp_path)},
        )
        assert opts.aih_context_window == window
        assert opts.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == str(window)


# ---------------------------------------------------------------------------
# Only a person's turn re-binds the session
# ---------------------------------------------------------------------------


class TestSubmitPersistsTheBinding:
    def test_user_turn_persists_the_pick(self, mock_db):
        make_profile("agent", {})
        db.create_session("s-user", profile_id="agent", model="sonnet")
        turn_manager.submit(
            "s-user",
            "user",
            prompt="hi",
            profile_id="agent",
            overrides={"model": "opus", "effort": "high", "context_window": 200_000},
            no_kick=True,
        )
        row = db.get_session("s-user")
        assert row["model"] == "opus"
        assert row["effort"] == "high"
        assert row["context_window"] == 200_000

    def test_outdated_window_does_not_replace_a_newer_pin(self, mock_db):
        """#1895: the phone picked 200k; a tab that still saw 1M sends 1M."""
        make_profile("agent", {})
        db.create_session("s-stale", profile_id="agent", model="opus", context_window=200_000)
        turn_id = turn_manager.submit(
            "s-stale", "user", prompt="hi", profile_id="agent",
            overrides={"model": "opus", "context_window": 1_000_000,
                       "context_window_base": 1_000_000},
            no_kick=True,
        )
        assert db.get_session("s-stale")["context_window"] == 200_000
        from app.db import turns
        saved = turns.get_turn(turn_id)["overrides"]
        assert saved["context_window"] == 200_000
        assert "context_window_base" not in saved

    def test_window_from_a_client_with_the_current_pin_is_saved(self, mock_db):
        make_profile("agent", {})
        db.create_session("s-fresh", profile_id="agent", model="opus", context_window=200_000)
        turn_manager.submit(
            "s-fresh", "user", prompt="hi", profile_id="agent",
            overrides={"model": "opus", "context_window": 1_000_000,
                       "context_window_base": 200_000},
            no_kick=True,
        )
        assert db.get_session("s-fresh")["context_window"] == 1_000_000

    def test_zero_base_means_no_pin_seen(self, mock_db):
        make_profile("agent", {})
        db.create_session("s-none", profile_id="agent", model="opus")
        turn_manager.submit(
            "s-none", "user", prompt="hi", profile_id="agent",
            overrides={"model": "opus", "context_window": 200_000, "context_window_base": 0},
            no_kick=True,
        )
        assert db.get_session("s-none")["context_window"] == 200_000
        # A client that saw no pin cannot replace one saved since.
        turn_manager.submit(
            "s-none", "user", prompt="hi", profile_id="agent",
            overrides={"model": "opus", "context_window": 1_000_000, "context_window_base": 0},
            no_kick=True,
        )
        assert db.get_session("s-none")["context_window"] == 200_000

    def test_wakeup_replay_does_not_rebind(self, mock_db):
        """A wakeup replays the model the SCHEDULING turn used, which may be
        older than what the user has since chosen."""
        make_profile("agent", {})
        db.create_session("s-wake", profile_id="agent", model="opus")
        turn_manager.submit(
            "s-wake",
            "wakeup",
            prompt="check",
            profile_id="agent",
            overrides={"model": "haiku"},
            no_kick=True,
        )
        assert db.get_session("s-wake")["model"] == "opus"

    def test_automation_turn_does_not_rebind(self, mock_db):
        make_profile("agent", {})
        db.create_session("s-auto", profile_id="agent", model="opus")
        turn_manager.submit(
            "s-auto",
            "automation",
            prompt="run",
            profile_id="agent",
            overrides={"model": "haiku"},
            no_kick=True,
        )
        assert db.get_session("s-auto")["model"] == "opus"


# ---------------------------------------------------------------------------
# Fast mode (v81) — a BOOLEAN leg of the same binding
# ---------------------------------------------------------------------------
#
# Every other leg is a string, where "absent" and "empty" mean the same thing.
# ``fast`` is not: False is a real pick the user made, so it has to travel all
# the way to the row and to the turn, and only None may be treated as a no-op.


class TestFastModeBinding:
    def test_creation_stamps_fast(self, mock_db):
        make_profile("agent", {})
        row = db.create_session("f-new", profile_id="agent", fast=True)
        # The SQLite test shim hands BOOLEAN columns back as ints (the
        # tests/db/test_sessions_hidden.py precedent); Postgres returns bools.
        assert row["fast"] in (True, 1)
        assert db.get_session("f-new")["fast"] in (True, 1)

    def test_creation_without_fast_leaves_it_null(self, mock_db):
        make_profile("agent", {})
        assert db.create_session("f-bare", profile_id="agent")["fast"] is None

    def test_binding_writes_fast_on(self, mock_db):
        make_profile("agent", {})
        db.create_session("f-on", profile_id="agent")
        db.set_session_run_binding("f-on", fast=True)
        assert db.get_session("f-on")["fast"] in (True, 1)

    def test_binding_clears_fast_with_false(self, mock_db):
        """Turning fast mode off is a write, not a no-op — the empty-string
        idiom the string legs use would silently strand the session on fast."""
        make_profile("agent", {})
        db.create_session("f-off", profile_id="agent", fast=True)
        db.set_session_run_binding("f-off", fast=False)
        assert db.get_session("f-off")["fast"] in (False, 0)

    def test_binding_omitting_fast_leaves_it_alone(self, mock_db):
        make_profile("agent", {})
        db.create_session("f-keep", profile_id="agent", fast=True)
        db.set_session_run_binding("f-keep", model="sonnet")
        row = db.get_session("f-keep")
        assert row["model"] == "sonnet"
        assert row["fast"] in (True, 1)

    def test_session_row_drives_the_turn(self, mock_db, tmp_path):
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session("f-turn", profile_id="agent", fast=True)
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="f-turn",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.aih_fast is True

    def test_override_beats_the_session_row(self, mock_db, tmp_path):
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session("f-ovr", profile_id="agent", fast=False)
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="f-ovr",
            overrides={"cwd": str(tmp_path), "fast": True},
        )
        assert opts.aih_fast is True

    def test_false_override_clears_a_fast_session(self, mock_db, tmp_path):
        """The precedence chain must test the override for PRESENCE. A plain
        ``override or row`` would swallow False and keep running fast."""
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session("f-clear", profile_id="agent", fast=True)
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path)}},
            session_id="f-clear",
            overrides={"cwd": str(tmp_path), "fast": False},
        )
        assert opts.aih_fast is False

    def test_unbound_session_runs_standard(self, mock_db, tmp_path):
        """No row value and no override: None, and there is deliberately no
        agent-config fallback — fast mode is a per-session user choice."""
        make_profile("agent", {"cwd": str(tmp_path), "fast": True})
        db.create_session("f-none", profile_id="agent")
        opts = _build_options(
            {"id": "agent", "config": {"cwd": str(tmp_path), "fast": True}},
            session_id="f-none",
            overrides={"cwd": str(tmp_path)},
        )
        assert opts.aih_fast is None

    def test_turn_manager_persists_the_flip(self, mock_db, tmp_path):
        """A mid-session toggle sent with a user turn sticks server-side, the
        same way a model switch does."""
        make_profile("agent", {"cwd": str(tmp_path)})
        db.create_session("f-flip", profile_id="agent", fast=False)
        for fast in [True, False]:
            turn_manager.submit(
                "f-flip",
                "user",
                prompt="hi",
                profile_id="agent",
                overrides={"fast": fast},
                no_kick=True,
            )
            assert db.get_session("f-flip")["fast"] == fast


# ---------------------------------------------------------------------------
# A window the model does not offer is ignored on every saving source (#1894)
# ---------------------------------------------------------------------------
#
# Since #1866 a saved pin is a ceiling the options builder enforces even when
# the model does not offer it, so no send or create may save a client window
# the model does not offer. Sonnet offers 200K and 1M; Haiku offers no choice.
# The value is ignored, as the API reference documents: the saved pin is left
# alone and the turn runs on it (or on the model default when unpinned).


class TestUnofferedWindowIsIgnored:
    def _profile(self, tmp_path):
        make_profile("agent", {"cwd": str(tmp_path)})
        return {"id": "agent", "config": {"cwd": str(tmp_path)}}

    # Every human-facing turn source shares this save: a WebSocket send
    # (user/queue), the HTTP conversation API, a worktree chat's first message
    # and a connector message.
    @pytest.mark.parametrize("source", ["user", "queue", "api", "worktree", "connector"])
    def test_send_keeps_the_saved_pin_and_runs_on_it(self, mock_db, tmp_path, source):
        from app.db import turns

        profile = self._profile(tmp_path)
        db.create_session("s-pinned", profile_id="agent", model="sonnet",
                          context_window=200_000)
        turn_id = turn_manager.submit(
            "s-pinned", source, prompt="hi", profile_id="agent",
            overrides={"cwd": str(tmp_path), "context_window": 1000}, no_kick=True)
        assert db.get_session("s-pinned")["context_window"] == 200_000
        overrides = turns.get_turn(turn_id)["overrides"]
        assert overrides["context_window"] == 200_000
        opts = _build_options(profile, session_id="s-pinned", overrides=overrides)
        assert opts.aih_context_window == 200_000

    def test_send_on_an_unpinned_chat_runs_the_model_default(self, mock_db, tmp_path):
        from app.db import turns

        profile = self._profile(tmp_path)
        db.create_session("s-free", profile_id="agent", model="sonnet")
        turn_id = turn_manager.submit(
            "s-free", "user", prompt="hi", profile_id="agent",
            overrides={"cwd": str(tmp_path), "context_window": 150_000}, no_kick=True)
        assert db.get_session("s-free")["context_window"] is None
        overrides = turns.get_turn(turn_id)["overrides"]
        assert "context_window" not in overrides
        opts = _build_options(profile, session_id="s-free", overrides=overrides)
        assert opts.aih_context_window is None
        assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in opts.env

    def test_send_is_checked_against_the_model_it_picks(self, mock_db, tmp_path):
        self._profile(tmp_path)
        db.create_session("s-switch", profile_id="agent", model="sonnet")
        turn_manager.submit("s-switch", "user", prompt="hi", profile_id="agent",
                            overrides={"model": "haiku", "context_window": 1_000_000},
                            no_kick=True)
        row = db.get_session("s-switch")
        assert (row["model"], row["context_window"]) == ("haiku", None)
        turn_manager.submit("s-switch", "user", prompt="hi", profile_id="agent",
                            overrides={"model": "opus", "context_window": 1_000_000},
                            no_kick=True)
        row = db.get_session("s-switch")
        assert (row["model"], row["context_window"]) == ("opus", 1_000_000)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pin", [200_000, None])
    async def test_api_stream_keeps_the_saved_pin_and_runs_on_it(self, mock_db, tmp_path, pin):
        from app.core import credential_gate, query_engine

        self._profile(tmp_path)
        db.create_session("s-stream", profile_id="agent", model="sonnet", context_window=pin)
        built = []

        async def capture(options):
            built.append(options)
            return "stop before spawn"

        with patch.object(qo, "get_security_instructions", return_value=""), \
                patch.object(credential_gate, "refusal_for_options_async", capture):
            frames = [frame async for frame in query_engine.stream_query(
                "hello", "agent", session_id="s-stream",
                overrides={"cwd": str(tmp_path), "context_window": 1000})]
        assert frames[-1]["message"] == "stop before spawn"
        assert built[0].aih_context_window == pin
        assert db.get_session("s-stream")["context_window"] == pin

    @pytest.mark.asyncio
    async def test_api_stream_switching_a_provider_chat_to_claude_keeps_an_offered_window(
            self, mock_db, tmp_path):
        """The turn runs natively, so it is checked against the native model."""
        from app.core import credential_gate, query_engine

        _mk_provider()
        self._profile(tmp_path)
        db.create_session("s-prov", profile_id="agent", model="prov:op:openai/gpt-5.6")
        assert db.get_session("s-prov")["provider_id"] == "op"
        built = []

        async def capture(options):
            built.append(options)
            return "stop before spawn"

        with patch.object(qo, "get_security_instructions", return_value=""), \
                patch.object(credential_gate, "refusal_for_options_async", capture):
            [frame async for frame in query_engine.stream_query(
                "hello", "agent", session_id="s-prov",
                overrides={"cwd": str(tmp_path), "model": "opus", "context_window": 200_000})]
        assert built[0].aih_context_window == 200_000
        assert db.get_session("s-prov")["context_window"] == 200_000

    @pytest.mark.asyncio
    async def test_forwarded_create_and_turn_ignore_it(self, mock_db, tmp_path, monkeypatch):
        """A chat created and driven from another worker (app.network.control)."""
        from app.db import turns
        from app.network import catalog, control

        monkeypatch.setattr(catalog, "publish_owned", lambda: 0)
        monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
        self._profile(tmp_path)
        turn = {"source": "user", "prompt": "hi", "profile_id": "agent", "owner_send": True,
                "overrides": {"context_window": 1000}}
        result = await control._sessions({
            "session": {"id": "s-fwd", "profile_id": "agent", "title": "t",
                        "model": "sonnet", "context_window": 150_000},
            "turn": turn,
        }, "dev-other")
        assert db.get_session("s-fwd")["context_window"] is None
        assert "context_window" not in turns.get_turn(result["turn"]["turn_id"])["overrides"]

        await control._turns({**turn, "session_id": "s-fwd",
                              "overrides": {"context_window": 1_000_000}}, "dev-other")
        assert db.get_session("s-fwd")["context_window"] == 1_000_000

    def test_voice_context_patch_ignores_it(self, mock_db, tmp_path):
        from app.api.voice_context import VoiceContextPatch, _persist_patch

        self._profile(tmp_path)
        db.create_session(session_id="s-voice", profile_id="agent", mode="voice",
                          provider_id="realtime-provider", model="sonnet",
                          context_window=200_000)
        assert _persist_patch("s-voice", VoiceContextPatch(context_window=150_000))[
            "context_window"] == 200_000
        row = _persist_patch("s-voice", VoiceContextPatch(model="haiku", context_window=1_000_000))
        assert (row["model"], row["context_window"]) == ("haiku", 200_000)
        _persist_patch("s-voice", VoiceContextPatch(model="sonnet"))
        # The voice row's provider funds Realtime, not the text model.
        assert _persist_patch("s-voice", VoiceContextPatch(context_window=1_000_000))[
            "context_window"] == 1_000_000

    def test_promptless_worktree_create_ignores_it(self, mock_db, tmp_path):
        from app.api.sessions_worktree import persist_promptless_run_binding

        self._profile(tmp_path)
        db.create_session("s-wt", profile_id="agent")
        persist_promptless_run_binding("s-wt", {"model": "sonnet", "context_window": 150_000})
        row = db.get_session("s-wt")
        assert (row["model"], row["context_window"]) == ("sonnet", None)
        persist_promptless_run_binding("s-wt", {"context_window": 1_000_000})
        assert db.get_session("s-wt")["context_window"] == 1_000_000

    def test_branch_create_overrides_ignore_it(self, mock_db, tmp_path):
        """The create fields a voice call starting on a branch passes."""
        from app.api.sessions_worktree import _merge_session_create_overrides

        self._profile(tmp_path)
        base = {"session_id": "s-branch", "profile_id": "agent"}
        merged = _merge_session_create_overrides(
            base, {"model": "haiku", "context_window": 1_000_000})
        assert merged["context_window"] is None
        merged = _merge_session_create_overrides(base, {
            "model": "sonnet", "mode": "voice", "provider_id": "realtime-provider",
            "context_window": 1_000_000})
        assert merged["context_window"] == 1_000_000

    def test_rest_create_ignores_it(self, authenticated_client, tmp_path):
        self._profile(tmp_path)
        for window, saved in ((150_000, None), (1_000_000, 1_000_000)):
            response = authenticated_client.post("/api/v1/sessions", json={
                "profile_id": "agent",
                "overrides": {"model": "sonnet", "context_window": window}})
            assert response.status_code == 200, response.text
            assert db.get_session(response.json()["session_id"])["context_window"] == saved

    @pytest.mark.asyncio
    async def test_conversation_api_create_ignores_it(self, mock_db, tmp_path):
        from unittest.mock import AsyncMock

        from app.api.query import conversation
        from app.core.models import ConversationRequest

        self._profile(tmp_path)
        with patch("app.core.turn_manager.submit_async", new=AsyncMock(return_value="t")), \
                patch("app.core.turn_manager.wait_session_idle", new=AsyncMock(return_value=True)), \
                patch("app.core.turn_outcome.final_turn_outcome",
                      return_value={"usage": {}, "error": None, "interrupted": False}), \
                patch("app.core.turn_outcome.extract_final_assistant_text", return_value="ok"):
            result = await conversation(ConversationRequest(
                prompt="hi", profile="agent",
                overrides={"model": "sonnet", "context_window": 150_000}),
                {"id": None, "profile_ids": ["agent"]})
        assert db.get_session(result["session_id"])["context_window"] is None

    @pytest.mark.asyncio
    async def test_background_query_ignores_it(self, mock_db, tmp_path):
        from unittest.mock import AsyncMock

        import app.core.query_background as qb

        self._profile(tmp_path)
        db.create_session("s-bg", profile_id="agent", model="sonnet", context_window=200_000)
        with patch.object(qb, "_run_background_query", new=AsyncMock()) as run:
            await qb.start_background_query(
                "hi", "agent", session_id="s-bg",
                overrides={"cwd": str(tmp_path), "context_window": 1000})
            await asyncio.sleep(0)
        assert db.get_session("s-bg")["context_window"] == 200_000
        assert "context_window" not in run.call_args.kwargs["overrides"]
