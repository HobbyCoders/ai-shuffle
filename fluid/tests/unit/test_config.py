"""M6 — layered config precedence, TOML parsing, and the engine factory."""

from collections.abc import AsyncIterator

from fluid.config.loader import (
    BUILTIN_DEFAULTS,
    build_engine,
    deep_merge,
    env_overrides,
    load_config,
)
from fluid.config.model import HarnessConfig
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.native import NativeLoop
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore


class FakeProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        if False:  # pragma: no cover - never actually streamed here
            yield StreamChunk("message_stop", {})


def _write_toml(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_deep_merge_is_recursive() -> None:
    base = {"a": 1, "nested": {"x": 1, "y": 2}}
    overlay = {"nested": {"y": 20, "z": 30}, "b": 2}
    merged = deep_merge(base, overlay)
    assert merged == {"a": 1, "b": 2, "nested": {"x": 1, "y": 20, "z": 30}}
    assert base["nested"] == {"x": 1, "y": 2}  # base untouched


def test_defaults_when_no_files(tmp_path) -> None:
    config = load_config(cwd=tmp_path, include_system=False, include_user=False, env={})
    assert config.default_provider == BUILTIN_DEFAULTS["default_provider"]
    assert config.permission.approval_policy == "on_write"
    assert "anthropic" in config.providers
    assert config.providers["claude"].kind == "delegated_claude"


def test_project_overrides_user_overrides_default(tmp_path, monkeypatch) -> None:
    # User layer is owned by Fluid under the platform home.
    user_home = tmp_path / "home"
    (user_home / ".fluid").mkdir(parents=True)
    _write_toml(
        user_home / ".fluid" / "config.toml",
        'default_model = "user-model"\ncode_mode = true\n'
        '[permission]\napproval_policy = "always_ask"\n',
    )
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))

    # Project layer (./.fluid/config.toml) wins over user for default_model.
    proj = tmp_path / "proj"
    (proj / ".fluid").mkdir(parents=True)
    _write_toml(proj / ".fluid" / "config.toml", 'default_model = "project-model"\n')

    config = load_config(cwd=proj, include_system=False, env={})
    assert config.default_model == "project-model"  # project wins
    assert config.code_mode is True  # inherited from user
    assert config.permission.approval_policy == "always_ask"  # inherited from user


def test_env_override_beats_files(tmp_path, monkeypatch) -> None:
    proj = tmp_path / "proj"
    (proj / ".fluid").mkdir(parents=True)
    _write_toml(proj / ".fluid" / "config.toml", 'default_provider = "openai"\n')

    env = {
        "FLUID_DEFAULT_PROVIDER": "openrouter",
        "FLUID_CODE_MODE": "true",
        "FLUID_APPROVAL_POLICY": "never_ask",
    }
    assert env_overrides(env)["default_provider"] == "openrouter"
    config = load_config(cwd=proj, include_system=False, include_user=False, env=env)
    assert config.default_provider == "openrouter"  # env beats the project file
    assert config.code_mode is True
    assert config.permission.approval_policy == "never_ask"


def test_explicit_overrides_beat_env(tmp_path) -> None:
    env = {"FLUID_DEFAULT_MODEL": "from-env"}
    config = load_config(
        cwd=tmp_path, include_system=False, include_user=False, env=env,
        overrides={"default_model": "from-cli"},
    )
    assert config.default_model == "from-cli"


def test_toml_rules_parse(tmp_path) -> None:
    proj = tmp_path / "proj"
    (proj / ".fluid").mkdir(parents=True)
    _write_toml(
        proj / ".fluid" / "config.toml",
        '[permission]\napproval_policy = "on_write"\n'
        '[[permission.rules]]\ntool = "Bash"\ndecision = "deny"\n'
        '[permission.rules.input_globs]\ncommand = "rm *"\n',
    )
    config = load_config(cwd=proj, include_system=False, include_user=False, env={})
    assert len(config.permission.rules) == 1
    rule = config.permission.rules[0]
    assert rule.tool == "Bash"
    assert rule.decision == "deny"
    assert rule.input_globs == {"command": "rm *"}


def test_provider_ecosystem_switches_are_not_runtime_config() -> None:
    config = HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {"claude_ecosystem": {"settings": True}})
    )
    assert "claude_ecosystem" not in config.to_dict()
    assert "claude_ecosystem" not in env_overrides({"FLUID_CLAUDE_SETTINGS": "true"})


