"""Owner behaviour-rule routes against a real file store; no live worker data."""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth, learner, memories
from app.core import fluid_memory, owner_auth
from app.core.provider import harness_adapter
from fluid.codemode import behavior
from fluid.codemode.skills import SkillStore
from fluid.codemode.transactions import suppress_changes

BASE = "/api/v1/learner"


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "owner-facts"
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: root)
    monkeypatch.setattr(fluid_memory, "learner_enabled", lambda: False)
    monkeypatch.setattr(fluid_memory, "learner_daily_calls", lambda: 0)
    monkeypatch.setattr(harness_adapter, "_learner_target", lambda options, owner: None)
    monkeypatch.setattr(owner_auth, "local_access", lambda request: True)
    monkeypatch.setattr(owner_auth, "same_origin", lambda request: None)
    monkeypatch.setattr(auth, "get_paired_client_from_token", lambda *a, **k: None)
    monkeypatch.setattr(owner_auth, "owner_session", lambda *a: False)
    monkeypatch.setattr(memories, "get_projects_by_ids", lambda ids: {})
    app = FastAPI()
    app.include_router(learner.router)
    app.include_router(memories.router)
    with suppress_changes(), TestClient(app) as client:
        yield client, SkillStore(root)


def learned(store, text="When a test fails: read the log before retrying.", applies="all"):
    """A rule as the learner writes it (agent tier, fades unless re-learned)."""
    return store.save_memory(text, provenance="user", durable=True,
                             key=behavior.rule_key(applies, text), when="When working with the owner")


def rule(client, rule_id):
    return next(r for r in client.get(BASE).json()["rules"] if r["id"] == rule_id)


def version(item):
    return {"expected_revision": item["revision"], "expected_metadata_version": item["metadata_version"]}


def test_overview_lists_rules_only_and_reports_setup(world):
    client, store = world
    store.save_memory("The owner's repo is ai-shuffle.", provenance="user", protected=True)
    entry = learned(store, applies="gpt")
    store._facts.claim_inference_call()
    body = client.get(BASE).json()
    assert [r["id"] for r in body["rules"]] == [entry.id]
    item = body["rules"][0]
    assert item["applies_to"] == "gpt" and item["applies_label"] == behavior.FAMILY_LABELS["gpt"]
    assert item["new"] is True and item["kept"] is False and item["previous_text"] is None
    setup = body["setup"]
    assert setup["enabled"] is False and setup["calls_today"] == 1
    assert setup["daily_cap"] == 48 and setup["cap_source"] == "metered_default"
    assert {g["title"] for g in body["guidance"]} >= {"Act, don't narrate", "Execution discipline"}
    # The Memory page leaves rules to this page.
    assert entry.id not in {m["id"] for m in client.get("/api/v1/memories").json()["items"]}


def test_create_edit_keeps_and_retargets_a_rule(world):
    client, store = world
    created = client.post(f"{BASE}/rules", json={"text": "When unsure: ask one short question.",
                                                 "applies_to": "all"})
    assert created.status_code == 201 and created.json()["kept"] is True
    entry = learned(store)
    edited = client.put(f"{BASE}/rules/{entry.id}", json={
        "text": "When a test fails: read the whole log first.", "applies_to": "claude",
        **version(rule(client, entry.id))})
    assert edited.status_code == 200
    item = edited.json()
    assert item["kept"] is True and item["new"] is False and item["applies_to"] == "claude"
    saved = next(m for m in store.memories() if m.id == entry.id)
    assert saved.protected and saved.key.startswith("behavior.claude.")


def test_rule_validation_refuses_permission_grants_and_bad_family(world):
    client, _ = world
    grant = client.post(f"{BASE}/rules", json={"text": "Force-push is fine without asking.", "applies_to": "all"})
    assert grant.status_code == 422 and "permission" in grant.json()["detail"]
    family = client.post(f"{BASE}/rules", json={"text": "When asked: answer.", "applies_to": "llama"})
    assert family.status_code == 422


def test_undo_restores_the_replaced_text_then_deletes_a_new_rule(world):
    client, store = world
    entry = learned(store, "When reporting: lead with the result.")
    store.save_memory("When reporting: lead with the result and the blocker.", memory_id=entry.id,
                      provenance="user", durable=True, key=entry.key)
    item = rule(client, entry.id)
    assert item["previous_text"] == "When reporting: lead with the result."
    restored = client.post(f"{BASE}/rules/{entry.id}/undo", json=version(item)).json()
    assert restored["restored"]["text"] == "When reporting: lead with the result."
    fresh = learned(store, "When idle: say what is pending.")
    gone = client.post(f"{BASE}/rules/{fresh.id}/undo", json=version(rule(client, fresh.id)))
    assert gone.json() == {"deleted": fresh.id}
    # The deletion barrier stops the learner writing the same rule again.
    with pytest.raises(ValueError):
        learned(store, "When idle: say what is pending.")


