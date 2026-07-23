"""
Chunk 4 (respawn on provider change) regression tests.

`session_pool.requires_reconnect` is the LIVE path (its callers are
query_engine.py / query_background.py); `claude_adapter.requires_reconnect` /
`requires_reconnect_sdk` are dead on the live path today but are fixed for
latent correctness — see docs/MULTI_PROVIDER_PLAN.md R2/R3. These tests target
the live path primarily, with a light mirror pass over the adapter methods.

R3: the comparison is the named `PROVIDER_ENV_KEYS` constant (the set of
overlay-controlled provider env keys), not full env-dict equality (would force
respawns on benign env churn, killing background subagents) and not a
narrower 3-key tuple (would miss provider config edits).
"""

from unittest.mock import MagicMock

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
from app.core.provider.claude_adapter import ClaudeAgentAdapter
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


def test_native_1m_boundary_auto_compact_flip_is_hotswap():
    """FIX 1: a native [1m]-boundary switch flips CLAUDE_CODE_AUTO_COMPACT_WINDOW
    between "200000" and absent with NO ANTHROPIC_BASE_URL — must stay a hot-swap
    (respawning would kill background subagents)."""
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


# ─── claude_adapter mirror (latent-correctness only, per R2) ────────────────


def _connected_adapter(env) -> ClaudeAgentAdapter:
    adapter = ClaudeAgentAdapter()
    adapter._connect_options = ClaudeAgentOptions(model="claude-sonnet-4-5", env=env)
    return adapter


def test_adapter_requires_reconnect_sdk_provider_env_delta():
    adapter = _connected_adapter({"ANTHROPIC_AUTH_TOKEN": "old"})
    new = ClaudeAgentOptions(model="claude-sonnet-4-5", env={"ANTHROPIC_AUTH_TOKEN": "new"})
    assert adapter.requires_reconnect_sdk(new) is True


def test_adapter_requires_reconnect_sdk_identical_env_no_reconnect():
    adapter = _connected_adapter({"ANTHROPIC_AUTH_TOKEN": "same"})
    new = ClaudeAgentOptions(model="claude-sonnet-4-5", env={"ANTHROPIC_AUTH_TOKEN": "same"})
    assert adapter.requires_reconnect_sdk(new) is False


def test_adapter_requires_reconnect_provider_env_delta():
    adapter = _connected_adapter({"ANTHROPIC_API_KEY": "old"})
    new_options = SessionOptions(
        model="claude-sonnet-4-5",
        env={"ANTHROPIC_API_KEY": "new"},
    )
    assert adapter.requires_reconnect(new_options) is True


def test_adapter_requires_reconnect_unrelated_env_no_reconnect():
    adapter = _connected_adapter({"FOO": "1"})
    new_options = SessionOptions(model="claude-sonnet-4-5", env={"FOO": "2"})
    assert adapter.requires_reconnect(new_options) is False
