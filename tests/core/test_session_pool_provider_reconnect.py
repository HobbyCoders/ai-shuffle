"""
Chunk 4 (respawn on provider change) regression tests.

`session_pool.requires_reconnect` is the LIVE path (its callers are
query_engine.py / query_background.py); `HarnessAdapter.requires_reconnect` /
`requires_reconnect_sdk` are dead on the live path today but are fixed for
latent correctness — see docs/MULTI_PROVIDER_PLAN.md R2/R3. These tests target
the live path primarily, with a light mirror pass over the adapter methods.

R3: the comparison is the named `PROVIDER_ENV_KEYS` constant (the set of
overlay-controlled provider env keys), not full env-dict equality (would force
respawns on benign env churn, killing background subagents) and not a
narrower 3-key tuple (would miss provider config edits).
"""

from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from app.core.session_pool import (
    PROVIDER_ENV_KEYS,
    PROVIDER_IDENTITY_ENV_KEYS,
    PROVIDER_OVERLAY_ENV_KEYS,
    SessionState,
    provider_env_requires_reconnect,
    requires_reconnect,
)
from app.core.provider.types import SessionOptions


def _make_state(env) -> SessionState:
    adapter = MagicMock()
    return SessionState(
        adapter=adapter,
        connect_options=ClaudeAgentOptions(model="claude-sonnet-4-5", env=env),
    )


def _new_options(env) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(model="claude-sonnet-4-5", env=env)


# ─── session_pool.requires_reconnect (the live path) ────────────────────────


def test_reconnect_reports_all_changed_names_without_values():
    from app.core.session_pool import reconnect_option_names
    state = _make_state({"ANTHROPIC_AUTH_TOKEN": "old-secret"})
    state.connect_options.system_prompt = "private prompt"
    options = _new_options({"ANTHROPIC_AUTH_TOKEN": "new-secret"})
    options.cwd = "/private/workspace"
    assert reconnect_option_names(state, options) == ["system_prompt", "cwd", "provider_env"]


