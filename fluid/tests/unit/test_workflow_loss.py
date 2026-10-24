"""Workflow evidence loss survives prefix reaping, restarts and legacy tails."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from fluid.codemode.continual import Synthesizer, _SYNTH_SYSTEM
from fluid.codemode.journal import ToolJournal
from fluid.codemode.skills import SkillStore, project_key_for
from fluid.codemode.workflow_loss import load_loss, session_lost
from fluid.codemode.workflows import (
    WorkflowProposals, _content, _evidence, _hash, repeated_evidence,
)
from .test_workflow_proposals import journal_success, output


@pytest.mark.asyncio
async def test_reaped_correction_before_later_request_blocks_learning_and_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid.codemode import skills
    cwd = tmp_path / "project"
    cwd.mkdir()
    project = project_key_for(cwd)
    store = SkillStore(tmp_path / "store")
    store.journal_context(session_id="first", project=project)
    ToolJournal(store).record_event("user", "workflow corrected: fixture-summary")
    await journal_success(store, cwd, "first")
    await journal_success(store, cwd, "second")
    entries = store.journal_entries()
    body = json.loads(output().removeprefix("<workflow>").removesuffix("</workflow>"))
    # Model an existing proposal produced by the old suffix-only validator.
    queue = WorkflowProposals(store)
    identity = {"owner": queue.owner, "project": project, "content": _content(body)}
    row = {
        **identity, "id": _hash(identity), "name": body["name"],
        "description": body["description"], "status": "pending",
        "evidence": _evidence(entries[1:], ["first", "second"], body["name"], project),
    }
    queue._write([row])
    store.set_synth_cursor(1)
    monkeypatch.setattr(skills, "JOURNAL_KEEP_ENTRIES", 8)
    assert store.reap_journal() == 1
    # The bug: the surviving first row is another genuine user request, and
    # both execution/completion/acceptance sequences remain byte-for-byte.
    retained = store.journal_entries()
    assert retained[0]["kind"] == "user" and "Summarize" in retained[0]["text"]
    assert row["evidence"] == _evidence(retained, ["first", "second"], body["name"], project)
    store = SkillStore(store.root)  # completeness must outlive the process
    lost = load_loss(store.root)
    assert session_lost(lost, project, "first")
    assert not session_lost(lost, project, "second")
    assert repeated_evidence(retained, retained[-4:], project, lost=lost) == []
    synth = Synthesizer(None, "fixture", store, project=project)
    assert synth._synthesis_input(retained[-4:])[1:] == (_SYNTH_SYSTEM, [])
    with pytest.raises(ValueError, match="incomplete"):
        WorkflowProposals(store).propose(body, retained, project)
    with pytest.raises(ValueError, match="incomplete"):
        WorkflowProposals(store).promote(row["id"], cwd, approved_digest=row["id"])
    assert not (cwd / ".fluid" / "skills").exists()


@pytest.mark.asyncio
async def test_unrelated_reap_keeps_complete_new_sessions_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid.codemode import skills
    store = SkillStore(tmp_path / "store")
    store.journal_context(session_id="old", project="old-project")
    ToolJournal(store).record_event("user", "Discard this old fixture session.")
    cwd = tmp_path / "project"
    cwd.mkdir()
    await journal_success(store, cwd, "first")
    await journal_success(store, cwd, "second")
    store.set_synth_cursor(1)
    monkeypatch.setattr(skills, "JOURNAL_KEEP_ENTRIES", 8)
    assert store.reap_journal() == 1
    entries = store.journal_entries()
    evidence = repeated_evidence(
        entries, entries[-4:], project_key_for(cwd), lost=load_loss(store.root),
    )
    assert len(evidence) == 8


def test_loss_metadata_failure_preserves_journal_before_trim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid.codemode import skills, workflow_loss
    store = SkillStore(tmp_path)
    store.journal_context(session_id="first", project="fixture")
    for text in ("workflow corrected: fixture-summary", "Later request"):
        ToolJournal(store).record_event("user", text)
    store.set_synth_cursor(1)
    monkeypatch.setattr(skills, "JOURNAL_KEEP_ENTRIES", 1)
    def fail(*args: object) -> None:
        raise OSError("fixture persistence failure")
    monkeypatch.setattr(workflow_loss, "_save", fail)
    assert store.reap_journal() == 0
    assert len(store.journal_entries()) == 2
    assert store.synth_cursor() == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows does not support directory fsync")
def test_directory_sync_failure_prevents_trim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid.codemode import skills, workflow_loss
    store = SkillStore(tmp_path)
    store.journal_context(session_id="first", project="fixture")
    for text in ("workflow corrected: fixture-summary", "Later request"):
        ToolJournal(store).record_event("user", text)
    store.set_synth_cursor(1)
    monkeypatch.setattr(skills, "JOURNAL_KEEP_ENTRIES", 1)
    real_fsync = os.fsync
    synced_directories = []

    def fail_directory_sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synced_directories.append(descriptor)
            raise OSError("fixture directory sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(workflow_loss.os, "fsync", fail_directory_sync)
    assert store.reap_journal() == 0
    assert synced_directories  # temp-file sync succeeded; directory sync failed
    assert len(store.journal_entries()) == 2
    assert store.synth_cursor() == 1
    monkeypatch.setattr(workflow_loss.os, "fsync", real_fsync)
    assert store.reap_journal() == 1  # retry must sync even unchanged loss bits


def test_legacy_tail_is_not_assumed_complete(tmp_path: Path) -> None:
    tmp_path.joinpath("journal.jsonl").write_text(json.dumps({
        "kind": "user", "text": "A later request", "project": "fixture", "session": "legacy",
    }) + "\n", encoding="utf-8")
    store = SkillStore(tmp_path)
    assert session_lost(load_loss(store.root), "fixture", "legacy")
    assert not session_lost(load_loss(store.root), "fixture", "new")


@pytest.mark.parametrize("contents", [None, "{}", '{"version": 1, "bits": "bad"}', "x" * 17000])
def test_missing_or_corrupt_loss_metadata_fails_closed(
    tmp_path: Path, contents: str | None,
) -> None:
    store = SkillStore(tmp_path)
    path = store.root / ".workflow-loss.json"
    if contents is None:
        path.unlink()  # only the fixture-owned file
    else:
        path.write_text(contents, encoding="utf-8")
    assert session_lost(load_loss(store.root), "fixture", "any-session")


def test_loss_filter_is_fixed_size_monotone_and_content_free(tmp_path: Path) -> None:
    from fluid.codemode.workflow_loss import record_loss
    store = SkillStore(tmp_path)
    lines = []
    for index in range(1000):
        # One call models a trim; no raw names/content may enter the sidecar.
        lines.append(json.dumps({"project": "fixture", "session": f"private-session-{index}",
                                 "text": "private workflow text"}))
    record_loss(store.root, lines)
    first = load_loss(store.root)
    record_loss(store.root, [json.dumps({"project": "fixture", "session": "last"})])
    second = load_loss(store.root)
    assert first & second == first
    for index in range(1000):
        assert session_lost(second, "fixture", f"private-session-{index}")
    text = (store.root / ".workflow-loss.json").read_text()
    assert "private" not in text and len(text) < 16512
