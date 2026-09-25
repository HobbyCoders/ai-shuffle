"""
Project Agent Service - Manages file-based agents in project directories.

This service discovers, creates, updates, and deletes agent .md files
in a project's .fluid/agents/ directory, where Fluid's agent discovery
(``fluid.context.agents.discover_agents``) finds them for both the native and
the delegated leg. Fluid never reads ``.claude/agents``; legacy definitions
there are copied (never moved) into ``.fluid/agents`` the first time the
project's agents are listed for its base path.

Agent files use YAML frontmatter for metadata:
---
description: When to invoke this agent
model: sonnet
tools: Read, Grep, Glob
---

System prompt content here...
"""

import logging
import re
import shutil
import threading
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Written to .fluid/agents once the legacy .claude/agents copy has run.
_LEGACY_MIGRATED_MARKER = ".legacy-claude-agents-copied"


@dataclass
class ProjectAgent:
    """Information about a file-based agent in a project"""
    name: str  # Filename stem (without .md)
    description: str
    model: Optional[str] = None
    tools: Optional[List[str]] = None
    file_path: str = ""  # Relative path within project
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "model": self.model,
            "tools": self.tools,
            "file_path": self.file_path,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class ProjectAgentDetail(ProjectAgent):
    """Detailed agent info including full prompt content"""
    prompt: str = ""
    raw_content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base.update({
            "prompt": self.prompt,
            "raw_content": self.raw_content,
        })
        return base


