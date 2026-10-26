"""Config layer 6 (``FLUID_*`` env vars) and native provider construction.

The env layer sits above every file layer and below explicit overrides; a
malformed number must drop out rather than clobber the layer beneath it."""

from __future__ import annotations

import pytest

from fluid.config.loader import (
    BUILTIN_DEFAULTS,
    _build_provider_client,
    deep_merge,
    env_overrides,
    load_config,
)
from fluid.config.model import HarnessConfig


def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".fluid").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


# ─── env_overrides: every recognized variable ────────────────────────────────


def test_env_overrides_maps_every_recognized_variable() -> None:
    env = {
        "FLUID_DEFAULT_PROVIDER": "openai",
        "FLUID_DEFAULT_MODEL": "m-default",
        "FLUID_MODEL": "m-override",
        "FLUID_CWD": "/work",
        "FLUID_CODE_MODE": "yes",
        "FLUID_BASE_PROMPT": "0",
        "FLUID_IDENTITY_FILE": "off",
        "FLUID_MODEL_GUIDANCE": "claude, gpt ,,",
        "FLUID_SKILLS_DIR": "/skills",
        "FLUID_SETTING_SOURCES": "user, local",
        "FLUID_CONTINUAL": "TRUE",
        "FLUID_CONTINUAL_MODEL": "m-small",
        "FLUID_CONTINUAL_PROVIDER": "openrouter",
        "FLUID_ANCHOR_BUDGET": "3",
        "FLUID_MAX_TOKENS": "4096",
        "FLUID_MAX_CALLS": "12",
        "FLUID_CONTEXT_WINDOW": "200000",
        "FLUID_APPROVAL_POLICY": "always_ask",
        "FLUID_CONTEXT_ENABLED": "on",
        "FLUID_CONTEXT_TRIGGER_TOKENS": "50000",
        "FLUID_CONTEXT_HEADROOM_TOKENS": "8000",
        "FLUID_CONTEXT_TRIGGER_WINDOW_RATIO": "0.5",
        "UNRELATED": "ignored",
    }
    assert env_overrides(env) == {
        "default_provider": "openai",
        "default_model": "m-default",
        "model_override": "m-override",
        "cwd": "/work",
        "code_mode": True,
        "base_prompt": False,
        "identity_file": False,
        "model_guidance": ["claude", "gpt"],
        "skills_dir": "/skills",
        "setting_sources": ["user", "local"],
        "continual": True,
        "continual_model": "m-small",
        "continual_provider": "openrouter",
        "anchor_budget": 3,
        "max_tokens": 4096,
        "max_model_calls": 12,
        "context_window": 200000,
        "permission": {"approval_policy": "always_ask"},
        "context": {
            "enabled": True,
            "trigger_tokens": 50000,
            "headroom_tokens": 8000,
            "trigger_window_ratio": 0.5,
        },
    }


def test_env_overrides_is_empty_without_fluid_vars() -> None:
    assert env_overrides({"PATH": "/bin"}) == {}


def test_env_overrides_reads_the_process_environment_by_default(monkeypatch) -> None:
    monkeypatch.setenv("FLUID_DEFAULT_MODEL", "from-process-env")
    assert env_overrides()["default_model"] == "from-process-env"


@pytest.mark.parametrize("var", [
    "FLUID_ANCHOR_BUDGET", "FLUID_MAX_TOKENS", "FLUID_MAX_CALLS", "FLUID_CONTEXT_WINDOW",
])
def test_malformed_integers_are_dropped(var) -> None:
    assert env_overrides({var: "lots"}) == {}


def test_malformed_context_numbers_are_dropped_individually() -> None:
    out = env_overrides({
        "FLUID_CONTEXT_TRIGGER_TOKENS": "many",
        "FLUID_CONTEXT_HEADROOM_TOKENS": "9000",
        "FLUID_CONTEXT_TRIGGER_WINDOW_RATIO": "most",
    })
    assert out == {"context": {"headroom_tokens": 9000}}
    assert env_overrides({"FLUID_CONTEXT_TRIGGER_WINDOW_RATIO": "x"}) == {}


def test_single_model_guidance_value_stays_a_string() -> None:
    assert env_overrides({"FLUID_MODEL_GUIDANCE": "  off  "})["model_guidance"] == "off"


def test_empty_setting_sources_means_none() -> None:
    assert env_overrides({"FLUID_SETTING_SOURCES": " , "})["setting_sources"] == []


# ─── precedence: files < env < overrides ─────────────────────────────────────


def test_env_layer_beats_user_and_project_files_but_not_overrides(tmp_path, monkeypatch) -> None:
    home = _isolated_home(tmp_path, monkeypatch)
    (home / ".fluid" / "config.toml").write_text(
        'default_model = "user"\nmax_tokens = 111\n'
        '[permission]\napproval_policy = "never_ask"\n'
        '[context]\ntrigger_tokens = 1000\nheadroom_tokens = 2000\n',
        encoding="utf-8",
    )
    project = tmp_path / "project"
    (project / ".fluid").mkdir(parents=True)
    (project / ".fluid" / "config.toml").write_text(
        'default_model = "project"\nmax_tokens = 222\n', encoding="utf-8"
    )
    env = {
        "FLUID_DEFAULT_MODEL": "env",
        "FLUID_MAX_TOKENS": "333",
        "FLUID_APPROVAL_POLICY": "always_ask",
        "FLUID_CONTEXT_TRIGGER_TOKENS": "5000",
    }

    config = load_config(cwd=project, include_system=False, env=env)
    assert config.default_model == "env"
    assert config.max_tokens == 333
    assert config.permission.approval_policy == "always_ask"
    # Env context keys merge into the file's table instead of replacing it.
    assert config.context.trigger_tokens == 5000
    assert config.context.headroom_tokens == 2000

    config = load_config(
        cwd=project, include_system=False, env=env,
        overrides={"default_model": "flag", "permission": {"approval_policy": "on_write"}},
    )
    assert config.default_model == "flag"
    assert config.permission.approval_policy == "on_write"
    assert config.max_tokens == 333


