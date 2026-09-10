"""Reproduce Fluid memory contract gaps without a provider, DB, or real user data.

Run from any directory: python tools/research/memory_contract_probe.py
Exit 0 means the audit completed, NOT that the contracts hold. --strict exits 1
when any contract fails. Each case uses a disposable directory. Network cases
execute the actual function bodies with an in-memory database double; they do
not test transport, authorization, or a running app. Scripted model output
exercises allowed outcomes, not their likelihood with a production model.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "fluid"))

from fluid.codemode.continual import Synthesizer  # noqa: E402
from fluid.codemode.skills import (  # noqa: E402
    MAX_MEMORIES, SkillStore, atomic_write_text, project_key_for,
)
from fluid.providers.base import StreamChunk  # noqa: E402
from fluid.tools.builtins import recall, remember  # noqa: E402
from fluid.tools.registry import ToolCtx  # noqa: E402


class ScriptedProvider:
    def __init__(self, output: str):
        self.output = output

    async def stream(self, request):
        yield StreamChunk(kind="text_delta", payload={"text": self.output})


def journal_context(store):
    # Durable memory is independent of the optional inference journal.
    def journal(text, scope=None):
        entry = {"kind": "remember", "text": text}
        if scope:
            entry["scope"] = scope
        store.journal_append(entry)
    return ToolCtx(extra={"continual_journal": journal, "memory_store": store})


def synth(store, output):
    return Synthesizer(ScriptedProvider(output), "scripted-audit", store)


async def acknowledged_save(root):
    store = SkillStore(root)
    outcome = await remember.handler(
        {"text": "The user's name is AuditExample."}, journal_context(store)
    )
    fresh = SkillStore(root)
    return bool(fresh.memories()), {
        "tool_error": outcome.is_error,
        "journal_entries": fresh.journal_count(),
        "durable_facts_after_acknowledgment": len(fresh.memories()),
    }


async def explicit_noop(root):
    store = SkillStore(root)
    await remember.handler(
        {"text": "The user's name is AuditExample."}, journal_context(store)
    )
    await synth(store, "NO_CHANGES").run()
    pending = store.journal_count() - store.synth_cursor()
    return bool(store.memories()) or pending > 0, {
        "durable_facts": len(store.memories()), "pending_journal_entries": pending,
        "synthesis_cursor": store.synth_cursor(),
    }


async def explicit_eviction(root):
    store = SkillStore(root)
    fact = "The user's name is AuditExample."
    await remember.handler({"text": fact}, journal_context(store))
    await synth(store, f"<memory>{fact}</memory>").run()
    for _ in range(10):
        store.render_memory_section()
    for i in range(MAX_MEMORIES):
        store.save_memory(f"Synthetic project detail {i}", scope="project-audit")
    return fact in (store.render_memory_section() or ""), {
        "explicit_fact_in_prompt": fact in (store.render_memory_section() or ""),
        "explicit_fact_archived": any(m.text == fact for m in store.archived_memories()),
        "live_count": len(store.memories()),
    }


async def duplicate_retries(root):
    store = SkillStore(root)
    a = store.save_memory("The user prefers concise answers.", provenance="user")
    b = store.save_memory("The user prefers concise answers.", provenance="user")
    return a.id == b.id and len(store.memories()) == 1, {
        "returned_ids": [a.id, b.id], "live_count": len(store.memories()),
    }


async def archived_forget(root):
    store = SkillStore(root)
    fact = store.save_memory("The user's nickname is AuditNickname.")
    for i in range(MAX_MEMORIES):
        store.save_memory(f"Synthetic filler {i}")
    deleted = store.delete_memory(fact.id)
    matches = store.search_memories("AuditNickname")
    return not matches, {"delete_returned": deleted, "remaining_matches": len(matches)}


async def trust_boundary(root):
    store = SkillStore(root / "live")
    fact = "The user prefers orange umbrellas."
    store.save_memory(fact, provenance="tool")
    before = await recall.handler({"query": "umbrellas"}, ToolCtx(extra={"recall_store": store}))
    # Use a second store: the live Recall above increments LFU and would
    # protect that entry from the eviction this half needs to exercise.
    store = SkillStore(root / "archive")
    store.save_memory(fact, provenance="tool")
    for i in range(MAX_MEMORIES):
        store.save_memory(f"Synthetic filler {i}")
    archived = next(m for m in store.archived_memories() if m.text == fact)
    after = await recall.handler({"query": "umbrellas"}, ToolCtx(extra={"recall_store": store}))
    return fact not in before.text and fact not in after.text and archived.provenance == "tool", {
        "tool_only_fact_returned_live": fact in before.text,
        "tool_only_fact_returned_archived": fact in after.text,
        "archived_provenance": archived.provenance,
    }


async def concurrent_saves(root):
    a, b = SkillStore(root), SkillStore(root)
    barrier = threading.Barrier(2)
    first_saved = threading.Event()
    # Overlap reads, then order writes. This isolates lost updates from
    # same-process temp-file collisions in atomic_write_text on Windows.
    def coordinated_load(load, second=False):
        def wrapped():
            doc = load()
            barrier.wait(timeout=10)
            if second and not first_saved.wait(timeout=10):
                raise TimeoutError("First writer did not finish")
            return doc
        return wrapped
    save_a = a._save_memories
    def finish_first(doc):
        try:
            save_a(doc)
        finally:
            first_saved.set()
    with patch.object(a, "_load_memories", coordinated_load(a._load_memories)), \
         patch.object(b, "_load_memories", coordinated_load(b._load_memories, second=True)), \
         patch.object(a, "_save_memories", finish_first):
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(a.save_memory, "Synthetic fact alpha")
            fb = pool.submit(b.save_memory, "Synthetic fact beta")
            ids = [fa.result(timeout=15).id, fb.result(timeout=15).id]
    facts = SkillStore(root).memories()
    return len(facts) == 2 and len(set(ids)) == 2, {
        "returned_ids": ids, "live_count_after_two_successful_saves": len(facts),
    }


async def model_deletion(root):
    store = SkillStore(root)
    fact = "The user's name is AuditExample."
    await remember.handler({"text": fact}, journal_context(store))
    await synth(store, f"<memory>{fact}</memory>").run()
    entry = store.memories()[0]
    await synth(store, f'<delete-memory id="{entry.id}"/>').consolidate()
    return any(m.id == entry.id for m in store.memories()), {
        "explicit_fact_survives_model_only_cleanup": bool(store.memories()),
    }


async def unknown_update(root):
    store = SkillStore(root)
    await synth(store, '<memory id="m999">Synthetic unexpected insertion</memory>').consolidate()
    return not store.memories(), {"insertions_by_rewrite_only_consolidation": len(store.memories())}


async def project_identity(root):
    checkout_a, checkout_b = root / "checkout-a", root / "checkout-b"
    shared_gitdir = root / "shared.git"
    shared_gitdir.mkdir()
    for path in (checkout_a, checkout_b):
        path.mkdir()
        (path / ".git").write_text(f"gitdir: {shared_gitdir}", encoding="utf-8")
    a, b = project_key_for(checkout_a), project_key_for(checkout_b)
    store = SkillStore(root / "memory")
    store.save_memory("Synthetic project convention", scope=a)
    return a == b, {
        "same_project_distinct_checkout_keys": a != b,
        "project_fact_loaded_in_other_checkout": "Synthetic project convention" in (store.render_memory_section(b) or ""),
    }


def network_functions(root, records):
    # Compile selected, unmodified production function bodies. Do NOT import
    # app.network: importing the application can load the real DB settings.
    path = ROOT / "app/network/memory_records.py"
    names = {"_store", "_stores", "apply_to_files", "refresh_from_files", "_hash"}
    module = ast.parse(path.read_text(encoding="utf-8"))
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(functions) == len(names)
    program = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    ast.fix_missing_locations(program)
    class FakeDB:
        def __init__(self):
            self.deleted = []
            self.published = []
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def cursor(self):
            return self
        def execute(self, sql, args=()):
            if sql.startswith("UPDATE network_memory_records SET deleted"):
                self.deleted.append(args[0])
            elif sql.startswith("INSERT INTO network_memory_records"):
                record_id, kind, scope, content, digest = args
                self.published.append({"id": record_id, "kind": kind, "scope": scope,
                                       "content": content, "content_hash": digest, "deleted": False})
        def fetchall(self):
            return records
    db = FakeDB()
    ctx = SimpleNamespace(device_id="audit-local")
    namespace = {
        "_root": lambda: root, "engine": SimpleNamespace(context=lambda: ctx),
        "get_db": lambda: db, "json": json, "hashlib": hashlib,
        "_atomic_write": atomic_write_text, "_SKILL_NAME": __import__("re").compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}$"),
        "MAX_SKILL_BYTES": 65536, "log": __import__("logging").getLogger("audit"),
    }
    exec(compile(program, str(path), "exec"), namespace)
    return namespace, db


async def sync_cap(root):
    store = SkillStore(root / "consumer")
    records = []
    # Produce valid, under-cap source stores and publish through the actual
    # publisher body before applying their combined records on a third store.
    for peer in range(2):
        peer_root = root / f"peer-{peer}"
        peer_store = SkillStore(peer_root)
        for i in range(peer, MAX_MEMORIES + 1, 2):
            peer_store.save_memory(f"Synthetic synced fact {i}")
        publisher, db = network_functions(peer_root, [])
        publisher["refresh_from_files"](ctx=SimpleNamespace(device_id=f"audit-peer-{peer}"))
        records.extend(db.published)
    functions, _ = network_functions(root / "consumer", records)
    functions["apply_to_files"]([r["id"] for r in records])
    return len(store.memories()) <= MAX_MEMORIES, {
        "live_count_after_sync": len(store.memories()), "configured_live_cap": MAX_MEMORIES,
    }


async def archive_tombstone(root):
    store = SkillStore(root)
    first = store.save_memory("Synthetic fact to archive")
    records = [{"id": f"fact:audit-local:{first.id}", "content_hash": "synthetic-hash", "deleted": False}]
    for i in range(MAX_MEMORIES):
        store.save_memory(f"Synthetic filler {i}")
    functions, db = network_functions(root, records)
    functions["refresh_from_files"]()
    return records[0]["id"] not in db.deleted, {
        "locally_archived_fact_published_as_deleted": records[0]["id"] in db.deleted,
        "fact_still_locally_searchable": bool(store.search_memories("archive")),
    }


async def peer_update_parser(root):
    store = SkillStore(root)
    peer = SkillStore(root / "peer")
    peer.save_memory("Synthetic old preference")
    records = [{"id": "memory-v2:audit-peer:local", "kind": "fact", "deleted": False,
                "content": json.dumps(peer._facts.export())}]
    functions, _ = network_functions(root, records)
    functions["apply_to_files"]([records[0]["id"]])
    imported_id = store.memories()[0].id
    await synth(store, f'<memory id="{imported_id}">Synthetic corrected preference</memory>').consolidate()
    changed = any(m.text == "Synthetic corrected preference" for m in store.memories())
    return changed, {"namespaced_peer_fact_can_be_rewritten_by_curator": changed}


async def stalled_journal(root):
    from fluid.codemode.skills import JOURNAL_MAX_BYTES
    store = SkillStore(root)
    text = "Synthetic unprocessed conversation. " * 256
    for _ in range(JOURNAL_MAX_BYTES // len(text.encode("utf-8")) + 2):
        store.journal_append({"kind": "user", "text": text})
    dropped = store.reap_journal()
    size = (root / "journal.jsonl").stat().st_size
    return size <= JOURNAL_MAX_BYTES, {
        "unprocessed_bytes": size, "reap_threshold_bytes": JOURNAL_MAX_BYTES,
        "lines_dropped": dropped, "pending_entries": store.journal_count(),
    }


async def control_durable_store(root):
    store = SkillStore(root)
    saved = store.save_memory("Synthetic durable control", provenance="user")
    fresh = SkillStore(root)
    matches = fresh.search_memories("durable control")
    holds = len(matches) == 1 and matches[0][0].id == saved.id
    return holds, {"fresh_store_read_matches_saved_id": holds}


async def control_retention(root):
    from fluid.codemode.skills import MAX_ARCHIVED_MEMORIES
    store = SkillStore(root)
    for i in range(MAX_MEMORIES + MAX_ARCHIVED_MEMORIES + 2):
        store.save_memory(f"Synthetic retention control {i}")
    store.reap()
    live, archive = len(store.memories()), len(store.archived_memories())
    return live <= MAX_MEMORIES and archive <= MAX_ARCHIVED_MEMORIES, {
        "live_count": live, "archive_count": archive,
        "live_cap": MAX_MEMORIES, "archive_cap": MAX_ARCHIVED_MEMORIES,
    }


async def control_secret_rejection(root):
    store = SkillStore(root)
    outcome = await remember.handler(
        {"text": "The API key is sk-ant-api03-abcdefghijklmnop"}, journal_context(store)
    )
    return outcome.is_error and store.journal_count() == 0, {
        "tool_rejected_synthetic_credential": outcome.is_error,
        "journal_entries": store.journal_count(),
    }


CASES = [
    ("C01", "Direct store writes survive a fresh store read", control_durable_store),
    ("C02", "Local save and reap enforce live and archive caps", control_retention),
    ("C03", "Remember rejects a credential-shaped synthetic value", control_secret_rejection),
    ("M01", "An acknowledged explicit save is immediately durable", acknowledged_save),
    ("M02", "A model no-op cannot consume an unfulfilled explicit save", explicit_noop),
    ("M03", "Explicit identity survives ordinary context eviction", explicit_eviction),
    ("M04", "Repeated identical saves are idempotent", duplicate_retries),
    ("M05", "Forgetting reaches the archive", archived_forget),
    ("M06", "Tool-only provenance survives retrieval and archiving", trust_boundary),
    ("M07", "Concurrent successful saves retain both records", concurrent_saves),
    ("M08", "Model-only cleanup cannot delete an explicit fact", model_deletion),
    ("M09", "A rewrite of an unknown ID cannot insert a record", unknown_update),
    ("M10", "Project identity is portable across checkout paths", project_identity),
    ("M11", "Sync imports obey the same live capacity limit", sync_cap),
    ("M12", "Archiving is not published as forgetting", archive_tombstone),
    ("M13", "The curator can address namespaced peer IDs", peer_update_parser),
    ("M14", "An unprocessed journal has a hard storage bound", stalled_journal),
]


async def run():
    results = []
    for case_id, contract, case in CASES:
        with tempfile.TemporaryDirectory(prefix="fluid-memory-audit-") as directory:
            holds, observed = await case(Path(directory))
            results.append({"id": case_id, "contract": contract, "contract_holds": holds, "observed": observed})
    sources = ["fluid/fluid/codemode/skills.py", "fluid/fluid/codemode/continual.py",
               "fluid/fluid/codemode/facts.py", "fluid/fluid/codemode/transactions.py",
               "fluid/fluid/tools/builtins/remember.py", "fluid/fluid/tools/builtins/recall.py",
               "fluid/fluid/tools/builtins/forget.py", "app/core/fluid_memory.py",
               "app/core/session_modes.py", "app/core/provider/harness_adapter.py",
               "app/mcp/builtin_tools.py", "app/network/memory_records.py"]
    return {
        "audit_completed": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Synthetic isolated reproductions; no live UI, provider, database, or network transport exercised",
        "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources},
        "cases": results,
        "contracts_failed": sum(not r["contract_holds"] for r in results),
        "contracts_checked": len(results),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any proposed contract fails")
    args = parser.parse_args()
    report = asyncio.run(run())
    print(json.dumps(report, indent=2))
    sys.exit(1 if args.strict and report["contracts_failed"] else 0)
