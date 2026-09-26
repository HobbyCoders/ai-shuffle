"""
Unit tests for UserAgentService — discovery of user-global file-based agents
in ~/.fluid/agents/.

These agents are loaded by Fluid at runtime (via a profile's
``setting_sources`` including ``"user"``) and surfaced in the app's
autocomplete. UserAgentService roots ProjectAgentService's parsing/CRUD logic
at <fluid_dir>/agents instead of <project>/.fluid/agents.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from app.core.project_agent_service import (
    UserAgentService,
    get_user_agent_service,
    clear_agent_service_cache,
)


def _write_agent(agents_dir: Path, name: str, description: str, body: str = "Prompt body.") -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.md").write_text(
        f"---\ndescription: {description}\nmodel: sonnet\ntools: Read, Grep\n---\n{body}",
        encoding="utf-8",
    )


class TestUserAgentService:
    """UserAgentService scans <claude_dir>/agents directly (not .claude/agents)."""

    def test_agents_dir_is_rooted_at_claude_dir(self):
        """agents_dir should be <claude_dir>/agents, NOT <claude_dir>/.claude/agents."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir)
            service = UserAgentService(claude_dir)
            assert service.agents_dir == claude_dir / "agents"
            assert service.project_path == claude_dir

    def test_get_agents_discovers_user_global_agents(self):
        """Should discover agents directly under <claude_dir>/agents/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir)
            _write_agent(claude_dir / "agents", "my-helper", "A user-global helper")

            service = UserAgentService(claude_dir)
            agents = service.get_agents()

            assert len(agents) == 1
            assert agents[0].name == "my-helper"
            assert agents[0].description == "A user-global helper"
            assert agents[0].model == "sonnet"
            assert agents[0].tools == ["Read", "Grep"]

    def test_get_agent_returns_detail_with_prompt(self):
        """get_agent should return full detail including the prompt body."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir)
            _write_agent(claude_dir / "agents", "reviewer", "Reviews code", body="Do a review.")

            service = UserAgentService(claude_dir)
            detail = service.get_agent("reviewer")

            assert detail is not None
            assert detail.name == "reviewer"
            assert detail.prompt == "Do a review."

    def test_get_agents_empty_when_no_agents_dir(self):
        """Should return an empty list when <claude_dir>/agents does not exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = UserAgentService(Path(tmpdir))
            assert service.get_agents() == []

    def test_get_agents_sorted_by_name(self):
        """Agents should be returned sorted by name (case-insensitive)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir)
            _write_agent(claude_dir / "agents", "zebra", "Z")
            _write_agent(claude_dir / "agents", "alpha", "A")

            agents = UserAgentService(claude_dir).get_agents()

            assert [a.name for a in agents] == ["alpha", "zebra"]


class TestGetUserAgentServiceFactory:
    """The cached factory should resolve the claude dir and survive cache clears."""

    def test_factory_uses_resolved_claude_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir)
            _write_agent(claude_dir / "agents", "global-agent", "From the user home")

            clear_agent_service_cache()
            try:
                with patch(
                    "app.core.plugin_models._get_fluid_dir",
                    return_value=claude_dir,
                ):
                    service = get_user_agent_service()
                    names = [a.name for a in service.get_agents()]
                    assert "global-agent" in names
            finally:
                clear_agent_service_cache()

    def test_factory_is_cached(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir)

            clear_agent_service_cache()
            try:
                with patch(
                    "app.core.plugin_models._get_fluid_dir",
                    return_value=claude_dir,
                ):
                    first = get_user_agent_service()
                    second = get_user_agent_service()
                    assert first is second
            finally:
                clear_agent_service_cache()

    def test_clear_cache_resets_user_service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir)

            clear_agent_service_cache()
            try:
                with patch(
                    "app.core.plugin_models._get_fluid_dir",
                    return_value=claude_dir,
                ):
                    first = get_user_agent_service()
                    clear_agent_service_cache()
                    second = get_user_agent_service()
                    assert first is not second
            finally:
                clear_agent_service_cache()
