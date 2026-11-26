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
