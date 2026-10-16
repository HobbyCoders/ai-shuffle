"""Regression guard for the # Memory snapshot's per-request cost.

Runs scripts/measure_memory_reminder.py's scripted 20-turn session (a Remember
at turn 8, a compaction before turn 14) on both legs, with a small fixed
prefix so the run stays fast. Measured when the snapshot stopped riding every
message (Oct 2026), memory tokens summed over the 20 requests:
native 290,015 -> 42,328, delegated 273,707 -> 42,488 (about -85%), and the
modelled prompt-cache hit rate rose on both legs. A change that puts the
snapshot back on every message, or that stops re-sending it after a
compaction, fails here.
"""
from __future__ import annotations

import pytest

from scripts import measure_memory_reminder as m

# A full snapshot in this fixture is ~2.1k tokens. After the change, requests
# carry one copy (plus one small change list until the compaction), so the
# 20-request sum stays near 20 copies; before, request n carried n copies.
MAX_MEMORY_SHARE_OF_LEGACY = 0.2
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
def test_snapshot_survives_the_compaction(runs, leg):
    """Every request after the compaction still carries one snapshot."""
    per_request = runs[1][leg]["per_request"]
    one_copy = per_request[0]["memory_tokens"]
    assert all(r["memory_tokens"] >= 0.8 * one_copy for r in per_request[m.COMPACT_BEFORE - 1:])
