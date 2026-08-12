"""M6 — layered config precedence, TOML parsing, and the engine factory."""

from typing import AsyncIterator

from loom.config.loader import (
    BUILTIN_DEFAULTS,
    build_engine,
    deep_merge,
    env_overrides,
    load_config,
)
from loom.config.model import HarnessConfig
from loom.engine.delegated import DelegatedLoop
from loom.engine.native import NativeLoop
from loom.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from loom.store.base import MemoryStore


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
    # User layer via XDG_CONFIG_HOME.
    user_home = tmp_path / "xdg"
    (user_home / "loom").mkdir(parents=True)
    _write_toml(
        user_home / "loom" / "config.toml",
        'default_model = "user-model"\ncode_mode = true\n'
        '[permission]\napproval_policy = "always_ask"\n',
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_home))

    # Project layer (./.loom/config.toml) wins over user for default_model.
    proj = tmp_path / "proj"
    (proj / ".loom").mkdir(parents=True)
    _write_toml(proj / ".loom" / "config.toml", 'default_model = "project-model"\n')

    config = load_config(cwd=proj, include_system=False, env={})
    assert config.default_model == "project-model"  # project wins
    assert config.code_mode is True  # inherited from user
    assert config.permission.approval_policy == "always_ask"  # inherited from user


def test_env_override_beats_files(tmp_path, monkeypatch) -> None:
    proj = tmp_path / "proj"
    (proj / ".loom").mkdir(parents=True)
    _write_toml(proj / ".loom" / "config.toml", 'default_provider = "openai"\n')

    env = {
        "LOOM_DEFAULT_PROVIDER": "openrouter",
        "LOOM_CODE_MODE": "true",
        "LOOM_APPROVAL_POLICY": "never_ask",
    }
    assert env_overrides(env)["default_provider"] == "openrouter"
    config = load_config(cwd=proj, include_system=False, include_user=False, env=env)
    assert config.default_provider == "openrouter"  # env beats the project file
    assert config.code_mode is True
    assert config.permission.approval_policy == "never_ask"


def test_explicit_overrides_beat_env(tmp_path) -> None:
    env = {"LOOM_DEFAULT_MODEL": "from-env"}
    config = load_config(
        cwd=tmp_path, include_system=False, include_user=False, env=env,
        overrides={"default_model": "from-cli"},
    )
    assert config.default_model == "from-cli"


def test_toml_rules_parse(tmp_path) -> None:
    proj = tmp_path / "proj"
    (proj / ".loom").mkdir(parents=True)
    _write_toml(
        proj / ".loom" / "config.toml",
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
