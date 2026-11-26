"""Memory v3 phase 2a: write-time guards in Remember.

* Task progress (a PR, issue or branch reference with progress wording) is
  refused for an agent's save and kept, with a note, for the owner's.
* A new save's receipt names the most similar live facts by id, revision and
  when-line, above a threshold the invented store below separates cleanly.
"""
from __future__ import annotations

import pytest

from fluid.codemode import facts
from fluid.codemode.skills import SkillStore
from fluid.codemode.task_state import task_progress
from fluid.tools.builtins.remember import handler as remember
from fluid.tools.registry import ToolCtx

from tests.fixtures.memory_similar import NEW_SUBJECTS, RESTATED, STORE


def ctx(store):
    return ToolCtx(extra={"memory_store": store, "recall_store": store})


# ─── task-state guard ────────────────────────────────────────────────────────

# (text, when) an agent might try to save that is really where a task stands.
PROGRESS = [
    ("Continuing the foldable cover-screen scroll fix in PR #1824: the gesture test still "
     "needs a Robolectric run.", "When continuing the cover-screen scroll fix"),
    ("Left off on feat/usage-panel-tabs after splitting the store; the next step is the "
     "Settings link.", "When resuming the usage panel work"),
    ("Issue 412 is in progress: the parser rewrite is half done and the CLI flag is still "
     "missing.", "When working on the parser rewrite"),
    ("WIP: PR 77 waiting on CI before the docs pass.", "When picking up PR 77"),
    ("https://github.com/example/widgets/pull/301 is not yet merged; remaining work is the "
     "migration test.", "When checking the widgets migration"),
    ("Session notes for the cache refactor live in the WorkingRecord.",
     "When continuing the work on fix/cache-keys"),
    # Terse continuation: the verb names the reference directly.
    ("Continuing PR #1824", "When picking up the cover-screen fix"),
    ("Resuming #2210 after the rebase.", "When the importer rebase is done"),
    # A branch named as one, whatever its prefix, anchors the whole fact.
    ("On branch docs/update-readme; next step is to check the links.",
     "When checking the readme links"),
    # A status whose subject is the referenced work.
    ("Issue #88 is in progress; the importer is still missing a test.",
     "When working on the importer"),
    # The when-line names the work, so the text's status is about it.
    ("The gesture fix is half done.", "When working on PR #1824"),
    # A continuation naming a full PR link.
    ("Continuing https://github.com/example/widgets/pull/1824", "When resuming the widgets fix"),
    # "On branch <name>" with a single-component name.
    ("On branch main; next step is to rerun CI.", "When rerunning CI"),
    # A reference opening a clause, its status after the colon.
    ("PR #1824: next step is to add the scroll test.", "When adding the scroll test"),
    # A bare "waiting on CI" whose subject is the reference.
    ("PR 77 waiting on CI before the docs pass.", "When checking the docs pass"),
]

# Lasting rules that cite a PR, issue or branch, or use progress-like words
# without one: all must save.
LASTING = [
    ("Since PR #1909 the pre-push hook refuses a push that skipped the secret scan; never "
     "pass --no-verify.", "Before pushing a branch"),
    ("Merge with plain gh pr merge, never --delete-branch (#1650): sibling worktrees make "
     "the delete fail after the merge landed.", "When merging a pull request"),
    ("Since #1958 a PR can be watched; the watch ends by itself when the PR is merged or "
     "closed.", "When the owner asks an agent to follow a PR"),
    ("The merge gate refuses pull requests titled WIP since PR #1800; mark them draft "
     "instead.", "When opening a pull request"),
    ("Pull request bodies end with a Running-app checks section (added in #1712).",
     "When writing a pull request description"),
    ("Docs pages live under docs/content and their frontend mirror; see issue #88 for the "
     "rationale.", "When changing user documentation"),
    ("When a CI job is still failing on an unrelated PR, re-run it once before "
     "investigating.", "When a CI job fails on an unrelated pull request"),
    ("The next step after a release tag is publishing the nightly notes.",
     "When cutting a release"),
    ("C# projects build with dotnet 8 and the entity &#123; is escaped in XML docs.",
     "When building the C# tools"),
    # A generic condition in a rule is not the cited issue's status.
    ("Never merge while a migration is in progress; see issue #88.",
     "When merging a pull request"),
    ("Don't deploy while a migration is in progress (issue #88 explains why).",
     "When deploying the server"),
    ("PR #1800 made the merge gate refuse branches whose checks are still failing.",
     "When a pull request is blocked by the merge gate"),
    # A docs/ path is not a branch.
    ("Keep docs/update-readme.md short; the next step after editing it is the link check.",
     "When editing the readme"),
    # Near the reference, but the reference is not the status's subject.
    ("Issue #88 explains why migrations in progress block deploys.",
     "When deploying during a migration"),
    # A named branch as recurring context in the when-line anchors nothing.
    ("Never deploy while waiting for CI.", "When deploying on branch main"),
    # "On branch creation" is prose, not a branch called "creation".
    ("On branch creation, the next step after naming the branch is to set its upstream "
     "before the first push.", "When creating a branch"),
]


