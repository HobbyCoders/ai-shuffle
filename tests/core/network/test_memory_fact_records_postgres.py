# ruff: noqa: F811
"""A memory change syncs as one small per-fact record, not a whole-store copy (#1167).

Real PostgreSQL, independent worker schemas, real fact stores, the real
producer (``memory_records.publish``) and receiver (``apply_to_files``), and
signed exchange through the actual peer router. Capability support comes from
each worker's own signed presence, carried by the same exchange. Never the
configured application database.
"""
import json
import time

import pytest
from fluid.codemode.facts import FactStore
from fluid.codemode.skills import SkillStore

from app.db import network as store
from app.network import engine, membership, memory_records, startup, transport
from tests.core.network.test_transcript_op_retention_postgres import (  # noqa: F401
    admit,
    cluster,
    pg_workers,
    sync,
)
from tests.core.network.test_transport import ServedBy

SEED_FACTS = 30
FILLER = "the owner prefers concise, plain answers. " * 6


@pytest.fixture(autouse=True)
def no_scanner(monkeypatch):
    monkeypatch.setattr(transport, "request_scan", lambda *args, **kwargs: None)


@pytest.fixture
def touched(monkeypatch):
    """The memory record ids each sync applied: what the app hands ``apply_to_files``."""
    seen = []
    unsubscribe = engine.on_applied(lambda items: seen.extend(r for c, r, _op in items if c == "memory"))
    yield seen
    unsubscribe()


def facts(worker):
    return SkillStore(worker.data_dir / "fluid" / "skills")


def publish(worker):
    with worker.active():
        time.sleep(memory_records.WRITE_QUIET_SECONDS + .01)
        return memory_records.publish()


def records(worker, prefix):
    return {r["id"]: r for r in worker.sql(
        "SELECT id, content, deleted FROM network_memory_records WHERE id LIKE %s", (prefix + "%",))}


def own(worker, prefix):
    return records(worker, f"{prefix}{worker.device_id}:")


def texts(worker):
    with worker.active():
        return {m.text for m in facts(worker).memories()}


def presence(worker, *, capable=True):
    with worker.active():
        engine.publish_presence(capabilities=None if capable else {memory_records.CAPABILITY: 0})


async def receive(target, source, touched):
    """One real signed exchange from ``source``, then the app's apply step."""
    touched.clear()
    await sync(target, ServedBy(source, f"https://{source.name}.example.com"))
    ids = sorted(set(touched))
    with target.active():
        memory_records.apply_to_files(ids)
    return ids


async def pair(pg_workers, touched, *, b_capable=True):
    a, b = pg_workers("a"), pg_workers("b")
    with a.active():
        engine.create_network("Memory facts")
    admit(a, b)
    presence(a)
    presence(b, capable=b_capable)
    await receive(a, b, touched)
    await receive(b, a, touched)
    return a, b


async def test_one_fact_change_writes_one_small_record_when_every_worker_reads_them(pg_workers, touched):
    a, b = await pair(pg_workers, touched)
    with a.active():
        for n in range(SEED_FACTS):
            facts(a).save_memory(f"Seed fact {n}: " + FILLER, protected=True)
        snapshot_bytes = len(json.dumps(facts(a)._facts.export()))
    assert publish(a) == SEED_FACTS + 1  # every fact once, plus the barrier
    assert not own(a, memory_records.SNAPSHOT_PREFIX)
    before = {rid: r["content"] for rid, r in own(a, memory_records.FACT_PREFIX).items()}
    assert len(before) == SEED_FACTS
    assert len(await receive(b, a, touched)) == SEED_FACTS + 1
    assert len(texts(b)) == SEED_FACTS

    top = a.sql("SELECT MAX(ingest_id) AS n FROM sync_ops")[0]["n"]
    with a.active():
        facts(a).save_memory("One more thing to remember about deploys.", protected=True)
    assert publish(a) == 1
    new_ops = a.sql("SELECT record_id, collection FROM sync_ops WHERE ingest_id > %s", (top,))
    assert len(new_ops) == 1 and new_ops[0]["record_id"].startswith(memory_records.FACT_PREFIX)
    after = own(a, memory_records.FACT_PREFIX)
    changed = {rid for rid, r in after.items() if before.get(rid) != r["content"]}
    assert changed == {new_ops[0]["record_id"]}
    size = len(after[new_ops[0]["record_id"]]["content"])
    assert size < 2000 and size * 10 < snapshot_bytes  # the fact's size, not the store's
    # Nothing staged a whole-store record on this pass either.
    assert not own(a, memory_records.SNAPSHOT_PREFIX)

    ids = await receive(b, a, touched)
    assert ids == [new_ops[0]["record_id"]]
    assert "One more thing to remember about deploys." in texts(b)