def test_native_mcp_json_loads_and_toml_wins_same_name(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    fluid_home = home / ".fluid"
    fluid_home.mkdir(parents=True)
    (fluid_home / "mcp.json").write_text(
        '{"mcpServers":{"shared":{"command":"imported"},"json-only":{"command":"json"}}}',
        encoding="utf-8",
    )
    _write_toml(
        fluid_home / "config.toml",
        '[mcp.shared]\ncommand = "native"\n',
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    config = load_config(cwd=tmp_path, include_system=False, env={})
    assert config.mcp["shared"]["command"] == "native"
    assert config.mcp["json-only"]["command"] == "json"


def test_provider_settings_are_inert_until_explicit_import(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        '{"permissions": {"deny": ["Bash(rm *)"]}, "env": {"FLUID_TOGGLE_TEST": "1"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)}))
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert engine.claude_settings == {}
    assert "settings_env" not in engine.ctx.extra
    assert all(r.tool != "Bash" for r in engine.permissions.rules)


def test_build_engine_native_for_anthropic(tmp_path) -> None:
    config = HarnessConfig.from_dict(BUILTIN_DEFAULTS)
    store = MemoryStore()
    engine = build_engine(config, store, provider=FakeProvider())
    assert isinstance(engine, NativeLoop)
    assert engine.model == config.default_model
    # A builtins registry is wired by default.
    names = {s.name for s in engine.registry.list_tools()}
    assert {"Read", "Edit", "Bash"} <= names
    assert engine.permissions is not None


def test_build_engine_wires_session_search_from_the_store(tmp_path) -> None:
    """SessionSearch's backend is whatever the store offers — or nothing."""
    from fluid.store.jsonl import JsonlStore

    config = HarnessConfig.from_dict(BUILTIN_DEFAULTS)
    store = JsonlStore(tmp_path / "sessions")
    engine = build_engine(config, store, provider=FakeProvider())
    assert engine.ctx.extra["session_search"] == store.search
    assert engine.ctx.extra["session_recent"] == store.recent_sessions
    assert engine.ctx.extra["session_view"] == store.session_slice

    # MemoryStore keeps no durable record, so the capabilities stay unwired
    # and the tools never reach the request schemas.
    plain = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert "session_search" not in plain.ctx.extra
    assert "session_recent" not in plain.ctx.extra
    assert "session_view" not in plain.ctx.extra


def test_build_engine_code_mode_wires_session(tmp_path) -> None:
    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {"code_mode": True}))
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert isinstance(engine, NativeLoop)
    assert engine._codemode is not None
    # code-mode replaces the schema with the four-tool surface
    tool_names = {t["name"] for t in engine._build_request([]).tools}
    assert "run_python" in tool_names and "Bash" not in tool_names


def test_build_engine_delegated_for_claude_kind() -> None:
    config = HarnessConfig.from_dict(BUILTIN_DEFAULTS)
    engine = build_engine(config, MemoryStore(), provider_name="claude")
    assert isinstance(engine, DelegatedLoop)
    assert engine.model == config.model_for("claude")


def test_provider_and_model_resolution() -> None:
    config = HarnessConfig.from_dict(BUILTIN_DEFAULTS)
    assert config.provider("openai").kind == "openai_responses"
    # empty provider model falls back to default_model
    merged = deep_merge(BUILTIN_DEFAULTS, {"providers": {"anthropic": {"kind": "anthropic", "model": ""}}})
    cfg2 = HarnessConfig.from_dict(merged)
    assert cfg2.model_for("anthropic") == cfg2.default_model


def test_max_model_calls_default_and_env_override() -> None:
    assert env_overrides({}).get("max_model_calls") is None
    assert env_overrides({"FLUID_MAX_CALLS": "500"})["max_model_calls"] == 500
    assert "max_model_calls" not in env_overrides({"FLUID_MAX_CALLS": "lots"})
    assert HarnessConfig.from_dict(dict(BUILTIN_DEFAULTS)).max_model_calls == 200


def test_max_model_calls_zero_means_unlimited() -> None:
    """``0`` is the config spelling of "no ceiling"; NativeLoop spells it None."""
    config = HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {"max_model_calls": 0})
    )
    assert config.max_model_calls == 0
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert isinstance(engine, NativeLoop)
    assert engine.max_model_calls_per_turn is None

    default_engine = build_engine(
        HarnessConfig.from_dict(dict(BUILTIN_DEFAULTS)),
        MemoryStore(),
        provider=FakeProvider(),
    )
    assert default_engine.max_model_calls_per_turn == 200
