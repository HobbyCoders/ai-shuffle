"""Real child dispatch binding/turn -> options -> captured provider HTTP or CLI.

Only transport, checkout provisioning and worker completion are replaced; effort
resolution, persistence, dispatch, adapter, loop and serializer remain real.
"""
from collections.abc import Callable, Iterator
from pathlib import Path
from sqlite3 import Connection
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core import provider_agent as pa
from app.core.model_effort import EFFORT_LEVELS, effort_preference_key
from app.core.provider import harness_adapter as ha
from app.db import database, model_providers, turns
from tests.core.test_effort_wire import provider, test_picker_to_wire as assert_wire


# Expectations are independent of the capability mapper under test. A child's
# remembered/inherited pick fits to the nearest supported level (ties go down).
CHILD_ROUTES = [
    ("anthropic", "claude-opus-4-6", None, ("low", "medium", "high", "max"), "high"),
    ("delegated", "claude-opus-4-6", None, ("low", "medium", "high", "max"), "high"),
    ("openai", "gpt-6.1-sol", None, ("low", "medium", "high", "xhigh", "max"), "medium"),
    ("chatgpt", "gpt-6.1-sol", ("low", "medium", "high", "xhigh", "max", "ultra"),
     ("low", "medium", "high", "xhigh", "max", "ultra"), "medium"),
    ("xai", "grok-4.6", None, ("low", "medium", "high", "xhigh"), "high"),
    ("openrouter", "catalog-model", ("none", "low", "high"), ("none", "low", "high"), "low"),
    ("custom", "local-reasoner", ("none", "low", "medium", "high"), ("none", "low", "medium", "high"), "medium"),
    ("custom", "local-model", (), (), None),
]


@pytest.fixture
def child_runtime(
    mock_db: Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Iterator[None]:
    from app.core import config, preference_identity, turn_manager, worker_checkout

    monkeypatch.setattr(config.settings, "fluid_projects_dir", str(tmp_path / "state"))
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda _: ("account", "owner"))
    monkeypatch.setattr(worker_checkout, "prepare_worker_checkout", lambda *a, **kw: None)
    monkeypatch.setattr(turn_manager, "wait_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pa, "final_turn_outcome", lambda _: {
        "usage": {}, "error": None, "interrupted": False, "terminal_found": True})
    monkeypatch.setattr(pa, "final_assistant_text", lambda _: "offline completion")

    async def submit(session_id: str, source: str, **kwargs: Any) -> str:
        # Persist the producer's exact overrides, without starting a scheduler
        # or replacing them with an expected effort fixture at the consumer.
        row = turns.record_adhoc_turn(session_id, source, prompt=kwargs["prompt"],
            profile_id=kwargs["profile_id"], overrides=kwargs["overrides"])
        return row["id"]

    monkeypatch.setattr(turn_manager, "submit_async", submit)
    pa.reset_worker_sessions()
    yield
    pa.reset_worker_sessions()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,model,catalog,levels,default", CHILD_ROUTES)
@pytest.mark.parametrize("pick", [None, *EFFORT_LEVELS])
@pytest.mark.parametrize("source", ["session", "turn", "spawn", "remembered"])
async def test_child_inheritance_to_transport(
    child_runtime: None,
    mock_db: Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    kind: str,
    model: str,
    catalog: tuple[str, ...] | None,
    levels: tuple[str, ...],
    default: str | None,
    pick: str | None,
    source: str,
) -> None:
    from app.core import claude_auth_mode

    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: claude_auth_mode.MODE_SUBSCRIPTION)
    row = None if kind == "delegated" else provider(
        kind, model, list(levels) if kind == "anthropic" else
        list(catalog) if catalog is not None else None, default)
    monkeypatch.setattr(model_providers, "get_model_provider", lambda *a, **kw: row)
    monkeypatch.setattr(database, "get_model_provider", lambda *a, **kw: row)
    monkeypatch.setattr(ha, "_lookup_provider_row", lambda _: row)
    # Unrelated role defaults must not replace a worker's selected binding.
    database.create_profile("child-role", "Child", None, {"model": "sonnet", "effort": "max"})
    database.create_profile("parent-role", "Parent", None, {"model": "sonnet"})
    database.create_session(session_id="root", profile_id="parent-role", mode="code", effort="low")
    database.create_session(session_id="caller", profile_id="parent-role", mode="code",
                            effort=pick if source == "session" else "low",
                            is_worker=True, parent_session_id="root")
    if source == "turn" and pick is not None:
        queued = turns.enqueue_turn("caller", "user", overrides={"effort": pick})
        assert turns.start_turn(queued["id"])
        # A newer queued turn must not override the actual calling turn.
        turns.enqueue_turn("caller", "user", overrides={"effort": "max"})
    if source == "remembered":
        database.set_user_preference("account", "owner", effort_preference_key(model), pick)

    result = await pa.dispatch_worker(parent_session_id="root", context_session_id="caller",
        profile_id="child-role", prompt="offline", model_id=model,
        provider_id="fixture" if row else None,
        effort=pick if source == "spawn" else None)
    assert result.ok, result.error
    selected = pick if pick is not None else (None if source == "session" else "low")
    if not levels:
        expected = None
    elif selected is None:
        expected = default
    else:
        expected = min(levels, key=lambda v: (
            abs(EFFORT_LEVELS.index(v) - EFFORT_LEVELS.index(selected)), EFFORT_LEVELS.index(v)))
    binding = database.get_session(result.session_id)
    turn = turns.get_latest_turn(result.session_id)
    assert binding["effort"] == expected
    assert turn["overrides"].get("effort") == expected
    # Check both the submitted first turn and a later wake/resume that has only
    # the durable worker binding. No mapping helper is mocked or re-run here.
    for phase, overrides in (("first-turn", turn["overrides"]), ("resume", {})):
        await assert_wire(mock_db, monkeypatch, tmp_path, record_property,
            kind, model, row, pick, expected, f"child-{source}-{phase}",
            worker=(result.session_id, overrides))
