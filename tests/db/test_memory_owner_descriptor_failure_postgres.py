"""A session's memory owner answers to its session row, whatever the descriptor says.

``managed-postgres.json`` is read once to open the pool; a running worker keeps
using that pool whatever later happens to the file. These tests fail the file
through the real reader while the real pool still answers, create the sessions
with the real ``app.db.sessions`` functions, and use real memory stores through
the real consumers: the delegated leg's bridge tools and the adapter's memory
section.

A failed descriptor must not read as "this process has no database": a session
running as an API key would then be handed the installation owner's store.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from fluid.tools.inproc import tool

from app.core.config import settings
from app.core.fluid_memory import owner_root, session_memory, store_for_owner
from app.core.harness_state import project_state_root, session_state_root
from app.core.provider.harness_adapter import (
    HarnessAdapter,
    SessionOptions,
    _delegated_learning_inputs,
)
from app.db import base, checkouts, database, projects_v2, sessions
from app.mcp.builtin_tools import _build_memory_bridge_tools
from tests.db.test_preview_descriptor_failure_postgres import (  # noqa: F401 — fixtures
    PROFILE,
    cluster,
    failed_descriptor,
    worker,
)

OWNER_FACT = "the owner keeps the spare key under the blue planter"
KEY_FACT = "this key's team deploys from the green box"


def _owner_session(**fields) -> str:
    session_id = "descriptor-" + uuid4().hex
    sessions.create_session(session_id, PROFILE, title="Memory descriptor", model="sonnet", **fields)
    return session_id


def _api_key_session(**fields) -> tuple[str, str]:
    key = "descriptor-key-" + uuid4().hex
    database.create_api_user(key, key, key)
    return _owner_session(api_user_id=key, **fields), f"api:{key}"


def _texts(store) -> list[str]:
    return [entry.text for entry in store.memories()]


def _adapter(session_id: str, cwd: Path) -> HarnessAdapter:
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="sonnet", cwd=str(cwd), extra={"app_session_id": session_id})
    return adapter


def _bridge(session_id: str, cwd: Path) -> dict:
    # The memory tools are built before the fault: this is a running session.
    return {entry.name: entry for entry in _build_memory_bridge_tools(tool, session_id=session_id, cwd=str(cwd))}


def _text(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.fixture
def owner_store(worker: Path):  # noqa: F811
    store = store_for_owner("local")
    store.save_memory(OWNER_FACT, protected=True, scope="global", provenance="user")
    return store


def test_api_key_session_keeps_its_own_store(failed_descriptor: Callable[[], None], owner_store) -> None:  # noqa: F811
    session_id, owner = _api_key_session()
    store_for_owner(owner).save_memory(KEY_FACT, protected=True, scope="global", provenance="user")
    sdk_id = "ses_" + uuid4().hex
    sessions.update_session(session_id, sdk_session_id=sdk_id)

    failed_descriptor()

    for lookup in (session_id, sdk_id):
        store, _ = session_memory(lookup)
        assert store._facts.owner == owner
        assert store.root == owner_root(owner)
        assert _texts(store) == [KEY_FACT]


def test_project_scope_still_follows_the_row(failed_descriptor: Callable[[], None], tmp_path: Path) -> None:  # noqa: F811
    project = projects_v2.upsert_project("local:memory-descriptor-" + uuid4().hex, name="fixture")
    session_id = _owner_session(project_v2_id=project["id"])

    failed_descriptor()

    assert session_memory(session_id, str(tmp_path))[1] == f"project:{project['id']}"


def test_owner_session_keeps_the_owner_store(failed_descriptor: Callable[[], None], owner_store) -> None:  # noqa: F811
    """The pool never needed the file again, so nothing here is an outage."""
    session_id = _owner_session()

    failed_descriptor()

    store, _ = session_memory(session_id)
    assert store._facts.owner == "local"
    assert _texts(store) == [OWNER_FACT]


async def test_bridge_remember_and_recall_stay_on_the_key(
    failed_descriptor: Callable[[], None], owner_store, tmp_path: Path,  # noqa: F811
) -> None:
    session_id, owner = _api_key_session()
    tools = _bridge(session_id, tmp_path)

    failed_descriptor()

    saved = await tools["Remember"].handler(
        {"text": KEY_FACT, "scope": "global", "when": "When deploying"}
    )
    assert not saved.get("isError"), _text(saved)
    assert _texts(store_for_owner(owner)) == [KEY_FACT]
    assert _texts(owner_store) == [OWNER_FACT]

    recalled = await tools["Recall"].handler({"query": "spare key blue planter"})
    assert "planter" not in _text(recalled)


async def test_bridge_forget_cannot_reach_the_owner_store(
    failed_descriptor: Callable[[], None], owner_store, tmp_path: Path,  # noqa: F811
) -> None:
    session_id, _ = _api_key_session()
    tools = _bridge(session_id, tmp_path)
    (entry,) = owner_store.memories()

    failed_descriptor()

    await tools["Forget"].handler(
        {"memory_id": entry.id, "expected_revision": entry.revision, "confirm": True}
    )
    assert _texts(owner_store) == [OWNER_FACT]


def test_memory_section_and_candidates_show_only_the_key(
    failed_descriptor: Callable[[], None], owner_store, tmp_path: Path,  # noqa: F811
) -> None:
    session_id, owner = _api_key_session()
    store_for_owner(owner).save_memory(KEY_FACT, protected=True, scope="global", provenance="user")
    adapter = _adapter(session_id, tmp_path)

    failed_descriptor()

    assert KEY_FACT in (adapter._memory_section() or "")
    assert "planter" not in (adapter._memory_section() or "")
    assert not any("planter" in line for line in adapter._memory_eligible_lines())


def test_delegated_learning_journals_for_the_key(
    failed_descriptor: Callable[[], None], owner_store, tmp_path: Path,  # noqa: F811
) -> None:
    session_id, owner = _api_key_session()
    options = SessionOptions(model="sonnet", cwd=str(tmp_path), extra={"app_session_id": session_id})

    failed_descriptor()

    store, _, _ = _delegated_learning_inputs(session_id, options)
    assert store._facts.owner == owner


async def test_a_failed_lookup_fails_closed(
    failed_descriptor: Callable[[], None], owner_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """The supervisor stopping: no file and no pool is a refusal, never ``local``."""
    session_id, owner = _api_key_session()
    tools = _bridge(session_id, tmp_path)
    adapter = _adapter(session_id, tmp_path)

    failed_descriptor()
    monkeypatch.setattr(base, "_pg_pool", None)

    with pytest.raises(RuntimeError, match="pool not initialized"):
        session_memory(session_id)
    assert adapter._memory_section() is None
    assert adapter._memory_eligible_lines() == []
    for result in (
        await tools["Remember"].handler({"text": "must not land", "scope": "global", "when": "Never"}),
        await tools["Recall"].handler({"query": "spare key blue planter"}),
    ):
        assert result.get("isError")
        assert "planter" not in _text(result)
    assert _texts(owner_store) == [OWNER_FACT]
    assert not owner_root(owner).exists()


def test_a_missing_identity_or_row_fails_closed(failed_descriptor: Callable[[], None], owner_store) -> None:  # noqa: F811
    failed_descriptor()

    with pytest.raises(ValueError, match="without a session"):
        session_memory(None)
    with pytest.raises(ValueError, match="does not exist"):
        session_memory("no-such-session")


def test_state_root_still_follows_the_row(
    failed_descriptor: Callable[[], None], tmp_path: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A branch checkout's state stays with its registered project's base
    checkout; the file problem must not split it onto a path-derived root."""
    monkeypatch.setattr(settings, "fluid_projects_dir", str(tmp_path / "projects"))
    base_checkout = tmp_path / "base"
    base_checkout.mkdir()
    branch = tmp_path / "branch"
    branch.mkdir()
    project = projects_v2.upsert_project("local:state-descriptor-" + uuid4().hex, name="fixture")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(base_checkout))
    session_id = _owner_session(project_v2_id=project["id"])
    expected = project_state_root(cwd=str(base_checkout))
    assert session_state_root(session_id, str(branch)) == expected

    failed_descriptor()

    assert session_state_root(session_id, str(branch)) == expected
