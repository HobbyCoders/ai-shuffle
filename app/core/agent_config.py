"""
Agent and subagent configuration for Claude queries.

Contains functions for writing/cleaning up subagent definition files
and resolving note references in prompts.
"""

import logging
import re
from pathlib import Path
from typing import Optional, Dict, Collection

from claude_agent_sdk import AgentDefinition

from app.db import database

logger = logging.getLogger(__name__)


def resolve_subagent_frontmatter_model(
    raw: Optional[str],
    *,
    gateway_routed: bool,
    honored_leg_models: Collection[str],
    session_default: Optional[str],
    main_serves_claude_ids: bool,
) -> Optional[str]:
    """Resolve the gateway-routable frontmatter ``model:`` value for a subagent.

    Returns the value to stamp into the agent's ``.claude/agents/<id>.md``
    frontmatter, or ``None`` to omit the ``model:`` line entirely (the agent then
    inherits the main model). The returned value is always a bare model id that
    the translator gateway can route by exact match — never a ``prov:...``
    composite.

    See ``docs/SUBAGENT_MODEL_CONFIG_PLAN.md`` — "DECISION POINT 3 — RESOLVED
    2026-07-20: FRONTMATTER-STAMPING" and the "Per-agent frontmatter resolver"
    table. On a not-honored gateway leg (or unpinned / foreign-Claude) this
    stamps ``session_default`` rather than omitting, so every enabled subagent on
    a gateway session ends up with an explicit, gateway-routable ``model:``.

    ``raw`` is the stored subagent model: a Claude alias/id (e.g. ``sonnet``,
    ``claude-opus-4-8``), a provider composite ``prov:<providerId>:<modelId>``
    (split on the FIRST colon after the ``prov:`` prefix — model ids may contain
    colons/slashes), or ``None`` (unpinned).

    Native sessions (``gateway_routed is False``) preserve today's behavior and
    ignore the gateway-only parameters: composites are omitted (inherit main),
    Claude pins are kept, unpinned inherits main.

    Called at subagent assembly; the spawn-side wiring that computes
    ``session_default`` / ``honored_leg_models`` / ``main_serves_claude_ids``
    lands in S3 — this function is added now and consumed next chunk.
    """
    # Parse `raw` into (is_composite, provider_id, model_id).
    is_composite = bool(raw) and raw.startswith("prov:")
    model_id: Optional[str] = None
    malformed_composite = False
    if is_composite and raw is not None:  # `raw is not None` narrows for the type checker
        remainder = raw[len("prov:"):]
        # Only the model id matters here; the provider id is parsed by the S3
        # leg-map assembly, a separate spawn-side path.
        _provider_id, sep, model_id = remainder.partition(":")
        # Malformed if there is no second colon (no model id) or model id empty.
        if not sep or not model_id:
            malformed_composite = True

    if not gateway_routed:
        # Native session: preserve today's verbatim behavior.
        if is_composite:
            # Composite (well-formed or malformed) → omit → inherit main.
            return None
        # Claude alias/id → keep as-is; None (unpinned) → None.
        return raw

    # Gateway session.
    if is_composite:
        if malformed_composite:
            # Cannot route a composite with no model id.
            return session_default
        if model_id in honored_leg_models:
            return model_id
        return session_default

    if raw:
        # Claude alias/id.
        if main_serves_claude_ids:
            return raw
        return session_default

    # None (unpinned).
    return session_default