class ProjectAgentService:
    """
    Service for managing file-based agents in project directories.

    Agents are stored as .md files in <project>/.fluid/agents/ with
    YAML frontmatter for metadata and markdown content for the system prompt.
    """

    def __init__(self, project_path: Path):
        """
        Initialize the project agent service.

        Args:
            project_path: Path to the project directory
        """
        self.project_path = project_path
        self.agents_dir = project_path / ".fluid" / "agents"
        # Where UI-created agents were written before they moved to .fluid.
        self.legacy_agents_dir: Optional[Path] = project_path / ".claude" / "agents"

    def _ensure_agents_dir(self) -> Path:
        """Ensure the agents directory exists"""
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        return self.agents_dir

    def _parse_frontmatter(self, content: str) -> Dict[str, Any]:
        """
        Parse YAML frontmatter from agent markdown file.

        Args:
            content: Full file content

        Returns:
            Dict with description, model, tools, and prompt
        """
        result = {
            "description": "",
            "model": None,
            "tools": None,
            "prompt": ""
        }

        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            result["prompt"] = content
            return result

        # Find end of frontmatter
        end_idx = -1
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                end_idx = i
                break

        if end_idx == -1:
            result["prompt"] = content
            return result

        # Parse frontmatter (simple YAML parsing)
        frontmatter_lines = lines[1:end_idx]
        for line in frontmatter_lines:
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip()

                # Remove quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]

                if key == "description":
                    result["description"] = value
                elif key == "model":
                    result["model"] = value
                elif key == "tools":
                    # Parse comma-separated or YAML list
                    if value.startswith("["):
                        # YAML list format: [Read, Grep, Glob]
                        value = value.strip("[]")
                    result["tools"] = [t.strip() for t in value.split(",") if t.strip()]

        # Rest is the prompt
        result["prompt"] = "\n".join(lines[end_idx + 1:]).strip()

        return result

    def _generate_frontmatter(
        self,
        description: str,
        model: Optional[str] = None,
        tools: Optional[List[str]] = None
    ) -> str:
        """
        Generate YAML frontmatter for an agent file.

        Args:
            description: Agent description
            model: Optional model name
            tools: Optional list of tool names

        Returns:
            Frontmatter string including --- delimiters
        """
        lines = ["---"]
        lines.append(f"description: {description}")

        if model:
            lines.append(f"model: {model}")

        if tools:
            tools_str = ", ".join(tools)
            lines.append(f"tools: {tools_str}")

        lines.append("---")
        return "\n".join(lines)

    def _validate_agent_name(self, name: str) -> bool:
        """
        Validate agent name for use as filename.

        Args:
            name: Proposed agent name

        Returns:
            True if valid
        """
        # Must be non-empty, alphanumeric with hyphens/underscores
        if not name:
            return False
        return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', name))

    def _migrate_legacy_agents(self) -> None:
        """Copy legacy ``.claude/agents/*.md`` files into ``.fluid/agents``.

        ``.claude/agents`` is Claude Code's own project-agents folder and is
        often committed, so the source files are always kept. A file whose name
        already exists in ``.fluid/agents`` is skipped: the existing definition
        wins and is never overwritten. Symlinks are left alone, matching the
        settings import. A marker in ``.fluid/agents`` makes this run once per
        project, so an agent deleted from ``.fluid/agents`` is not copied back.
        """
        legacy_dir = self.legacy_agents_dir
        if legacy_dir is None or legacy_dir.is_symlink() or not legacy_dir.is_dir():
            return
        marker = self.agents_dir / _LEGACY_MIGRATED_MARKER
        if marker.exists():
            return
        found = failed = False
        for legacy_file in sorted(legacy_dir.glob("*.md")):
            target = self.agents_dir / legacy_file.name
            if legacy_file.is_symlink() or not legacy_file.is_file():
                continue
            found = True
            if target.exists():
                continue
            created = False
            try:
                data = legacy_file.read_bytes()
                self._ensure_agents_dir()
                with open(target, "xb") as out:  # exclusive: never overwrite
                    created = True
                    out.write(data)
                shutil.copystat(legacy_file, target)
            except FileExistsError:
                continue
            except OSError as e:
                if created:
                    target.unlink(missing_ok=True)
                logger.warning(f"Failed to copy legacy agent {legacy_file}: {e}")
                failed = True
                continue
            logger.info(f"Copied legacy project agent {legacy_file} to {target}")
        if not found or failed:  # nothing to remember, or retry next listing
            return
        try:
            self._ensure_agents_dir()
            marker.touch(exist_ok=True)
        except OSError as e:
            logger.warning(f"Failed to mark legacy agents copied in {self.agents_dir}: {e}")

    def get_agents(self, migrate_legacy: bool = False) -> List[ProjectAgent]:
        """
        Discover all agents in the project's .fluid/agents/ directory.

        Args:
            migrate_legacy: Copy legacy ``.claude/agents`` definitions first.
                Pass True only for the project's base path, never for a
                session worktree, so listing never adds files to a worktree.

        Returns:
            List of ProjectAgent objects
        """
        agents = []
        if migrate_legacy:
            self._migrate_legacy_agents()

        if not self.agents_dir.exists():
            return agents

        for agent_file in self.agents_dir.glob("*.md"):
            try:
                content = agent_file.read_text(encoding='utf-8')
                parsed = self._parse_frontmatter(content)

                stat = agent_file.stat()

                agents.append(ProjectAgent(
                    name=agent_file.stem,
                    description=parsed["description"],
                    model=parsed["model"],
                    tools=parsed["tools"],
                    file_path=str(agent_file.relative_to(self.project_path)),
                    created_at=datetime.fromtimestamp(stat.st_ctime),
                    updated_at=datetime.fromtimestamp(stat.st_mtime),
                ))
            except (IOError, UnicodeDecodeError) as e:
                logger.error(f"Failed to read agent file {agent_file}: {e}")

        # Sort by name
        agents.sort(key=lambda a: a.name.lower())
        return agents

    def get_agent(self, name: str) -> Optional[ProjectAgentDetail]:
        """
        Get detailed information about a specific agent.

        Args:
            name: Agent name (filename stem)

        Returns:
            ProjectAgentDetail or None if not found
        """
        # Validate name to prevent path traversal
        if not self._validate_agent_name(name):
            logger.warning(f"Invalid agent name format: {name}")
            return None

        agent_file = self.agents_dir / f"{name}.md"

        if not agent_file.exists():
            return None

        try:
            content = agent_file.read_text(encoding='utf-8')
            parsed = self._parse_frontmatter(content)

            file_stat = agent_file.stat()

            return ProjectAgentDetail(
                name=name,
                description=parsed["description"],
                model=parsed["model"],
                tools=parsed["tools"],
                file_path=str(agent_file.relative_to(self.project_path)),
                created_at=datetime.fromtimestamp(file_stat.st_ctime),
                updated_at=datetime.fromtimestamp(file_stat.st_mtime),
                prompt=parsed["prompt"],
                raw_content=content,
            )
        except (IOError, UnicodeDecodeError) as e:
            logger.error(f"Failed to read agent file {agent_file}: {e}")
            return None

    def create_agent(
        self,
        name: str,
        description: str,
        prompt: str,
        model: Optional[str] = None,
        tools: Optional[List[str]] = None,
    ) -> ProjectAgent:
        """
        Create a new agent file.

        Args:
            name: Agent name (will be used as filename)
            description: Agent description (when to invoke)
            prompt: System prompt content
            model: Optional model name
            tools: Optional list of tool names

        Returns:
            Created ProjectAgent

        Raises:
            ValueError: If name is invalid or agent already exists
        """
        if not self._validate_agent_name(name):
            raise ValueError(
                f"Invalid agent name: '{name}'. Must start with a letter and "
                "contain only letters, numbers, hyphens, and underscores."
            )

        self._ensure_agents_dir()
        agent_file = self.agents_dir / f"{name}.md"

        if agent_file.exists():
            raise ValueError(f"Agent already exists: {name}")

        # Generate file content
        frontmatter = self._generate_frontmatter(description, model, tools)
        content = f"{frontmatter}\n\n{prompt}"

        # Write file
        agent_file.write_text(content, encoding='utf-8')

        stat = agent_file.stat()

        return ProjectAgent(
            name=name,
            description=description,
            model=model,
            tools=tools,
            file_path=str(agent_file.relative_to(self.project_path)),
            created_at=datetime.fromtimestamp(stat.st_ctime),
            updated_at=datetime.fromtimestamp(stat.st_mtime),
        )

    def update_agent(
        self,
        name: str,
        description: Optional[str] = ...,  # type: ignore[assignment]
        prompt: Optional[str] = ...,  # type: ignore[assignment]
        model: Optional[str] = ...,  # type: ignore[assignment]
        tools: Optional[List[str]] = ...,  # type: ignore[assignment]
    ) -> ProjectAgent:
        """
        Update an existing agent file.

        Uses Ellipsis (...) as default to distinguish between "not provided" and "explicitly set to None".
        This allows clearing fields by passing None explicitly.

        Args:
            name: Agent name
            description: New description (... to keep existing, None not allowed for description)
            prompt: New prompt (... to keep existing, None not allowed for prompt)
            model: New model (... to keep existing, None to clear)
            tools: New tools (... to keep existing, None to clear)

        Returns:
            Updated ProjectAgent

        Raises:
            ValueError: If agent doesn't exist or name is invalid
        """
        # Validate name to prevent path traversal
        if not self._validate_agent_name(name):
            raise ValueError(f"Invalid agent name format: {name}")

        agent_file = self.agents_dir / f"{name}.md"

        if not agent_file.exists():
            raise ValueError(f"Agent not found: {name}")

        # Read existing content
        existing = self.get_agent(name)
        if not existing:
            raise ValueError(f"Failed to read agent: {name}")

        # Merge with updates - use ... to mean "keep existing"
        new_description = description if description is not ... else existing.description
        new_prompt = prompt if prompt is not ... else existing.prompt
        new_model = model if model is not ... else existing.model
        new_tools = tools if tools is not ... else existing.tools

        # Generate new file content
        frontmatter = self._generate_frontmatter(new_description, new_model, new_tools)
        content = f"{frontmatter}\n\n{new_prompt}"

        # Write file
        agent_file.write_text(content, encoding='utf-8')

        file_stat = agent_file.stat()

        return ProjectAgent(
            name=name,
            description=new_description,
            model=new_model,
            tools=new_tools,
            file_path=str(agent_file.relative_to(self.project_path)),
            created_at=datetime.fromtimestamp(file_stat.st_ctime),
            updated_at=datetime.fromtimestamp(file_stat.st_mtime),
        )

    def delete_agent(self, name: str) -> bool:
        """
        Delete an agent file.

        Args:
            name: Agent name

        Returns:
            True if deleted

        Raises:
            ValueError: If agent doesn't exist or name is invalid
        """
        # Validate name to prevent path traversal
        if not self._validate_agent_name(name):
            raise ValueError(f"Invalid agent name format: {name}")

        agent_file = self.agents_dir / f"{name}.md"

        if not agent_file.exists():
            raise ValueError(f"Agent not found: {name}")

        agent_file.unlink()
        return True

    def rename_agent(self, old_name: str, new_name: str) -> ProjectAgent:
        """
        Rename an agent file.

        Args:
            old_name: Current agent name
            new_name: New agent name

        Returns:
            Renamed ProjectAgent

        Raises:
            ValueError: If old agent doesn't exist or names are invalid/taken
        """
        # Validate both names to prevent path traversal
        if not self._validate_agent_name(old_name):
            raise ValueError(f"Invalid agent name format: {old_name}")

        if not self._validate_agent_name(new_name):
            raise ValueError(
                f"Invalid agent name: '{new_name}'. Must start with a letter and "
                "contain only letters, numbers, hyphens, and underscores."
            )

        old_file = self.agents_dir / f"{old_name}.md"
        new_file = self.agents_dir / f"{new_name}.md"

        if not old_file.exists():
            raise ValueError(f"Agent not found: {old_name}")

        if new_file.exists():
            raise ValueError(f"Agent already exists: {new_name}")

        old_file.rename(new_file)

        # Return updated agent info
        agent = self.get_agent(new_name)
        if not agent:
            raise ValueError(f"Failed to read renamed agent: {new_name}")
        return agent