@pytest.mark.parametrize("peer", ["old", "unknown"])
async def test_old_or_unknown_worker_keeps_the_whole_store_record(pg_workers, touched, peer):
    if peer == "old":
        a, b = await pair(pg_workers, touched, b_capable=False)
    else:
        a, b = pg_workers("a"), pg_workers("b")
        with a.active():
            engine.create_network("Memory facts")
        admit(a, b)  # B's presence never reaches A
    with a.active():
        facts(a).save_memory("Tabs, not spaces.", protected=True)
        facts(a).save_memory("Deploys go out on Tuesdays.", protected=True)
        export = facts(a)._facts.export()
    publish(a)
    snapshot = own(a, memory_records.SNAPSHOT_PREFIX)
    assert len(snapshot) == 1
    (row,) = snapshot.values()
    for entry in export["entries"]:
        entry.pop("uses", None)
        entry.pop("last_recalled_at", None)
    assert not row["deleted"] and json.loads(row["content"]) == export  # today's format
    assert len(own(a, memory_records.FACT_PREFIX)) == 2
    assert len(own(a, memory_records.BARRIER_PREFIX)) == 1

    # Once the old worker advertises support, the whole-store record is retracted.
    if peer == "old":
        presence(b)
        await receive(a, b, touched)
        with a.active():
            facts(a).save_memory("A third fact.", protected=True)
        publish(a)
        (row,) = own(a, memory_records.SNAPSHOT_PREFIX).values()
        assert row["deleted"] and row["content"] == ""


async def test_phone_member_does_not_need_whole_store_records(pg_workers, touched):
    a = pg_workers("a")
    with a.active():
        engine.create_network("Memory facts")
        store.upsert_member(device_id="dev_" + "0" * 24, name="Phone", kind=membership.KIND_CONSUMER,
                            signing_public="x", encryption_public="y", generation=0)
        facts(a).save_memory("Single worker with a phone.", protected=True)
    publish(a)
    assert not own(a, memory_records.SNAPSHOT_PREFIX)
    assert len(own(a, memory_records.FACT_PREFIX)) == 1


async def test_facts_converge_through_per_fact_and_barrier_records(pg_workers, touched):
    a, b = await pair(pg_workers, touched)
    with a.active():
        kept = facts(a).save_memory("The staging database is called orchid.", protected=True)
        gone = facts(a).save_memory("The old build host was retired.", protected=True)
        archived = facts(a).save_memory("A fact that will be archived.")
    publish(a)
    await receive(b, a, touched)
    assert texts(b) >= {"The staging database is called orchid.", "The old build host was retired.",
                        "A fact that will be archived."}

    # An edit wins by clock on B.
    with a.active():
        facts(a).save_memory("The staging database is called lotus.", kept.id, protected=True,
                             expected_revision=kept.revision)
    publish(a)
    await receive(b, a, touched)
    assert "The staging database is called lotus." in texts(b)
    assert "The staging database is called orchid." not in texts(b)

    # A Forget on A reaches B through the barrier record.
    with a.active():
        assert facts(a).delete_memory(gone.id, explicit=True, expected_revision=gone.revision)
    publish(a)
    ids = await receive(b, a, touched)
    assert f"{memory_records.BARRIER_PREFIX}{a.device_id}:" in " ".join(ids)
    assert "The old build host was retired." not in texts(b)
    # The forgotten fact's own record is retracted, never read as a deletion.
    retracted = [r for r in own(a, memory_records.FACT_PREFIX).values() if r["deleted"]]
    assert len(retracted) == 1 and retracted[0]["content"] == ""

    # A retired (archived) fact stays archived on the replica.
    with a.active():
        live = next(m for m in facts(a).memories() if m.id == archived.id)
        assert facts(a).retire_memory(archived.id, reason="test", expected_revision=live.revision)
    publish(a)
    await receive(b, a, touched)
    with b.active():
        doc = facts(b)._facts.snapshot()
    assert any(e["text"] == "A fact that will be archived." for e in doc["archived"])
    assert all(e["text"] != "A fact that will be archived." for e in doc["entries"])