def write_agents_to_filesystem(agents_dict: Dict[str, AgentDefinition], cwd: str) -> Optional[Path]:
    """
    Write subagent definitions to .claude/agents/ directory in the working directory.

    SUPPLEMENTAL DISCOVERY: Writes agent .md files so the CLI can discover agent
    names and descriptions for its internal routing. Tool restrictions are NOT
    enforced by filesystem agent files — only the programmatic AgentDefinition.tools
    (passed via options.agents) restricts tool access at runtime.

    On all platforms, agents are also passed programmatically via options.agents
    for tool enforcement. This filesystem write is supplemental for Windows local mode.

    Args:
        agents_dict: Dict mapping agent IDs to AgentDefinition objects
        cwd: Working directory where .claude/agents/ will be created

    Returns:
        Path to the agents directory if created, None if no agents
    """
    if not agents_dict:
        return None

    # Create .claude/agents directory in the working directory
    agents_dir = Path(cwd) / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    written_files = []

    for agent_id, agent_def in agents_dict.items():
        # Build YAML frontmatter with proper escaping
        def _yaml_quote(s: str) -> str:
            """Quote a string if it contains YAML special characters."""
            if not s:
                return '""'
            # Characters that require quoting in YAML values
            needs_quoting = any(c in s for c in ':{}[]|>&*!#%@`\'"?,\n\\')
            if needs_quoting or s.strip() != s:
                # Use double quotes with escaped internal double quotes
                escaped = s.replace('\\', '\\\\').replace('"', '\\"')
                return f'"{escaped}"'
            return s

        frontmatter_lines = [
            "---",
            f"name: {_yaml_quote(agent_id)}",
            f"description: {_yaml_quote(agent_def.description)}",
        ]

        # Add tools if specified
        if agent_def.tools:
            tools_str = ", ".join(agent_def.tools)
            frontmatter_lines.append(f"tools: {_yaml_quote(tools_str)}")

        # Add model if specified
        if agent_def.model:
            frontmatter_lines.append(f"model: {_yaml_quote(agent_def.model)}")

        # Add skills if specified (SDK 0.1.49+)
        if agent_def.skills:
            skills_str = ", ".join(agent_def.skills)
            frontmatter_lines.append(f"skills: {_yaml_quote(skills_str)}")

        # Add memory if specified (SDK 0.1.49+)
        if agent_def.memory:
            frontmatter_lines.append(f"memory: {_yaml_quote(agent_def.memory)}")

        # Add mcpServers if specified (SDK 0.1.49+)
        if agent_def.mcpServers:
            # mcpServers can be a list of strings or dicts — serialize as YAML list
            mcp_items = []
            for s in agent_def.mcpServers:
                if isinstance(s, str):
                    mcp_items.append(s)
                else:
                    # Complex config — skip for filesystem workaround (SDK handles these via --agents)
                    pass
            if mcp_items:
                mcp_str = ", ".join(mcp_items)
                frontmatter_lines.append(f"mcpServers: {_yaml_quote(mcp_str)}")

        # New AgentDefinition fields (SDK 0.1.51+)
        if getattr(agent_def, 'disallowedTools', None):
            dt_str = ", ".join(agent_def.disallowedTools)
            frontmatter_lines.append(f"disallowedTools: {_yaml_quote(dt_str)}")
        if getattr(agent_def, 'maxTurns', None) is not None:
            frontmatter_lines.append(f"maxTurns: {agent_def.maxTurns}")
        if getattr(agent_def, 'initialPrompt', None):
            frontmatter_lines.append(f"initialPrompt: {_yaml_quote(agent_def.initialPrompt)}")

        frontmatter_lines.append("---")
        frontmatter_lines.append("")  # Empty line after frontmatter

        # Build full file content
        content = "\n".join(frontmatter_lines) + agent_def.prompt

        # Write to file
        agent_file = agents_dir / f"{agent_id}.md"
        agent_file.write_text(content, encoding="utf-8")
        written_files.append(agent_file)
        logger.debug(f"Wrote subagent file: {agent_file}")

    logger.debug(f"Wrote {len(written_files)} subagent files to {agents_dir}")
    return agents_dir


def cleanup_agents_directory(agents_dir: Path, agent_ids: list) -> None:
    """
    Clean up subagent files that were written for this session.

    Only removes files that match the agent IDs we wrote, to avoid
    removing user-created agents.

    Args:
        agents_dir: Path to .claude/agents/ directory
        agent_ids: List of agent IDs to remove
    """
    if not agents_dir or not agents_dir.exists():
        return

    for agent_id in agent_ids:
        agent_file = agents_dir / f"{agent_id}.md"
        if agent_file.exists():
            try:
                agent_file.unlink()
                logger.debug(f"Removed subagent file: {agent_file}")
            except Exception as e:
                logger.warning(f"Failed to remove subagent file {agent_file}: {e}")


def resolve_note_references(prompt: str) -> str:
    """Resolve legacy @Notes/note-id references in a prompt.

    New notes use physical file paths that the SDK reads natively.
    This handles legacy virtual references from older messages by
    redirecting to the physical file or inlining the content.
    """
    def replace_note(match):
        note_id = match.group(1)
        note = database.get_note(note_id)
        if note:
            # If the note has a physical file, redirect to it
            if note.get("file_path"):
                from app.core.note_files import get_absolute_file_path
                abs_path = str(get_absolute_file_path(note["file_path"]))
                return f"@{abs_path}"
            # Otherwise inline the content (pre-migration fallback)
            title = note.get("title", "Untitled")
            content = note.get("content", "")
            return f"[Note: {title}]\n{content}"
        return match.group(0)  # Keep original if not found

    return re.sub(r'@Notes/(note-[a-f0-9]+)', replace_note, prompt)
