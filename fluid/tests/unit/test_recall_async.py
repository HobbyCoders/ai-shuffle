"""Recall work must leave the event loop free; classification is per query."""
import asyncio
import threading

import pytest

from fluid.codemode import relevance
from fluid.codemode.skills import SkillStore
from fluid.tools.builtins import recall
from fluid.tools.registry import ToolCtx


@pytest.mark.asyncio
async def test_recall_lookup_runs_off_event_loop(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    fact = store.save_memory("The fixture repository uses pytest for release tests.")
    original = store.search_memories
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    threads = []

    def blocked_search(*args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        if not release.wait(5):
            raise RuntimeError("event loop did not release the fixture search")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "search_memories", blocked_search)
    for name in ("memories", "list", "archived_skills"):
        original_method = getattr(store, name)

        def checked(*args, method=original_method, **kwargs):
            threads.append(threading.get_ident())
            return method(*args, **kwargs)

        monkeypatch.setattr(store, name, checked)
    ctx = ToolCtx(extra={"recall_store": store})
    task = asyncio.create_task(recall.handler({"query": "pytest"}, ctx))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 10)
        assert not task.done(), "blocked disk work must not block this coroutine"
    finally:
        release.set()
    outcome = await asyncio.wait_for(task, 10)
    assert not outcome.is_error
    assert fact.text in outcome.text
    assert threads and main_thread not in threads


@pytest.mark.asyncio
async def test_recall_thread_preserves_search_errors(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("fixture read failure")

    monkeypatch.setattr(store, "search_memories", fail)
    result = await recall.handler({"query": "pytest"}, ToolCtx(extra={"recall_store": store}))
    assert result.is_error
    assert result.text == "recall failed: fixture read failure"


def test_term_types_are_classified_once_for_many_facts(monkeypatch):
    calls = []
    original = relevance.re.fullmatch

    def counted(pattern, text, *args, **kwargs):
        calls.append(text)
        return original(pattern, text, *args, **kwargs)

    terms = relevance.recall_terms("UI deploy 192.0.2.10 C++ テスト")
    monkeypatch.setattr(relevance.re, "fullmatch", counted)
    score = relevance.recall_scorer(terms)
    assert len(calls) == len(terms)
    for i in range(100):
        assert score(f"Fixture {i} deploy uses UI C++ 192.0.2.10 and テスト") == len(terms)
    assert len(calls) == len(terms)


def test_memory_search_prepares_once_per_query(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    for i in range(12):
        store.save_memory(f"Fixture {i} uses pytest automation for service releases.")
    calls = []
    original = relevance.recall_scorer

    def counted(terms):
        calls.append(terms)
        return original(terms)

    monkeypatch.setattr(relevance, "recall_scorer", counted)
    assert store.search_memories("pytest releases", limit=3)
    assert calls == [relevance.recall_terms("pytest releases")]
