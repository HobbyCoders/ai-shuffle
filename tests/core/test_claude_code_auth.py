"""
Tests for the Claude Code credential injection under the explicit auth mode.

The user chooses ``subscription`` or ``api_key`` (``app.core.claude_auth_mode``).
In ``api_key`` mode the stored ``anthropic_api_key`` is injected as
``ANTHROPIC_API_KEY`` and other Anthropic credential variables are kept from the
CLI. In ``subscription`` mode nothing is injected and any Anthropic key from the
profile env or the host environment is kept from the CLI. The key's mere
presence never picks the mode.

These tests exercise the injection helper in isolation by patching the mode and
the key lookup.
"""

import pytest
from unittest.mock import patch

import app.core.environment as env_mod
from app.core import claude_auth_mode
from app.core.environment import inject_claude_code_auth, _build_env_with_ai_tools
from app.core.query_options import _ai_tools_credential_fingerprint
from fluid.tools.child_env import UNSET_ENV, child_env_with_overlay


def _mode(value):
    return patch.object(claude_auth_mode, "current_mode", return_value=value)


def _unset(env):
    return set(filter(None, env.get(UNSET_ENV, "").split(",")))


class TestApiKeyMode:
    def test_key_set_injects_anthropic_api_key(self):
        with _mode("api_key"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-decrypted"):
            env = inject_claude_code_auth({})
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-decrypted"
        assert _unset(env) == {"ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}

    def test_other_credentials_in_profile_env_are_dropped(self):
        base = {"ANTHROPIC_AUTH_TOKEN": "tok", "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "FOO": "bar"}
        with _mode("api_key"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-x"):
            env = inject_claude_code_auth(base)
        assert "ANTHROPIC_AUTH_TOKEN" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["FOO"] == "bar"

    def test_no_stored_key_injects_nothing_and_blocks_inherited_key(self):
        with _mode("api_key"), patch.object(env_mod, "_get_decrypted_api_key", return_value=None):
            env = inject_claude_code_auth({"ANTHROPIC_API_KEY": "from-profile"})
        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" in _unset(env)

    def test_reads_anthropic_api_key_setting_name(self):
        with _mode("api_key"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-x") as m:
            inject_claude_code_auth({})
        m.assert_called_once_with("anthropic_api_key")

    def test_mutates_and_returns_same_dict(self):
        original = {}
        with _mode("api_key"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-x"):
            returned = inject_claude_code_auth(original)
        assert returned is original
        assert original["ANTHROPIC_API_KEY"] == "sk-ant-x"


class TestSubscriptionMode:
    def test_stored_key_is_not_injected(self):
        """Both credentials present: the explicit choice wins, never the key."""
        with _mode("subscription"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-x") as m:
            env = inject_claude_code_auth({})
        assert "ANTHROPIC_API_KEY" not in env
        m.assert_not_called()

    def test_profile_env_key_is_dropped(self):
        with _mode("subscription"):
            env = inject_claude_code_auth({"ANTHROPIC_API_KEY": "sk-profile", "FOO": "bar"})
        assert "ANTHROPIC_API_KEY" not in env
        assert env["FOO"] == "bar"
        assert _unset(env) == {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}

    def test_host_key_never_reaches_the_cli_child(self):
        with _mode("subscription"):
            env = inject_claude_code_auth({"FOO": "bar"})
        child = child_env_with_overlay(env, {"ANTHROPIC_API_KEY": "sk-host", "PATH": "/bin"})
        assert "ANTHROPIC_API_KEY" not in child
        assert UNSET_ENV not in child
        assert child["FOO"] == "bar" and child["PATH"] == "/bin"


class TestOffMode:
    def test_every_anthropic_credential_is_stripped(self):
        base = {
            "ANTHROPIC_API_KEY": "sk-profile",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
            "FOO": "bar",
        }
        with _mode("off"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-x") as m:
            env = inject_claude_code_auth(base)
        m.assert_not_called()
        assert env["FOO"] == "bar"
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            assert name not in env
        assert _unset(env) == {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}

    def test_host_credentials_never_reach_the_cli_child(self):
        with _mode("off"):
            env = inject_claude_code_auth({})
        child = child_env_with_overlay(env, {
            "ANTHROPIC_API_KEY": "sk-host", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-host", "PATH": "/bin",
        })
        assert "ANTHROPIC_API_KEY" not in child and "CLAUDE_CODE_OAUTH_TOKEN" not in child
        assert child["PATH"] == "/bin"


def test_provider_session_is_left_alone():
    with _mode("api_key"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-x"):
        env = inject_claude_code_auth({"ANTHROPIC_AUTH_TOKEN": "gw"}, provider_id="p1")
    assert env == {"ANTHROPIC_AUTH_TOKEN": "gw"}


class TestMainSubprocessEnvAlwaysOnInjection:
    """``_build_env_with_ai_tools`` applies the mode even with NO ai tools."""

    def test_injected_when_ai_tools_config_is_none(self):
        with _mode("api_key"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-main"):
            env = _build_env_with_ai_tools(base_env=None, ai_tools_config=None)
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-main"
        assert env["CLAUDECODE"] == ""

    def test_injected_when_ai_tools_all_disabled(self):
        with _mode("api_key"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-main"):
            env = _build_env_with_ai_tools(
                base_env=None,
                ai_tools_config={"image_generation": False, "tts": False},
            )
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-main"

    def test_not_injected_in_subscription_mode(self):
        with _mode("subscription"), patch.object(env_mod, "_get_decrypted_api_key", return_value="sk-ant-main"):
            env = _build_env_with_ai_tools(base_env=None, ai_tools_config=None)
        assert "ANTHROPIC_API_KEY" not in env


class TestModeDefault:
    @pytest.mark.parametrize("key,signed_in,expected", [
        (False, False, "subscription"),
        (False, True, "subscription"),
        (True, True, "subscription"),
        (True, False, "api_key"),
        (True, None, "api_key"),
    ])
    def test_default(self, key, signed_in, expected):
        assert claude_auth_mode.default_mode(key, signed_in) == expected

    def test_recorded_once_and_never_switched(self, mock_db):
        with patch.object(claude_auth_mode, "has_stored_api_key", return_value=False):
            assert claude_auth_mode.ensure_mode_recorded(False) == "subscription"
        # A key appears later and the CLI is still signed out: the recorded
        # mode stays; only the user changes it.
        with patch.object(claude_auth_mode, "has_stored_api_key", return_value=True):
            assert claude_auth_mode.ensure_mode_recorded(False) == "subscription"
            assert claude_auth_mode.current_mode() == "subscription"

    def test_existing_key_only_install_defaults_to_api_key(self, mock_db):
        with patch.object(claude_auth_mode, "has_stored_api_key", return_value=True):
            assert claude_auth_mode.ensure_mode_recorded(False) == "api_key"
        assert claude_auth_mode.stored_mode() == "api_key"

    def test_set_mode_rejects_unknown(self, mock_db):
        with pytest.raises(ValueError):
            claude_auth_mode.set_mode("both")

    def test_off_is_a_mode_the_user_can_choose(self, mock_db):
        assert claude_auth_mode.set_mode("off") == "off"
        assert claude_auth_mode.current_mode() == "off" and claude_auth_mode.is_off()
        out = claude_auth_mode.summary({})
        assert out["mode"] == "off" and out["mode_label"] == "Off"
        assert {"value": "off", "label": "Off"} in out["modes"]

    @pytest.mark.parametrize("key", [False, True])
    @pytest.mark.parametrize("signed_in", [False, True, None])
    def test_the_default_is_never_off(self, key, signed_in):
        assert claude_auth_mode.default_mode(key, signed_in) != "off"

    def test_a_recorded_off_survives_startup(self, mock_db):
        claude_auth_mode.set_mode("off")
        with patch.object(claude_auth_mode, "has_stored_api_key", return_value=True):
            assert claude_auth_mode.ensure_mode_recorded(True) == "off"

    def test_summary_shows_last_four_only(self, mock_db):
        claude_auth_mode.set_mode("api_key")
        with patch.object(claude_auth_mode, "stored_api_key", return_value="sk-ant-api03-secret-WXYZ"):
            out = claude_auth_mode.summary({"installed": True, "signed_in": True, "email": "a@b.c"})
        assert out["mode"] == "api_key" and out["explicit"] is True
        assert out["api_key"] == {"set": True, "last4": "WXYZ"}
        assert "secret" not in repr(out)
        assert out["cli"]["email"] == "a@b.c"


def test_ai_tools_credential_fingerprint_is_non_secret_and_stable():
    first = _ai_tools_credential_fingerprint({"FAL_KEY": "fal-a"})
    assert first == _ai_tools_credential_fingerprint({"FAL_KEY": "fal-a"})
    assert first != "fal-a"
    assert _ai_tools_credential_fingerprint({}) is None


def test_ai_tools_server_passes_current_fal_key_to_fresh_node_worker():
    from app.mcp import ai_tools_sdk

    captured = {}

    class CapturingWorker:
        def __init__(self, worker_script, env=None, cwd=None):
            captured.update(worker_script=worker_script, env=env, cwd=cwd)

        async def call(self, *_args, **_kwargs):
            raise AssertionError("tool execution is outside this wiring test")

    with patch.object(ai_tools_sdk, "NodeWorker", CapturingWorker):
        server = ai_tools_sdk.create_ai_tools_server(
            {"image_generation": True},
            "C:/tools",
            tool_env={"FAL_KEY": "fal-current"},
            tool_cwd="C:/workspace",
        )

    assert server is not None
    assert captured["env"] == {"FAL_KEY": "fal-current"}
    assert captured["worker_script"].endswith("/dist/worker.js")
    assert captured["cwd"] == "C:/workspace"
