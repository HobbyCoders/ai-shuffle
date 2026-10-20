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