@pytest.mark.parametrize("text, when", PROGRESS)
def test_progress_is_detected(text, when):
    assert task_progress(text, when), text


@pytest.mark.parametrize("text, when", LASTING)
def test_lasting_rules_are_not_progress(text, when):
    assert task_progress(text, when) is None, text


async def test_agent_progress_save_is_refused_and_nothing_is_stored(tmp_path):
    store = SkillStore(tmp_path)
    text, when = PROGRESS[0]
    out = await remember({"text": text, "when": when}, ctx(store))
    assert out.is_error and out.text.startswith("Not saved:")
    assert "task progress" in out.text and "WorkingRecord" in out.text and "Note" in out.text
    assert store.memories() == []


async def test_agent_lasting_rule_citing_a_pr_saves(tmp_path):
    store = SkillStore(tmp_path)
    text, when = LASTING[0]
    out = await remember({"text": text, "when": when}, ctx(store))
    assert not out.is_error and out.text.startswith("Saved [")
    assert "task progress" not in out.text
    assert [m.text for m in store.memories()] == [text]


async def test_owner_progress_save_is_kept_with_a_note(tmp_path):
    store = SkillStore(tmp_path)
    text, when = PROGRESS[1]
    out = await remember({"text": text, "when": when, "user_requested": True}, ctx(store))
    assert not out.is_error and out.text.startswith("Saved [")
    assert "Kept because the user asked" in out.text and "working record" in out.text
    saved = store.memories()
    assert [m.text for m in saved] == [text] and saved[0].protected


async def test_agent_correction_into_progress_is_refused(tmp_path):
    store = SkillStore(tmp_path)
    first = await remember({"text": LASTING[4][0], "when": LASTING[4][1]}, ctx(store))
    assert not first.is_error
    fact = store.memories()[0]
    out = await remember({"text": "Continuing the PR body rework in PR #1712; next step is "
                                  "the checks section.",
                          "memory_id": fact.id, "expected_revision": fact.revision}, ctx(store))
    assert out.is_error and "task progress" in out.text
    assert store.memories()[0].text == LASTING[4][0]


async def test_correction_is_checked_against_the_stored_when_line(tmp_path):
    """A correction that omits ``when`` keeps the stored one, so the guard
    reads that: the same pair sent whole would be refused."""
    store = SkillStore(tmp_path)
    first = await remember({"text": "Gesture tests need a Robolectric run.",
                            "when": "When working on PR #1824"}, ctx(store))
    assert not first.is_error
    fact = store.memories()[0]
    out = await remember({"text": "The gesture fix is half done.", "memory_id": fact.id,
                          "expected_revision": fact.revision}, ctx(store))
    assert out.is_error and "task progress" in out.text
    assert store.memories()[0].text == "Gesture tests need a Robolectric run."


async def test_archived_fact_correction_is_checked_against_its_stored_when_line(tmp_path):
    """A correction by ID may revive an archived fact; the store resolves that
    target, so the guard reads its when-line too and nothing is revived."""
    store = SkillStore(tmp_path)
    fact = store.save_memory("Gesture tests need a Robolectric run.",
                             when="When working on PR #1824", durable=True)
    assert store.retire_memory(fact.id, reason="test")
    archived, = store.archived_memories()
    out = await remember({"text": "The gesture fix is half done.", "memory_id": archived.id,
                          "expected_revision": archived.revision}, ctx(store))
    assert out.is_error and "task progress" in out.text
    assert store.memories() == []
    assert [m.text for m in store.archived_memories()] == ["Gesture tests need a Robolectric run."]


