"""Layered TOML config loader + the engine factory.

Precedence (lowest → highest, later layers deep-merge over earlier ones):

  1. built-in defaults (:data:`BUILTIN_DEFAULTS`)
  2. settings  ``permissions.defaultMode`` from the settings.json stack, as an
               approval policy (so any file, env or flag below still wins)
  3. system    ``/etc/fluid/config.toml`` (``%ProgramData%/fluid/config.toml``
               on Windows), skipped if absent
  4. user      ``~/.fluid/mcp.json``, ``~/.fluid/hooks.json``, then ``~/.fluid/config.toml``
  5. project   for every directory from the git root down to the cwd, outer
               first: ``<dir>/.fluid/mcp.json``, ``<dir>/.fluid/hooks.json``,
               then ``<dir>/.fluid/config.toml`` or ``<dir>/fluid.toml``
  6. env       ``FLUID_*`` overrides
  7. explicit CLI/`overrides` argument

The TOML layer follows the JSON layers at each scope, and ``mcp.<name>`` is a
replace-level key, so a native ``[mcp.x]`` table wins a same-named imported
server definition outright. ``hooks`` is the one list that accumulates across
layers instead of being replaced: user hooks run first, project hooks after
them.

Parsing uses the stdlib :mod:`tomllib` (3.11+), so the core stays dependency-free.
``build_engine`` turns a resolved :class:`HarnessConfig` into a ready
:class:`~fluid.engine.native.NativeLoop` (native providers) or
:class:`~fluid.engine.delegated.DelegatedLoop` (``delegated_claude``), wired with a
builtins registry, a permission engine, and an optional code-mode session.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from fluid.config.model import HarnessConfig

logger = logging.getLogger(__name__)

# ─── built-in defaults ───────────────────────────────────────────────────────

BUILTIN_DEFAULTS: dict[str, Any] = {
    "default_provider": "anthropic",
    "default_model": "claude-sonnet-5",
    "code_mode": False,
    "skills_dir": None,
    "continual": False,
    "continual_model": None,
    # A native provider name whose client runs synthesis passes. Unset, the
    # session's own provider is used; the delegated ``claude`` leg has no
    # native client, so without this it journals but never synthesizes.
    "continual_provider": None,
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
        "trigger_window_ratio": 0.6,
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

def system_config_path(
    platform: str | None = None, env: dict[str, str] | None = None
) -> Path:
    """The machine-wide config file: ``/etc/fluid/config.toml``, or
    ``%ProgramData%\\fluid\\config.toml`` on Windows (where ``/etc`` would
    resolve to ``<drive>:\\etc``)."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if platform == "win32":
        base = env.get("ProgramData") or env.get("PROGRAMDATA") or r"C:\ProgramData"
        return Path(base) / "fluid" / "config.toml"
    return Path("/etc/fluid/config.toml")


SYSTEM_CONFIG = system_config_path()


def _user_config_path() -> Path:
    return Path.home() / ".fluid" / "config.toml"


def _user_mcp_path() -> Path:
    return Path.home() / ".fluid" / "mcp.json"


def project_config_paths(cwd: Path, name: str) -> list[Path]:
    """Every existing ``<dir>/.fluid/<name>`` for the project walk, outer first.

    The walk is the settings stack's (git root → ``cwd`` inside a checkout,
    the cwd alone outside one, the home directory excluded), so a session
    started in a subdirectory sees the same project ``mcp.json`` /
    ``hooks.json`` / ``config.toml`` as one started at the root. Later entries
    are nearer the cwd and merge over earlier ones.
    """
    from fluid.context.settings_stack import project_dirs

    return [p for d in project_dirs(cwd) if (p := d / ".fluid" / name).is_file()]


def _project_config_paths(cwd: Path) -> list[Path]:
    """Project TOML files, outer first: ``.fluid/config.toml`` per directory,
    or ``fluid.toml`` where the former is absent."""
    from fluid.context.settings_stack import project_dirs

    found: list[Path] = []
    for directory in project_dirs(cwd):
        for candidate in (directory / ".fluid" / "config.toml", directory / "fluid.toml"):
            if candidate.is_file():
                found.append(candidate)
                break
    return found


def _user_hooks_path() -> Path:
    return Path.home() / ".fluid" / "hooks.json"


# ─── merge + parse ───────────────────────────────────────────────────────────


