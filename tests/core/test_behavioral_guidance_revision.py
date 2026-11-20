"""Regression coverage for automatic behavioral review decisions and their budget."""
import asyncio

import pytest

from app.core import behavioral_learning as learning, behavioral_review as review
from app.core import guidance_storage as storage
from app.db import database
from tests.core.test_behavioral_learning import (
    BLOCKER, decision, env, saved, session, turn,  # noqa: F401 (env fixture)
)
from tests.shared_fixtures import pin_runner  # noqa: F401


NEUTRAL = 'When the owner quotes "Ready for the next step?", what is the surrounding context?'
CORRECTION = (
    "Going forward, when a routine PR check fails, report the failed check first and "
    "wait for me to ask before investigating locally."
)
RULES = (
    "- When a routine check blocks a PR: prefer investigating and fixing the issue locally before reporting status.",
    "- When handing off tests: prefer leading with the shortest rerun command.",
    "- When summarizing logs: prefer omitting repeated lines.",
    "- When reviewing a draft: prefer leading with the main finding.",
)


def seed_full_block():
    scope = learning._scope(database.get_session("source"))
    return storage.apply_automatic_guidance(
        scope.stem, review.HEADER + "\n".join(RULES) + "\n", expected_revision=None,
        model=scope.model, provider="ChatGPT",
    )


def replacement():
    return decision(CORRECTION, preference="reporting the failed check first and waiting for direction", replace=1)


@pytest.mark.asyncio
async def test_unrelated_rule_with_a_real_quote_is_not_evidence(env):
    fake, _ = env
    session()
    await turn("Look at the surrounding text.", "The previous check finished.")
    fake.side_effect = [decision(NEUTRAL, preference="stopping at the first failed check"), "null"]
    await turn(NEUTRAL, "It refers to the next item in the review.")
    assert saved() is None
    assert fake.await_count == 2


@pytest.mark.asyncio
async def test_explicit_contradiction_replaces_at_capacity(env):
    fake, _ = env
    session()
    original = seed_full_block()
    await turn("Run the PR check.", "The check failed. I investigated locally before reporting it.")
    fake.side_effect = [replacement(), '{"replace":1}']
    await turn(CORRECTION, "Understood.")
    revised = saved()
    assert revised.id == original.id and revised.revision != original.revision
    assert revised.content.removeprefix(review.HEADER).splitlines() == [
        "- When a routine check blocks a PR: prefer reporting the failed check first and waiting for direction.",
        *RULES[1:],
    ]
    assert fake.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", ["automatic", "owner edit", "owner delete"])
async def test_replacement_respects_revision_and_manual_markers(env, concurrent):
    fake, _ = env
    session()
    original = seed_full_block()
    await turn("Run the PR check.", "The check failed. I investigated locally before reporting it.")
    started, release = asyncio.Event(), asyncio.Event()

    async def review_or_verify(prompt, **kwargs):
        if prompt.startswith("Independently verify"):
            started.set()
            await release.wait()
            return '{"replace":1}'
        return replacement()

    fake.side_effect = review_or_verify
    await turn(CORRECTION, "Understood.", flush=False)
    await asyncio.wait_for(started.wait(), timeout=2)
    if concurrent == "automatic":
        winner = storage.apply_automatic_guidance(
            original.stem, original.content.replace("shortest rerun", "exact rerun"),
            expected_revision=original.revision, model=original.model, provider=original.provider,
        )
    elif concurrent == "owner edit":
        winner = storage.edit_saved_guidance(original.id, original.revision, "Show test results first.")
    else:
        storage.delete_saved_guidance(original.id, original.revision)
        winner = None
    release.set()
    await learning._worker
    assert saved() == winner


@pytest.mark.asyncio
async def test_disagreement_about_a_conflicting_rule_cannot_stack_it(env):
    fake, _ = env
    session()
    scope = learning._scope(database.get_session("source"))
    initial = storage.apply_automatic_guidance(
        scope.stem, review.HEADER + RULES[0] + "\n", expected_revision=None,
        model=scope.model, provider="ChatGPT",
    )
    await turn("Run the PR check.", "The check failed. I investigated locally before reporting it.")
    false_append = decision(CORRECTION, preference="reporting the failed check first and waiting for direction")
    fake.side_effect = [false_append, '{"replace":1}']
    await turn(CORRECTION, "Understood.")
    assert saved() == initial
    assert fake.await_count == 2


@pytest.mark.asyncio
async def test_routine_nulls_do_not_starve_seventh_candidate(env):
    fake, _ = env
    session()
    await turn("Check the PR.", "The check failed. Should I investigate?")
    for topic in ("logs", "tests", "first check", "second check", "report", "handoff"):
        await turn(f"What is the status of the {topic}?", "The PR check failed. I have not investigated.")
    fake.side_effect = [decision(BLOCKER, exchange=3), '{"replace":null}']
    await turn(BLOCKER, "I will inspect the failing check locally.")
    assert saved() and "fixing the issue locally" in saved().content
    assert fake.await_count == 5  # Three samples, then proposal and verification for #7.
