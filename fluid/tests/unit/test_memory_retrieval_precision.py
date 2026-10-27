"""Recall precision, scope, short-keyword and saved-skill ranking ratchets."""
import pytest
from fluid.codemode.relevance import recall_terms
from fluid.codemode.skills import SkillStore
from fluid.tools.builtins.recall import handler
from fluid.tools.registry import ToolCtx


def test_labeled_recall_lab_precision_and_scope_budget(tmp_path):
    store = SkillStore(tmp_path)
    for text, key in [
        ("The theme uses purple accents and dark surfaces.", "theme"),
        ("The security controller validates deployment permissions.", "security"),
        ("Install dependencies in the checkout before running tests.", "install"),
        ("The fluid harness runs native provider loops.", "harness"),
        ("The staging build ships through a protected pipeline.", "deploy"),
        ("Use uv to manage Python dependencies.", "python"),
    ]:
        store.save_memory(text, key=key, protected=True)
    for query, expected in [("UI theme colors", "theme"), ("native fluid harness", "harness"),
                            ("uv Python", "python"), ("staging pipeline", "deploy")]:
        assert [e.key for e, _ in store.search_memories(query)] == [expected]
    # Baseline fixture: 689 chars; 760 once the header told the model to
    # Recall index lines by id (#this change, +71). Wording room, never a
    # second copy.
    section = store.render_memory_section()
    assert len(section) <= 800
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
    assert recall_terms("the with") == frozenset({"the", "with"})
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
    for name, description in (("build", "Build a binary"), ("theme", "Theme palette"),
                              ("ui_theme", "UI theme colors")):
        store.save(name, f'"""{description}."""\ndef helper():\n    return 1\n')
    # Drive persistence and index production, not a pre-ranked fake skill list.
    reopened = SkillStore(tmp_path)
    ctx = ToolCtx(extra={"recall_store": reopened})
    result = await handler({"query": "UI theme colors", "limit": 1}, ctx)
    assert "- ui_theme:" in result.text
    assert "- build:" not in result.text and "- theme:" not in result.text
    result = await handler({"query": "UI", "limit": 3}, ctx)
    assert "- build:" not in result.text


async def test_conflicts_do_not_match_substrings(tmp_path):
    store = SkillStore(tmp_path / "a")
    other = SkillStore(tmp_path / "b")
    store.save_memory("Build controller alpha.", key="build.controller", protected=True)
    other.save_memory("Build controller beta.", key="build.controller", protected=True)
    store._facts.merge(other._facts.export())
    assert all(e.conflicted for e in store.memories())
    ctx = ToolCtx(extra={"recall_store": store})
    result = await handler({"query": "UI"}, ctx)
    assert "Unresolved conflict" not in result.text
    result = await handler({"query": "controller"}, ctx)
    assert "Unresolved conflict" in result.text