# Top-level keys whose list values accumulate across layers instead of the
# higher layer replacing the lower one.
ADDITIVE_LIST_KEYS = frozenset({"hooks"})
# Top-level tables whose entries are replaced whole rather than merged key by
# key: ``[mcp.x]`` in a higher layer is a complete server definition, and
# merging it over an ``mcp.json`` entry of the same name would leave the
# lower layer's ``env``/``args`` behind.
REPLACE_ENTRY_KEYS = frozenset({"mcp"})


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto a copy of ``base`` (dicts merge,
    scalars/lists replace — except the top-level ``hooks`` list, which
    appends so project hooks follow user hooks, and the top-level ``mcp``
    table, whose per-server entries replace rather than merge)."""
    out = dict(base)
    for key, value in overlay.items():
        if (
            key in REPLACE_ENTRY_KEYS
            and isinstance(value, dict)
            and isinstance(out.get(key), dict)
        ):
            out[key] = {**out[key], **value}
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
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


def _load_mcp_json(
    path: Path,
    *,
    cwd: Path | str = ".",
    env: dict[str, str] | None = None,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Load Fluid's canonical MCP file as a config overlay.

    The file accepts the common ``{"mcpServers": {...}}`` envelope or a plain
    name-to-config mapping, in Claude Code's server shape. Each entry goes
    through the plugin path's ``substitute_vars`` (``${VAR}`` from ``env`` —
    the process environment overlaid with the settings stack's ``env`` — and
    ``${CLAUDE_PROJECT_DIR}``) and ``normalize_mcp_server`` (``type: "http"``
    → streamable HTTP; ``type: "sse"`` is skipped with a warning rather than
    dialed as the wrong transport). An entry marked ``disabled: true`` or
    ``enabled: false`` is left out. ``only`` restricts the file to those
    server names (the ``enabledMcpjsonServers`` gate for project files).
    """
    from fluid.context.plugins import normalize_mcp_server, substitute_vars

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
        if only is not None and name not in only:
            continue
        if value.get("disabled") is True or value.get("enabled") is False:
            continue
        expanded = substitute_vars(
            {k: v for k, v in value.items() if k not in ("disabled", "enabled")},
            plugin_root=None, project_dir=cwd, env=env,
        )
        config = normalize_mcp_server(name, expanded)
        if config is None:
            continue
        config.setdefault("transport", "stdio")
        normalized[name] = config
    return {"mcp": normalized}