async def test_receiver_merges_every_live_whole_store_record(pg_workers, touched):
    # A presence can lag a downgrade: skipping a capable publisher's live
    # whole-store record on old presence would never be reconsidered.
    a, b = await pair(pg_workers, touched)
    legacy_store = SkillStore(b.data_dir / "legacy-source")
    legacy_store.save_memory("From a worker on the old version.", protected=True)
    capable_store = SkillStore(b.data_dir / "capable-source")
    capable_store.save_memory("Only in a capable worker's whole-store record.", protected=True)
    old_device = "dev_" + "1" * 24
    owner_hash = memory_records._hash("local")
    with b.active():
        assert memory_records._supports(engine.all_presence().get(a.device_id))
        for device, source in ((old_device, legacy_store), (a.device_id, capable_store)):
            b.sql("INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted) "
                  "VALUES (%s, 'fact', 'global', %s, 'h', FALSE)",
                  (f"{memory_records.SNAPSHOT_PREFIX}{device}:{owner_hash}", json.dumps(source._facts.export())))
        assert memory_records.apply_to_files([f"{memory_records.SNAPSHOT_PREFIX}{d}:{owner_hash}"
                                              for d in (old_device, a.device_id)]) == 2
    assert "From a worker on the old version." in texts(b)
    assert "Only in a capable worker's whole-store record." in texts(b)


async def test_dropped_fact_retracts_only_the_record_and_does_not_delete_the_replica(pg_workers, touched):
    a, b = await pair(pg_workers, touched)
    with a.active():
        x = facts(a).save_memory("Fact X, edited later.", protected=True)
        y = facts(a).save_memory("Fact Y, dropped on A only.", protected=True)
        facts(a).save_memory("Fact Z.", protected=True)
    publish(a)
    await receive(b, a, touched)
    assert {"Fact X, edited later.", "Fact Y, dropped on A only.", "Fact Z."} <= texts(b)

    # A drops Y from its own file without a Forget (expiry, a cap, a local
    # cleanup): A's record is retracted, B's copy stays.
    root = a.data_dir / "fluid" / "skills" / "memories.json"
    with a.active():
        doc = json.loads(root.read_text(encoding="utf-8"))
        doc["entries"] = [e for e in doc["entries"] if e["id"] != y.id]
        root.write_text(json.dumps(doc), encoding="utf-8")
    assert publish(a) == 1
    (retracted,) = [r for r in own(a, memory_records.FACT_PREFIX).values() if r["deleted"]]
    await receive(b, a, touched)
    assert "Fact Y, dropped on A only." in texts(b)

    # B drops Y locally too. An unrelated change to X on A re-sends only X.
    root_b = b.data_dir / "fluid" / "skills" / "memories.json"
    with b.active():
        doc = json.loads(root_b.read_text(encoding="utf-8"))
        doc["entries"] = [e for e in doc["entries"] if "Fact Y" not in e["text"]]
        root_b.write_text(json.dumps(doc), encoding="utf-8")
    with a.active():
        facts(a).save_memory("Fact X, now edited.", x.id, protected=True, expected_revision=x.revision)
    assert publish(a) == 1
    ids = await receive(b, a, touched)
    assert len(ids) == 1 and ids[0].startswith(f"{memory_records.FACT_PREFIX}{a.device_id}:")
    assert ids[0] != retracted["id"]
    assert "Fact X, now edited." in texts(b)
    assert "Fact Y, dropped on A only." not in texts(b)


async def test_bad_record_does_not_block_the_rest_of_its_group(pg_workers, touched):
    a, b = await pair(pg_workers, touched)
    with a.active():
        facts(a).save_memory("A good fact.", protected=True)
        facts(a).save_memory("Another good fact.", protected=True)
    publish(a)
    await receive(b, a, touched)
    rows = own(a, memory_records.FACT_PREFIX)
    good = next(iter(rows.values()))
    payload = json.loads(good["content"])
    payload["entries"][0] = dict(payload["entries"][0], id="bad id with spaces", text="Bad.")
    other = SkillStore(b.data_dir / "other")
    other.save_memory("Fact from a third store.", protected=True)
    third = other._facts.export()
    third["entries"][0].pop("uses", None)
    ids = [f"{memory_records.FACT_PREFIX}dev_{'2' * 24}:h:bad", f"{memory_records.FACT_PREFIX}dev_{'2' * 24}:h:ok"]
    with b.active():
        for rid, content in zip(ids, (dict(payload, store_id=third["store_id"]),
                                      {**{k: third[k] for k in ("schema", "owner", "store_id")},
                                       "entries": third["entries"]})):
            b.sql("INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted) "
                  "VALUES (%s, 'fact', 'global', %s, 'h', FALSE)", (rid, json.dumps(content, sort_keys=True)))
        b.sql("INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted) "
              "VALUES (%s, 'fact', 'global', 'not json', 'h', FALSE)", (ids[0] + "-json",))
        assert memory_records.apply_to_files([*ids, ids[0] + "-json"]) == 1
    assert "Fact from a third store." in texts(b)
    assert "Bad." not in texts(b)
    # A refused record is never retried: it stays as bad as it was.
    assert memory_records._reconcile_owed is False


