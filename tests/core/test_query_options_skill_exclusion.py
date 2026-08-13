"""The bundled ``claude-api`` skill is blocked on non-Claude provider sessions.

The CLI ships a ``claude-api`` skill whose invocation injects ~595K chars
(~142K tokens) of Anthropic SDK reference docs, and whose trigger text is broad
enough ("the prompt names Claude in any form") that a gateway-routed GPT-class
model fires it repeatedly — blowing the turn's context on a session that has no
use for the Anthropic SDK in the first place.

``build_options_from_profile`` therefore appends a ``Skill(claude-api)`` deny
rule to ``disallowed_tools`` for every session bound to a NON-Claude provider
(main sessions and ProviderAgent workers alike — a worker is just an ordinary
session with its own binding). Claude sessions — native (no binding) or an
``anthropic``-type provider row, both of which serve real ``claude-*`` ids —
must be byte-identical to before.

Mechanism note (verified against claude-agent-sdk 0.2.126 and its bundled CLI):
``disallowed_tools`` becomes ``--disallowedTools``, whose entries are parsed as
``ToolName(ruleContent)`` deny rules; the CLI's ``Skill`` tool
``checkPermissions`` matches those deny rules' content against the skill name
and refuses with "Skill execution blocked by permission rules".
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import app.core.query_options as qo
from app.core import encryption
from app.core import provider_agent as pa
from app.db import database as db

SKILL_RULE = "Skill(claude-api)"


@pytest.fixture(autouse=True)
def _clean_worker_registry():
    pa.reset_worker_sessions()
    yield
    pa.reset_worker_sessions()


@contextmanager
def _stubbed_pa_server():
    """(P2c) The standalone ProviderAgent server is retired — the Task tool
    rides the builtins server, so there is nothing to stub any more."""
    yield


def _mk_provider(provider_id, provider_type, *, models, token="gw-secret"):
    return db.create_model_provider(
        provider_id=provider_id,
        name=provider_id,
        provider_type=provider_type,
        base_url="https://example.invalid/api",
        auth_token=encryption.encrypt_value(token),
        models=models,
        enabled=True,
    )


def _mk_profile(profile_id, config):
    return db.create_profile(
        profile_id=profile_id,
        name=profile_id,
        description=None,
        config=config,
    )


def _build(profile, *, session_id=None, overrides=None):
    with patch.object(qo, "get_security_instructions", return_value=""):
        with _stubbed_pa_server():
            opts, _ = qo.build_options_from_profile(
                profile=profile, session_id=session_id, overrides=overrides
            )
    return opts


def _denied(opts):
    return list(opts.disallowed_tools or [])


# ---------------------------------------------------------------------------
# Non-Claude provider sessions get the deny rule
# ---------------------------------------------------------------------------

class TestNonClaudeProviderSessions:
    def test_openrouter_gpt_session_blocks_the_skill(self, mock_db, tmp_path):
        encryption.set_encryption_key("pw", db)
        _mk_provider("orp", "openrouter", models=[{"id": "openai/gpt-5.6"}])
        _mk_profile(
            "prov", {"model": "openai/gpt-5.6", "provider": "orp", "cwd": str(tmp_path)}
        )
        db.create_session("s-gpt", profile_id="prov")  # stamps provider_id='orp'

        opts = _build(
            {
                "id": "prov",
                "config": {
                    "model": "openai/gpt-5.6",
                    "provider": "orp",
                    "cwd": str(tmp_path),
                },
            },
            session_id="s-gpt",
            overrides={"cwd": str(tmp_path)},
        )

        assert SKILL_RULE in _denied(opts)

    def test_translated_openai_session_blocks_the_skill(self, mock_db, tmp_path):
        """The real gpt-5.6-sol shape: an ``openai``-type row, routed through the
        loopback translator gateway."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("oai", "openai", models=[{"id": "gpt-5.6-sol"}])
        _mk_profile(
            "gpt", {"model": "gpt-5.6-sol", "provider": "oai", "cwd": str(tmp_path)}
        )
        db.create_session("s-sol", profile_id="gpt")

        opts = _build(
            {
                "id": "gpt",
                "config": {
                    "model": "gpt-5.6-sol",
                    "provider": "oai",
                    "cwd": str(tmp_path),
                },
            },
            session_id="s-sol",
            overrides={"cwd": str(tmp_path)},
        )

        assert SKILL_RULE in _denied(opts)

    def test_provider_agent_worker_on_a_gpt_provider_blocks_the_skill(
        self, mock_db, tmp_path
    ):
        """A ProviderAgent worker is an ordinary session with its own binding, so
        it rides the same build — and picks up the AskUserQuestion exclusion too
        (ruling A3), proving the two are independent."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("oai", "openai", models=[{"id": "gpt-5.6-sol"}])
        _mk_profile(
            "gpt", {"model": "gpt-5.6-sol", "provider": "oai", "cwd": str(tmp_path)}
        )
        db.create_session("w-1", profile_id="gpt")
        pa.register_worker_session("w-1")

        opts = _build(
            {
                "id": "gpt",
                "config": {
                    "model": "gpt-5.6-sol",
                    "provider": "oai",
                    "cwd": str(tmp_path),
                },
            },
            session_id="w-1",
            overrides={"cwd": str(tmp_path)},
        )

        denied = _denied(opts)
        assert SKILL_RULE in denied
        assert qo.WORKER_DISALLOWED_TOOL in denied

    def test_profile_disallowed_tools_are_preserved_and_not_duplicated(
        self, mock_db, tmp_path
    ):
        encryption.set_encryption_key("pw", db)
        _mk_provider("orp", "openrouter", models=[{"id": "openai/gpt-5.6"}])
        _mk_profile(
            "prov", {"model": "openai/gpt-5.6", "provider": "orp", "cwd": str(tmp_path)}
        )
        db.create_session("s-keep", profile_id="prov")

        opts = _build(
            {
                "id": "prov",
                "config": {
                    "model": "openai/gpt-5.6",
                    "provider": "orp",
                    "cwd": str(tmp_path),
                    "disallowed_tools": ["Bash", SKILL_RULE],
                },
            },
            session_id="s-keep",
            overrides={"cwd": str(tmp_path)},
        )

        denied = _denied(opts)
        assert "Bash" in denied
        assert denied.count(SKILL_RULE) == 1


# ---------------------------------------------------------------------------
# Claude sessions are untouched
# ---------------------------------------------------------------------------

class TestClaudeSessionsUntouched:
    def test_native_session_keeps_the_skill(self, mock_db, tmp_path):
        _mk_profile("nat", {"model": "opus", "cwd": str(tmp_path)})
        db.create_session("s-native", profile_id="nat")

        opts = _build(
            {"id": "nat", "config": {"model": "opus", "cwd": str(tmp_path)}},
            session_id="s-native",
            overrides={"cwd": str(tmp_path)},
        )

        assert SKILL_RULE not in _denied(opts)

    def test_anthropic_provider_session_keeps_the_skill(self, mock_db, tmp_path):
        """An ``anthropic``-type row is a Claude session served by an API key —
        it runs real ``claude-*`` ids and must not lose the skill."""
        encryption.set_encryption_key("pw", db)
        _mk_provider(
            "anth", "anthropic", models=[{"id": "claude-opus-4-5-20260101"}]
        )
        _mk_profile(
            "cl",
            {
                "model": "claude-opus-4-5-20260101",
                "provider": "anth",
                "cwd": str(tmp_path),
            },
        )
        db.create_session("s-claude", profile_id="cl")

        opts = _build(
            {
                "id": "cl",
                "config": {
                    "model": "claude-opus-4-5-20260101",
                    "provider": "anth",
                    "cwd": str(tmp_path),
                },
            },
            session_id="s-claude",
            overrides={"cwd": str(tmp_path)},
        )

        assert SKILL_RULE not in _denied(opts)

    def test_sessionless_native_build_keeps_the_skill(self, mock_db, tmp_path):
        opts = _build({"id": "t", "config": {"model": "opus", "cwd": str(tmp_path)}})
        assert SKILL_RULE not in _denied(opts)