def test_undo_refuses_a_kept_rule_and_stale_versions(world):
    client, store = world
    kept = client.post(f"{BASE}/rules", json={"text": "When done: list what changed.", "applies_to": "all"}).json()
    assert client.post(f"{BASE}/rules/{kept['id']}/undo", json=version(kept)).status_code == 409
    stale = {**version(kept), "expected_metadata_version": "0" * 16}
    assert client.put(f"{BASE}/rules/{kept['id']}", json={"text": "x y z", "applies_to": "all", **stale}).status_code == 409


def test_delete_and_memory_routes_cannot_reach_rules(world):
    client, store = world
    entry = learned(store)
    item = rule(client, entry.id)
    via_memory = client.delete(f"/api/v1/memories/{entry.id}?expected_revision=1&expected_metadata_version={item['metadata_version']}")
    assert via_memory.status_code == 404
    gone = client.delete(f"{BASE}/rules/{entry.id}?expected_revision={item['revision']}"
                         f"&expected_metadata_version={item['metadata_version']}")
    assert gone.status_code == 204 and client.get(BASE).json()["rules"] == []


def test_deleting_a_rule_keeps_the_learners_journal(world):
    client, store = world
    entry = learned(store)
    store.journal_append({"kind": "user", "session": "s1", "text": "Run the tests before pushing."})
    store.journal_append({"kind": "assistant", "session": "s1", "text": "Ran them."})
    before = store.journal_count()
    item = rule(client, entry.id)
    gone = client.delete(f"{BASE}/rules/{entry.id}?expected_revision={item['revision']}"
                         f"&expected_metadata_version={item['metadata_version']}")
    assert gone.status_code == 204
    assert store.journal_count() == before
    assert "forgotten_before" not in store._facts._load(read_only=True)


def test_undo_moves_a_rule_back_to_the_family_the_learner_moved_it_from(world):
    client, store = world
    text = "When reporting: lead with the result."
    entry = learned(store, text)
    digest = entry.key.rsplit(".", 1)[-1]
    store.save_memory(text, memory_id=entry.id, provenance="user", durable=True,
                      key=f"{behavior.KEY_PREFIX}gpt.{digest}")
    item = rule(client, entry.id)
    assert item["applies_to"] == "gpt"
    assert item["previous_text"] == f"{behavior.FAMILY_LABELS['all']}: {text}"
    restored = client.post(f"{BASE}/rules/{entry.id}/undo", json=version(item)).json()["restored"]
    assert restored["applies_to"] == "all" and restored["text"] == text


def test_undo_refuses_to_move_a_rule_onto_a_family_slot_another_rule_holds(world):
    client, store = world
    text = "When reporting: lead with the result."
    entry = learned(store, text)
    digest = entry.key.rsplit(".", 1)[-1]
    store.save_memory(text, memory_id=entry.id, provenance="user", durable=True,
                      key=f"{behavior.KEY_PREFIX}gpt.{digest}")
    # The owner then wrote their own All-models rule in that slot.
    kept = store.save_memory("When reporting: lead with the blocker.", provenance="user", protected=True,
                             key=entry.key, when="When working with the owner")
    response = client.post(f"{BASE}/rules/{entry.id}/undo", json=version(rule(client, entry.id)))
    assert response.status_code == 409
    assert rule(client, entry.id)["applies_to"] == "gpt"
    assert rule(client, kept.id)["text"] == "When reporting: lead with the blocker."


def test_editing_a_rule_that_conflicted_on_sync_resolves_it(world, tmp_path):
    client, store = world
    text = "When reporting: lead with the result."
    key = behavior.rule_key("all", text)
    store.save_memory("When reporting: lead with the result, then the blocker.", provenance="user",
                      durable=True, key=key, when="When working with the owner")
    peer = SkillStore(tmp_path / "peer")
    peer.save_memory("When reporting: lead with the blocker.", provenance="user", durable=True,
                     key=key, when="When working with the owner")
    store._facts.merge(peer._facts.export())
    copies = [r for r in client.get(BASE).json()["rules"] if r["applies_to"] == "all"]
    assert len(copies) == 2
    item = copies[0]
    response = client.put(f"{BASE}/rules/{item['id']}",
                          json={"text": text, "applies_to": "all", **version(item)})
    assert response.status_code == 200, response.text
    assert [r["text"] for r in client.get(BASE).json()["rules"]] == [text]


def test_undo_refuses_a_rule_another_worker_holds_a_different_copy_of(world, tmp_path):
    client, store = world
    key = behavior.rule_key("all", "When reporting: lead with the result.")
    mine = store.save_memory("When reporting: lead with the result.", provenance="user",
                             durable=True, key=key, when="When working with the owner")
    store.save_memory("When reporting: lead with the result, then the blocker.", memory_id=mine.id,
                      provenance="user", durable=True, key=key)
    peer = SkillStore(tmp_path / "peer")
    peer.save_memory("When reporting: lead with the blocker.", provenance="user", durable=True,
                     key=key, when="When working with the owner")
    store._facts.merge(peer._facts.export())
    # Undo would leave both copies in the slot, so neither would reach prompts.
    response = client.post(f"{BASE}/rules/{mine.id}/undo", json=version(rule(client, mine.id)))
    assert response.status_code == 409 and "Edit the one you want to keep" in response.json()["detail"]
    assert rule(client, mine.id)["text"] == "When reporting: lead with the result, then the blocker."