async def test_api_owner_store_routes_by_owner(pg_workers, touched):
    a, b = await pair(pg_workers, touched)
    owner = "api:user-1"
    path = a.data_dir / "fluid" / "skills" / ".users" / memory_records._hash(owner)
    with a.active():
        SkillStore(path, owner=owner).save_memory("An API user's own fact.", protected=True)
    publish(a)
    assert any(rid.startswith(f"{memory_records.FACT_PREFIX}{a.device_id}:{memory_records._hash(owner)}:")
               for rid in own(a, memory_records.FACT_PREFIX))
    await receive(b, a, touched)
    with b.active():
        replica = SkillStore(b.data_dir / "fluid" / "skills" / ".users" / memory_records._hash(owner), owner=owner)
        assert "An API user's own fact." in {m.text for m in replica.memories()}
        assert "An API user's own fact." not in {m.text for m in facts(b).memories()}


async def test_state_copy_notice_materializes_every_fact_record(pg_workers, touched):
    a, b = await pair(pg_workers, touched)
    with a.active():
        for n in range(3):
            facts(a).save_memory(f"Fact {n} copied by a state copy.", protected=True)
    publish(a)
    touched.clear()
    await sync(b, ServedBy(a, "https://a.example.com"))
    assert not {t for t in texts(b) if "state copy" in t}  # rows only, files untouched
    with b.active():
        # What a finished state copy hands the app: the collection, no ids.
        assert memory_records.apply_to_files([""]) == 4
    assert {f"Fact {n} copied by a state copy." for n in range(3)} <= texts(b)


async def test_startup_reconcile_writes_records_committed_without_a_notice(pg_workers, touched):
    """A crash after the commit and before the apply: no notice comes again."""
    a, b = await pair(pg_workers, touched)
    with a.active():
        gone = facts(a).save_memory("Forgotten while B was down.", protected=True)
        dropped = facts(a).save_memory("Dropped on A without a Forget.", protected=True)
    publish(a)
    await receive(b, a, touched)
    assert "Forgotten while B was down." in texts(b)
    with a.active():
        facts(a).save_memory("Committed on B, never written.", protected=True)
        assert facts(a).delete_memory(gone.id, explicit=True, expected_revision=gone.revision)
    publish(a)
    touched.clear()
    await sync(b, ServedBy(a, "https://a.example.com"))
    assert {i.split(":")[0] + ":" for i in touched} >= {memory_records.FACT_PREFIX, memory_records.BARRIER_PREFIX}
    assert "Committed on B, never written." not in texts(b)  # rows only, files untouched
    with b.active():
        await startup.reconcile_memory()  # what network start runs
    assert "Committed on B, never written." in texts(b)
    assert "Forgotten while B was down." not in texts(b)  # the barrier arrived too

    # A worker's own records never flow back into its own store: a fact it
    # dropped before its next publish pass stays dropped.
    root = a.data_dir / "fluid" / "skills" / "memories.json"
    with a.active():
        doc = json.loads(root.read_text(encoding="utf-8"))
        doc["entries"] = [e for e in doc["entries"] if e["id"] != dropped.id]
        root.write_text(json.dumps(doc), encoding="utf-8")
        assert any(not r["deleted"] for r in own(a, memory_records.FACT_PREFIX).values()
                   if "Dropped on A" in r["content"])
        await startup.reconcile_memory()
    assert "Dropped on A without a Forget." not in texts(a)


async def test_failed_write_is_retried_on_the_next_publish_pass(pg_workers, touched, monkeypatch):
    a, b = await pair(pg_workers, touched)
    real = FactStore.merge
    broken = [True]

    def merge(self, snapshot):
        if broken[0]:
            raise OSError("disk briefly unavailable")
        return real(self, snapshot)

    monkeypatch.setattr(FactStore, "merge", merge)
    with a.active():
        facts(a).save_memory("Arrives despite a failed write.", protected=True)
    publish(a)
    ids = await receive(b, a, touched)
    assert ids and "Arrives despite a failed write." not in texts(b)
    assert memory_records._reconcile_owed is True
    broken[0] = False
    before = {r["id"]: r["content"] for r in b.sql("SELECT id, content FROM network_memory_records")}
    publish(b)  # the record itself has not changed
    assert {i: before[i] for i in ids} == {r["id"]: r["content"] for r in b.sql(
        "SELECT id, content FROM network_memory_records WHERE id = ANY(%s)", (ids,))}
    assert "Arrives despite a failed write." in texts(b)
    assert memory_records._reconcile_owed is False
