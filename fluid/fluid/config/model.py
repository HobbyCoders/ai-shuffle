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
PROVIDER_KINDS = ("anthropic", "openai_responses", "openrouter", "delegated_claude")
APPROVAL_POLICIES = ("never_ask", "on_write", "always_ask")


@dataclass(frozen=True, kw_only=True)
class ProviderConfig:
    kind: str  # one of PROVIDER_KINDS
    model: str = ""  # empty → fall back to HarnessConfig.default_model
    base_url: str | None = None
    api_key_env: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProviderConfig":
        kind = d.get("kind", "anthropic")
        if kind not in PROVIDER_KINDS:
            raise ValueError(f"unknown provider kind: {kind!r} (expected one of {PROVIDER_KINDS})")
        return cls(
            kind=kind,
            model=d.get("model", ""),
            base_url=d.get("base_url"),
            api_key_env=d.get("api_key_env"),
        )


@dataclass(frozen=True, kw_only=True)
class RuleConfig:
    tool: str
    decision: str  # "allow" | "deny" | "ask"
    input_globs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuleConfig":
        return cls(
            tool=d["tool"],
            decision=d.get("decision", "ask"),
            input_globs=dict(d.get("input_globs", {})),
        )


@dataclass(frozen=True, kw_only=True)
class PermissionConfig:
    approval_policy: str = "on_write"
    rules: tuple[RuleConfig, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PermissionConfig":
        policy = d.get("approval_policy", "on_write")
        if policy not in APPROVAL_POLICIES:
            raise ValueError(
                f"invalid approval_policy: {policy!r} (expected one of {APPROVAL_POLICIES})"
            )
        rules = tuple(RuleConfig.from_dict(r) for r in d.get("rules", []))
        return cls(approval_policy=policy, rules=rules)


@dataclass(frozen=True, kw_only=True)
class InstructionFilesConfig:
    """Discovery toggles for the instruction stack (locked decision #5)."""

    enabled: bool = True
    discover_global: bool = True
    discover_project: bool = True
    discover_skills: bool = True
    discover_commands: bool = True
    discover_nested: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InstructionFilesConfig":
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
        )


@dataclass(frozen=True, kw_only=True)
class ClaudeEcosystemConfig:
    """Opt-out switches for Claude Code ecosystem pickup (ruling CE9).

    Discovery is filesystem-native and on by default (CE1) — a host app can
    only *disable* a piece, never has to wire one up. ``settings`` reads the
    settings.json stack (CE4), ``plugins`` loads enabled plugins' components
    (CE5), ``agents`` discovers ``.claude/agents`` definitions (CE6).
    """

    settings: bool = True
    plugins: bool = True
    agents: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClaudeEcosystemConfig":
        def pick(key: str, *, default: bool) -> bool:
            return bool(d[key]) if key in d else default

        return cls(
            settings=pick("settings", default=True),
            plugins=pick("plugins", default=True),
            agents=pick("agents", default=True),
        )


@dataclass(frozen=True, kw_only=True)
class HarnessConfig:
    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-5"
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
    claude_ecosystem: ClaudeEcosystemConfig = field(default_factory=ClaudeEcosystemConfig)
    cwd: str = "."
    max_tokens: int = 8192
    # Model context window, when the host app knows it (model ids come and go
    # faster than any table fluid could ship, so nothing is inferred). Feeds
    # maxTokens/rawMaxTokens/percentage in the context-usage breakdown; None
    # leaves those None rather than guessing a window.
    context_window: int | None = None
    system: str | None = None
    # MCP servers to connect at session start: name → transport config, e.g.
    # {"transport": "stdio", "command": "npx", "args": [...]} or
    # {"transport": "http", "url": "https://..."}. Kept as raw dicts — the
    # transport layer (McpClient) owns interpretation.
    mcp: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HarnessConfig":
        providers = {
            name: ProviderConfig.from_dict(pc)
            for name, pc in d.get("providers", {}).items()
        }
        return cls(
            default_provider=d.get("default_provider", "anthropic"),
            default_model=d.get("default_model", "claude-sonnet-5"),
            providers=providers,
            permission=PermissionConfig.from_dict(d.get("permission", {})),
            code_mode=bool(d.get("code_mode", False)),
            skills_dir=d.get("skills_dir"),
            continual=bool(d.get("continual", False)),
            continual_model=d.get("continual_model"),
            instruction_files=InstructionFilesConfig.from_dict(d.get("instruction_files", {})),
            claude_ecosystem=ClaudeEcosystemConfig.from_dict(d.get("claude_ecosystem", {})),
            cwd=d.get("cwd", "."),
            max_tokens=int(d.get("max_tokens", 8192)),
            context_window=(
                int(d["context_window"]) if d.get("context_window") else None
            ),
            system=d.get("system"),
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
        pc = self.provider(name)
        return pc.model or self.default_model

    def to_dict(self) -> dict[str, Any]:
        """Round-trippable plain-dict view (used by ``fluid config``)."""
        return {
            "default_provider": self.default_provider,
            "default_model": self.default_model,
            "code_mode": self.code_mode,
            "skills_dir": self.skills_dir,
            "continual": self.continual,
            "continual_model": self.continual_model,
            "cwd": self.cwd,
            "max_tokens": self.max_tokens,
            "context_window": self.context_window,
            "system": self.system,
            "permission": {
                "approval_policy": self.permission.approval_policy,
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
            },
            "claude_ecosystem": {
                "settings": self.claude_ecosystem.settings,
                "plugins": self.claude_ecosystem.plugins,
                "agents": self.claude_ecosystem.agents,
            },
            "providers": {
                name: {
                    "kind": pc.kind,
                    "model": pc.model,
                    "base_url": pc.base_url,
                    "api_key_env": pc.api_key_env,
                }
                for name, pc in self.providers.items()
            },
            "mcp": {name: dict(cfg) for name, cfg in self.mcp.items()},
        }
