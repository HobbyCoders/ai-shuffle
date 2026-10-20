"""Recall precision, scope, short-keyword and saved-skill ranking ratchets."""
from types import SimpleNamespace
import pytest
from fluid.codemode.relevance import recall_terms
from fluid.codemode.skills import SkillStore
from fluid.tools.builtins.recall import handler
from fluid.tools.registry import ToolCtx
from scripts.measure_memory_learning import measure


def test_labeled_recall_lab_precision_and_scope_budget(tmp_path):
    result = measure(tmp_path)
    assert result["recall_precision_at_3"] == 1
    assert result["recall_recall_at_3"] == 1
    assert result["cross_project_correction_leaks"] == 0
    # Baseline fixture: 689 chars; allow wording room, never a second copy.
    assert result["snapshot_chars"] <= 750
    section = SkillStore(tmp_path).render_memory_section("project:lab")
    paragraphs = [line.partition("] ")[2] for line in section.splitlines() if line.startswith("- [")]
    assert len(paragraphs) == len(set(paragraphs))


def test_short_names_keys_punctuation_repeated_terms_and_stop_words(tmp_path):
    store = SkillStore(tmp_path)
    good = store.save_memory("Prefer dark purple surfaces.", key="ui.theme")
    uv = store.save_memory("Use uv for dependencies.")
    store.save_memory("Build the controller with permissions.")
    assert [e.id for e, _ in store.search_memories("UI theme colors")] == [good.id]
    assert [e.id for e, _ in store.search_memories("uv")] == [uv.id]
    assert [e.id for e, _ in store.search_memories("UI.THEME")] == [good.id]
    assert recall_terms("the THE ui UI") == frozenset({"ui"})
    assert not store.search_memories("the with")
    assert recall_terms("Go") == frozenset({"go"})
    assert recall_terms("Make") == frozenset({"make"})
    assert len(recall_terms(" ".join(f"word{i}" for i in range(100)))) == 64


def test_archived_scope_and_conflict_screens_still_apply(tmp_path):
    store = SkillStore(tmp_path)
    archived = store.save_memory("Purple theme uses accessible contrast.", scope="project:one")
    store.retire_memory(archived.id, reason="fixture")
    store.save_memory("Purple theme belongs to another project.", scope="project:two")
    hits = store.search_memories("purple theme", project="project:one")
    assert [(e.id, old) for e, old in hits] == [(archived.id, True)]


async def test_skill_results_rank_before_limit_and_do_not_match_build_for_ui(tmp_path):
    store = SkillStore(tmp_path)
    # Handler fixture exercises the index consumer without persisting fake code.
    store.list = lambda: [SimpleNamespace(name="build", description="Build a binary", uses=0),
                         SimpleNamespace(name="theme", description="Theme palette", uses=0),
                         SimpleNamespace(name="ui_theme", description="UI theme colors", uses=0)]
    store.archived_skills = lambda: []
    ctx = ToolCtx(extra={"recall_store": store})
    result = await handler({"query": "UI theme colors", "limit": 1}, ctx)
    assert "- ui_theme:" in result.text
    assert "- build:" not in result.text and "- theme:" not in result.text
    result = await handler({"query": "UI", "limit": 3}, ctx)
    assert "- build:" not in result.text


async def test_conflicts_do_not_match_substrings(tmp_path):
    store = SkillStore(tmp_path)
    store.memories = lambda: [SimpleNamespace(text="Build controller", key=None,
                                             conflicted=True, provenance="user", scope="global")]
    store.search_memories = lambda *a, **kw: []
    ctx = ToolCtx(extra={"recall_store": store})
    result = await handler({"query": "UI"}, ctx)
    assert "Unresolved conflict" not in result.text