class UserAgentService(ProjectAgentService):
    """
    Discovers user-global file-based agents in ~/.fluid/agents/.

    Fluid loads these at runtime whenever a profile's ``setting_sources``
    includes ``"user"`` (the default). This service exposes them for
    discovery, reusing all of ``ProjectAgentService``'s parsing/CRUD logic but
    rooted at the user-global Fluid dir instead of a project's ``.fluid``
    directory.
    """

    def __init__(self, claude_dir: Path):
        # Intentionally bypass ProjectAgentService.__init__ (which appends
        # ".fluid/agents"); user-global agents live directly under
        # <claude_dir>/agents.
        self.project_path = claude_dir
        self.agents_dir = claude_dir / "agents"
        self.legacy_agents_dir = None


# Service instance cache (keyed by project_path)
_agent_services: Dict[str, ProjectAgentService] = {}
_agent_services_lock = threading.Lock()
_user_agent_service: Optional[UserAgentService] = None


def get_project_agent_service(project_path: Path) -> ProjectAgentService:
    """
    Get a project agent service instance for a project directory.

    Args:
        project_path: Path to the project directory

    Returns:
        ProjectAgentService instance (cached per project_path)
    """
    cache_key = str(project_path)

    with _agent_services_lock:
        if cache_key not in _agent_services:
            _agent_services[cache_key] = ProjectAgentService(project_path)
        return _agent_services[cache_key]


def get_user_agent_service() -> UserAgentService:
    """
    Get the cached user-global agent service that scans ~/.fluid/agents/.

    The directory is resolved per deployment mode (Docker uses the appuser
    home), so this mirrors what Fluid loads via ``setting_sources: ["user"]``.
    """
    global _user_agent_service

    with _agent_services_lock:
        if _user_agent_service is None:
            # Imported lazily to avoid pulling plugin machinery at module load.
            from app.core.plugin_models import _get_fluid_dir
            _user_agent_service = UserAgentService(_get_fluid_dir())
        return _user_agent_service


def clear_agent_service_cache():
    """Clear the agent service cache (useful for testing)"""
    global _agent_services, _user_agent_service
    with _agent_services_lock:
        _agent_services = {}
        _user_agent_service = None
