"""Suggested-task chips are gone (owner decision, Nov 2026) and nothing brings them back.

Work is done or delegated by the chat that owns it. Old ``session_task_chips``
rows may remain so a rollback keeps working, but nothing reads or acts on them.
These read what the producers send, on every surface a chip's tool or rule used
to reach, and try each way an old client or peer could still start one.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from app.core import session_modes, task_dispatch
from app.core.background_permissions import TOOL_LEVELS
from app.core.models import Session
from app.core.provider import fluid_tool_bridge as bridge
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions
from app.mcp.builtin_tools import create_builtin_tools_server, get_builtin_tool_names
from app.network import control
from fluid.cli import prompt as fluid_prompt
from fluid.codemode.vfs import CAPABILITY_TOOL_REASONS
from fluid.tools.builtins.session_orchestration import SPECS
from fluid.tools.mcp.bundled_server import _answer
from scripts.measure_prompt_catalog import compile_fixture
from tests.core.test_delegated_bridge_tiers import _bridge, _servers
from tests.core.test_delegated_bridge_tiers import _isolated as _bridge_isolated  # noqa: F401
from tests.core.test_native_first_request_budget import _first_request

ROOT = Path(__file__).parents[2]
TOOL = "SuggestTask"
# What the retired tool and its rule told an agent to do.
RETIRED_WORDS = (TOOL, "Work outside the user's request", "owner-click", "chip for separate")
# The table's only remaining mentions: its schema, a deleted chat's cleanup, the sync tombstone.
INERT_TABLE_FILES = {"app/db/database.py", "app/db/schema_postgres.py", "app/db/sessions.py", "app/network/policy.py"}
PRODUCTION_SOURCES = ("app", "fluid/fluid", "scripts")
RETIRED_NAMES = re.compile(
    r"SuggestTask|suggest_task|suggested_task|task[_-]chip|TaskChip|ADJACENT_WORK_RULE|CHIP_APPROVED|chip_access")


def _assert_not_offered(*texts: str) -> None:
    for text in texts:
        for words in RETIRED_WORDS:
            assert words not in text, words


def _leaf(name: str) -> str:
    return name.rsplit("__", 1)[-1]


def _worker(monkeypatch) -> None:
    monkeypatch.setattr(task_dispatch, "_is_worker_session", lambda _sid: True)
    monkeypatch.setattr(task_dispatch, "caller_chain", lambda _sid: ([], "parent", 1))


def _worker_options(tmp_path, **kwargs) -> SessionOptions:
    options = SessionOptions(model="fake", cwd=str(tmp_path), **kwargs)
    options.extra.update(session_mode="code", automatic_workflows=True, app_session_id="worker")
    return options


def _production_feature_references(root: Path) -> list[str]:
    found = []
    for folder in PRODUCTION_SOURCES:
        for path in sorted((root / folder).rglob("*")):
            if path.suffix not in (".py", ".json") or not path.is_file() or "node_modules" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            if relative == "scripts/devloop/journey_weights.json":
                weights = json.loads(text)
                journeys = {p.name for p in (root / "frontend/tests/journeys").glob("*.journey.mjs")
                            if p.is_file()}
                # Only existing journey filename keys in these named inventory maps
                # are not production references. Keep every value and all metadata.
                for inventory in ("seconds", "case_counts"):
                    if inventory in weights:
                        weights[inventory] = [[None if name in journeys else name, value]
                                              for name, value in weights[inventory].items()]
                text = json.dumps(weights)
            if relative in INERT_TABLE_FILES:
                text = text.replace("session_task_chips", "").replace("idx_session_task_chips", "")
            if RETIRED_NAMES.search(text) or TOOL.lower() in relative.replace("-", "").lower():
                found.append(relative)
    return found


def test_no_production_source_names_the_feature():
    assert _production_feature_references(ROOT) == []


@pytest.mark.parametrize("inventory", ["seconds", "case_counts"])
@pytest.mark.parametrize("reference", [None, "metadata", "unknown-key", "value", "other-map", "production"])
def test_journey_weight_exemption_only_covers_existing_journey_keys(tmp_path, reference, inventory):
    journey = "retired-task-chips.journey.mjs"
    journey_path = tmp_path / "frontend/tests/journeys" / journey
    journey_path.parent.mkdir(parents=True)
    journey_path.write_text("", encoding="utf-8")
    weights_path = tmp_path / "scripts/devloop/journey_weights.json"
    weights_path.parent.mkdir(parents=True)
    weights = {
        "_comment": "Hosted runtime measurements",
        "seconds": {journey: 10},
        "case_counts": {journey: 2},
    }
    expected = "scripts/devloop/journey_weights.json"
    if reference == "metadata":
        weights["_comment"] = TOOL
    elif reference == "unknown-key":
        weights[inventory]["missing-task-chips.journey.mjs"] = 20
    elif reference == "value":
        weights[inventory][journey] = TOOL
    elif reference == "other-map":
        weights["other"] = {journey: 10}
    elif reference == "production":
        source = tmp_path / "app/feature.py"
        source.parent.mkdir()
        source.write_text(f"name = {TOOL!r}\n", encoding="utf-8")
        expected = "app/feature.py"
    weights_path.write_text(json.dumps(weights), encoding="utf-8")
    assert _production_feature_references(tmp_path) == ([expected] if reference else [])


@pytest.mark.parametrize("module", ["app.core.task_chips", "app.db.task_chips"])
def test_its_modules_are_gone(module):
    assert importlib.util.find_spec(module) is None


def test_no_tool_table_lists_it():
    assert TOOL not in {spec.name for spec, _handler in SPECS}
    for names in (fluid_prompt.BUILTIN_TOOL_NAMES, fluid_prompt.HOST_ONLY_TOOL_NAMES, fluid_prompt.TOOL_CAPABILITIES,
                  CAPABILITY_TOOL_REASONS, bridge.BUILTINS_RISK_MAP, bridge.NATIVE_SHADOWED_BUILTINS,
                  bridge.NATIVE_EAGER_TOOLS, TOOL_LEVELS, session_modes.CHAT_MODE_ORCHESTRATION_TOOLS,
                  session_modes.PROACTIVE_DENIED_TOOLS):
        assert TOOL not in names
    assert not hasattr(fluid_prompt, "ADJACENT_WORK_RULE")
    assert not {name for name in (*bridge.DELEGATED_LOADED_TOOLS, *get_builtin_tool_names()) if _leaf(name) == TOOL}


@pytest.mark.parametrize("native", [True, False], ids=["native", "delegated"])
def test_the_measured_catalog_does_not_offer_it(native):
    compiled, catalog, specs = compile_fixture(native=native)
    assert TOOL not in {_leaf(spec.name) for spec in specs}
    _assert_not_offered(compiled.text, *(text for text, _name in catalog))


@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [True, False], ids=["code-mode", "json-tools"])
async def test_a_native_request_does_not_offer_it(monkeypatch, tmp_path, code_mode):
    """Through a real connect, as captured at the provider."""
    root = tmp_path / "native"
    root.mkdir()
    _engine, request, _reported = await _first_request(monkeypatch, root, code_mode=code_mode)
    assert TOOL not in {_leaf(tool["name"]) for tool in request.tools}
    _assert_not_offered(request.system, *(tool["description"] for tool in request.tools))


@pytest.mark.asyncio
async def test_the_delegated_bridge_does_not_offer_it(tmp_path):
    options, registry, tools = await _bridge(tmp_path)
    assert TOOL not in {_leaf(tool["name"]) for tool in tools}
    _assert_not_offered(ha._delegated_append_text("PROFILE", options, registry))


def test_the_session_tool_server_cannot_be_asked_for_it(tmp_path):
    with pytest.raises(TypeError):
        create_builtin_tools_server(session_id="s", profile_id="p", is_admin=True, cwd=str(tmp_path), suggest_task=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("system_prompt", [
    {"type": "preset", "preset": "claude_code", "append": "PROFILE"},
    "CUSTOM AGENT",
], ids=["subagent-preset", "subagent-custom-prompt"])
async def test_a_real_delegated_connect_does_not_offer_it(monkeypatch, tmp_path, system_prompt):
    """What the CLI receives for a Task worker, on both shapes a delegated prompt takes."""
    _worker(monkeypatch)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude"])
    options = _worker_options(tmp_path, system_prompt=system_prompt, permission_mode="default",
                              mcp_servers=_servers(str(tmp_path)))
    options.model = "opus"
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    try:
        loop = adapter.engine
        sent = "\n\n".join(part for part in (loop.system, loop.append_system_prompt) if part)
        listed = await _answer(adapter._registry, loop.ctx, {"op": "list"},
                               tool_filter=loop._bridge_tool_permitted, deferred=loop.bridge_deferred)
    finally:
        await adapter.disconnect()
    assert "# Subagent context" in sent
    assert TOOL not in {_leaf(tool["name"]) for tool in listed["tools"]}
    _assert_not_offered(sent)


def test_a_native_subagent_brief_does_not_offer_it(monkeypatch, tmp_path):
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    _worker(monkeypatch)
    registry = ToolRegistry()
    register_builtins(registry)
    assert TOOL not in {spec.name for spec in registry.list_tools(loaded_only=False)}
    compiled = ha.HarnessAdapter._compile_prompt_sections(
        "AGENT", None, _worker_options(tmp_path), registry=registry, wired=frozenset({"task_chips"}))
    assert "# Subagent context" in compiled.text
    _assert_not_offered(compiled.text)


def test_subagent_guidance_keeps_ordinary_delegation_and_reporting(monkeypatch):
    _worker(monkeypatch)
    role = task_dispatch.subagent_system_prompt("worker")
    assert "You may delegate only through your own provider-scoped Task roster" in role
    assert "returned to the parent" in role
    _assert_not_offered(role)


def test_retired_control_actions_are_unknown():
    for action in ("task-chip", "task-chip-suggest"):
        assert not control.known_action(action)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["task-chip", "task-chip-suggest"])
async def test_an_old_peers_action_is_refused_without_a_side_effect(mock_db, action):
    from app.db import database

    with pytest.raises(control.UnknownControlAction):
        await control.handle(action, {"session_id": "chat", "chip_id": "chip", "action": "start"}, from_device="old")
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM sessions")
        assert cursor.fetchone()["n"] == 0


@pytest.mark.parametrize("method, path", [
    ("get", "/api/v1/sessions/chat/task-chips"),
    ("post", "/api/v1/sessions/chat/task-chips/chip/start"),
    ("post", "/api/v1/sessions/chat/task-chips/chip/dismiss"),
])
def test_an_old_clients_request_finds_nothing(authenticated_client, mock_db, method, path):
    from app.db import database

    database.create_session(session_id="chat", profile_id="default", title="Chat")
    response = getattr(authenticated_client, method)(path)
    assert response.status_code == 404
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM sessions")
        assert cursor.fetchone()["n"] == 1
        cursor.execute("SELECT COUNT(*) AS n FROM turns")
        assert cursor.fetchone()["n"] == 0


def test_a_legacy_count_on_a_session_row_is_dropped():
    session = Session(id="chat", title="Chat", created_at="2026-11-03T00:00:00Z", updated_at="2026-11-03T00:00:00Z",
                      pending_task_chips=3)
    assert "pending_task_chips" not in session.model_dump()


def _legacy_chip(session_id: str, chip_id: str = "chip-1") -> None:
    from app.db import database

    with database.get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO session_task_chips (id, session_id, origin_session_id, request_id, title, summary, prompt, "
            "model, state) VALUES (%s, %s, %s, 'r', 'Old', 'Old', 'Old', 'opus', 'pending')",
            (chip_id, session_id, session_id))
        conn.commit()


def _legacy_states(session_id: str) -> list:
    from app.db import database

    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT state FROM session_task_chips WHERE session_id=%s", (session_id,))
        return [row["state"] for row in cursor.fetchall()]


def test_old_rows_stay_inert_until_their_chat_is_deleted(mock_db):
    """Clearing no longer touches them, and they raise no attention on the row."""
    from app.api.sessions_helpers import enrich_sessions_with_tags
    from app.db import database, sessions

    database.create_session(session_id="chat", profile_id="default", title="Chat")
    _legacy_chip("chat")

    row = enrich_sessions_with_tags([dict(database.get_session("chat"))])[0]
    assert "pending_task_chips" not in row
    sessions.clear_session_conversation("chat")
    assert _legacy_states("chat") == ["pending"]
    assert sessions.delete_session("chat")
    assert _legacy_states("chat") == []


def test_a_chat_a_chip_started_still_runs(mock_db):
    """Its queued first turn carries the old keys, which nothing reads now."""
    from app.db import database, turns

    database.create_session(session_id="started", profile_id="default", title="Started")
    payload = {"peer_message_content": "Brief", "task_chip_id": "chip-1", "task_chip_source": "parent",
               "peer_sender_label": "Parent"}
    queued = turns.enqueue_turn("started", "agent_message", prompt="Brief", payload=payload,
                                command_id="task_chip:start:abc")
    started = turns.start_turn(queued["id"])
    assert started and started["id"] == queued["id"]
    assert started["state"] == "running"