async def test_key_correction_reads_the_when_line_of_the_fact_it_resolves(tmp_path):
    """A key correction resolves to this project's or the global slot; another
    project's fact under the same key must not decide the guard."""
    store = SkillStore(tmp_path)
    store.save_memory("Fixture runner is alpha.", key="fixture.runner",
                      when="When running the fixture suite", durable=True)
    store.save_memory("Fixture runner needs the gesture build.", key="fixture.runner",
                      scope="project:two", when="When working on PR #1824", durable=True)
    target = next(m for m in store.memories() if m.scope == "global")
    out = await remember({"text": "The gesture fix is half done.", "key": "fixture.runner",
                          "expected_revision": target.revision},
                         ToolCtx(extra={"memory_store": store, "memory_project": "project:one"}))
    assert not out.is_error, out.text
    texts = {m.scope: m.text for m in store.memories()}
    assert texts == {"global": "The gesture fix is half done.",
                     "project:two": "Fixture runner needs the gesture build."}


# ─── similar facts at save time ──────────────────────────────────────────────


def _seed(store):
    return [store.save_memory(text, when=when, durable=True).id for text, when in STORE]


def _shared(a: str, b: str) -> int:
    from fluid.codemode.supersession import subject_terms

    def terms(text):
        return {w for w in subject_terms(text) if not w.isdigit() and w not in facts._HINT_NOISE}
    return len(terms(a) & terms(b))


def test_threshold_has_margin_on_both_sides():
    """Why SIMILAR_MIN_SHARED (5 shared subject terms) holds on this store:
    every restatement shares at least two more terms with its fact, and no
    new subject shares as many with any fact, though they reuse its words."""
    floor = facts.SIMILAR_MIN_SHARED
    assert min(_shared(text, STORE[i][0]) for text, _, i in RESTATED) >= floor + 2
    assert max(_shared(text, old) for text, _ in NEW_SUBJECTS for old, _ in STORE) <= floor - 1


@pytest.mark.parametrize("text, when, index", RESTATED)
async def test_restated_save_lists_the_fact_it_restates(tmp_path, text, when, index):
    store = SkillStore(tmp_path)
    ids = _seed(store)
    target = next(m for m in store.memories() if m.id == ids[index])
    out = await remember({"text": text, "when": when}, ctx(store))
    assert not out.is_error and "Similar saved facts: [" in out.text, text
    hint = out.text.split("Similar saved facts: ", 1)[1]
    assert hint.startswith(f"[{target.id} r{target.revision}] {STORE[index][1]}")
    assert hint.count("[m") <= facts.MAX_SIMILAR
    assert f'replaces=["{target.id}"' in hint
    # A hint only: the restated fact is still live.
    assert target.id in {m.id for m in store.memories()}


@pytest.mark.parametrize("text, when", NEW_SUBJECTS)
async def test_new_subject_save_lists_nothing(tmp_path, text, when):
    store = SkillStore(tmp_path)
    _seed(store)
    out = await remember({"text": text, "when": when}, ctx(store))
    assert not out.is_error and "Similar saved facts" not in out.text, text


async def test_hint_excludes_the_new_fact_and_what_it_replaced(tmp_path):
    store = SkillStore(tmp_path)
    ids = _seed(store)
    text, when, index = RESTATED[0]
    out = await remember({"text": text, "when": when, "replaces": [ids[index]]}, ctx(store))
    assert not out.is_error and f"[{ids[index]}]" in out.text.split("Replaced", 1)[1]
    assert "Similar saved facts" not in out.text


async def test_hint_failure_never_fails_a_committed_save(tmp_path, monkeypatch):
    store = SkillStore(tmp_path)
    _seed(store)

    def broken(*_a, **_k):
        raise RuntimeError("scorer bug")

    monkeypatch.setattr(facts.FactStore, "_similar", staticmethod(broken))
    text, when, _ = RESTATED[1]
    out = await remember({"text": text, "when": when}, ctx(store))
    assert not out.is_error and out.text.startswith("Saved [")
    assert "Similar saved facts" not in out.text
    assert text in {m.text for m in store.memories()}
