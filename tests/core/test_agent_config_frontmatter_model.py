"""Unit tests for ``resolve_subagent_frontmatter_model`` (Chunk S2).

Pins the per-agent frontmatter resolver from
``docs/SUBAGENT_MODEL_CONFIG_PLAN.md`` — "DECISION POINT 3 — RESOLVED
2026-07-20: FRONTMATTER-STAMPING" and its resolver table. The resolver decides
the gateway-routable ``model:`` value stamped into a subagent's
``.claude/agents/<id>.md`` frontmatter (or ``None`` to omit → inherit main).

Native sessions preserve today's behavior (composites omitted, Claude pins kept,
unpinned inherits). Gateway sessions stamp the session default on any leg that is
not honored (composite-not-honored, foreign-Claude, or unpinned).
"""

from claude_agent_sdk import AgentDefinition

from app.core.agent_config import (
    resolve_subagent_frontmatter_model,
    write_agents_to_filesystem,
)


# --- Native session (gateway_routed=False) -------------------------------------
# session_default / main_serves_claude_ids / honored_leg_models are ignored here.


def test_native_composite_returns_none():
    assert (
        resolve_subagent_frontmatter_model(
            "prov:openrouter-1:llama-3.3",
            gateway_routed=False,
            honored_leg_models={"llama-3.3"},
            session_default="ignored",
            main_serves_claude_ids=True,
        )
        is None
    )


def test_native_claude_alias_passes_through():
    assert (
        resolve_subagent_frontmatter_model(
            "sonnet",
            gateway_routed=False,
            honored_leg_models=set(),
            session_default="ignored",
            main_serves_claude_ids=False,
        )
        == "sonnet"
    )


def test_native_claude_id_passes_through():
    assert (
        resolve_subagent_frontmatter_model(
            "claude-opus-4-8",
            gateway_routed=False,
            honored_leg_models=set(),
            session_default="ignored",
            main_serves_claude_ids=False,
        )
        == "claude-opus-4-8"
    )


def test_native_unpinned_returns_none():
    assert (
        resolve_subagent_frontmatter_model(
            None,
            gateway_routed=False,
            honored_leg_models=set(),
            session_default="ignored",
            main_serves_claude_ids=True,
        )
        is None
    )


# --- Gateway session (gateway_routed=True) -------------------------------------

SESSION_DEFAULT = "claude-haiku-4-5"


def test_gateway_composite_honored_returns_bare_model():
    assert (
        resolve_subagent_frontmatter_model(
            "prov:openrouter-1:llama-3.3",
            gateway_routed=True,
            honored_leg_models={"llama-3.3"},
            session_default=SESSION_DEFAULT,
            main_serves_claude_ids=False,
        )
        == "llama-3.3"
    )


def test_gateway_composite_not_honored_returns_session_default():
    assert (
        resolve_subagent_frontmatter_model(
            "prov:openrouter-1:llama-3.3",
            gateway_routed=True,
            honored_leg_models=set(),
            session_default=SESSION_DEFAULT,
            main_serves_claude_ids=False,
        )
        == SESSION_DEFAULT
    )


def test_gateway_composite_model_id_with_colon_and_slash_kept_intact():
    # First-colon split: everything after the provider id stays as the model id,
    # including internal colons and slashes.
    assert (
        resolve_subagent_frontmatter_model(
            "prov:or-1:anthropic/claude-3.5:beta",
            gateway_routed=True,
            honored_leg_models={"anthropic/claude-3.5:beta"},
            session_default=SESSION_DEFAULT,
            main_serves_claude_ids=False,
        )
        == "anthropic/claude-3.5:beta"
    )


def test_gateway_malformed_composite_returns_session_default():
    # No second colon → no model id → cannot route → session default.
    assert (
        resolve_subagent_frontmatter_model(
            "prov:only-a-provider",
            gateway_routed=True,
            honored_leg_models=set(),
            session_default=SESSION_DEFAULT,
            main_serves_claude_ids=False,
        )
        == SESSION_DEFAULT
    )


def test_gateway_claude_alias_main_serves_claude_passes_through():
    assert (
        resolve_subagent_frontmatter_model(
            "sonnet",
            gateway_routed=True,
            honored_leg_models=set(),
            session_default=SESSION_DEFAULT,
            main_serves_claude_ids=True,
        )
        == "sonnet"
    )


def test_gateway_claude_alias_main_does_not_serve_claude_returns_session_default():
    assert (
        resolve_subagent_frontmatter_model(
            "sonnet",
            gateway_routed=True,
            honored_leg_models=set(),
            session_default=SESSION_DEFAULT,
            main_serves_claude_ids=False,
        )
        == SESSION_DEFAULT
    )


def test_gateway_unpinned_returns_session_default():
    assert (
        resolve_subagent_frontmatter_model(
            None,
            gateway_routed=True,
            honored_leg_models=set(),
            session_default=SESSION_DEFAULT,
            main_serves_claude_ids=True,
        )
        == SESSION_DEFAULT
    )


# --- Writer integration --------------------------------------------------------


def test_writer_honored_composite_writes_bare_model_no_prov(tmp_path):
    model = resolve_subagent_frontmatter_model(
        "prov:or-1:llama-3.3",
        gateway_routed=True,
        honored_leg_models={"llama-3.3"},
        session_default="x",
        main_serves_claude_ids=False,
    )
    agent_def = AgentDefinition(description="d", prompt="p", model=model)
    write_agents_to_filesystem({"my-agent": agent_def}, str(tmp_path))

    content = (tmp_path / ".claude" / "agents" / "my-agent.md").read_text(
        encoding="utf-8"
    )
    assert "model: llama-3.3" in content
    assert "prov:" not in content


def test_writer_native_composite_omits_model_line(tmp_path):
    model = resolve_subagent_frontmatter_model(
        "prov:or-1:llama-3.3",
        gateway_routed=False,
        honored_leg_models=set(),
        session_default="x",
        main_serves_claude_ids=False,
    )
    assert model is None
    agent_def = AgentDefinition(description="d", prompt="p", model=model)
    write_agents_to_filesystem({"my-agent": agent_def}, str(tmp_path))

    content = (tmp_path / ".claude" / "agents" / "my-agent.md").read_text(
        encoding="utf-8"
    )
    assert "model:" not in content
