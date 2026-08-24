"""Fluid settings.json stack — three-scope merge, permissions, env, sources.

The project walk is bounded by a ``.git`` marker (same convention as
test_instructions.py) and HOME is redirected to a tmp dir so the developer's
real ~/.fluid never leaks into an assertion.
"""

import json
import logging
from pathlib import Path

from fluid.context.settings_stack import (
    default_permission_mode,
    enabled_plugins,
    layered_env,
    load_settings_stack,
    mcp_toggles,
    parse_permission_entry,
    permission_rules,
    settings_env,
)
from fluid.permissions.rules import evaluate_rules


def _set_home(monkeypatch, home) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(Path(home) / ".config"))


def _write_settings(directory: Path, name: str, data: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _scoped(tmp_path, monkeypatch, *, user=None, project=None, local=None):
    """Lay out all three scopes and return the project dir."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _set_home(monkeypatch, home)
    if user is not None:
        _write_settings(home / ".fluid", "settings.json", user)

    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / ".git").mkdir(exist_ok=True)
    if project is not None:
        _write_settings(proj / ".fluid", "settings.json", project)
    if local is not None:
        _write_settings(proj / ".fluid", "settings.local.json", local)
    return proj


# ─── merge ───────────────────────────────────────────────────────────────────


def test_three_scope_merge_lists_accumulate_scalars_override(tmp_path, monkeypatch) -> None:
    proj = _scoped(
        tmp_path,
        monkeypatch,
        user={
            "model": "user-model",
            "permissions": {"allow": ["Read(./src/**)"]},
            "env": {"FROM_USER": "1", "SHARED": "user"},
        },
        project={
            "model": "project-model",
            "permissions": {"allow": ["Bash(npm run *)"]},
            "env": {"SHARED": "project"},
        },
        local={
            "permissions": {"allow": ["Bash(git *)"], "deny": ["Bash(rm *)"]},
            "env": {"SHARED": "local", "FROM_LOCAL": "1"},
        },
    )

    settings = load_settings_stack(proj)
    # Scalars: highest scope that sets them wins.
    assert settings["model"] == "project-model"
    # Lists accumulate in scope order.
    assert settings["permissions"]["allow"] == [
        "Read(./src/**)",
        "Bash(npm run *)",
        "Bash(git *)",
    ]
    assert settings["permissions"]["deny"] == ["Bash(rm *)"]
    # Dicts merge per key, higher scope winning the conflict.
    assert settings["env"] == {"FROM_USER": "1", "SHARED": "local", "FROM_LOCAL": "1"}


def test_local_scope_outranks_project(tmp_path, monkeypatch) -> None:
    proj = _scoped(
        tmp_path, monkeypatch,
        project={"model": "project-model"},
        local={"model": "local-model"},
    )
    assert load_settings_stack(proj)["model"] == "local-model"


def test_inner_project_dir_outranks_outer(tmp_path, monkeypatch) -> None:
    proj = _scoped(tmp_path, monkeypatch, project={"model": "outer", "env": {"A": "outer"}})
    inner = proj / "pkg"
    inner.mkdir()
    _write_settings(inner / ".fluid", "settings.json", {"model": "inner"})

    settings = load_settings_stack(inner)
    assert settings["model"] == "inner"
    assert settings["env"] == {"A": "outer"}  # outer keys survive


def test_no_fluid_setup_yields_empty(tmp_path, monkeypatch) -> None:
    proj = _scoped(tmp_path, monkeypatch)
    assert load_settings_stack(proj) == {}


def test_malformed_json_is_skipped_with_a_warning(tmp_path, monkeypatch, caplog) -> None:
    proj = _scoped(tmp_path, monkeypatch, user={"model": "user-model"})
    (proj / ".fluid").mkdir(parents=True, exist_ok=True)
    (proj / ".fluid" / "settings.json").write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        settings = load_settings_stack(proj)
    assert settings == {"model": "user-model"}  # the good scope still loads
    assert any("malformed" in r.getMessage() for r in caplog.records)


def test_non_object_json_is_skipped(tmp_path, monkeypatch) -> None:
    proj = _scoped(tmp_path, monkeypatch, user={"model": "user-model"})
    _write_settings(proj / ".fluid", "settings.json", {})
    (proj / ".fluid" / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_settings_stack(proj) == {"model": "user-model"}


# ─── permissions ─────────────────────────────────────────────────────────────


def test_permission_entry_grammar() -> None:
    bash = parse_permission_entry("Bash(npm run *)", "allow")
    assert bash.tool == "Bash" and bash.input_globs == {"command": "npm run *"}

    read = parse_permission_entry("Read(./src/**)", "deny")
    assert read.tool == "Read" and read.input_globs == {"file_path": "./src/**"}

    bare = parse_permission_entry("mcp__pencil", "allow")
    assert bare.tool == "mcp__pencil" and bare.input_globs == {}

    # An unknown tool's pattern cannot be mapped to an input field, so the rule
    # widens to the whole tool rather than being dropped.
    unknown = parse_permission_entry("WebFetch(domain:example.com)", "ask")
    assert unknown.tool == "WebFetch" and unknown.input_globs == {}

    assert parse_permission_entry("", "allow") is None


def test_permission_rules_emit_allow_then_ask_then_deny(tmp_path, monkeypatch) -> None:
    proj = _scoped(
        tmp_path, monkeypatch,
        project={
            "permissions": {
                "deny": ["Bash(rm *)"],
                "allow": ["Bash(git *)"],
                "ask": ["Bash(git push *)"],
            }
        },
    )
    rules = permission_rules(load_settings_stack(proj))
    assert [r.decision for r in rules] == ["allow", "ask", "deny"]


def test_deny_beats_ask_beats_allow_under_last_match_wins(tmp_path, monkeypatch) -> None:
    proj = _scoped(
        tmp_path, monkeypatch,
        project={
            "permissions": {
                "allow": ["Bash(*)"],
                "ask": ["Bash(git push *)"],
                "deny": ["Bash(rm *)"],
            }
        },
    )
    rules = permission_rules(load_settings_stack(proj))

    assert evaluate_rules(rules, "Bash", {"command": "git status"}) == "allow"
    assert evaluate_rules(rules, "Bash", {"command": "git push origin"}) == "ask"
    assert evaluate_rules(rules, "Bash", {"command": "rm -rf /"}) == "deny"
    assert evaluate_rules(rules, "Read", {"file_path": "x"}) is None  # untouched


def test_no_permissions_key_yields_no_rules() -> None:
    assert permission_rules({}) == []
    assert permission_rules({"permissions": "nonsense"}) == []


# ─── accessors ───────────────────────────────────────────────────────────────


def test_settings_env_accessor(tmp_path, monkeypatch) -> None:
    proj = _scoped(tmp_path, monkeypatch, project={"env": {"FOO": "bar", "N": 3}})
    assert settings_env(load_settings_stack(proj)) == {"FOO": "bar", "N": "3"}
    assert settings_env({}) == {}
    assert settings_env({"env": "nonsense"}) == {}


def test_enabled_plugins_accessor(tmp_path, monkeypatch) -> None:
    proj = _scoped(
        tmp_path, monkeypatch,
        user={"enabledPlugins": {"a@mp": True, "b@mp": True}},
        project={"enabledPlugins": {"b@mp": False}},
    )
    assert enabled_plugins(load_settings_stack(proj)) == {"a@mp": True, "b@mp": False}
    assert enabled_plugins({}) == {}


def test_default_permission_mode_maps_known_values(tmp_path, monkeypatch) -> None:
    for mode in ("default", "plan", "acceptEdits", "bypassPermissions"):
        proj = _scoped(
            tmp_path / mode, monkeypatch, project={"permissions": {"defaultMode": mode}}
        )
        assert default_permission_mode(load_settings_stack(proj)) == mode


def test_default_permission_mode_refuses_unknown_values(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert default_permission_mode({"permissions": {"defaultMode": "dontAsk"}}) is None
    assert any("defaultMode" in r.getMessage() for r in caplog.records)

    assert default_permission_mode({"permissions": {"defaultMode": "auto"}}) is None
    assert default_permission_mode({"permissions": {}}) is None
    assert default_permission_mode({"permissions": "nonsense"}) is None
    assert default_permission_mode({}) is None


def test_default_permission_mode_is_not_applied_to_a_session(tmp_path, monkeypatch) -> None:
    proj = _scoped(tmp_path, monkeypatch, project={"permissions": {"defaultMode": "plan"}})
    assert default_permission_mode(load_settings_stack(proj)) == "plan"

    loop = _loop(proj)
    assert loop.claude_settings == {}
    assert loop.permissions.approval_policy == "never_ask"
    assert loop.plan_mode is False


def test_mcp_toggles_accessor(tmp_path, monkeypatch) -> None:
    proj = _scoped(
        tmp_path, monkeypatch,
        user={"enabledMcpjsonServers": ["alpha"], "disabledMcpServers": ["beta"]},
        project={"enabledMcpjsonServers": ["gamma"]},
    )
    enabled, disabled = mcp_toggles(load_settings_stack(proj))
    assert enabled == ["alpha", "gamma"]
    assert disabled == ["beta"]
    assert mcp_toggles({}) == ([], [])


# ─── session seams (NativeLoop / Bash / MCP) ─────────────────────────────────


def _loop(cwd, *, rules=None, claude_settings=True):
    from fluid.engine.native import NativeLoop
    from fluid.permissions.engine import PermissionEngine
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolCtx

    class _Provider:
        name = "anthropic"
        caps = ProviderCaps()

        async def stream(self, request):  # pragma: no cover - never streamed
            if False:
                yield None

    return NativeLoop(
        provider=_Provider(),
        store=MemoryStore(),
        model="m",
        ctx=ToolCtx(cwd=str(cwd)),
        permissions=PermissionEngine("never_ask", rules=list(rules or [])),
        claude_settings=claude_settings,
    )


def test_native_loop_keeps_claude_settings_inert(tmp_path, monkeypatch) -> None:
    proj = _scoped(
        tmp_path, monkeypatch,
        project={
            "env": {"FLUID_ENV_PROBE": "from-settings"},
            "permissions": {"deny": ["Bash(rm *)"]},
        },
    )
    loop = _loop(proj)
    assert loop.claude_settings == {}
    assert "settings_env" not in loop.ctx.extra
    assert loop.permissions.rules == []


def test_native_loop_toggle_off_reads_nothing(tmp_path, monkeypatch) -> None:
    proj = _scoped(
        tmp_path, monkeypatch,
        project={"env": {"FLUID_ENV_PROBE": "x"}, "permissions": {"deny": ["Bash(rm *)"]}},
    )
    loop = _loop(proj, claude_settings=False)
    assert loop.claude_settings == {}
    assert "settings_env" not in loop.ctx.extra
    assert loop.permissions.rules == []


def test_host_rules_outrank_settings_rules(tmp_path, monkeypatch) -> None:
    """Settings rules go *before* host rules — last-match-wins leaves the host
    with the final say over its own session."""
    from fluid.permissions.rules import Rule

    proj = _scoped(tmp_path, monkeypatch, project={"permissions": {"deny": ["Bash(rm *)"]}})
    host_rule = Rule(tool="Bash", decision="allow", input_globs={"command": "rm *"})
    loop = _loop(proj, rules=[host_rule])

    assert loop.permissions.rules[-1] is host_rule
    assert loop.permissions.evaluate(_spec("Bash"), {"command": "rm -rf /"}) == "allow"


def _spec(name: str):
    from fluid.tools.registry import ToolSpec

    return ToolSpec(name=name, description="", input_schema={}, risk="write")


async def test_bash_builtin_does_not_inherit_claude_settings_env(tmp_path, monkeypatch) -> None:
    import sys

    from fluid.tools.builtins.bash import handler

    proj = _scoped(tmp_path, monkeypatch, project={"env": {"FLUID_ENV_PROBE": "from-settings"}})
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os; print(os.environ.get('FLUID_ENV_PROBE', 'MISSING'))", encoding="utf-8"
    )
    command = f'"{sys.executable}" "{probe}"'

    loop = _loop(proj)
    assert "MISSING" in (await handler({"command": command}, loop.ctx)).text
    assert "FLUID_ENV_PROBE" not in __import__("os").environ


def test_native_loop_has_no_implicit_provider_mcp_rewrite(tmp_path, monkeypatch) -> None:
    from fluid.engine.native import NativeLoop

    proj = _scoped(tmp_path, monkeypatch, project={"env": {"FLUID_ENV_PROBE": "1"}})
    loop = _loop(proj)
    assert loop._mcp_servers == {}
    assert not hasattr(NativeLoop, "_mcp_config")


def test_layered_env_never_mutates_os_environ() -> None:
    import os

    before = dict(os.environ)
    merged = layered_env({"FLUID_TEST_ONLY": "1"})
    assert merged["FLUID_TEST_ONLY"] == "1"
    assert "FLUID_TEST_ONLY" not in os.environ
    assert os.environ == before
    # An explicit base replaces the inherited environment.
    assert layered_env({"B": "2"}, base={"A": "1"}) == {"A": "1", "B": "2"}


# ─── setting_sources gating ──────────────────────────────────────────────────


def test_setting_sources_gate_scopes(tmp_path, monkeypatch) -> None:
    proj = _scoped(
        tmp_path, monkeypatch,
        user={"model": "user-model", "env": {"U": "1"}},
        project={"model": "project-model"},
        local={"model": "local-model"},
    )
    assert load_settings_stack(proj, sources=("user",))["model"] == "user-model"
    assert load_settings_stack(proj, sources=("user", "project"))["model"] == "project-model"
    assert load_settings_stack(proj, sources=("local",))["model"] == "local-model"
    assert load_settings_stack(proj, sources=()) == {}
    # Default: all three scopes, local wins.
    assert load_settings_stack(proj)["model"] == "local-model"
