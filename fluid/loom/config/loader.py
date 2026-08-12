"""Layered TOML config loader + the engine factory.

Precedence (lowest → highest, later layers deep-merge over earlier ones):

  1. built-in defaults (:data:`BUILTIN_DEFAULTS`)
  2. system   ``/etc/loom/config.toml``            (skipped if absent)
  3. user     ``~/.config/loom/config.toml``       (skipped if absent)
  4. project  ``./.loom/config.toml`` or ``./loom.toml``
  5. env      ``LOOM_*`` overrides
  6. explicit CLI/`overrides` argument

Parsing uses the stdlib :mod:`tomllib` (3.11+), so the core stays dependency-free.
``build_engine`` turns a resolved :class:`HarnessConfig` into a ready
:class:`~loom.engine.native.NativeLoop` (native providers) or
:class:`~loom.engine.delegated.DelegatedLoop` (``delegated_claude``), wired with a
builtins registry, a permission engine, and an optional code-mode session.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from loom.config.model import HarnessConfig

# ─── built-in defaults ───────────────────────────────────────────────────────

BUILTIN_DEFAULTS: dict[str, Any] = {
    "default_provider": "anthropic",
    "default_model": "claude-sonnet-5",
    "code_mode": False,
    "cwd": ".",
    "max_tokens": 8192,
    "system": None,
    "permission": {"approval_policy": "on_write", "rules": []},
    "instruction_files": {
        "enabled": True,
        "global": True,
        "project": True,
        "skills": True,
        "commands": True,
        "nested": True,
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
        "claude": {
            "kind": "delegated_claude",
            "model": "claude-sonnet-5",
        },
    },
}

SYSTEM_CONFIG = Path("/etc/loom/config.toml")


def _user_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "loom" / "config.toml"


def _project_config_path(cwd: Path) -> Path | None:
    for candidate in (cwd / ".loom" / "config.toml", cwd / "loom.toml"):
        if candidate.is_file():
            return candidate
    return None


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


def _coerce_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_overrides(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Translate the recognized ``LOOM_*`` env vars into a config overlay dict."""
    env = os.environ if env is None else env
    out: dict[str, Any] = {}
    if "LOOM_DEFAULT_PROVIDER" in env:
        out["default_provider"] = env["LOOM_DEFAULT_PROVIDER"]
    if "LOOM_DEFAULT_MODEL" in env:
        out["default_model"] = env["LOOM_DEFAULT_MODEL"]
    if "LOOM_CWD" in env:
        out["cwd"] = env["LOOM_CWD"]
    if "LOOM_CODE_MODE" in env:
        out["code_mode"] = _coerce_bool(env["LOOM_CODE_MODE"])
    if "LOOM_MAX_TOKENS" in env:
        try:
            out["max_tokens"] = int(env["LOOM_MAX_TOKENS"])
        except ValueError:
            pass
    if "LOOM_APPROVAL_POLICY" in env:
        out["permission"] = {"approval_policy": env["LOOM_APPROVAL_POLICY"]}
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
        user_path = _user_config_path()
        if user_path.is_file():
            merged = deep_merge(merged, _load_toml(user_path))
    project_path = _project_config_path(cwd_path)
    if project_path is not None:
        merged = deep_merge(merged, _load_toml(project_path))

    merged = deep_merge(merged, env_overrides(env))
    if overrides:
        merged = deep_merge(merged, overrides)

    return HarnessConfig.from_dict(merged)


# ─── engine factory ──────────────────────────────────────────────────────────


def _build_permissions(config: HarnessConfig):
    from loom.permissions.engine import PermissionEngine
    from loom.permissions.rules import Rule

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
        from loom.providers.anthropic.client import AnthropicClient

        kwargs: dict[str, Any] = {"api_key": api_key}
        if pc.base_url:
            kwargs["base_url"] = pc.base_url
        return AnthropicClient(**kwargs)
    if pc.kind == "openai_responses":
        from loom.providers.openai_responses.client import OpenAIResponsesClient

        kwargs = {"api_key": api_key}
        if pc.base_url:
            kwargs["base_url"] = pc.base_url
        return OpenAIResponsesClient(**kwargs)
    if pc.kind == "openrouter":
        from loom.providers.openrouter.client import OpenRouterClient

        kwargs = {"api_key": api_key}
        if pc.base_url:
            kwargs["base_url"] = pc.base_url
        return OpenRouterClient(**kwargs)
    raise ValueError(f"{pc.kind!r} is not a native provider kind")


def build_engine(
    config: HarnessConfig,
    store,
    *,
    provider_name: str | None = None,
    bus=None,
    session_id: str | None = None,
    system: str | None = None,
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
    from ``config`` when not supplied.
    """
    from loom.tools.registry import ToolCtx, ToolRegistry

    provider_name = provider_name or config.default_provider
    pc = config.provider(provider_name)
    model = pc.model or config.default_model

    if registry is None:
        registry = ToolRegistry()
        if with_builtins:
            from loom.tools.builtins import register_builtins

            register_builtins(registry)
    if ctx is None:
        ctx = ToolCtx(cwd=config.cwd, session_id=session_id)

    if pc.kind == "delegated_claude":
        from loom.engine.delegated import DelegatedLoop

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
        )

    # Native providers.
    from loom.engine.native import NativeLoop

    client = provider if provider is not None else _build_provider_client(config, provider_name)
    if permissions is None:
        permissions = _build_permissions(config)

    codemode = None
    if config.code_mode:
        from loom.codemode.session import CodeModeSession

        codemode = CodeModeSession(registry)

    return NativeLoop(
        provider=client,
        store=store,
        bus=bus,
        session_id=session_id,
        model=model,
        system=system if system is not None else config.system,
        max_tokens=config.max_tokens,
        registry=registry,
        permissions=permissions,
        hooks=hooks,
        ctx=ctx,
        plan_mode=plan_mode,
        codemode=codemode,
    )
