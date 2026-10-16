"""Regression guard for the # Memory snapshot's per-request cost.

Runs scripts/measure_memory_reminder.py's scripted 20-turn session on both
legs (a Remember at turn 8; native compacts automatically inside turns, the
delegated CLI compacts mid-turn at turn 14), with a small fixed prefix so the
run stays fast. Measured when the snapshot stopped riding every message
(Oct 2026), memory tokens summed over the 20 requests at the full prefix:
native 117,905 -> 43,539 (17 -> 1 automatic folds), delegated 273,707 ->
42,537. A change that puts the snapshot back on every message, or that lets a
compaction hide it from a request, fails here.
"""
from __future__ import annotations

import pytest

from scripts import measure_memory_reminder as m

# A full snapshot in this fixture is ~2.1k tokens. After the change, requests
# carry one copy (plus one small change list until the compaction), so the
# 20-request sum stays near 20 copies; before, request n carried n copies.
MAX_MEMORY_SHARE_OF_LEGACY = 0.5
MAX_COPIES_IN_A_REQUEST = 1.2


@pytest.fixture(scope="module")
def runs():
    prefixes = (4_000, 4_000)
    return m.measure(True, prefixes), m.measure(False, prefixes)


@pytest.mark.parametrize("leg", ["native", "delegated"])
def test_snapshot_does_not_pile_up_in_history(runs, leg):
    legacy, current = (run[leg] for run in runs)
    before, after = legacy["summary"], current["summary"]
    assert after["memory_tokens_total"] <= MAX_MEMORY_SHARE_OF_LEGACY * before["memory_tokens_total"]
    one_copy = current["per_request"][0]["memory_tokens"]
    assert one_copy > 1_000  # the snapshot is sent at all
    assert max(r["memory_tokens"] for r in current["per_request"]) <= MAX_COPIES_IN_A_REQUEST * one_copy
    assert after["tokens_total"] < before["tokens_total"]
    assert after["cache_hit_rate"] >= before["cache_hit_rate"]


@pytest.mark.parametrize("leg", ["native", "delegated"])
def test_every_request_shows_the_snapshot(runs, leg):
    """Every model request carries one snapshot, before and after compaction."""
    per_request = runs[1][leg]["per_request"]
    one_copy = per_request[0]["memory_tokens"]
    assert all(r["memory_tokens"] >= 0.8 * one_copy for r in per_request)


def test_native_in_turn_compaction_is_exercised(runs):
    """The deduplicated native run folds the turn that carried the snapshot
    during a later turn, and that request still shows it (via the fold)."""
    summary = runs[1]["native"]["summary"]
    assert summary["compactions_tier2plus"] >= 1
    assert summary["requests_with_memory_via_fold"] >= 1
