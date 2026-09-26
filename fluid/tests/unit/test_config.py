"""M6 — layered config precedence, TOML parsing, and the engine factory."""

import json
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


def _non_default_config_dict() -> dict:
    """Every HarnessConfig field (nested ones too) set away from its default."""
    return {
        "default_provider": "p1",
        "default_model": "m",
        "model_override": "mo",
        "providers": {
            "p1": {
                "kind": "openrouter",
                "model": "x",
                "base_url": "http://h",
                "api_key_env": "K",
                "tool_style": "text",
                "stream_deadline": 12.5,
                "first_byte_timeout": 90.0,
                "extra": {"a": 1},
            },
        },
        "permission": {
            "approval_policy": "always_ask",
            "protect_sensitive_files": False,
            "rules": [{"tool": "Bash", "decision": "deny", "input_globs": {"command": "rm*"}}],
        },
        "code_mode": True,
        "skills_dir": "/s",
        "continual": True,
        "continual_model": "cm",
        "continual_provider": "p1",
        "anchor_budget": 3,
        "instruction_files": {
            "enabled": False, "global": False, "project": False, "skills": False,
            "commands": False, "nested": False, "plugins": False,
        },
        "setting_sources": ["user"],
        "tools": {"allowed": ["Read"], "disallowed": ["Bash"]},
        "context": {
            "enabled": False,
            "trim_only": True,
            "trigger_tokens": 1,
            "trigger_window_ratio": 0.1,
            "headroom_tokens": 2,
            "target_ratio": 0.2,
            "keep_recent_turns": 4,
            "image_trigger_ratio": 0.3,
            "max_resident_span_digests": 5,
            "epoch_fold_spans": 6,
            "intake_stub_chars": 7,
            "tool_result_stub_chars": 8,
            "keep_recent_tool_results": 9,
            "min_result_chars": 10,
        },
        "hooks": [{"event": "PreToolUse", "command": "echo", "matcher": "Bash", "timeout": 5.0}],
        "cwd": "/w",
        "max_tokens": 1000,
        "max_model_calls": 50,
        "context_window": 200_000,
        "cache_key": "ck",
        "system": "sys",
        "base_prompt": False,
        "identity": "id",
        "identity_file": False,
        "model_guidance": ["gpt"],
        "mcp": {"srv": {"transport": "stdio", "command": "x"}},
    }


def _assert_every_field_non_default(obj) -> None:
    from dataclasses import MISSING, fields

    for f in fields(obj):
        if f.default is not MISSING:
            default = f.default
        elif f.default_factory is not MISSING:
            default = f.default_factory()
        else:
            continue
        assert getattr(obj, f.name) != default, (
            f"{type(obj).__name__}.{f.name} is at its default; add it to "
            "_non_default_config_dict so the to_dict round trip covers it"
        )


def test_to_dict_round_trips_every_field() -> None:
    config = HarnessConfig.from_dict(_non_default_config_dict())
    # A new field fails here until the fixture sets it, so to_dict can't drift.
    for obj in (
        config, config.permission, config.instruction_files, config.tools,
        config.context, *config.providers.values(), *config.permission.rules,
        *config.hooks,
    ):
        _assert_every_field_non_default(obj)
    dumped = config.to_dict()
    assert HarnessConfig.from_dict(json.loads(json.dumps(dumped))) == config


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


# ─── context window: config wins, the model table fills the gap ──────────────


def test_build_engine_falls_back_to_the_models_window() -> None:
    config = HarnessConfig.from_dict(BUILTIN_DEFAULTS)  # claude-sonnet-5
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert config.context_window is None  # the config is not rewritten
    assert isinstance(engine, NativeLoop)
    assert engine.context_window == 1_000_000
    # A model switch re-resolves the fallback.
    engine.model = "claude-haiku-4-5"
    assert engine.context_window == 200_000


def test_configured_window_beats_the_model_table() -> None:
    config = HarnessConfig.from_dict(
        deep_merge(BUILTIN_DEFAULTS, {"context_window": 123_000})
    )
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert engine.context_window == 123_000
    engine.model = "claude-haiku-4-5"
    assert engine.context_window == 123_000
    assert env_overrides({"FLUID_CONTEXT_WINDOW": "77000"})["context_window"] == 77_000
    assert "context_window" not in env_overrides({"FLUID_CONTEXT_WINDOW": "big"})