def _load_hooks_json(path: Path) -> dict[str, Any]:
    """Load a Claude Code-shaped ``hooks.json`` as a ``{"hooks": [...]}`` overlay.

    Accepts ``{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type":
    "command", "command": "...", "timeout": 30}]}]}}`` (the envelope is
    optional) so a user can copy the ``hooks`` block straight out of Claude
    Code's settings.json. ``UserPromptSubmit`` maps to ``TurnStart`` and
    ``Stop`` to ``TurnEnd`` (``fluid.config.model.HOOK_EVENT_ALIASES``).
    Events fluid has no bus for, and non-command hook types, are skipped
    with a logged warning naming them. A malformed file is logged and
    ignored rather than fatal.
    """
    import logging

    from fluid.config.model import resolve_hook_event

    log = logging.getLogger(__name__)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("%s: hooks file ignored: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("%s: hooks file ignored: expected a JSON object", path)
        return {}
    groups = raw.get("hooks", raw)
    if not isinstance(groups, dict):
        log.warning("%s: hooks file ignored: 'hooks' is not an object", path)
        return {}
    entries: list[dict[str, Any]] = []
    for event_name, matchers in groups.items():
        event = resolve_hook_event(event_name)
        if event is None:
            log.warning("%s: hooks for %r dropped: fluid has no such event", path, event_name)
            continue
        if not isinstance(matchers, list):
            log.warning("%s: hooks for %r dropped: expected a list", path, event_name)
            continue
        for group in matchers:
            if not isinstance(group, dict):
                log.warning("%s: %s hook group dropped: not an object", path, event_name)
                continue
            matcher = group.get("matcher")
            for hook in group.get("hooks") or ():
                if not isinstance(hook, dict):
                    log.warning("%s: %s hook dropped: not an object", path, event_name)
                    continue
                if hook.get("type", "command") != "command":
                    log.warning(
                        "%s: %s hook of type %r dropped: only command hooks run",
                        path, event_name, hook.get("type"),
                    )
                    continue
                command = hook.get("command")
                if not isinstance(command, str) or not command.strip():
                    log.warning("%s: %s hook dropped: empty command", path, event_name)
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
    if "FLUID_CONTINUAL_PROVIDER" in env:
        out["continual_provider"] = env["FLUID_CONTINUAL_PROVIDER"]
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
    if "FLUID_CONTEXT_TRIGGER_WINDOW_RATIO" in env:
        try:
            context["trigger_window_ratio"] = float(
                env["FLUID_CONTEXT_TRIGGER_WINDOW_RATIO"]
            )
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
    system_path = system_config_path()
    system_toml = (
        _load_toml(system_path) if include_system and system_path.is_file() else {}
    )
    user_path = _user_config_path()
    user_toml = _load_toml(user_path) if include_user and user_path.is_file() else {}
    project_tomls = [_load_toml(p) for p in _project_config_paths(cwd_path)]
    env_layer = env_overrides(env)

    from fluid.config.model import _setting_sources_from

    raw_sources: Any = None
    for layer in (system_toml, user_toml, *project_tomls, env_layer, overrides or {}):
        if "setting_sources" in layer:
            raw_sources = layer["setting_sources"]
    sources = set(_setting_sources_from(raw_sources))
    if not include_user:
        sources.discard("user")

    # The settings.json stack feeds three things here: the defaultMode policy
    # layer (lowest, so every file/env/flag layer above still wins), the
    # ``${VAR}`` expansion environment for mcp.json, and the project-file
    # server gate.
    from fluid.context.settings_stack import (
        default_mode_policy,
        layered_env,
        load_settings_stack,
        mcp_toggles,
        settings_env,
    )

    settings = load_settings_stack(cwd_path, sources=tuple(sources))
    mode_policy, _mode_rules = default_mode_policy(settings)
    if mode_policy is not None:
        merged = deep_merge(merged, {"permission": {"approval_policy": mode_policy}})
    expansion_env = layered_env(settings_env(settings))
    enabled_json, _disabled = mcp_toggles(settings)
    project_gate = set(enabled_json) if enabled_json else None

    merged = deep_merge(merged, system_toml)
    if include_user:
        if "user" in sources:
            user_mcp_path = _user_mcp_path()
            if user_mcp_path.is_file():
                merged = deep_merge(
                    merged, _load_mcp_json(user_mcp_path, cwd=cwd_path, env=expansion_env)
                )
            user_hooks_path = _user_hooks_path()
            if user_hooks_path.is_file():
                merged = deep_merge(merged, _load_hooks_json(user_hooks_path))
        merged = deep_merge(merged, user_toml)
    if "project" in sources:
        for project_mcp_path in project_config_paths(cwd_path, "mcp.json"):
            merged = deep_merge(
                merged,
                _load_mcp_json(
                    project_mcp_path, cwd=cwd_path, env=expansion_env, only=project_gate
                ),
            )
        for project_hooks_path in project_config_paths(cwd_path, "hooks.json"):
            merged = deep_merge(merged, _load_hooks_json(project_hooks_path))
    for project_toml in project_tomls:
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
    from fluid.tools.hardening import (
        SENSITIVE_BASENAME_GLOBS,
        SENSITIVE_EXCEPTIONS,
        SENSITIVE_PATH_SUFFIXES,
    )

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
    asks = [
        Rule(tool=tool, decision="ask", input_globs={field: glob})
        for tool, field in field_by_tool.items()
        for glob in globs
    ]
    # Template files (.env.example and friends) hold no values and are meant
    # to be read. They match ``.env.*`` above, so an allow rule after the
    # asks lets them through; last match wins, and a user rule still comes
    # after both.
    allows = [
        Rule(tool=tool, decision="allow", input_globs={field: f"*{name}"})
        for tool, field in field_by_tool.items()
        for name in sorted(SENSITIVE_EXCEPTIONS)
    ]
    return [*asks, *allows]


def _session_settings(config: HarnessConfig) -> dict[str, Any]:
    """The merged settings.json stack for this session's cwd and sources."""
    from fluid.context.settings_stack import load_settings_stack

    return load_settings_stack(config.cwd, sources=config.setting_sources)


def _build_permissions(config: HarnessConfig, settings: dict[str, Any] | None = None):
    """The session's permission engine.

    Rule order, first to last (last match wins): the sensitive-file asks, the
    allow rules ``permissions.defaultMode = "acceptEdits"`` implies, the
    settings stack's ``permissions.{allow,ask,deny}``, then ``config.toml``
    ``[[permission.rules]]`` — so a host's TOML rule outranks a settings rule
    of the same shape, and a settings deny outranks the mode's allows.
    """
    from fluid.context.settings_stack import default_mode_policy, permission_rules
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

    if settings is None:
        settings = _session_settings(config)
    rules = []
    if (
        config.permission.protect_sensitive_files
        and config.permission.approval_policy != "never_ask"
    ):
        rules.extend(_sensitive_file_rules())
    _policy, mode_rules = default_mode_policy(settings)
    rules.extend(mode_rules)
    rules.extend(permission_rules(settings))
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


def _wire_continual(
    config: HarnessConfig,
    *,
    ctx: Any,
    bus: Any,
    session_id: str | None,
    model: str,
    client: Any | None,
    synthesizer: Any | None = None,
    agent: str | None = None,
) -> tuple[Any, Any | None]:
    """The skill store and, when ``client`` is given, its synthesizer.

    Wires the Recall builtin (``recall_store``) and, under ``continual``, the
    Remember builtin (``continual_journal``) onto ``ctx`` so both legs of the
    engine serve them. ``client`` is the ProviderClient synthesis passes run
    on; None keeps the journal without a synthesizer, which is what the
    delegated leg gets when no ``continual_provider`` is configured. A
    ``synthesizer`` handed in (a subagent host passing its parent's) is used
    as is, so one store has one learner. ``agent`` stamps the journal.
    """
    from fluid.codemode.skills import SkillStore, project_key_for

    skills = SkillStore(config.skills_dir)  # None → ~/.fluid/skills
    skills.journal_context(session_id=session_id, agent=agent)
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
    if not config.continual:
        return skills, None

    if synthesizer is None and client is not None:
        from fluid.codemode.continual import Synthesizer

        on_change: Any = None
        if bus is not None:
            from fluid.events.model import ContinualUpdate

            _bus, _sid = bus, session_id or ""
            learner: list[Any] = []

            async def _announce(changes: list[str]) -> None:
                # Learning that lands invisibly might as well not have
                # happened — announce what the pass changed, what it cost,
                # and which memories are waiting on the user.
                usage = dict(learner[0].last_pass_usage) if learner else {}
                held = tuple(learner[0].last_held) if learner else ()
                await _bus.publish(ContinualUpdate(
                    session_id=_sid, changes=tuple(changes), usage=usage, held=held,
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

        # continual_model pins synthesis to one model so skill quality does
        # not drift with the session's provider choice. Passes are background
        # and best-effort (see continual.py).
        synthesizer = Synthesizer(
            client, config.continual_model or model, skills,
            project=project_key_for(config.cwd),
            on_change=on_change,
            session_model=model,
            guidance_snapshot=guidance_snapshot or None,
        )
        if bus is not None:
            learner.append(synthesizer)

    def _remember(text: str, scope: str | None = None) -> None:
        # The Remember builtin's write path: journal a high-salience entry
        # and, when a synthesizer is running, pull its next pass forward.
        entry: dict[str, Any] = {"kind": "remember", "text": text}
        if scope:
            entry["scope"] = scope
        skills.journal_append(entry)
        if synthesizer is not None:
            synthesizer.mark_urgent()
            synthesizer.kick()

    ctx.extra.setdefault("continual_journal", _remember)
    return skills, synthesizer


def _mcp_server_map(
    config: HarnessConfig, disabled_mcp: Any, child_env: dict[str, str] | None,
) -> dict[str, Any]:
    """The MCP servers a session connects: config, project mcp.json, plugins.

    Plugin servers join the set with a config-declared server of the same
    name winning (gated like every other plugin component: the "user" source
    must be enabled and plugin discovery on). ``disabledMcpServers`` applies
    to the whole map; ``enabledMcpjsonServers`` was applied where the project
    mcp.json files were read (load_config). The settings ``env`` reaches each
    stdio server's spawn environment under the server's own ``env`` (the MCP
    SDK layers that over its default environment); HTTP servers have no
    child process.
    """
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
    for name in disabled_mcp:
        mcp_servers.pop(name, None)
    if child_env:
        for name, server in list(mcp_servers.items()):
            if server.get("transport", "stdio") != "stdio":
                continue
            own = server.get("env")
            mcp_servers[name] = {
                **server,
                "env": {**child_env, **(own if isinstance(own, dict) else {})},
            }
    return mcp_servers


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
    synthesizer: Any | None = None,
    agent: str | None = None,
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
    and none was passed). ``synthesizer`` shares an existing learner with
    this engine instead of building one (a subagent host passes its
    parent's); ``agent`` names the engine in the journal it writes.
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
    # The settings.json stack, read once for this build: permission rules,
    # the child-process ``env`` (Bash builtin, MCP stdio spawns) and the MCP
    # server toggles all come from it.
    from fluid.context.settings_stack import mcp_toggles, settings_env

    settings = _session_settings(config)
    child_env = settings_env(settings)
    if child_env:
        # Read by the Bash builtin; layered over the process environment for
        # the child only. Never written to os.environ.
        ctx.extra.setdefault("settings_env", child_env)
    _enabled_json, disabled_mcp = mcp_toggles(settings)
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

        # The delegated leg gates fluid's builtins at the bundled bridge, so
        # it needs the same permission engine and config hooks the native
        # loop gets. Without these the child CLI's own mode is the only policy.
        if config.hooks:
            from fluid.hooks.bus import HookBus
            from fluid.hooks.command import register_config_hooks

            if hooks is None:
                hooks = HookBus()
            register_config_hooks(hooks, config.hooks, cwd=config.cwd)
        if permissions is None:
            permissions = _build_permissions(config, settings)
        if bus is None:
            from fluid.events.bus import EventBus

            bus = EventBus()
        # Continual learning rides this leg too: the journal is fed from the
        # tool results the CLI reports, Recall and Remember reach the model
        # over the bundled bridge, and synthesis runs when a native provider
        # is configured for it (``continual_provider``); the CLI itself is not
        # a client the synthesizer can call.
        journal = None
        if config.continual:
            from fluid.codemode.journal import ToolJournal

            synth_client = None
            if config.continual_provider:
                synth_client = _build_provider_client(config, config.continual_provider)
            else:
                logger.warning(
                    "continual: the claude provider has no native client for "
                    "synthesis; set continual_provider to a native provider "
                    "or the journal is kept without synthesis passes"
                )
            skills, synthesizer = _wire_continual(
                config, ctx=ctx, bus=bus, session_id=session_id, model=model,
                client=synth_client, synthesizer=synthesizer, agent=agent,
            )
            journal = ToolJournal(skills, synthesizer)

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
            permissions=permissions,
            hooks=hooks,
            journal=journal,
            mcp_servers=_mcp_server_map(config, disabled_mcp, child_env) or None,
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
        permissions = _build_permissions(config, settings)

    codemode = None
    journal = None
    if config.code_mode or config.continual:
        synth_client = client
        if config.continual and config.continual_provider:
            synth_client = _build_provider_client(config, config.continual_provider)
        skills, synthesizer = _wire_continual(
            config, ctx=ctx, bus=bus, session_id=session_id, model=model,
            client=synth_client if config.continual else None,
            synthesizer=synthesizer, agent=agent,
        )
        if config.code_mode:
            from fluid.codemode.session import CodeModeSession

            codemode = CodeModeSession(registry, skills=skills, synthesizer=synthesizer)
        elif config.continual:
            # JSON-mode leg of the learning loop: same journal file, same
            # synthesizer, fed from the engine's turn/tool seams.
            from fluid.codemode.journal import ToolJournal

            journal = ToolJournal(skills, synthesizer)

    mcp_servers = _mcp_server_map(config, disabled_mcp, child_env)

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
            trigger_window_ratio=cc.trigger_window_ratio,
            keep_recent_tool_results=cc.keep_recent_tool_results,
            min_result_chars=cc.min_result_chars,
        ),
        trigger_tokens=cc.trigger_tokens,
        trigger_window_ratio=cc.trigger_window_ratio,
        headroom_tokens=cc.headroom_tokens,
        target_ratio=cc.target_ratio,
        keep_recent_turns=cc.keep_recent_turns,
        max_resident_span_digests=cc.max_resident_span_digests,
        epoch_fold_spans=cc.epoch_fold_spans,
        intake_stub_chars=cc.intake_stub_chars,
        tool_result_stub_chars=cc.tool_result_stub_chars,
        image_trigger_ratio=getattr(cc, "image_trigger_ratio", 1.0),
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
        cache_key=config.cache_key,
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
