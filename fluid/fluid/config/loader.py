"""Layered TOML config loader + the engine factory.

Precedence (lowest → highest, later layers deep-merge over earlier ones):

  1. built-in defaults (:data:`BUILTIN_DEFAULTS`)
  2. system   ``/etc/fluid/config.toml``            (skipped if absent)
  3. user     ``~/.fluid/mcp.json``, ``~/.fluid/hooks.json``, then ``~/.fluid/config.toml``
  4. project  ``./.fluid/mcp.json``, ``./.fluid/hooks.json``, then
              ``./.fluid/config.toml`` or ``./fluid.toml``
  5. env      ``FLUID_*`` overrides
  6. explicit CLI/`overrides` argument

The TOML layer follows the JSON layers at each scope, so a native ``[mcp]``
entry wins a same-named imported server definition. ``hooks`` is the one list
that accumulates across layers instead of being replaced: user hooks run
first, project hooks after them.

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
    "anchor_budget": 1,
    "cwd": ".",
    "max_tokens": None,
    "max_model_calls": 200,
    "context_window": None,
    # CLI prompt. ``system`` is your own base prompt: when set it REPLACES the
    # shipped default (fluid.cli.prompt.DEFAULT_BASE_PROMPT). ``base_prompt``
    # turns the shipped default off without supplying a replacement.
    "system": None,
    "base_prompt": True,
    # Identity layer: inline persona text, or (when unset) ~/.fluid/IDENTITY.md
    # via ``identity_file``. Replaces only the base prompt's identity block;
    # the operating contract always applies.
    "identity": None,
    "identity_file": True,
    # Model-family guidance blocks: "auto" (shipped family tables) | true |
    # false | list of model-id substrings. See fluid.cli.prompt.
    "model_guidance": "auto",
    "permission": {
        "approval_policy": "on_write",
        "protect_sensitive_files": True,
        "rules": [],
    },
    "instruction_files": {
        "enabled": True,
        "global": True,
        "project": True,
        "skills": True,
        "commands": True,
        "nested": True,
        "plugins": True,
    },
    # Which filesystem scopes feed context/settings discovery (instructions,
    # skills, agents, commands, plugins, settings.json, mcp.json, hooks.json).
    # Agent SDK vocabulary: any subset of ["user", "project", "local"]; an
    # empty list runs filesystem-isolated. config.toml layers are NOT gated —
    # they are harness configuration and where this key itself can live.
    "setting_sources": ["user", "project", "local"],
    # Session tool restriction (ruling CE12). ``allowed = None`` → every
    # registered tool; a list makes the surface exactly that set, and
    # ``disallowed`` subtracts from it. Names or fnmatch patterns.
    "tools": {
        "allowed": None,
        "disallowed": [],
    },
    # Shell-command hooks ([[hooks]] entries; see docs/hooks.md). Lists from
    # every layer are concatenated, not replaced.
    "hooks": [],
    # The mechanical eviction ladder (context/compaction.py): trigger,
    # headroom reserve, digest/epoch shape, intake stub size, tier-1 knobs.
    "context": {
        "enabled": True,
        "trim_only": False,
        "trigger_tokens": 120_000,
        "headroom_tokens": 16_000,
        "target_ratio": 0.7,
        "keep_recent_turns": 3,
        "max_resident_span_digests": 20,
        "epoch_fold_spans": 10,
        "intake_stub_chars": 25_000,
        "tool_result_stub_chars": 50_000,
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


def _user_hooks_path() -> Path:
    return Path.home() / ".fluid" / "hooks.json"


def _project_hooks_path(cwd: Path) -> Path:
    return cwd / ".fluid" / "hooks.json"


# ─── merge + parse ───────────────────────────────────────────────────────────


# Top-level keys whose list values accumulate across layers instead of the
# higher layer replacing the lower one.
ADDITIVE_LIST_KEYS = frozenset({"hooks"})


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto a copy of ``base`` (dicts merge,
    scalars/lists replace — except the top-level ``hooks`` list, which
    appends so project hooks follow user hooks)."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif (
            key in ADDITIVE_LIST_KEYS
            and isinstance(value, list)
            and isinstance(out.get(key), list)
        ):
            out[key] = [*out[key], *value]
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


# Claude Code event names that fluid's bus spells differently.
_CLAUDE_HOOK_EVENT_ALIASES = {
    "UserPromptSubmit": "TurnStart",
    "Stop": "TurnEnd",
}


def _load_hooks_json(path: Path) -> dict[str, Any]:
    """Load a Claude Code-shaped ``hooks.json`` as a ``{"hooks": [...]}`` overlay.

    Accepts ``{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type":
    "command", "command": "...", "timeout": 30}]}]}}`` (the envelope is
    optional) so a user can copy the ``hooks`` block straight out of Claude
    Code's settings.json. ``UserPromptSubmit`` maps to ``TurnStart`` and
    ``Stop`` to ``TurnEnd``; events fluid has no bus for, and non-command hook
    types, are skipped. A malformed file is ignored rather than fatal.
    """
    from fluid.config.model import HOOK_EVENTS

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    groups = raw.get("hooks", raw)
    if not isinstance(groups, dict):
        return {}
    entries: list[dict[str, Any]] = []
    for event_name, matchers in groups.items():
        event = _CLAUDE_HOOK_EVENT_ALIASES.get(event_name, event_name)
        if event not in HOOK_EVENTS or not isinstance(matchers, list):
            continue
        for group in matchers:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            for hook in group.get("hooks") or ():
                if not isinstance(hook, dict):
                    continue
                if hook.get("type", "command") != "command":
                    continue
                command = hook.get("command")
                if not isinstance(command, str) or not command.strip():
                    continue
                entry: dict[str, Any] = {"event": event, "command": command}
                if isinstance(matcher, str) and matcher:
                    entry["matcher"] = matcher
                if isinstance(hook.get("timeout"), (int, float)):
                    entry["timeout"] = hook["timeout"]
                entries.append(entry)
    return {"hooks": entries}


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
    if "FLUID_MODEL" in env:
        out["model_override"] = env["FLUID_MODEL"]
    if "FLUID_CWD" in env:
        out["cwd"] = env["FLUID_CWD"]
    if "FLUID_CODE_MODE" in env:
        out["code_mode"] = _coerce_bool(env["FLUID_CODE_MODE"])
    if "FLUID_BASE_PROMPT" in env:
        out["base_prompt"] = _coerce_bool(env["FLUID_BASE_PROMPT"])
    if "FLUID_IDENTITY_FILE" in env:
        out["identity_file"] = _coerce_bool(env["FLUID_IDENTITY_FILE"])
    if "FLUID_MODEL_GUIDANCE" in env:
        raw = env["FLUID_MODEL_GUIDANCE"].strip()
        out["model_guidance"] = (
            [p.strip() for p in raw.split(",") if p.strip()] if "," in raw else raw
        )
    if "FLUID_SKILLS_DIR" in env:
        out["skills_dir"] = env["FLUID_SKILLS_DIR"]
    if "FLUID_SETTING_SOURCES" in env:
        # Comma list of user/project/local; an empty value means "none".
        out["setting_sources"] = [
            p.strip() for p in env["FLUID_SETTING_SOURCES"].split(",") if p.strip()
        ]
    if "FLUID_CONTINUAL" in env:
        out["continual"] = _coerce_bool(env["FLUID_CONTINUAL"])
    if "FLUID_CONTINUAL_MODEL" in env:
        out["continual_model"] = env["FLUID_CONTINUAL_MODEL"]
    if "FLUID_ANCHOR_BUDGET" in env:
        try:
            out["anchor_budget"] = int(env["FLUID_ANCHOR_BUDGET"])
        except ValueError:
            pass
    if "FLUID_MAX_TOKENS" in env:
        try:
            out["max_tokens"] = int(env["FLUID_MAX_TOKENS"])
        except ValueError:
            pass
    if "FLUID_MAX_CALLS" in env:
        try:
            out["max_model_calls"] = int(env["FLUID_MAX_CALLS"])
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

    # Parse the TOML layers up front: they are always applied (harness config
    # is never source-gated — it is where ``setting_sources`` itself can live),
    # and together with env + overrides they resolve which sources gate the
    # mcp.json / hooks.json context layers below.
    system_toml = (
        _load_toml(SYSTEM_CONFIG) if include_system and SYSTEM_CONFIG.is_file() else {}
    )
    user_path = _user_config_path()
    user_toml = _load_toml(user_path) if include_user and user_path.is_file() else {}
    project_path = _project_config_path(cwd_path)
    project_toml = _load_toml(project_path) if project_path is not None else {}
    env_layer = env_overrides(env)

    from fluid.config.model import _setting_sources_from

    raw_sources: Any = None
    for layer in (system_toml, user_toml, project_toml, env_layer, overrides or {}):
        if "setting_sources" in layer:
            raw_sources = layer["setting_sources"]
    sources = set(_setting_sources_from(raw_sources))

    merged = deep_merge(merged, system_toml)
    if include_user:
        if "user" in sources:
            user_mcp_path = _user_mcp_path()
            if user_mcp_path.is_file():
                merged = deep_merge(merged, _load_mcp_json(user_mcp_path))
            user_hooks_path = _user_hooks_path()
            if user_hooks_path.is_file():
                merged = deep_merge(merged, _load_hooks_json(user_hooks_path))
        merged = deep_merge(merged, user_toml)
    if "project" in sources:
        project_mcp_path = _project_mcp_path(cwd_path)
        if project_mcp_path.is_file():
            merged = deep_merge(merged, _load_mcp_json(project_mcp_path))
        project_hooks_path = _project_hooks_path(cwd_path)
        if project_hooks_path.is_file():
            merged = deep_merge(merged, _load_hooks_json(project_hooks_path))
    merged = deep_merge(merged, project_toml)

    merged = deep_merge(merged, env_layer)
    if overrides:
        merged = deep_merge(merged, overrides)

    return HarnessConfig.from_dict(merged)


# ─── engine factory ──────────────────────────────────────────────────────────


def _sensitive_file_rules():
    """Default ask-rules for credential files, per file-taking builtin.

    Prepended BEFORE user rules — last match wins, so one user rule allows a
    path through. Bash gets the same protection through its risk classifier
    (a read-only command targeting a credential file classifies as write).
    """
    from fluid.permissions.rules import Rule
    from fluid.tools.hardening import SENSITIVE_BASENAME_GLOBS, SENSITIVE_PATH_SUFFIXES

    # "*" prefixes make basename globs match directory-qualified inputs too
    # ("src/.env"); fnmatch "*" crosses "/" so this is a suffix match.
    globs = [
        pattern if pattern.startswith("*") else f"*{pattern}"
        for pattern in (*SENSITIVE_BASENAME_GLOBS, *SENSITIVE_PATH_SUFFIXES)
    ]
    field_by_tool = {
        "Read": "file_path", "Edit": "file_path", "Write": "file_path",
        "Grep": "path", "Glob": "path",
    }
    return [
        Rule(tool=tool, decision="ask", input_globs={field: glob})
        for tool, field in field_by_tool.items()
        for glob in globs
    ]


def _build_permissions(config: HarnessConfig):
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

    rules = []
    if (
        config.permission.protect_sensitive_files
        and config.permission.approval_policy != "never_ask"
    ):
        rules.extend(_sensitive_file_rules())
    rules.extend(
        Rule(tool=r.tool, decision=r.decision, input_globs=dict(r.input_globs))
        for r in config.permission.rules
    )
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
    allowed_tools: Any | None = None,
    disallowed_tools: Any | None = None,
):
    """Build the engine for ``provider_name`` (default: ``config.default_provider``).

    Native kinds → :class:`NativeLoop`; ``delegated_claude`` → :class:`DelegatedLoop`.
    ``provider`` injects a ready ProviderClient (the test/offline hook), bypassing
    :func:`_build_provider_client`. ``registry``/``permissions``/``ctx`` are built
    from ``config`` when not supplied. ``system_sections`` is the
    :class:`~fluid.context.compiler.CompiledPrompt` ``system`` was compiled from,
    when the caller has it — it only feeds the context-usage breakdown's
    per-section attribution (the delegated leg ignores it: the CLI compiles, and
    reports on, its own prompt). ``allowed_tools``/``disallowed_tools`` override
    ``config.tools`` — a host building a child engine with a narrower tool
    surface (an agent definition's ``tools:`` list) passes them here.
    ``hooks`` is the caller's own :class:`~fluid.hooks.bus.HookBus`; the
    config's ``[[hooks]]`` commands are registered onto it after whatever the
    caller already attached (a fresh bus is created when the config has hooks
    and none was passed).
    """
    from fluid.tools.registry import ToolCtx, ToolRegistry

    if allowed_tools is None:
        allowed_tools = config.tools.allowed
    if disallowed_tools is None:
        disallowed_tools = config.tools.disallowed

    provider_name = provider_name or config.default_provider
    pc = config.provider(provider_name)
    model = config.model_for(provider_name)  # honours --model / FLUID_MODEL

    if registry is None:
        registry = ToolRegistry()
        if with_builtins:
            from fluid.tools.builtins import (
                refine_builtin_descriptions,
                register_builtins,
            )

            register_builtins(registry)
            refine_builtin_descriptions(
                registry,
                allowed=allowed_tools,
                disallowed=disallowed_tools,
            )
    if ctx is None:
        ctx = ToolCtx(cwd=config.cwd, session_id=session_id)
    # Builtin wiring that rides the tool context: the recall builtins borrow
    # the store's own optional hooks when it has them (a store with no durable
    # record, like MemoryStore, simply leaves the tools unwired and invisible).
    # ``recent_sessions`` backs SessionSearch's no-query listing;
    # ``session_slice`` backs SessionView.
    if callable(getattr(store, "search", None)):
        ctx.extra.setdefault("session_search", store.search)
    if callable(getattr(store, "recent_sessions", None)):
        ctx.extra.setdefault("session_recent", store.recent_sessions)
    if callable(getattr(store, "session_slice", None)):
        ctx.extra.setdefault("session_view", store.session_slice)

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
                list(allowed_tools) if allowed_tools is not None else None
            ),
            disallowed_tools=list(disallowed_tools) or None,
        )

    # Native providers.
    from fluid.engine.native import NativeLoop

    # Materialize the bus here rather than letting NativeLoop default one:
    # loader-wired background emitters (the continual synthesizer's
    # ContinualUpdate) must publish to the SAME bus the engine streams from.
    if bus is None:
        from fluid.events.bus import EventBus

        bus = EventBus()

    if config.hooks:
        from fluid.hooks.bus import HookBus
        from fluid.hooks.command import register_config_hooks

        if hooks is None:
            hooks = HookBus()
        register_config_hooks(hooks, config.hooks, cwd=config.cwd)

    client = provider if provider is not None else _build_provider_client(config, provider_name)
    if permissions is None:
        permissions = _build_permissions(config)

    codemode = None
    journal = None
    if config.code_mode or config.continual:
        from fluid.codemode.skills import SkillStore, project_key_for

        skills = SkillStore(config.skills_dir)  # None → ~/.fluid/skills
        try:
            # Opportunistic lifecycle pass (interval-gated, deterministic,
            # archive-not-delete). Best-effort: curation must never block an
            # engine build.
            skills.curate()
        except Exception:
            pass
        # The Recall builtin searches this store (live + archived memories,
        # saved skills); wiring the handle makes the tool visible.
        ctx.extra.setdefault("recall_store", skills)
        synthesizer = None
        if config.continual:
            from fluid.codemode.continual import Synthesizer

            on_change: Any = None
            if bus is not None:
                from fluid.events.model import ContinualUpdate

                _bus, _sid = bus, session_id or ""

                async def _announce(changes: list[str]) -> None:
                    # Learning that lands invisibly might as well not have
                    # happened — announce what the pass changed.
                    await _bus.publish(ContinualUpdate(
                        session_id=_sid, changes=tuple(changes),
                    ))

                on_change = _announce

            # Phase 2.3: the synthesizer sees the SESSION model id (so a
            # guidance-proposal stem can be validated against it) and the raw
            # guidance file(s) currently matching it (so proposals amend the
            # file instead of overwriting blind). Gated on the "user" setting
            # source like the prompt-side guidance load.
            guidance_snapshot: dict[str, str] = {}
            if "user" in config.setting_sources:
                try:
                    from fluid.context.guidance import guidance_sources

                    guidance_snapshot = guidance_sources(model)
                except Exception:
                    pass

            # Session provider; continual_model pins synthesis to one model so
            # skill quality doesn't drift with the session's provider choice.
            # Passes are background and best-effort (see continual.py).
            synthesizer = Synthesizer(
                client, config.continual_model or model, skills,
                project=project_key_for(config.cwd),
                on_change=on_change,
                session_model=model,
                guidance_snapshot=guidance_snapshot or None,
            )

            def _remember(text: str, scope: str | None = None) -> None:
                # The Remember builtin's write path: journal a high-salience
                # entry and pull the next synthesis pass forward.
                entry: dict[str, Any] = {"kind": "remember", "text": text}
                if scope:
                    entry["scope"] = scope
                skills.journal_append(entry)
                synthesizer.mark_urgent()
                synthesizer.kick()

            ctx.extra.setdefault("continual_journal", _remember)
        if config.code_mode:
            from fluid.codemode.session import CodeModeSession

            codemode = CodeModeSession(registry, skills=skills, synthesizer=synthesizer)
        elif config.continual:
            # JSON-mode leg of the learning loop: same journal file, same
            # synthesizer, fed from the engine's turn/tool seams.
            from fluid.codemode.journal import ToolJournal

            journal = ToolJournal(skills, synthesizer)

    # Plugin MCP servers join the lazy-connect set; a config-declared server of
    # the same name always wins. Gated like every other plugin component: the
    # "user" source must be enabled and plugin discovery on.
    mcp_servers = dict(config.mcp)
    if "user" in config.setting_sources and config.instruction_files.discover_plugins:
        from fluid.context.plugins import discover_plugins, plugin_mcp_servers

        try:
            for name, server in plugin_mcp_servers(
                discover_plugins(config.cwd, sources=config.setting_sources)
            ).items():
                mcp_servers.setdefault(name, server)
        except Exception:  # noqa: BLE001 — CE11: a broken plugin never blocks the build
            pass

    from fluid.context.compaction import CompactionConfig
    from fluid.context.trim import TrimConfig

    cc = config.context
    # ``trim_only`` splits the ladder at tier 1: results are trimmed to
    # recoverable placeholders, but no turn is digested and no epoch folded.
    # Inert when the whole section is disabled.
    trim_only = cc.enabled and getattr(cc, "trim_only", False)
    compaction = CompactionConfig(
        enabled=cc.enabled and not trim_only,
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
        tool_result_stub_chars=cc.tool_result_stub_chars,
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
        # 0 means unlimited; NativeLoop spells that None.
        max_model_calls_per_turn=config.max_model_calls or None,
        registry=registry,
        permissions=permissions,
        hooks=hooks,
        ctx=ctx,
        plan_mode=plan_mode,
        codemode=codemode,
        journal=journal,
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        compaction=compaction,
        setting_sources=config.setting_sources,
    )