def test_unknown_model_keeps_the_window_unknown() -> None:
    config = HarnessConfig.from_dict(deep_merge(
        BUILTIN_DEFAULTS,
        {"providers": {"anthropic": {"kind": "anthropic", "model": "my-local-llm"}}},
    ))
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert engine.model == "my-local-llm"
    assert engine.context_window is None


# ─── project walk, mcp.json normalization, settings toggles, system path ─────


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _home(monkeypatch, path):
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    return path


def test_project_layers_walk_up_from_the_git_root(tmp_path, monkeypatch) -> None:
    _home(monkeypatch, tmp_path / "home")
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write(root / ".fluid" / "config.toml", 'default_model = "root"\ncode_mode = true\n')
    _write(root / ".fluid" / "mcp.json", '{"mcpServers": {"rootsrv": {"command": "r"}, "shared": {"command": "outer"}}}')
    _write(root / ".fluid" / "hooks.json", '{"hooks": {"TurnEnd": [{"hooks": [{"command": "root-hook"}]}]}}')
    sub = root / "pkg"
    _write(sub / ".fluid" / "config.toml", 'default_model = "pkg"\n')
    _write(sub / ".fluid" / "mcp.json", '{"mcpServers": {"shared": {"command": "inner"}}}')
    deeper = sub / "src" / "mod"
    deeper.mkdir(parents=True)

    config = load_config(cwd=deeper, include_system=False, env={})
    assert config.default_model == "pkg"  # nearest wins
    assert config.code_mode is True  # outer still applies
    assert config.mcp["rootsrv"]["command"] == "r"
    assert config.mcp["shared"]["command"] == "inner"
    assert [h.command for h in config.hooks] == ["root-hook"]
    # fluid.toml is the per-directory fallback.
    _write(deeper / "fluid.toml", 'default_model = "leaf"\n')
    assert load_config(cwd=deeper, include_system=False, env={}).default_model == "leaf"


def test_outside_a_git_checkout_only_the_cwd_is_project(tmp_path, monkeypatch) -> None:
    _home(monkeypatch, tmp_path / "home")
    outer = tmp_path / "outer"
    _write(outer / ".fluid" / "config.toml", 'default_model = "outer"\n')
    inner = outer / "inner"
    _write(inner / ".fluid" / "config.toml", 'code_mode = true\n')
    config = load_config(cwd=inner, include_system=False, env={})
    assert config.code_mode is True
    assert config.default_model == BUILTIN_DEFAULTS["default_model"]


def test_toml_mcp_entry_replaces_the_json_entry_whole(tmp_path, monkeypatch) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    _write(
        home / ".fluid" / "mcp.json",
        '{"mcpServers":{"shared":{"command":"imported","env":{"LEFTOVER":"1"},"args":["-x"]}}}',
    )
    _write_toml(home / ".fluid" / "config.toml", '[mcp.shared]\ncommand = "native"\n')
    config = load_config(cwd=tmp_path, include_system=False, env={})
    assert config.mcp["shared"] == {"command": "native"}


def test_mcp_json_normalizes_transports_and_skips_sse(tmp_path, monkeypatch) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    _write(home / ".fluid" / "mcp.json", json.dumps({"mcpServers": {
        "stdio": {"command": "srv", "args": ["a"]},
        "http": {"type": "http", "url": "https://example.test/mcp"},
        "sse": {"type": "sse", "url": "https://example.test/sse"},
        "off": {"command": "srv", "disabled": True},
        "off2": {"command": "srv", "enabled": False},
    }}))
    config = load_config(cwd=tmp_path, include_system=False, env={})
    assert set(config.mcp) == {"stdio", "http"}
    assert config.mcp["stdio"] == {"command": "srv", "args": ["a"], "transport": "stdio"}
    assert config.mcp["http"] == {"url": "https://example.test/mcp", "transport": "streamable_http"}


