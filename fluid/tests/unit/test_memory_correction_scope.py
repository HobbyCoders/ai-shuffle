"""Producer-path scope guards: corrections must not broaden visibility."""
import pytest
from fluid.codemode.skills import SkillStore
from fluid.tools.builtins.remember import handler
from fluid.tools.registry import ToolCtx


@pytest.mark.parametrize("stored_scope", ["global", "project:one"])
async def test_id_correction_preserves_scope_after_reopen_and_replication(tmp_path, stored_scope):
    store = SkillStore(tmp_path / "source")
    first = store.save_memory("Fixture runner is alpha.", scope=stored_scope, protected=True)
    ctx = ToolCtx(extra={"memory_store": store, "memory_project": "project:one"})
    result = await handler({"text": "Fixture runner is beta.", "memory_id": first.id,
                            "expected_revision": first.revision, "user_requested": True}, ctx)
    assert not result.is_error
    reopened = SkillStore(tmp_path / "source")
    entry, = reopened.memories()
    assert entry.scope == stored_scope
    assert entry.id == first.id and entry.revision == first.revision + 1
    replica = SkillStore(tmp_path / "replica")
    replica._facts.merge(reopened._facts.export())
    for view in (reopened, replica):
        assert "runner is beta" in view.render_memory_section("project:one")
        assert ("runner is beta" in view.render_memory_section("project:two")) == (stored_scope == "global")
        assert "runner is alpha" not in view.render_memory_section("project:one")


@pytest.mark.parametrize("scope,expected", [(None, "global"), ("global", "global"), ("project", "project:one")])
async def test_new_fact_defaults_and_explicit_correction_moves(tmp_path, scope, expected):
    store = SkillStore(tmp_path)
    ctx = ToolCtx(extra={"memory_store": store, "memory_project": "project:one"})
    data = {"text": "Fixture runner is alpha.", "user_requested": True}
    if scope is not None:
        data["scope"] = scope
    assert not (await handler(data, ctx)).is_error
    first, = store.memories()
    assert first.scope == expected
    # Explicit scope remains an intentional move, not ignored.
    data.update(text="Fixture runner is beta.", memory_id=first.id,
                expected_revision=first.revision, scope="project" if expected == "global" else "global")
    assert not (await handler(data, ctx)).is_error
    assert store.memories()[0].scope == ("project:one" if expected == "global" else "global")


async def test_scope_inheritance_does_not_bypass_protection_or_revision(tmp_path):
    store = SkillStore(tmp_path)
    first = store.save_memory("Fixture runner is alpha.", scope="project:one", protected=True)
    ctx = ToolCtx(extra={"memory_store": store, "memory_project": "project:one"})
    for extras in ({}, {"user_requested": True}, {"user_requested": True, "expected_revision": 99}):
        result = await handler({"text": "Fixture runner is beta.", "memory_id": first.id, **extras}, ctx)
        assert result.is_error
        assert store.memories()[0].scope == "project:one"


async def test_explicit_null_scope_is_not_an_input_schema_escape(tmp_path):
    store = SkillStore(tmp_path)
    ctx = ToolCtx(extra={"memory_store": store})
    result = await handler({"text": "Fixture runner is alpha.", "scope": None}, ctx)
    assert result.is_error


@pytest.mark.parametrize("forgotten_scope,blocked", [("project:one", True), ("global", False)])
async def test_omitted_scope_checks_the_resolved_project_deletion_barrier(tmp_path, forgotten_scope, blocked):
    store = SkillStore(tmp_path)
    deleted = store.save_memory("Fixture forgotten slot.", key="runner.target", scope=forgotten_scope, protected=True)
    assert store.delete_memory(deleted.id, explicit=True, expected_revision=deleted.revision)
    live = store.save_memory("Fixture retained convention.", scope="project:one", protected=True)
    ctx = ToolCtx(extra={"memory_store": store, "memory_project": "project:one"})
    result = await handler({"text": "Fixture corrected convention.", "memory_id": live.id,
                            "key": "runner.target", "expected_revision": live.revision, "user_requested": True}, ctx)
    assert result.is_error == blocked
    assert store.memories()[0].scope == "project:one"
    if blocked:
        assert "deletion barrier" in result.text or "forgotten" in result.text


async def test_project_id_correction_does_not_gain_global_profile_key(tmp_path):
    store = SkillStore(tmp_path)
    first = store.save_memory("Fixture project naming convention.", scope="project:one", protected=True)
    ctx = ToolCtx(extra={"memory_store": store, "memory_project": "project:one"})
    result = await handler({"text": "The user's name is Fixture.", "memory_id": first.id,
                            "expected_revision": first.revision, "user_requested": True}, ctx)
    assert not result.is_error
    assert store.memories()[0].key is None


async def test_replica_id_addressed_correction_preserves_scope_on_round_trip(tmp_path):
    source, replica = SkillStore(tmp_path / "source"), SkillStore(tmp_path / "replica")
    first = source.save_memory("Fixture source convention.", scope="project:one", protected=True)
    replica._facts.merge(source._facts.export())
    imported, = replica.memories()
    assert ":" in imported.id
    ctx = ToolCtx(extra={"memory_store": replica, "memory_project": "project:one"})
    result = await handler({"text": "Fixture corrected on replica.", "memory_id": imported.id,
                            "expected_revision": imported.revision, "user_requested": True}, ctx)
    assert not result.is_error
    source._facts.merge(replica._facts.export())
    entry, = source.memories()
    assert entry.id == first.id and entry.scope == "project:one"
    assert "corrected on replica" not in source.render_memory_section("project:two")
