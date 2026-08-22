"""Layered TOML config loader + the engine factory.

Precedence (lowest → highest, later layers deep-merge over earlier ones):

  1. built-in defaults (:data:`BUILTIN_DEFAULTS`)
  2. system   ``/etc/fluid/config.toml``            (skipped if absent)
  3. user     ``~/.fluid/mcp.json`` then ``~/.fluid/config.toml``
  4. project  ``./.fluid/mcp.json`` then ``./.fluid/config.toml`` or ``./fluid.toml``
  5. env      ``FLUID_*`` overrides
  6. explicit CLI/`overrides` argument

The TOML layer follows the MCP JSON layer at each scope, so a native ``[mcp]``
entry wins a same-named imported server definition.

Parsing uses the stdlib :mod:`tomllib` (3.11+), so the core stays dependency-free.
``build_engine`` turns a resolved :class:`HarnessConfig` into a ready
:class:`~fluid.engine.native.NativeLoop` (native providers) or
:class:`~fluid.engine.delegated.DelegatedLoop` (``delegated_claude``), wired with a
builtins registry, a permission engine, and an optional code-mode session.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from fluid.config.model import HarnessConfig

# ─── built-in defaults ───────────────────────────────────────────────────────

BUILTIN_DEFAULTS: dict[str, Any] = {
    "default_provider": "anthropic",
    "default_model": "claude-sonnet-5",
    "code_mode": False,
    "skills_dir": None,
    "continual": False,
    "continual_model": None,
    "cwd": ".",
    "max_tokens": 8192,
    "context_window": None,
    # CLI prompt. ``system`` is your own base prompt: when set it REPLACES the
    # shipped default (fluid.cli.prompt.DEFAULT_BASE_PROMPT). ``base_prompt``
    # turns the shipped default off without supplying a replacement.
    "system": None,
    "base_prompt": True,
    "permission": {"approval_policy": "on_write", "rules": []},
    "instruction_files": {
        "enabled": True,
        "global": True,
        "project": True,
        "skills": True,
        "commands": True,
        "nested": True,
    },
    # Session tool restriction (ruling CE12). ``allowed = None`` → every
    # registered tool; a list makes the surface exactly that set, and
    # ``disallowed`` subtracts from it. Names or fnmatch patterns.
    "tools": {
        "allowed": None,
        "disallowed": [],
    },
    # The mechanical eviction ladder (context/compaction.py): trigger,
    # headroom reserve, digest/epoch shape, intake stub size, tier-1 knobs.
    "context": {
        "enabled": True,
        "trigger_tokens": 120_000,
        "headroom_tokens": 16_000,
        "target_ratio": 0.7,
        "keep_recent_turns": 3,
        "max_resident_span_digests": 20,
        "epoch_fold_spans": 10,
        "intake_stub_chars": 25_000,
        "keep_recent_tool_results": 5,
        "min_result_chars": 2_000,
    },
    "providers": {
        "anthropic": {
            "kind": "anthropic",
            "model": "claude-sonnet-5",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
        "openai": {
            "kind": "openai_responses",
            "model": "gpt-5-codex",  # placeholder — Codex model ids are account/plan-specific; override in config
            "api_key_env": "OPENAI_API_KEY",
        },
        "openrouter": {
            "kind": "openrouter",
            "model": "openai/gpt-5-mini",
            "api_key_env": "OPENROUTER_API_KEY",
        },
        # Local OpenAI-compatible server (llama.cpp llama-server on its default
        # port; for Ollama set base_url to http://127.0.0.1:11434 and model to
        # an installed model name). llama-server ignores the model field; set
        # tool_style = "text" for models without reliable native tool calling.
        "local": {
            "kind": "local",
            "base_url": "http://127.0.0.1:8080",
        },
        "claude": {
            "kind": "delegated_claude",
            "model": "claude-sonnet-5",
        },
    },
}

SYSTEM_CONFIG = Path("/etc/fluid/config.toml")


def _user_config_path() -> Path:
    return Path.home() / ".fluid" / "config.toml"


def _user_mcp_path() -> Path:
    return Path.home() / ".fluid" / "mcp.json"


def _project_config_path(cwd: Path) -> Path | None:
    for candidate in (cwd / ".fluid" / "config.toml", cwd / "fluid.toml"):
        if candidate.is_file():
            return candidate
    return None


def _project_mcp_path(cwd: Path) -> Path:
    return cwd / ".fluid" / "mcp.json"


# ─── merge + parse ───────────────────────────────────────────────────────────


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto a copy of ``base`` (dicts merge,
    scalars/lists replace)."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _load_mcp_json(path: Path) -> dict[str, Any]:
    """Load Fluid's canonical MCP file as a config overlay.

    The file accepts the common ``{"mcpServers": {...}}`` envelope or a plain
    name-to-config mapping. Transport defaults are inferred only when unambiguous.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    servers = raw.get("mcpServers", raw)
    if not isinstance(servers, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for name, value in servers.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        config = dict(value)
        if "transport" not in config:
            if isinstance(config.get("command"), str):
                config["transport"] = "stdio"
            elif isinstance(config.get("url"), str):
                config["transport"] = "http"
        normalized[name] = config
    return {"mcp": normalized}


def _coerce_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_overrides(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Translate the recognized ``FLUID_*`` env vars into a config overlay dict."""
    env = os.environ if env is None else env
    out: dict[str, Any] = {}
    if "FLUID_DEFAULT_PROVIDER" in env:
        out["default_provider"] = env["FLUID_DEFAULT_PROVIDER"]
    if "FLUID_DEFAULT_MODEL" in env:
        out["default_model"] = env["FLUID_DEFAULT_MODEL"]
    if "FLUID_CWD" in env:
        out["cwd"] = env["FLUID_CWD"]
    if "FLUID_CODE_MODE" in env:
        out["code_mode"] = _coerce_bool(env["FLUID_CODE_MODE"])
    if "FLUID_BASE_PROMPT" in env:
        out["base_prompt"] = _coerce_bool(env["FLUID_BASE_PROMPT"])
    if "FLUID_SKILLS_DIR" in env:
        out["skills_dir"] = env["FLUID_SKILLS_DIR"]
    if "FLUID_CONTINUAL" in env:
        out["continual"] = _coerce_bool(env["FLUID_CONTINUAL"])
    if "FLUID_CONTINUAL_MODEL" in env:
        out["continual_model"] = env["FLUID_CONTINUAL_MODEL"]
    if "FLUID_MAX_TOKENS" in env:
        try:
            out["max_tokens"] = int(env["FLUID_MAX_TOKENS"])
        except ValueError:
            pass
    if "FLUID_CONTEXT_WINDOW" in env:
        try:
            out["context_window"] = int(env["FLUID_CONTEXT_WINDOW"])
        except ValueError:
            pass
    if "FLUID_APPROVAL_POLICY" in env:
        out["permission"] = {"approval_policy": env["FLUID_APPROVAL_POLICY"]}
    context: dict[str, Any] = {}
    if "FLUID_CONTEXT_ENABLED" in env:
        context["enabled"] = _coerce_bool(env["FLUID_CONTEXT_ENABLED"])
    for var, key in (
        ("FLUID_CONTEXT_TRIGGER_TOKENS", "trigger_tokens"),
        ("FLUID_CONTEXT_HEADROOM_TOKENS", "headroom_tokens"),
    ):
        if var in env:
            try:
                context[key] = int(env[var])
            except ValueError:
                pass
    if context:
        out["context"] = context
    return out


def load_config(
    cwd: str | os.PathLike[str] | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    env: dict[str, str] | None = None,
    include_system: bool = True,
    include_user: bool = True,
) -> HarnessConfig:
    """Merge every layer and materialize a :class:`HarnessConfig`.

    ``overrides`` is the highest-precedence layer — the CLI passes parsed flags
    here. ``cwd`` seeds project discovery and becomes the config's ``cwd`` unless
    a layer overrides it.
    """
    cwd_path = Path(cwd) if cwd is not None else Path.cwd()
    merged: dict[str, Any] = deep_merge(BUILTIN_DEFAULTS, {"cwd": str(cwd_path)})

    if include_system and SYSTEM_CONFIG.is_file():
        merged = deep_merge(merged, _load_toml(SYSTEM_CONFIG))
    if include_user:
        user_mcp_path = _user_mcp_path()
        if user_mcp_path.is_file():
            merged = deep_merge(merged, _load_mcp_json(user_mcp_path))
        user_path = _user_config_path()
        if user_path.is_file():
            merged = deep_merge(merged, _load_toml(user_path))
    project_mcp_path = _project_mcp_path(cwd_path)
    if project_mcp_path.is_file():
        merged = deep_merge(merged, _load_mcp_json(project_mcp_path))
    project_path = _project_config_path(cwd_path)
    if project_path is not None:
        merged = deep_merge(merged, _load_toml(project_path))

    merged = deep_merge(merged, env_overrides(env))
    if overrides:
        merged = deep_merge(merged, overrides)

    return HarnessConfig.from_dict(merged)


# ─── engine factory ──────────────────────────────────────────────────────────


def _build_permissions(config: HarnessConfig):
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

    rules = [
        Rule(tool=r.tool, decision=r.decision, input_globs=dict(r.input_globs))
        for r in config.permission.rules
    ]
    return PermissionEngine(config.permission.approval_policy, rules=rules)


def _build_provider_client(config: HarnessConfig, provider_name: str):
    """Instantiate the native ProviderClient for a config entry.

    Construction never touches the network (the clients only make requests when
    ``stream`` is iterated), so this is safe to call in offline tests.
    """
    pc = config.provider(provider_name)
    api_key = os.environ.get(pc.api_key_env) if pc.api_key_env else None

    if pc.kind == "anthropic":
        from fluid.providers.anthropic.client import AnthropicClient

        kwargs: dict[str, Any] = {"api_key": api_key}
        if pc.base_url:
            kwargs["base_url"] = pc.base_url
        client: Any = AnthropicClient(**kwargs)
    elif pc.kind == "openai_responses":
        from fluid.providers.openai_responses.client import OpenAIResponsesClient

        kwargs = {"api_key": api_key}
        if pc.base_url:
            kwargs["base_url"] = pc.base_url
        client = OpenAIResponsesClient(**kwargs)
    elif pc.kind == "openrouter":
        from fluid.providers.openrouter.client import OpenRouterClient

        kwargs = {"api_key": api_key}
        if pc.base_url:
            kwargs["base_url"] = pc.base_url
        client = OpenRouterClient(**kwargs)
    elif pc.kind == "local":
        from fluid.providers.local.client import LocalClient

        if not pc.base_url:
            raise ValueError(
                f"provider {provider_name!r} (kind 'local') requires base_url "
                "(e.g. http://127.0.0.1:8080 for llama-server, "
                "http://127.0.0.1:11434 for Ollama)"
            )
        client = LocalClient(api_key, base_url=pc.base_url, extra=pc.extra)
    else:
        raise ValueError(f"{pc.kind!r} is not a native provider kind")

    if pc.tool_style == "text":
        # Opt-in text-protocol tool calling for models without reliable native
        # tool calls — a decorating ProviderClient; the engine loop is unaware.
        from fluid.providers.local.text_tools import TextToolsAdapter

        client = TextToolsAdapter(client)
    return client


def build_engine(
    config: HarnessConfig,
    store,
    *,
    provider_name: str | None = None,
    bus=None,
    session_id: str | None = None,
    system: str | None = None,
    system_sections: Any | None = None,
    provider: Any | None = None,
    registry: Any | None = None,
    permissions: Any | None = None,
    hooks: Any | None = None,
    ctx: Any | None = None,
    plan_mode: bool = False,
    with_builtins: bool = True,
    claude_cmd: list[str] | None = None,
):
    """Build the engine for ``provider_name`` (default: ``config.default_provider``).

    Native kinds → :class:`NativeLoop`; ``delegated_claude`` → :class:`DelegatedLoop`.
    ``provider`` injects a ready ProviderClient (the test/offline hook), bypassing
    :func:`_build_provider_client`. ``registry``/``permissions``/``ctx`` are built
    from ``config`` when not supplied. ``system_sections`` is the
    :class:`~fluid.context.compiler.CompiledPrompt` ``system`` was compiled from,
    when the caller has it — it only feeds the context-usage breakdown's
    per-section attribution (the delegated leg ignores it: the CLI compiles, and
    reports on, its own prompt).
    """
    from fluid.tools.registry import ToolCtx, ToolRegistry

    provider_name = provider_name or config.default_provider
    pc = config.provider(provider_name)
    model = pc.model or config.default_model

    if registry is None:
        registry = ToolRegistry()
        if with_builtins:
            from fluid.tools.builtins import register_builtins

            register_builtins(registry)
    if ctx is None:
        ctx = ToolCtx(cwd=config.cwd, session_id=session_id)

    if pc.kind == "delegated_claude":
        from fluid.engine.delegated import DelegatedLoop

        return DelegatedLoop(
            store=store,
            model=model,
            bus=bus,
            session_id=session_id,
            system=system if system is not None else config.system,
            registry=registry,
            ctx=ctx,
            cwd=config.cwd,
            claude_cmd=claude_cmd,
            permission_mode="plan" if plan_mode else None,
            allowed_tools=(
                list(config.tools.allowed) if config.tools.allowed is not None else None
            ),
            disallowed_tools=list(config.tools.disallowed) or None,
        )

    # Native providers.
    from fluid.engine.native import NativeLoop

    client = provider if provider is not None else _build_provider_client(config, provider_name)
    if permissions is None:
        permissions = _build_permissions(config)

    codemode = None
    if config.code_mode:
        from fluid.codemode.session import CodeModeSession
        from fluid.codemode.skills import SkillStore

        skills = SkillStore(config.skills_dir)  # None → ~/.fluid/skills
        synthesizer = None
        if config.continual:
            from fluid.codemode.continual import Synthesizer

            # Session provider; continual_model pins synthesis to one model so
            # skill quality doesn't drift with the session's provider choice.
            # Passes are background and best-effort (see continual.py).
            synthesizer = Synthesizer(client, config.continual_model or model, skills)
        codemode = CodeModeSession(registry, skills=skills, synthesizer=synthesizer)

    from fluid.context.compaction import CompactionConfig
    from fluid.context.trim import TrimConfig

    cc = config.context
    compaction = CompactionConfig(
        enabled=cc.enabled,
        trim=TrimConfig(
            enabled=cc.enabled,
            trigger_tokens=cc.trigger_tokens,
            keep_recent_tool_results=cc.keep_recent_tool_results,
            min_result_chars=cc.min_result_chars,
        ),
        trigger_tokens=cc.trigger_tokens,
        headroom_tokens=cc.headroom_tokens,
        target_ratio=cc.target_ratio,
        keep_recent_turns=cc.keep_recent_turns,
        max_resident_span_digests=cc.max_resident_span_digests,
        epoch_fold_spans=cc.epoch_fold_spans,
        intake_stub_chars=cc.intake_stub_chars,
    )

    return NativeLoop(
        provider=client,
        store=store,
        bus=bus,
        session_id=session_id,
        model=model,
        system=system if system is not None else config.system,
        system_sections=system_sections,
        context_window=config.context_window,
        max_tokens=config.max_tokens,
        registry=registry,
        permissions=permissions,
        hooks=hooks,
        ctx=ctx,
        plan_mode=plan_mode,
        codemode=codemode,
        mcp_servers=config.mcp,
        allowed_tools=config.tools.allowed,
        disallowed_tools=config.tools.disallowed,
        compaction=compaction,
    )