def test_mcp_json_expands_variables_from_env_and_settings(tmp_path, monkeypatch) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("FLUID_TEST_HOST", "example.test")
    monkeypatch.delenv("FLUID_TEST_TOKEN", raising=False)
    _write(home / ".fluid" / "settings.json", json.dumps({"env": {"FLUID_TEST_TOKEN": "from-settings"}}))
    _write(home / ".fluid" / "mcp.json", json.dumps({"mcpServers": {
        "remote": {
            "url": "https://${FLUID_TEST_HOST}/mcp",
            "headers": {"Authorization": "Bearer ${FLUID_TEST_TOKEN}", "X-Missing": "${FLUID_TEST_ABSENT}"},
        },
        "local": {"command": "srv", "args": ["${CLAUDE_PROJECT_DIR}"]},
    }}))
    proj = tmp_path / "proj"
    proj.mkdir()
    config = load_config(cwd=proj, include_system=False, env={})
    assert config.mcp["remote"]["url"] == "https://example.test/mcp"
    assert config.mcp["remote"]["headers"] == {"Authorization": "Bearer from-settings", "X-Missing": ""}
    assert config.mcp["local"]["args"] == [str(proj)]


def test_settings_toggles_filter_mcp_servers(tmp_path, monkeypatch) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    _write(home / ".fluid" / "mcp.json", json.dumps({"mcpServers": {
        "usersrv": {"command": "u"}, "gone": {"command": "g"},
    }}))
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    _write(proj / ".fluid" / "mcp.json", json.dumps({"mcpServers": {
        "approved": {"command": "a"}, "unlisted": {"command": "x"},
    }}))
    _write(proj / ".fluid" / "settings.json", json.dumps({
        "enabledMcpjsonServers": ["approved"],
        "disabledMcpServers": ["gone"],
    }))
    config = load_config(cwd=proj, include_system=False, env={})
    # enabledMcpjsonServers gates the project mcp.json only.
    assert set(config.mcp) == {"usersrv", "gone", "approved"}
    engine = build_engine(config, MemoryStore(), provider=FakeProvider())
    assert set(engine._mcp_servers) == {"usersrv", "approved"}


def test_system_config_path_per_platform() -> None:
    from pathlib import Path

    from fluid.config.loader import system_config_path

    assert system_config_path("linux", {}) == Path("/etc/fluid/config.toml")
    assert system_config_path("darwin", {}) == Path("/etc/fluid/config.toml")
    # Joined, not spelled with backslashes: on POSIX ``Path`` treats ``\`` as
    # an ordinary character, so a literal ``D:\PD\fluid`` is one component.
    assert system_config_path("win32", {"ProgramData": r"D:\PD"}) == Path(r"D:\PD") / "fluid" / "config.toml"
    assert system_config_path("win32", {}) == Path(r"C:\ProgramData") / "fluid" / "config.toml"



def test_build_engine_shares_a_synthesizer_and_stamps_the_agent(tmp_path) -> None:
    """CL-3: a subagent host hands its parent's learner in, so one store has
    one synthesizer, and every entry the child journals names the agent."""
    from types import SimpleNamespace

    from fluid.codemode.skills import SkillStore

    class Learner:
        pass

    learner = Learner()
    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {
        "cwd": str(tmp_path), "continual": True, "skills_dir": str(tmp_path / "skills"),
    }))
    engine = build_engine(
        config, MemoryStore(), provider=SimpleNamespace(), session_id="ses_child",
        synthesizer=learner, agent="worker",
    )
    assert engine._journal._synthesizer is learner
    engine.ctx.extra["memory_store"].journal_append(
        {"kind": "user", "text": "the user likes short replies"})
    entry = SkillStore(tmp_path / "skills").journal_entries(0)[0]
    assert entry["session"] == "ses_child" and entry["agent"] == "worker"


def test_cli_host_hands_the_parent_learner_to_children() -> None:
    from types import SimpleNamespace

    from fluid.cli.host import CliHost

    host = CliHost.__new__(CliHost)
    marker = object()
    host.engine = SimpleNamespace(_journal=SimpleNamespace(_synthesizer=marker))
    assert host._learner() is marker
    host.engine = SimpleNamespace(_codemode=SimpleNamespace(_synthesizer=marker), _journal=None)
    assert host._learner() is marker
    host.engine = SimpleNamespace()
    assert host._learner() is None