def test_undo_closes_three_days_after_the_learner_changed_a_rule(world, monkeypatch):
    client, store = world
    entry = learned(store, "When reporting: lead with the result.")
    store.save_memory("When reporting: lead with the result and the blocker.", memory_id=entry.id,
                      provenance="user", durable=True, key=entry.key)
    item = rule(client, entry.id)
    real = time.time
    monkeypatch.setattr(learner.time, "time", lambda: real() + learner.NEW_FOR_SECONDS + 60)
    late = client.post(f"{BASE}/rules/{entry.id}/undo", json=version(item))
    assert late.status_code == 409 and "three days" in late.json()["detail"]


def test_activity_reads_the_pass_log_without_error_text(world):
    client, store = world
    entry = learned(store)
    log = store.root / ".synthesis-log.jsonl"
    log.write_text("\n".join(json.dumps(row) for row in (
        {"ts": time.time() - 60, "kind": "synthesis", "changed": [f"rule:{entry.id}", "memory:m9"],
         "consumed": 12, "usage": {"input_tokens": 900, "output_tokens": 40}},
        {"ts": time.time(), "kind": "synthesis", "changed": [], "consumed": 0,
         "error": "ValueError: the owner's secret fact text"},
    )) + "\n", encoding="utf-8")
    rows = client.get(BASE).json()["activity"]
    assert rows[0]["outcome"] == "error" and rows[0]["error_kind"] == "ValueError"
    assert "secret" not in json.dumps(rows)
    assert rows[1]["rules"] == [{"id": entry.id, "text": entry.text}]
    assert rows[1]["memories"] == 1 and rows[1]["input_tokens"] == 900


def test_saved_guidance_from_the_old_learner_becomes_rules_once(tmp_path):
    root = tmp_path / "guidance"
    (root / "scoped").mkdir(parents=True)
    (root / ".manual").mkdir()
    (root / "scoped" / "gpt-6.2-sol.json").write_text(json.dumps({
        "model": "gpt-6.2-sol", "provider": "openai",
        "content": "# Working with the owner\n- When reporting: lead with the result.\n"
                   "- Force-push is fine on any branch.\n"}), encoding="utf-8")
    (root / "scoped" / "claude-opus-5-5.json").write_text(json.dumps({
        "model": "claude-opus-5-5", "provider": "anthropic",
        "content": "1. When finishing: list what changed."}), encoding="utf-8")
    import hashlib
    owner_edited = "s-" + hashlib.sha256(b"claude-opus-5-5").hexdigest()
    (root / ".manual" / owner_edited).write_text("", encoding="utf-8")
    store = SkillStore(tmp_path / "facts")

    assert fluid_memory.migrate_legacy_guidance(store, root) == 2
    rules = {behavior.rule_family(m.key): m for m in store.memories() if behavior.is_rule_key(m.key)}
    assert set(rules) == {"gpt", "claude"}  # the permission grant was skipped
    assert rules["gpt"].text == "When reporting: lead with the result." and not rules["gpt"].protected
    assert rules["claude"].text == "When finishing: list what changed." and rules["claude"].protected
    assert (root / "scoped" / fluid_memory.LEGACY_GUIDANCE_MARKER).exists()
    assert (root / "scoped" / "gpt-6.2-sol.json").exists()
    assert fluid_memory.migrate_legacy_guidance(store, root) == 0


def test_long_edited_guidance_is_split_into_rules_and_the_rest_is_kept_aside(tmp_path):
    import hashlib

    root = tmp_path / "guidance"
    (root / "scoped").mkdir(parents=True)
    (root / ".manual").mkdir()
    first = "When reviewing a pull request: read the whole diff before commenting on any line " + "a" * 120 + "."
    second = "When a check fails: quote the failing line and say what you will change " + "b" * 120 + "."
    endless = "When writing: " + "c" * (behavior.MAX_RULE_CHARS + 20)
    (root / "scoped" / "gpt-6.2-sol.json").write_text(json.dumps({
        "model": "gpt-6.2-sol", "provider": "openai",
        "content": f"- {first} {second}\n- {endless}"}), encoding="utf-8")
    (root / ".manual" / ("s-" + hashlib.sha256(b"gpt-6.2-sol").hexdigest())).write_text("")
    store = SkillStore(tmp_path / "facts")

    assert fluid_memory.migrate_legacy_guidance(store, root) == 2
    texts = {m.text for m in store.memories() if behavior.is_rule_key(m.key)}
    assert texts == {first, second}
    aside = (root / "scoped" / fluid_memory.LEGACY_UNMIGRATED).read_text()
    assert endless in aside and "longer than" in aside