def test_identical_env_no_reconnect():
    state = _make_state({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
    new = _new_options({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"})
    assert requires_reconnect(state, new) is False


@pytest.mark.parametrize("key", [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
])
def test_provider_env_key_delta_forces_reconnect(key):
    state = _make_state({key: "old-value"})
    new = _new_options({key: "new-value"})
    assert requires_reconnect(state, new) is True


def test_context_window_change_forces_reconnect():
    state = _make_state({})
    state.connect_options.aih_context_window = 200_000
    new = _new_options({})
    new.aih_context_window = 1_000_000
    assert requires_reconnect(state, new) is True


@pytest.mark.parametrize("old_fp,new_fp,expected", [
    (None, "fp-a", True),
    ("fp-a", "fp-b", True),
    ("fp-a", None, True),
    ("fp-a", "fp-a", False),
])
def test_ai_tools_credential_fingerprint_controls_reconnect(old_fp, new_fp, expected):
    """A pooled Node worker must not retain an old fal credential."""
    state = _make_state({})
    state.connect_options.aih_ai_tools_credential_fingerprint = old_fp
    new = _new_options({})
    new.aih_ai_tools_credential_fingerprint = new_fp
    assert requires_reconnect(state, new) is expected


def test_fal_rotation_reconnects_even_when_sdk_server_identity_is_equivalent():
    """A fresh Fluid SDK instance must replace the stale worker on rotation."""
    old_server = {"type": "sdk", "name": "ai-tools", "instance": object()}
    new_server = {"type": "sdk", "name": "ai-tools", "instance": object()}
    state = _make_state({})
    state.connect_options.mcp_servers = {"ai-tools": old_server}
    state.connect_options.aih_ai_tools_credential_fingerprint = "fp-a"
    new = _new_options({})
    new.mcp_servers = {"ai-tools": new_server}
    new.aih_ai_tools_credential_fingerprint = "fp-b"

    assert requires_reconnect(state, new) is True


@pytest.mark.parametrize("profile_ai_tools", [
    {"image_generation": True},
    # The live case in #1398: ruling T5 attaches the tools from the picker, and
    # an ordinary profile never sets the retired ``ai_tools`` switches.
    None,
    {},
])
def test_build_options_propagates_fal_credential_and_reconnects_pooled_worker(
    tmp_path, profile_ai_tools,
):
    """The real profile builder bridges the settings key to the MCP worker.

    The MCP factory is captured before it starts a subprocess; the resulting
    options still carry the producer's fingerprint used by the live pool.
    """
    import app.core.environment as environment
    import app.core.query_options as query_options

    profile = {
        "id": "fal-profile",
        "config": {
            "model": "claude-sonnet-4-5",
            "cwd": str(tmp_path),
            "tools_preset": "claude_code",
            "ai_tools": profile_ai_tools,
        },
    }
    captured_envs = []
    captured_configs = []

    def capture_mcp_servers(**kwargs):
        captured_envs.append(kwargs["tool_env"])
        captured_configs.append(kwargs["ai_tools_config"])
        return {}

    def build(fal_key):
        def get_key(name):
            return fal_key if name == "fal_api_key" else None

        with (
            patch.object(query_options, "get_security_instructions", return_value=""),
            patch.object(
                query_options,
                "_build_mcp_servers_dict_with_ai_tools",
                side_effect=capture_mcp_servers,
            ),
            patch.object(query_options, "_ai_tools_available", return_value=True),
            patch.object(environment, "_get_decrypted_api_key", side_effect=get_key),
        ):
            options, _ = query_options.build_options_from_profile(profile=profile)
        return options

    absent = build(None)
    first = build("fal-a")
    rotated = build("fal-b")
    removed = build(None)

    assert "FAL_KEY" not in captured_envs[0]
    assert captured_envs[1]["FAL_KEY"] == "fal-a"
    assert captured_envs[2]["FAL_KEY"] == "fal-b"
    assert "FAL_KEY" not in captured_envs[3]
    assert captured_configs[1]["image_generation"] is True
    assert any(captured_configs[1].values())
    assert absent.aih_ai_tools_credential_fingerprint is None
    assert first.aih_ai_tools_credential_fingerprint
    assert first.aih_ai_tools_credential_fingerprint != rotated.aih_ai_tools_credential_fingerprint
    assert removed.aih_ai_tools_credential_fingerprint is None

    state = _make_state({})
    state.connect_options = absent
    assert requires_reconnect(state, first) is True
    state.connect_options = first
    assert requires_reconnect(state, rotated) is True
    assert requires_reconnect(state, first) is False
    state.connect_options = rotated
    assert requires_reconnect(state, removed) is True


def test_identity_env_keys_covered_individually():
    """Every IDENTITY key independently triggers a reconnect — even with no
    ANTHROPIC_BASE_URL present (identity keys are always compared)."""
    for key in PROVIDER_IDENTITY_ENV_KEYS:
        state = _make_state({key: "a"})
        new = _new_options({key: "b"})
        assert requires_reconnect(state, new) is True, f"{key} did not force reconnect"


def test_overlay_env_keys_covered_individually_for_provider_sessions():
    """Every OVERLAY key independently triggers a reconnect ONLY when the session
    is a provider session (ANTHROPIC_BASE_URL present on either side)."""
    for key in PROVIDER_OVERLAY_ENV_KEYS:
        # Provider session (base URL present, unchanged) → overlay delta reconnects.
        base = "https://gateway.example.com"
        state = _make_state({"ANTHROPIC_BASE_URL": base, key: "a"})
        new = _new_options({"ANTHROPIC_BASE_URL": base, key: "b"})
        assert requires_reconnect(state, new) is True, f"{key} did not force reconnect (provider)"

        # Native session (no base URL) → the same overlay delta is a hot-swap.
        state_native = _make_state({key: "a"})
        new_native = _new_options({key: "b"})
        assert requires_reconnect(state_native, new_native) is False, (
            f"{key} wrongly forced reconnect on a native session"
        )


def test_native_auto_compact_flip_is_hotswap():
    """FIX 1: a native model switch can flip CLAUDE_CODE_AUTO_COMPACT_WINDOW
    between a profile-preset value and absent with NO ANTHROPIC_BASE_URL — must
    stay a hot-swap (respawning would kill background subagents)."""
    state = _make_state({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"})
    new = _new_options({})  # key absent after the switch
    assert requires_reconnect(state, new) is False
    # And the reverse direction (absent → present).
    state2 = _make_state({})
    new2 = _new_options({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"})
    assert requires_reconnect(state2, new2) is False


def test_provider_auto_compact_flip_forces_reconnect():
    """FIX 1: the SAME AUTO_COMPACT_WINDOW flip on a PROVIDER session (base URL
    present) MUST reconnect — overlay keys are meaningful there."""
    base = "https://gateway.example.com"
    state = _make_state({"ANTHROPIC_BASE_URL": base, "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"})
    new = _new_options({"ANTHROPIC_BASE_URL": base})  # window key dropped
    assert requires_reconnect(state, new) is True


def test_max_context_tokens_delta_forces_reconnect_for_provider_session():
    """CLAUDE_CODE_MAX_CONTEXT_TOKENS differing between old/new env on a
    provider session (ANTHROPIC_BASE_URL present) requires a reconnect — a
    provider context_window edit must respawn to take effect."""
    base = "https://gateway.example.com"
    state = _make_state({"ANTHROPIC_BASE_URL": base, "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000"})
    new = _new_options({"ANTHROPIC_BASE_URL": base, "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "272000"})
    assert requires_reconnect(state, new) is True


def test_max_context_tokens_delta_is_hotswap_for_native_session():
    """The same CLAUDE_CODE_MAX_CONTEXT_TOKENS delta with no ANTHROPIC_BASE_URL
    on either side (native session) is a hot-swap, not a reconnect."""
    state = _make_state({"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000"})
    new = _new_options({"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "272000"})
    assert requires_reconnect(state, new) is False


def test_identity_key_change_always_reconnects_even_native():
    """FIX 1: an identity-key change reconnects regardless of provider-ness."""
    state = _make_state({"ANTHROPIC_AUTH_TOKEN": "old"})
    new = _new_options({"ANTHROPIC_AUTH_TOKEN": "new"})
    assert requires_reconnect(state, new) is True


def test_provider_env_keys_is_identity_plus_overlay():
    """The union constant is preserved for existing importers."""
    assert PROVIDER_ENV_KEYS == PROVIDER_IDENTITY_ENV_KEYS + PROVIDER_OVERLAY_ENV_KEYS


def test_provider_env_requires_reconnect_helper_directly():
    """The extracted helper: identity always, overlay only for provider sessions."""
    # Identity delta → reconnect.
    assert provider_env_requires_reconnect(
        {"ANTHROPIC_BASE_URL": "a"}, {"ANTHROPIC_BASE_URL": "b"}
    ) is True
    # Native overlay delta → no reconnect.
    assert provider_env_requires_reconnect(
        {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}, {}
    ) is False
    # Provider overlay delta → reconnect.
    base = "https://gw.example"
    assert provider_env_requires_reconnect(
        {"ANTHROPIC_BASE_URL": base, "CLAUDE_CODE_SUBAGENT_MODEL": "x"},
        {"ANTHROPIC_BASE_URL": base, "CLAUDE_CODE_SUBAGENT_MODEL": "y"},
    ) is True
    # None-safe.
    assert provider_env_requires_reconnect(None, None) is False


def test_unrelated_env_key_delta_no_reconnect():
    state = _make_state({"FOO": "1"})
    new = _new_options({"FOO": "2"})
    assert requires_reconnect(state, new) is False


def test_env_none_on_one_side_with_provider_keys_on_other_forces_reconnect():
    state = _make_state(None)
    new = _new_options({"ANTHROPIC_BASE_URL": "https://gateway.example.com"})
    assert requires_reconnect(state, new) is True

    # And the reverse direction.
    state2 = _make_state({"ANTHROPIC_BASE_URL": "https://gateway.example.com"})
    new2 = _new_options(None)
    assert requires_reconnect(state2, new2) is True


def test_env_both_none_no_reconnect():
    state = _make_state(None)
    new = _new_options(None)
    assert requires_reconnect(state, new) is False


def test_env_both_empty_dict_no_reconnect():
    state = _make_state({})
    new = _new_options({})
    assert requires_reconnect(state, new) is False


# ─── S6: post-S3 leg semantics ──────────────────────────────────────────────


class TestPostS3LegSemantics:
    """S6 (resolved 2026-07-20). After S3, nothing WRITES
    ``CLAUDE_CODE_SUBAGENT_MODEL`` — subagent model selection is frontmatter-
    stamped. These tests pin the two rulings:

      * Transition guard — the key stays in the overlay set so pre-S3 pooled
        sessions carrying it are respawned (its present→absent diff), purging a
        stale pin that S0 proved overrides all frontmatter config.
      * Post-S3 respawn semantics — the only leg-driven respawn is the legs
        empty↔non-empty flip (ANTHROPIC_BASE_URL identity flip). A leg-CONTENT
        change re-mints the same token → byte-identical env → no respawn
        (immediate routing + next-spawn stamping is the designed contract).
    """

    def test_transition_guard_stale_subagent_model_key_forces_reconnect(self):
        """A pre-S3 pooled session carries CLAUDE_CODE_SUBAGENT_MODEL in its
        old_env; the new env (post-S3) no longer sets it. The present→absent
        diff on this overlay key MUST respawn to purge the stale pin. Both sides
        are gateway sessions (ANTHROPIC_BASE_URL present) so overlay keys apply."""
        base = "https://gateway.example.com"
        old_env = {
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_AUTH_TOKEN": "gw-token",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-3-5-haiku",
        }
        new_env = {
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_AUTH_TOKEN": "gw-token",
        }
        assert provider_env_requires_reconnect(old_env, new_env) is True

    def test_leg_content_change_is_byte_identical_env_no_reconnect(self):
        """A leg-CONTENT change (same non-empty legs, different subagent
        models/providers) re-mints the SAME gateway token → byte-identical env.
        The reconnect check sees no diff → no respawn. The gateway map refresh
        routes immediately; frontmatter stamping applies next spawn."""
        env = {
            "ANTHROPIC_BASE_URL": "https://gateway.example.com",
            "ANTHROPIC_AUTH_TOKEN": "gw-token-stable",
        }
        # Two byte-identical dicts stand in for before/after a leg-content edit.
        assert provider_env_requires_reconnect(dict(env), dict(env)) is False

    def test_legs_empty_to_nonempty_flip_forces_reconnect(self):
        """The legs empty↔non-empty flip on an anthropic-provider main flips
        ANTHROPIC_BASE_URL between direct (absent) and gateway (present) — an
        identity-key change that MUST respawn, in both directions."""
        direct = {"ANTHROPIC_AUTH_TOKEN": "direct-token"}
        gateway = {
            "ANTHROPIC_BASE_URL": "https://gateway.example.com",
            "ANTHROPIC_AUTH_TOKEN": "gw-token",
        }
        assert provider_env_requires_reconnect(direct, gateway) is True
        # And the reverse direction (gateway → direct).
        assert provider_env_requires_reconnect(gateway, direct) is True


# ─── PA13 / W-R4: the resolved provider BINDING forces a reconnect ──────────


class TestProviderBindingReconnect:
    """A cross-provider mid-session switch must evict + reconnect.

    ``query_options`` stamps the turn's RESOLVED provider on the options object
    as ``aih_provider_id`` (None = native). Before W-R4 nothing compared it: two
    native-leg providers produce identical (empty) gateway-overlay envs, so the
    env comparison above sees no change and the pooled client — built for the
    OLD provider's engine and credentials — was silently reused.
    """

    @staticmethod
    def _state(provider_id, env=None):
        state = _make_state(env)
        state.connect_options.aih_provider_id = provider_id
        return state

    @staticmethod
    def _options(provider_id, env=None):
        opts = _new_options(env)
        opts.aih_provider_id = provider_id
        return opts

    def test_provider_change_forces_reconnect(self):
        state = self._state("prov-a")
        new = self._options("prov-b")
        assert requires_reconnect(state, new) is True

    def test_native_to_provider_forces_reconnect(self):
        """None → a provider id (a ``prov:`` composite pick on a native session)."""
        state = self._state(None)
        new = self._options("prov-a")
        assert requires_reconnect(state, new) is True

    def test_provider_to_native_forces_reconnect(self):
        """A provider id → None (a native Claude pick CLEARING the binding)."""
        state = self._state("prov-a")
        new = self._options(None)
        assert requires_reconnect(state, new) is True

    def test_same_provider_is_still_a_hotswap(self):
        """Same binding, different model → no reconnect; the model change stays a
        ``set_model`` hot-swap so background workers survive."""
        state = self._state("prov-a")
        new = self._options("prov-a")
        new.model = "some-other-model"
        assert requires_reconnect(state, new) is False

    def test_both_native_is_still_a_hotswap(self):
        assert requires_reconnect(self._state(None), self._options(None)) is False

    def test_unstamped_options_are_treated_as_native(self):
        """Options built by a path that never stamps the attribute (older pooled
        state, a hand-built ClaudeAgentOptions) read as native on both sides —
        no spurious respawn."""
        state = _make_state({})
        new = _new_options({})
        assert not hasattr(state.connect_options, "aih_provider_id")
        assert requires_reconnect(state, new) is False


def test_harness_adapter_requires_reconnect_mirrors_provider_binding():
    """HarnessAdapter mirrors the pool rule off ``extra["provider_id"]``."""
    from app.core.provider.harness_adapter import HarnessAdapter

    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="m", extra={"provider_id": "prov-a"})

    assert adapter.requires_reconnect(
        SessionOptions(model="m", extra={"provider_id": "prov-b"})
    ) is True
    # provider → native (extra key absent) also reconnects.
    assert adapter.requires_reconnect(SessionOptions(model="m")) is True
    # Same binding → hot-swap.
    assert adapter.requires_reconnect(
        SessionOptions(model="m2", extra={"provider_id": "prov-a"})
    ) is False


def test_harness_adapter_native_both_sides_no_reconnect():
    from app.core.provider.harness_adapter import HarnessAdapter

    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="m")
    assert adapter.requires_reconnect(SessionOptions(model="m2")) is False


# ─── CE12: a changed tool surface is a spawn-time change ─────────────────────


def _tool_state(allowlist=None, disallowed=None) -> SessionState:
    opts = ClaudeAgentOptions(
        model="claude-sonnet-4-5", disallowed_tools=disallowed or []
    )
    opts.aih_tool_allowlist = allowlist
    return SessionState(adapter=MagicMock(), connect_options=opts)


def _tool_options(allowlist=None, disallowed=None) -> ClaudeAgentOptions:
    opts = ClaudeAgentOptions(
        model="claude-sonnet-4-5", disallowed_tools=disallowed or []
    )
    opts.aih_tool_allowlist = allowlist
    return opts


def test_tool_allowlist_change_forces_reconnect():
    """fluid freezes ``allowed_tools`` at engine construction (ruling CE12), so
    a worker's restricted surface can never be a ``set_model`` hot-swap."""
    assert requires_reconnect(_tool_state(["Read"]), _tool_options(["Read"])) is False
    assert requires_reconnect(_tool_state(["Read"]), _tool_options(["Read", "Bash"])) is True
    # Restricted → unrestricted (and back) is a real change, not a default.
    assert requires_reconnect(_tool_state(["Read"]), _tool_options(None)) is True
    assert requires_reconnect(_tool_state(None), _tool_options(["Read"])) is True


def test_disallowed_tools_change_forces_reconnect_order_insensitively():
    """The blocklist is a CLI arg on the delegated leg and a construction-time
    filter on the native one — spawn-time either way. Order is not a change."""
    assert requires_reconnect(
        _tool_state(disallowed=["Bash", "Write"]),
        _tool_options(disallowed=["Write", "Bash"]),
    ) is False
    assert requires_reconnect(
        _tool_state(disallowed=["Bash"]), _tool_options(disallowed=[])
    ) is True

def test_catch_all_fal_tools_alone_still_carry_the_key(tmp_path):
    """``runFalModel`` alone runs on the fal key too, so it must carry it."""
    import app.core.environment as environment
    import app.core.query_options as query_options

    profile = {
        "id": "fal-generic-only",
        "config": {
            "model": "claude-sonnet-4-5",
            "cwd": str(tmp_path),
            "tools_preset": "claude_code",
            "allowed_tools": ["mcp__ai-tools__runFalModel"],
        },
    }
    captured_envs = []

    def capture_mcp_servers(**kwargs):
        captured_envs.append(kwargs["tool_env"])
        return {}

    with (
        patch.object(query_options, "get_security_instructions", return_value=""),
        patch.object(
            query_options, "_build_mcp_servers_dict_with_ai_tools",
            side_effect=capture_mcp_servers,
        ),
        patch.object(query_options, "_ai_tools_available", return_value=True),
        patch.object(
            environment, "_get_decrypted_api_key",
            side_effect=lambda name: "fal-a" if name == "fal_api_key" else None,
        ),
    ):
        query_options.build_options_from_profile(profile=profile)

    assert captured_envs[0]["FAL_KEY"] == "fal-a"
