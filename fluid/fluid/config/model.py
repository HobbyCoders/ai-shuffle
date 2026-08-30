"""Layered-config data model — plain frozen dataclasses, no pydantic.

The loader (``fluid.config.loader``) merges TOML/env/CLI layers into a nested
``dict`` and then materializes it here via :meth:`HarnessConfig.from_dict`. Keeping
the on-disk shape as dicts until the very end is what makes deep-merge across
layers trivial; the dataclasses are the typed, validated result the CLI drives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Provider kinds the engine factory knows how to build.
PROVIDER_KINDS = ("anthropic", "openai_responses", "openrouter", "local", "delegated_claude")
APPROVAL_POLICIES = ("never_ask", "on_write", "always_ask")
# How the model calls tools: "native" = provider tool_calls (the default);
# "text" = the <tool_call> text protocol for local models that can't do
# reliable native tool calling (see fluid.providers.local.text_tools).
TOOL_STYLES = ("native", "text")
# Lifecycle events a ``[[hooks]]`` entry may subscribe to (fluid.hooks.bus).
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "TurnStart", "TurnEnd")


def _model_guidance_from(raw: Any) -> bool | str | tuple[str, ...]:
    """Normalize the ``model_guidance`` knob: auto | bool | substring list."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    value = str(raw or "auto").strip().lower()
    if value in ("true", "always", "on", "yes", "1"):
        return True
    if value in ("false", "never", "off", "no", "0"):
        return False
    return "auto"



