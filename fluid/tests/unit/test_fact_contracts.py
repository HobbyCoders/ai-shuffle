"""Memory lifecycle contracts: exercise writes before retrieval, never seed consumers."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from fluid.codemode import facts
from fluid.codemode.skills import SkillStore, MAX_MEMORIES
from fluid.codemode.continual import Synthesizer
from fluid.tools.builtins import remember, recall, forget
from fluid.tools.registry import ToolCtx


def ctx(store, project="project:one", session="source-one"):
    return ToolCtx(session_id=session, extra={"memory_store": store, "recall_store": store, "memory_project": project})


@pytest.mark.asyncio
async def test_save_receipt_means_immediate_durability_without_learner(tmp_path):
    store = SkillStore(tmp_path)
    out = await remember.handler({"text": "The user's name is AuditName."}, ctx(store))
    assert not out.is_error and "Saved" in out.text
    assert store.journal_count() == 0
    fresh = SkillStore(tmp_path)
    assert fresh.memories()[0].protected
    assert "AuditName" in fresh.render_memory_section()
    out = await recall.handler({"query": "name"}, ctx(fresh, session="new-session"))
    assert "AuditName" in out.text and "revision: 1" in out.text


@pytest.mark.asyncio
async def test_correct_forget_and_restore_through_tools(tmp_path):
    store = SkillStore(tmp_path)
    c = ctx(store)
    await remember.handler({"text": "The user's name is Before."}, c)
    old = store.memories()[0]
    bad = await remember.handler({"text": "The user's name is After."}, c)
    assert bad.is_error and "revision" in bad.text
    out = await remember.handler({"text": "The user's name is After.", "memory_id": old.id,
                                   "expected_revision": old.revision}, c)
    assert not out.is_error
    assert len(store.memories()) == 1
    assert "Before" not in store.render_memory_section()
    assert not store.search_memories("Before")
    current = store.memories()[0]
    out = await forget.handler({"memory_id": current.id, "expected_revision": current.revision, "confirm": True}, c)
    assert not out.is_error and "Forgotten" in out.text
    assert not store.search_memories("After")
    raw = (tmp_path / "memories.json").read_text()
    assert "Before" not in raw and "After" not in raw
    denied = await remember.handler({"text": "The user's name is After."}, c)
    assert denied.is_error
    restored = await remember.handler({"text": "The user's name is After.", "restore": True}, c)
    assert not restored.is_error
    assert store.memories()[0].id != current.id


@pytest.mark.asyncio
async def test_new_fact_saves_even_when_a_revision_is_supplied(tmp_path):
    # GPT-6-Luna sends expected_revision=1 with an empty memory_id for a new
    # fact; the guard used to refuse it as "Memory changed since it was read".
    store = SkillStore(tmp_path)
    c = ctx(store)
    out = await remember.handler({"text": "Prefers short answers.", "key": "profile.response_style",
                                   "scope": "global", "memory_id": "", "expected_revision": 1}, c)
    assert not out.is_error and "Saved" in out.text
    saved = store.memories()[0]
    assert saved.key == "profile.response_style" and saved.revision == 1
    stale = await remember.handler({"text": "Prefers long answers.", "key": "profile.response_style",
                                     "memory_id": saved.id, "expected_revision": 7}, c)
    assert stale.is_error and "changed since it was read" in stale.text


def test_retry_is_idempotent_and_scope_is_part_of_identity(tmp_path):
    store = SkillStore(tmp_path)
    first = store.save_memory("A durable preference", protected=True)
    retry = store.save_memory("A durable preference", protected=True)
    assert (first.id, first.revision) == (retry.id, retry.revision)
    other = store.save_memory("A durable preference", protected=True, scope="project:two")
    assert first.id != other.id


def test_parallel_writes_and_usage_do_not_lose_successful_records(tmp_path):
    def write(i):
        store = SkillStore(tmp_path)
        entry = store.save_memory(f"Explicit concurrent fact {i}", protected=True)
        store.note_memories_recalled([entry.id])
        return entry.id
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write, range(24)))
    assert len(set(ids)) == 24
    assert len(SkillStore(tmp_path).memories()) == 24
    assert all(e.uses == 1 for e in SkillStore(tmp_path).memories())


def test_protected_fact_survives_inference_flood_and_prompt_has_budget(tmp_path):
    store = SkillStore(tmp_path)
    name = store.save_memory("The user's name is Protected.", protected=True)
    for i in range(MAX_MEMORIES + 10):
        store.save_memory(f"Temporary project observation {i}", scope="project:one")
    assert any(e.id == name.id for e in store.memories())
    assert "Protected" in store.render_memory_section("project:one")
    assert len([e for e in store.memories() if not e.protected]) == MAX_MEMORIES
    assert not store.delete_memory(name.id)
    with pytest.raises(ValueError, match="explicitly saved"):
        store.save_memory("A curator rewrite", memory_id=name.id)


def test_inferred_and_harvested_facts_survive_protected_flood(tmp_path):
    # The desktop store: ~28 KB of protected Remember facts filled the whole
    # 8,000-char snapshot, so no learner fact or env.* harvest ever rendered.
    store = SkillStore(tmp_path)
    for i in range(60):
        store.save_memory(f"Explicit project note {i} " + "x" * 280, protected=True, scope="project:x")
    store.save_memory("Prefers short answers that lead with the outcome.", provenance="user")
    store.save_memory("pytest is importable from /usr/local/bin/python.",
                      provenance="tool", key="env.pytest", scope="project:x")
    store.save_memory("The dashboard keeps its filter in localStorage key dash.filter.",
                      provenance="session", scope="project:x")
    out = store.render_memory_section("project:x")
    assert "Prefers short answers" in out
    assert "pytest is importable" in out
    assert "dash.filter" in out
    assert "Explicit project note 59" in out  # protected still fills the rest
    assert "scoped to other projects" not in out  # budget drops are not scope drops
    assert "omitted for space" in out
    body = out.split("\n\n")[1]  # the fact lines, between header and footers
    assert len(body) <= facts.MAX_PROMPT_CHARS


def test_six_live_shaped_unprotected_facts_all_render_under_a_protected_flood(tmp_path):
    # The verified claim is "0 of 6 to 6 of 6": the desktop store's six
    # in-scope unprotected facts, at its median line length of about 306
    # chars, four of them tool-harvested with the 80-char hint. Together
    # they need about 2.2k chars, more than the first 2,000-char reserve.
    store = SkillStore(tmp_path)
    scope = "project:pv2-5ed440e85cf268c371bfd3db"
    for i in range(92):
        store.save_memory(f"Explicit project note {i} " + "x" * 280, protected=True, scope=scope)
    unprotected = [
        ("user", None, "global"),
        ("tool", "env.pytest", scope),
        ("tool", "env.node_modules", scope),
        ("tool", "env.browser_host", scope),
        ("tool", None, scope),
        ("session", None, scope),
    ]
    for n, (provenance, key, where) in enumerate(unprotected):
        store.save_memory(
            f"Unprotected fact {n} " + "y" * 255, provenance=provenance, key=key, scope=where,
        )
    out = store.render_memory_section(scope)
    shown = [n for n in range(6) if f"Unprotected fact {n} " in out]
    assert shown == list(range(6))
    assert "Explicit project note" in out  # protected still fills the rest
    body = out.split("\n\n")[1]
    assert len(body) <= facts.MAX_PROMPT_CHARS


def test_a_fact_larger_than_the_budget_is_not_reported_as_no_facts(tmp_path, monkeypatch):
    # A fact line longer than the whole budget used to produce "(no saved
    # facts yet)" next to "1 saved fact omitted for space".
    monkeypatch.setattr(facts, "MAX_PROMPT_CHARS", 200)
    store = SkillStore(tmp_path)
    store.save_memory("Long note " + "z" * 300, protected=True)
    out = store.render_memory_section()
    assert "(no saved facts yet)" not in out
    assert "(no saved fact fits in this snapshot)" in out
    assert "(1 saved fact omitted for space; Recall finds it by keyword.)" in out


def test_memory_footer_counts_scope_and_space_drops_separately(tmp_path):
    store = SkillStore(tmp_path)
    store.save_memory("A fact about another project.", protected=True, scope="project:other")
    for i in range(60):
        store.save_memory(f"Explicit project note {i} " + "x" * 280, protected=True, scope="project:x")
    out = store.render_memory_section("project:x")
    rendered = sum(1 for line in out.splitlines() if line.startswith("- ["))
    dropped = 60 - rendered
    assert 0 < dropped < 60
    assert "(1 entry scoped to other projects not shown.)" in out
    assert f"({dropped} saved facts omitted for space; Recall finds them by keyword.)" in out
    # Without a project nothing is out of scope; the drops are all for space.
    everything = store.render_memory_section()
    assert "scoped to other projects" not in everything
    assert "saved facts omitted for space" in everything


def test_full_explicit_quota_does_not_evict_an_earlier_promise(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "MAX_EXPLICIT", 2)
    store = SkillStore(tmp_path)
    for i in range(2):
        store.save_memory(f"Explicit preference {i}", protected=True)
    with pytest.raises(ValueError, match="quota"):
        store.save_memory("Extra explicit preference", protected=True)
    assert [e.text for e in store.memories()] == ["Explicit preference 0", "Explicit preference 1"]


def test_unknown_rewrite_cannot_insert(tmp_path):
    with pytest.raises(ValueError, match="Unknown memory ID"):
        SkillStore(tmp_path).save_memory("Unexpected insertion", memory_id="m999")


def test_archive_forget_preserves_provenance_and_removes_retained_text(tmp_path):
    store = SkillStore(tmp_path)
    first = store.save_memory("Synthetic low-trust observation", provenance="tool")
    for i in range(MAX_MEMORIES):
        store.save_memory(f"Filler observation {i}")
    assert store.archived_memories()[0].provenance == "tool"
    # Round 9: tool-provenance facts are searchable (Recall flags them).
    hits = store.search_memories("low-trust")
    assert hits and hits[0][0].provenance == "tool" and hits[0][1] is True
    assert store.delete_memory(first.id)
    assert "low-trust" not in (tmp_path / "memories.json").read_text()


def test_expiry_is_not_refreshed_by_reads_or_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "INFERRED_TTL", 10)
    now = 1000.0
    monkeypatch.setattr(facts.time, "time", lambda: now)
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    a.save_memory("Time-bound observation")
    stale = a._facts.export()
    a.search_memories("observation")
    now = 1011.0
    assert not a.memories()
    b._facts.merge(stale)
    assert not b.memories()


def test_sync_retains_archive_and_applies_ingress_limits(tmp_path):
    a, b, consumer = (SkillStore(tmp_path / s) for s in ("a", "b", "consumer"))
    for i in range(MAX_MEMORIES):
        a.save_memory(f"Peer a fact {i}")
        b.save_memory(f"Peer b fact {i}")
    consumer._facts.merge(a._facts.export())
    consumer._facts.merge(b._facts.export())
    assert len(consumer.memories()) == MAX_MEMORIES
    assert len(consumer.archived_memories()) == MAX_MEMORIES
    assert len(consumer._facts.export()["entries"]) == MAX_MEMORIES * 2
    assert not consumer._facts.export()["tombstones"]


def test_old_replica_cannot_resurrect_after_tombstone_compaction(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "MAX_TOMBSTONES", 1)
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    first = a.save_memory("Synthetic forgotten original", protected=True)
    stale = a._facts.export()
    assert a.delete_memory(first.id, explicit=True)
    second = a.save_memory("Another fact to delete", protected=True)
    assert a.delete_memory(second.id, explicit=True)
    assert len(a._facts.snapshot()["tombstones"]) == 1
    b._facts.merge(a._facts.export())
    b._facts.merge(stale)
    assert not b.memories()
    assert not b.search_memories("original")


def test_same_revision_disagreement_is_withheld_not_guessed(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    first = a.save_memory("The user's name is Initial.", protected=True)
    b._facts.merge(a._facts.export())
    peer_id = b.memories()[0].id
    a.save_memory("The user's name is Alpha.", memory_id=first.id, expected_revision=1, protected=True)
    b.save_memory("The user's name is Beta.", memory_id=peer_id, expected_revision=1, protected=True)
    a._facts.merge(b._facts.export())
    assert a.memories()[0].conflicted
    assert not a.search_memories("name")
    assert "Alpha" not in (a.render_memory_section() or "")


def test_owner_and_project_filters_apply_before_search(tmp_path):
    a = SkillStore(tmp_path / "a", owner="api:a")
    b = SkillStore(tmp_path / "b", owner="api:b")
    a.save_memory("Private project word", scope="project:private")
    assert not a.search_memories("word", project="project:other")
    with pytest.raises(ValueError, match="owner"):
        b._facts.merge(a._facts.export())
    assert not b.memories()


def test_legacy_archive_migration_is_atomic_and_idempotent(tmp_path):
    archive = tmp_path / ".archive" / "memories-archive.jsonl"
    archive.parent.mkdir()
    archive.write_text(json.dumps({"id": "m2", "text": "Legacy archive fact", "provenance": "tool"}) + "\n")
    (tmp_path / "memories.json").write_text(json.dumps({"next_id": 3, "entries": [{"id": "m1", "text": "Legacy live fact"}]}))
    store = SkillStore(tmp_path)
    assert len(store.memories()) == 1
    assert len(store.archived_memories()) == 1
    assert store.archived_memories()[0].provenance == "tool"
    assert not archive.exists()
    assert len(SkillStore(tmp_path).archived_memories()) == 1


@pytest.mark.asyncio
async def test_failed_commit_never_returns_saved_receipt(tmp_path, monkeypatch):
    from fluid.codemode import skills
    store = SkillStore(tmp_path)
    store.save_memory("Existing durable value", protected=True)
    def fail(*args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(skills, "atomic_write_text", fail)
    out = await remember.handler({"text": "New value"}, ctx(store))
    assert out.is_error and "Not saved" in out.text
    assert len(store.memories()) == 1


def test_unprocessed_journal_has_a_hard_ceiling(tmp_path, monkeypatch):
    from fluid.codemode import skills
    monkeypatch.setattr(skills, "JOURNAL_MAX_BYTES", 1024)
    store = SkillStore(tmp_path)
    for i in range(30):
        store.journal_append({"kind": "user", "text": "Synthetic event " * 10})
    assert (tmp_path / "journal.jsonl").stat().st_size <= 1024


@pytest.mark.asyncio
async def test_curator_cannot_delete_protected_or_insert_unknown(tmp_path):
    from fluid.providers.base import StreamChunk
    class Provider:
        async def stream(self, request):
            yield StreamChunk(kind="text_delta", payload={"text": '<delete-memory id="m1"/><memory id="m999">New</memory>'})
    store = SkillStore(tmp_path)
    await remember.handler({"text": "The user's name is StillHere."}, ctx(store))
    await Synthesizer(Provider(), "test", store).consolidate()
    assert len(store.memories()) == 1
    assert "StillHere" in store.memories()[0].text


def test_causal_clock_detects_unequal_length_concurrent_edits(tmp_path):
    a, b = SkillStore(tmp_path / "a"), SkillStore(tmp_path / "b")
    initial = a.save_memory("Initial preference", protected=True)
    b._facts.merge(a._facts.export())
    peer_id = b.memories()[0].id
    a.save_memory("First local correction", memory_id=initial.id, expected_revision=1, protected=True)
    a.save_memory("Second local correction", memory_id=initial.id, expected_revision=2, protected=True)
    b.save_memory("Concurrent remote correction", memory_id=peer_id, expected_revision=1, protected=True)
    a._facts.merge(b._facts.export())
    assert a.memories()[0].conflicted
    assert not a.search_memories("correction")
    a.save_memory("User-resolved preference", memory_id=initial.id, expected_revision=3, protected=True)
    b._facts.merge(a._facts.export())
    assert b.memories()[0].text == "User-resolved preference"
    assert not b.memories()[0].conflicted


@pytest.mark.asyncio
async def test_daily_learning_budget_does_not_block_explicit_save(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "MAX_INFERENCE_CALLS_PER_DAY", 1)
    store = SkillStore(tmp_path)
    assert store._facts.claim_inference_call()
    assert not store._facts.claim_inference_call()
    out = await remember.handler({"text": "Still saved without a learner"}, ctx(store))
    assert not out.is_error and len(store.memories()) == 1


@pytest.mark.asyncio
async def test_forget_invalidates_inflight_inference_and_purges_raw_evidence(tmp_path):
    from fluid.providers.base import StreamChunk
    store = SkillStore(tmp_path)
    await remember.handler({"text": "Sensitive synthetic phrase"}, ctx(store))
    entry = store.memories()[0]
    store.journal_append({"kind": "user", "text": "Sensitive synthetic phrase"})
    (tmp_path / ".synthesis-log.jsonl").write_text('{"output_head":"Sensitive synthetic phrase"}\n')
    class Provider:
        async def stream(self, request):
            store.delete_memory(entry.id, explicit=True)
            yield StreamChunk(kind="text_delta", payload={"text": "<memory>Sensitive synthetic phrase</memory>"})
    await Synthesizer(Provider(), "test", store).run()
    assert not store.memories()
    assert not store.journal_entries()
    assert "Sensitive synthetic phrase" not in (tmp_path / "memories.json").read_text()
    log = tmp_path / ".synthesis-log.jsonl"
    assert not log.exists() or "Sensitive synthetic phrase" not in log.read_text()


@pytest.mark.asyncio
async def test_inference_resolves_each_source_project_not_current_project(tmp_path):
    from fluid.providers.base import StreamChunk
    store = SkillStore(tmp_path)
    for project in ("project:one", "project:two"):
        store.journal_context(session_id=project, project=project)
        store.journal_append({"kind": "user", "text": "A project-only convention"})
    class Provider:
        async def stream(self, request):
            yield StreamChunk(kind="text_delta", payload={"text": '<memory scope="project">Project convention</memory>'})
    await Synthesizer(Provider(), "test", store, project="project:unrelated").run()
    assert {e.scope for e in store.memories()} == {"project:one", "project:two"}


@pytest.mark.parametrize("permanent", [False, True])
def test_windows_replace_retry_is_bounded_and_preserves_old_document(tmp_path, monkeypatch, permanent):
    from fluid.codemode import skills
    store = SkillStore(tmp_path)
    store.save_memory("Original committed fact", protected=True)
    real_replace = skills.os.replace
    calls = []
    def replace(src, dst):
        calls.append(1)
        if permanent or len(calls) < 3:
            error = PermissionError("simulated Windows sharing violation")
            error.winerror = 5
            raise error
        return real_replace(src, dst)
    monkeypatch.setattr(skills.os, "replace", replace)
    monkeypatch.setattr(skills.time, "sleep", lambda _: None)
    if permanent:
        with pytest.raises(PermissionError):
            store.save_memory("Uncommitted fact", protected=True)
        assert len(calls) == 6
        assert [e.text for e in store.memories()] == ["Original committed fact"]
    else:
        store.save_memory("Second committed fact", protected=True)
        assert len(calls) == 3
        assert len(store.memories()) == 2
    assert not list(tmp_path.glob("*.tmp-*"))
