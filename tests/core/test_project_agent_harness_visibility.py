"""A project agent created in the UI must reach the harness's subagent roster.

``ProjectAgentService`` writes the definition file; Fluid's
``discover_agents`` is the only reader, on both legs: ``NativeLoop``
publishes it on ``ctx.extra["agent_registry"]`` for native sessions, and
``app.mcp.builtin_tools._discover_agent_registry`` builds the delegated leg's
Task registry from the same walk. Fluid reads ``.fluid/agents`` and never
``.claude/agents``, so the service has to write where Fluid looks.

HOME is redirected so the developer's real ``~/.fluid/agents`` cannot leak in.
"""

from pathlib import Path

import pytest

from app.core.project_agent_service import ProjectAgentService


@pytest.fixture()
def project(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    return proj


def _create_reviewer(project: Path) -> None:
    ProjectAgentService(project).create_agent(
        name="reviewer",
        description="Reviews diffs for correctness",
        prompt="You are a meticulous code reviewer.",
        tools=["Read", "Grep"],
    )


def _native_loop(cwd: Path):
    from fluid.engine.native import NativeLoop
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
    )


def test_service_writes_where_fluid_discovers(project: Path) -> None:
    _create_reviewer(project)
    assert (project / ".fluid" / "agents" / "reviewer.md").is_file()
    assert not (project / ".claude" / "agents").exists()


def test_project_agent_is_in_a_native_sessions_subagent_registry(project: Path) -> None:
    _create_reviewer(project)

    loop = _native_loop(project)

    registry = loop.ctx.extra["agent_registry"]
    assert set(registry) == {"reviewer"}
    definition = registry["reviewer"]
    assert definition.scope == "project"
    assert definition.description == "Reviews diffs for correctness"
    assert definition.tools == ("Read", "Grep")
    assert definition.body.strip() == "You are a meticulous code reviewer."


def test_project_agent_is_in_the_delegated_task_registry(project: Path) -> None:
    from app.mcp.builtin_tools import _discover_agent_registry

    _create_reviewer(project)

    registry = _discover_agent_registry(str(project))
    assert registry is not None and set(registry) == {"reviewer"}


def test_service_lists_what_it_created(project: Path) -> None:
    _create_reviewer(project)
    service = ProjectAgentService(project)
    assert [a.name for a in service.get_agents()] == ["reviewer"]
    detail = service.get_agent("reviewer")
    assert detail is not None
    assert detail.file_path == str(Path(".fluid") / "agents" / "reviewer.md")
