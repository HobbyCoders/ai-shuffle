"""Both legs carry the same when-line # Memory snapshot.

The adapter renders the snapshot (native reminder and the delegated CLI's
note alike) from the store's own render_memory_section, and the task slice
rotates index lines from the same plan, so ALWAYS memories, index lines and
the Recall-by-id header reach either model unchanged.
"""
from __future__ import annotations

from fluid.codemode.skills import ALWAYS_HEADING, SkillStore

import app.core.memory_reminder as mr
import app.core.provider.harness_adapter as ha
from app.core.provider.types import SessionOptions

ROUTING = "Code work goes to GPT 6.1 Sol; reviews and frontend go to Opus."
DEPLOY = "Deploy the hub before the site, from main only. Verify each one live."


def _adapter(monkeypatch, store, project):
    monkeypatch.setattr("app.core.fluid_memory.session_memory", lambda *a: (store, project))
    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="m", cwd=".", extra={"app_session_id": "app-sess-1"})
    return adapter


def test_adapter_snapshot_is_the_store_render_with_always_and_index(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    always = store.save_memory(ROUTING, protected=True, always=True,
                               when="When choosing a model for a subagent")
    deploy = store.save_memory(DEPLOY, protected=True, scope="project:x",
                               when="When deploying the hub or website")
    adapter = _adapter(monkeypatch, store, "project:x")
    section = adapter._memory_section()
    assert section == store.render_memory_section("project:x", checkout=".")
    assert ALWAYS_HEADING in section and f"- [{always.id} r1; session] {ROUTING}" in section
    assert f"- [{deploy.id} r1; session] When deploying the hub or website" in section
    assert DEPLOY not in section and "Recall(ids=[...])" in section
    note = adapter._memory_snapshot()
    assert note == f"<system-reminder>\n{section}\n</system-reminder>"
    # Index lines keep the id/revision shape change lists track facts by.
    ids = {mr._fact_id(item) for item in mr.section_items(section)} - {None}
    assert {always.id, deploy.id} <= ids


def test_task_slice_rotates_index_lines_not_full_text(tmp_path, monkeypatch):
    from fluid.codemode import facts

    monkeypatch.setattr(facts, "MAX_PROMPT_CHARS", 160)
    store = SkillStore(tmp_path)
    for i in range(4):
        store.save_memory(f"Unrelated convention {i}. Detail that stays in Recall.",
                          protected=True, when=f"When handling unrelated case {i}")
    target = store.save_memory("Billing totals round half-even. Detail that stays in Recall.",
                               protected=True, when="When computing billing currency totals")
    for i in range(4, 8):
        store.save_memory(f"Unrelated convention {i}. Detail that stays in Recall.",
                          protected=True, when=f"When handling unrelated case {i}")
    adapter = _adapter(monkeypatch, store, None)
    base = adapter._memory_section()
    eligible = adapter._memory_eligible_lines()
    assert target.id not in base
    sliced = mr.task_memory_section(base, None, eligible, "billing currency totals")
    added = sliced[len(base):]
    assert f"[{target.id} r1; session; task-match] When computing billing currency totals" in added
    assert "Detail that stays in Recall" not in sliced