def test_malformed_env_number_does_not_clobber_the_file_value(tmp_path, monkeypatch) -> None:
    home = _isolated_home(tmp_path, monkeypatch)
    (home / ".fluid" / "config.toml").write_text("max_tokens = 111\n", encoding="utf-8")

    config = load_config(cwd=tmp_path, include_system=False, env={"FLUID_MAX_TOKENS": "lots"})

    assert config.max_tokens == 111


def test_env_setting_sources_gates_the_user_mcp_file(tmp_path, monkeypatch) -> None:
    home = _isolated_home(tmp_path, monkeypatch)
    (home / ".fluid" / "mcp.json").write_text(
        '{"mcpServers": {"docs": {"command": "docs-server"}}}', encoding="utf-8"
    )

    with_user = load_config(cwd=tmp_path, include_system=False, env={})
    assert "docs" in with_user.mcp

    project_only = load_config(
        cwd=tmp_path, include_system=False, env={"FLUID_SETTING_SOURCES": "project"}
    )
    assert "docs" not in project_only.mcp

    # An override still outranks the env layer's source list.
    flag = load_config(
        cwd=tmp_path, include_system=False, env={"FLUID_SETTING_SOURCES": "project"},
        overrides={"setting_sources": ["user"]},
    )
    assert "docs" in flag.mcp


def test_env_cwd_overrides_the_discovery_cwd_value(tmp_path) -> None:
    config = load_config(
        cwd=tmp_path, include_system=False, include_user=False, env={"FLUID_CWD": "/elsewhere"}
    )
    assert config.cwd == "/elsewhere"


# ─── _build_provider_client ──────────────────────────────────────────────────


def _config(**providers) -> HarnessConfig:
    return HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {"providers": providers}))


def test_unknown_provider_name_names_the_configured_ones() -> None:
    with pytest.raises(KeyError, match="no provider 'nope' configured"):
        _build_provider_client(_config(), "nope")


def test_local_provider_requires_a_base_url() -> None:
    config = _config(llama={"kind": "local"})
    with pytest.raises(ValueError, match="requires base_url"):
        _build_provider_client(config, "llama")


def test_delegated_kind_is_not_a_native_client() -> None:
    with pytest.raises(ValueError, match="not a native provider kind"):
        _build_provider_client(_config(), "claude")


def test_unknown_kind_or_tool_style_fails_at_parse_time() -> None:
    with pytest.raises(ValueError, match="unknown provider kind"):
        _config(bad={"kind": "carrier_pigeon"})
    with pytest.raises(ValueError, match="unknown tool_style"):
        _config(bad={"kind": "local", "tool_style": "telepathy"})


@pytest.mark.parametrize("kind, module, cls", [
    ("anthropic", "fluid.providers.anthropic.client", "AnthropicClient"),
    ("openai_responses", "fluid.providers.openai_responses.client", "OpenAIResponsesClient"),
    ("openrouter", "fluid.providers.openrouter.client", "OpenRouterClient"),
    ("local", "fluid.providers.local.client", "LocalClient"),
])
def test_each_native_kind_builds_with_key_url_and_deadline(monkeypatch, kind, module, cls) -> None:
    import importlib

    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-test")
    config = _config(p={
        "kind": kind, "base_url": "http://127.0.0.1:9", "api_key_env": "MY_PROVIDER_KEY",
        "stream_deadline": 0,
    })

    client = _build_provider_client(config, "p")

    assert type(client) is getattr(importlib.import_module(module), cls)
    assert client._api_key == "sk-test"
    # 0 in config means "no whole-stream deadline", passed through as None.
    assert getattr(client, "_stream_deadline", None) is None


def test_absent_deadline_keeps_the_client_default() -> None:
    from fluid.providers._transport import DEFAULT_STREAM_DEADLINE_S

    client = _build_provider_client(_config(p={"kind": "anthropic"}), "p")
    assert client._stream_deadline == DEFAULT_STREAM_DEADLINE_S


def test_unset_api_key_env_passes_no_key(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_KEY_VAR", raising=False)
    client = _build_provider_client(
        _config(p={"kind": "local", "base_url": "http://127.0.0.1:9", "api_key_env": "MISSING_KEY_VAR"}),
        "p",
    )
    assert client._api_key == "local"  # the local client's placeholder


def test_text_tool_style_wraps_the_client() -> None:
    from fluid.providers.local.client import LocalClient
    from fluid.providers.local.text_tools import TextToolsAdapter

    config = _config(p={"kind": "local", "base_url": "http://127.0.0.1:9", "tool_style": "text"})
    client = _build_provider_client(config, "p")

    assert isinstance(client, TextToolsAdapter)
    assert isinstance(client._inner, LocalClient)
    assert client.name == client._inner.name