def _optional_int(value: object) -> int | None:
    """``None``/absent stays uncapped; anything numeric becomes an int cap."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


@dataclass(frozen=True, kw_only=True)
class ProviderConfig:
    kind: str  # one of PROVIDER_KINDS
    model: str = ""  # empty → fall back to HarnessConfig.default_model
    base_url: str | None = None
    api_key_env: str | None = None
    tool_style: str = "native"  # one of TOOL_STYLES
    # Standing server-specific request-body knobs, merged into every request
    # (llama.cpp cache_prompt/samplers, Ollama options, …). Currently honoured
    # by the "local" kind.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProviderConfig:
        kind = d.get("kind", "anthropic")
        if kind not in PROVIDER_KINDS:
            raise ValueError(f"unknown provider kind: {kind!r} (expected one of {PROVIDER_KINDS})")
        tool_style = d.get("tool_style", "native")
        if tool_style not in TOOL_STYLES:
            raise ValueError(
                f"unknown tool_style: {tool_style!r} (expected one of {TOOL_STYLES})"
            )
        return cls(
            kind=kind,
            model=d.get("model", ""),
            base_url=d.get("base_url"),
            api_key_env=d.get("api_key_env"),
            tool_style=tool_style,
            extra=dict(d.get("extra") or {}),
        )


@dataclass(frozen=True, kw_only=True)
class RuleConfig:
    tool: str
    decision: str  # "allow" | "deny" | "ask"
    input_globs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RuleConfig:
        return cls(
            tool=d["tool"],
            decision=d.get("decision", "ask"),
            input_globs=dict(d.get("input_globs", {})),
        )


@dataclass(frozen=True, kw_only=True)
class PermissionConfig:
    approval_policy: str = "on_write"
    rules: tuple[RuleConfig, ...] = ()
    # Default ask-before-touching rules for credential files (.env, key files,
    # .netrc, …), prepended BEFORE user rules so a later user rule overrides
    # them (last match wins). Skipped entirely under never_ask — that policy
    # is an explicit opt-out of gates. See fluid.tools.hardening.
    protect_sensitive_files: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PermissionConfig:
        policy = d.get("approval_policy", "on_write")
        if policy not in APPROVAL_POLICIES:
            raise ValueError(
                f"invalid approval_policy: {policy!r} (expected one of {APPROVAL_POLICIES})"
            )
        rules = tuple(RuleConfig.from_dict(r) for r in d.get("rules", []))
        return cls(
            approval_policy=policy,
            rules=rules,
            protect_sensitive_files=bool(d.get("protect_sensitive_files", True)),
        )


@dataclass(frozen=True, kw_only=True)
class HookConfig:
    """One ``[[hooks]]`` entry: a shell command run on a lifecycle event.

    ``matcher`` narrows tool events to a tool name (fnmatch, or a regex when it
    contains ``^``, ``|`` or ``(``); it is ignored for turn events. ``timeout``
    is in seconds. See docs/hooks.md for the stdin payload and exit-code
    contract.
    """

    event: str  # one of HOOK_EVENTS
    command: str
    matcher: str | None = None
    timeout: float = 60.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HookConfig:
        event = d.get("event")
        if event not in HOOK_EVENTS:
            raise ValueError(f"unknown hook event: {event!r} (expected one of {HOOK_EVENTS})")
        command = d.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"hook for {event} needs a non-empty command")
        matcher = d.get("matcher")
        timeout = float(d.get("timeout", 60.0))
        if timeout <= 0:
            raise ValueError(f"hook timeout must be positive, got {timeout!r}")
        return cls(
            event=event,
            command=command,
            matcher=str(matcher) if matcher not in (None, "") else None,
            timeout=timeout,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "matcher": self.matcher,
            "command": self.command,
            "timeout": self.timeout,
        }


@dataclass(frozen=True, kw_only=True)
class InstructionFilesConfig:
    """Discovery toggles for the instruction stack (locked decision #5)."""

    enabled: bool = True
    discover_global: bool = True
    discover_project: bool = True
    discover_skills: bool = True
    discover_commands: bool = True
    discover_nested: bool = True
    discover_plugins: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> InstructionFilesConfig:
        # Accept both the terse toml keys (``global``/``project``/...) and the
        # explicit ``discover_*`` field names.
        def pick(*keys: str, default: bool) -> bool:
            for k in keys:
                if k in d:
                    return bool(d[k])
            return default

        return cls(
            enabled=pick("enabled", default=True),
            discover_global=pick("discover_global", "global", default=True),
            discover_project=pick("discover_project", "project", default=True),
            discover_skills=pick("discover_skills", "skills", default=True),
            discover_commands=pick("discover_commands", "commands", default=True),
            discover_nested=pick("discover_nested", "nested", default=True),
            discover_plugins=pick("discover_plugins", "plugins", default=True),
        )


@dataclass(frozen=True, kw_only=True)
class ToolsConfig:
    """Session tool restriction (ruling CE12), from ``[tools]``.

    ``allowed = None`` (the default, and what a missing key means) exposes every
    registered tool; a list — including an empty one — makes the surface exactly
    that set. ``disallowed`` subtracts afterwards, so deny beats allow. Entries
    are exact names or fnmatch patterns (``mcp__chrome-devtools__*``).
    """

    allowed: tuple[str, ...] | None = None
    disallowed: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolsConfig:
        allowed = d.get("allowed")
        return cls(
            allowed=tuple(allowed) if allowed is not None else None,
            disallowed=tuple(d.get("disallowed") or ()),
        )


@dataclass(frozen=True, kw_only=True)
class ContextConfig:
    """The mechanical eviction ladder + recall, from ``[context]``.

    Scalar mirror of :class:`fluid.context.compaction.CompactionConfig` (plus
    the tier-1 trim knobs) so the trigger and headroom are finally
    configurable — before this section, ``build_engine`` never passed a trim
    config and the 120k trigger was effectively hardcoded.
    """

    enabled: bool = True
    # Tier 1 only: trim old tool results, never digest a turn or fold an epoch.
    trim_only: bool = False
    trigger_tokens: int = 120_000
    # Floor/ratio pair: with a known context window the trigger becomes
    # max(trigger_tokens, window * trigger_window_ratio), capped by headroom.
    # 0.0 pins the old fixed trigger regardless of window size.
    trigger_window_ratio: float = 0.6
    headroom_tokens: int = 16_000
    target_ratio: float = 0.7
    keep_recent_turns: int = 3
    max_resident_span_digests: int = 20
    epoch_fold_spans: int = 10
    intake_stub_chars: int = 25_000
    tool_result_stub_chars: int = 50_000
    # Tier-1 (tool-result trim) knobs.
    keep_recent_tool_results: int = 5
    min_result_chars: int = 2_000

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextConfig:
        return cls(
            enabled=bool(d.get("enabled", True)),
            trim_only=bool(d.get("trim_only", False)),
            trigger_tokens=int(d.get("trigger_tokens", 120_000)),
            trigger_window_ratio=float(d.get("trigger_window_ratio", 0.6)),
            headroom_tokens=int(d.get("headroom_tokens", 16_000)),
            target_ratio=float(d.get("target_ratio", 0.7)),
            keep_recent_turns=int(d.get("keep_recent_turns", 3)),
            max_resident_span_digests=int(d.get("max_resident_span_digests", 20)),
            epoch_fold_spans=int(d.get("epoch_fold_spans", 10)),
            intake_stub_chars=int(d.get("intake_stub_chars", 25_000)),
            tool_result_stub_chars=int(d.get("tool_result_stub_chars", 50_000)),
            keep_recent_tool_results=int(d.get("keep_recent_tool_results", 5)),
            min_result_chars=int(d.get("min_result_chars", 2_000)),
        )


# The filesystem scopes context/settings discovery can draw from, mirroring the
# Agent SDK's ``setting_sources`` vocabulary. ``user`` is ~/.fluid, ``project``
# is the git-root→cwd walk's ``.fluid``/``.agents`` trees plus AGENTS-family
# files, ``local`` is ``.fluid/settings.local.json``.
SETTING_SOURCES = ("user", "project", "local")


def _setting_sources_from(raw: Any) -> tuple[str, ...]:
    """Normalize a ``setting_sources`` config value.

    Accepts a list or a comma-separated string. Unknown source names raise —
    a typo that silently loaded everything (or nothing) would be exactly the
    permission-widening surprise this knob exists to prevent. An explicit empty
    list means "load no filesystem sources".
    """
    if raw is None:
        return SETTING_SOURCES
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    cleaned = tuple(dict.fromkeys(s.strip() for s in (str(i) for i in items) if s.strip()))
    unknown = set(cleaned) - set(SETTING_SOURCES)
    if unknown:
        raise ValueError(
            f"unknown setting_sources value(s): {', '.join(sorted(unknown))} "
            f"(valid: {', '.join(SETTING_SOURCES)})"
        )
    return cleaned


@dataclass(frozen=True, kw_only=True)
class HarnessConfig:
    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-5"
    # Highest-precedence model choice (CLI --model / FLUID_MODEL). It beats a
    # provider's own ``model`` line, which ``default_model`` does not.
    model_override: str | None = None
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    code_mode: bool = False
    # Continual harness (code-mode only): persistent skills root (None → the
    # SkillStore default, ~/.fluid/skills) and the auto-synthesis toggle.
    # continual_model pins synthesis to one model regardless of which model
    # runs the session, so skill quality doesn't drift with provider choice.
    skills_dir: str | None = None
    continual: bool = False
    continual_model: str | None = None
    instruction_files: InstructionFilesConfig = field(default_factory=InstructionFilesConfig)
    # Which filesystem scopes feed context and settings discovery (instruction
    # files, skills, agents, commands, plugins, settings.json, mcp.json,
    # hooks.json). The Agent SDK's ``setting_sources`` vocabulary: any subset of
    # ``("user", "project", "local")``; empty = filesystem-isolated. config.toml
    # layers are NOT gated by this — they are harness configuration (and where
    # this very key can live), not imported context.
    setting_sources: tuple[str, ...] = SETTING_SOURCES
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    # Shell-command hooks, user layer first then project layer (the loader
    # concatenates ``hooks`` lists instead of replacing them).
    hooks: tuple[HookConfig, ...] = ()
    cwd: str = "."
    # None = uncapped: each request gets the model's own output maximum.
    max_tokens: int | None = None
    # Model calls allowed in one turn before it stops with ``max_calls``.
    # ``0`` means unlimited (normalized to None for NativeLoop), leaving the
    # provider as the sole authority on when a turn has to stop.
    max_model_calls: int = 200
    # Model context window, when the host app knows it (model ids come and go
    # faster than any table fluid could ship, so nothing is inferred). Feeds
    # maxTokens/rawMaxTokens/percentage in the context-usage breakdown; None
    # leaves those None rather than guessing a window.
    context_window: int | None = None
    # Your own base prompt. When set it replaces the CLI's shipped default
    # (fluid.cli.prompt.DEFAULT_BASE_PROMPT); base_prompt=False drops the
    # default without replacing it. Embedding hosts pass their own base to the
    # compiler directly and ignore both.
    system: str | None = None
    base_prompt: bool = True
    # The identity layer: durable voice/persona injected as the base prompt's
    # first block, above the operating contract (which always applies).
    # ``identity`` is inline text; when unset and ``identity_file`` is on,
    # ~/.fluid/IDENTITY.md fills the slot. Neither replaces the contract —
    # that's what ``system`` is for.
    identity: str | None = None
    identity_file: bool = True
    # Model-family guidance blocks ("auto" matches the shipped family tables;
    # True = always inject, False = never, a list of substrings = custom
    # match). Resolved once per session; see fluid.cli.prompt.
    model_guidance: bool | str | tuple[str, ...] = "auto"
    # MCP servers to connect at session start: name → transport config, e.g.
    # {"transport": "stdio", "command": "npx", "args": [...]} or
    # {"transport": "http", "url": "https://..."}. Kept as raw dicts — the
    # transport layer (McpClient) owns interpretation.
    mcp: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HarnessConfig:
        providers = {
            name: ProviderConfig.from_dict(pc)
            for name, pc in d.get("providers", {}).items()
        }
        return cls(
            default_provider=d.get("default_provider", "anthropic"),
            default_model=d.get("default_model", "claude-sonnet-5"),
            model_override=d.get("model_override") or None,
            providers=providers,
            permission=PermissionConfig.from_dict(d.get("permission", {})),
            code_mode=bool(d.get("code_mode", False)),
            skills_dir=d.get("skills_dir"),
            continual=bool(d.get("continual", False)),
            continual_model=d.get("continual_model"),
            instruction_files=InstructionFilesConfig.from_dict(d.get("instruction_files", {})),
            setting_sources=_setting_sources_from(d.get("setting_sources")),
            tools=ToolsConfig.from_dict(d.get("tools", {})),
            context=ContextConfig.from_dict(d.get("context", {})),
            hooks=tuple(HookConfig.from_dict(h) for h in d.get("hooks") or ()),
            cwd=d.get("cwd", "."),
            max_tokens=_optional_int(d.get("max_tokens")),
            max_model_calls=int(d.get("max_model_calls", 200)),
            context_window=(
                int(d["context_window"]) if d.get("context_window") else None
            ),
            system=d.get("system"),
            base_prompt=bool(d.get("base_prompt", True)),
            identity=d.get("identity"),
            identity_file=bool(d.get("identity_file", True)),
            model_guidance=_model_guidance_from(d.get("model_guidance", "auto")),
            mcp={name: dict(cfg) for name, cfg in d.get("mcp", {}).items()},
        )

    def provider(self, name: str | None = None) -> ProviderConfig:
        """Resolve a provider config by name (default: ``default_provider``)."""
        name = name or self.default_provider
        try:
            return self.providers[name]
        except KeyError:
            raise KeyError(
                f"no provider {name!r} configured (have: {sorted(self.providers)})"
            ) from None

    def model_for(self, name: str | None = None) -> str:
        if self.model_override:
            return self.model_override
        pc = self.provider(name)
        return pc.model or self.default_model

    def to_dict(self) -> dict[str, Any]:
        """Round-trippable plain-dict view (used by ``fluid config``)."""
        return {
            "default_provider": self.default_provider,
            "default_model": self.default_model,
            "model_override": self.model_override,
            "code_mode": self.code_mode,
            "skills_dir": self.skills_dir,
            "continual": self.continual,
            "continual_model": self.continual_model,
            "cwd": self.cwd,
            "max_tokens": self.max_tokens,
            "max_model_calls": self.max_model_calls,
            "context_window": self.context_window,
            "system": self.system,
            "base_prompt": self.base_prompt,
            "identity": self.identity,
            "identity_file": self.identity_file,
            "model_guidance": (
                list(self.model_guidance)
                if isinstance(self.model_guidance, tuple)
                else self.model_guidance
            ),
            "permission": {
                "approval_policy": self.permission.approval_policy,
                "protect_sensitive_files": self.permission.protect_sensitive_files,
                "rules": [
                    {"tool": r.tool, "decision": r.decision, "input_globs": dict(r.input_globs)}
                    for r in self.permission.rules
                ],
            },
            "instruction_files": {
                "enabled": self.instruction_files.enabled,
                "global": self.instruction_files.discover_global,
                "project": self.instruction_files.discover_project,
                "skills": self.instruction_files.discover_skills,
                "commands": self.instruction_files.discover_commands,
                "nested": self.instruction_files.discover_nested,
                "plugins": self.instruction_files.discover_plugins,
            },
            "setting_sources": list(self.setting_sources),
            "tools": {
                "allowed": list(self.tools.allowed) if self.tools.allowed is not None else None,
                "disallowed": list(self.tools.disallowed),
            },
            "context": {
                "enabled": self.context.enabled,
                "trim_only": self.context.trim_only,
                "trigger_tokens": self.context.trigger_tokens,
                "trigger_window_ratio": self.context.trigger_window_ratio,
                "headroom_tokens": self.context.headroom_tokens,
                "target_ratio": self.context.target_ratio,
                "keep_recent_turns": self.context.keep_recent_turns,
                "max_resident_span_digests": self.context.max_resident_span_digests,
                "epoch_fold_spans": self.context.epoch_fold_spans,
                "intake_stub_chars": self.context.intake_stub_chars,
                "tool_result_stub_chars": self.context.tool_result_stub_chars,
                "keep_recent_tool_results": self.context.keep_recent_tool_results,
                "min_result_chars": self.context.min_result_chars,
            },
            "hooks": [h.to_dict() for h in self.hooks],
            "providers": {
                name: {
                    "kind": pc.kind,
                    "model": pc.model,
                    "base_url": pc.base_url,
                    "api_key_env": pc.api_key_env,
                    "tool_style": pc.tool_style,
                    "extra": dict(pc.extra),
                }
                for name, pc in self.providers.items()
            },
            "mcp": {name: dict(cfg) for name, cfg in self.mcp.items()},
        }
