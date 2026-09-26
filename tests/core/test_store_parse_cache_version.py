"""Regression: ``_store_parse_cache`` must validate against a store version stamp.

Cross-install cache-staleness bug (a recurring "rewind still broken" symptom):

* ``_store_parse_cache`` (an ``OrderedDict`` LRU keyed by ``sdk_session_id``)
  serves postgres_only transcript parses VERBATIM. It has no on-disk mtime to
  self-invalidate, and ``invalidate_parse_cache`` is process-local.
* In a multi-install cluster sharing ``session_store_entries`` via Postgres, a
  rewind on install A DELETEs shared rows and invalidates only A's cache.
  Install B — which warms its cache via the shared-DB read path and via the
  post-turn pre-seed — keeps serving the PRE-rewind transcript until LRU
  eviction or restart. Rewind visually no-ops on the other installs.

Fix: stamp each store cache entry with a cheap ``(count, max_id)`` store version
captured at parse time and re-validate it on read. ``session_store_entries`` is
an append-only log, so the pair changes on ANY insert (a new turn) or delete (a
rewind) — including a rewind on a DIFFERENT install. On mismatch (or an
unstamped legacy entry) the cache entry is dropped and a fresh parse is served.

The DB layer is mocked via an injected ``store_version_loader`` — there is no
live Postgres in this env (the same approach neighboring store tests use).
"""

from __future__ import annotations

from app.core import jsonl_parser as jp
from tests.core.jsonl_cache_helpers import sdk_entries, setup_function, teardown_function  # noqa: F401  (module hooks)


def _counting_loader(entries):
    """A store_loader that counts how many times the main transcript is fetched."""
    calls = {"n": 0}

    def _loader(session_id, subpath=None):
        if subpath is None:
            calls["n"] += 1
            return list(entries)
        return []

    return _loader, calls


def _mutable_version_loader(initial):
    """A store version loader whose returned (count, max_id) can be mutated.

    Models the shared Postgres ``session_store_entries`` changing underneath the
    cache (e.g. a rewind DELETE on another install), without a live DB.
    """
    state = {"version": initial}

    def _loader(_session_id):
        return state["version"]

    return _loader, state


# ---------------------------------------------------------------------------
# (1) store mutated (rewind on another install) -> read serves a FRESH parse
# ---------------------------------------------------------------------------


def test_store_version_change_drops_stale_cache_and_reparses():
    """A rewind (store version changes) must drop the cache and re-parse fresh.

    Simulates: install B primed its cache with the PRE-rewind transcript; a
    rewind on install A DELETEd rows (count/max_id drop); B's next read must
    NOT serve the pre-rewind transcript.
    """
    sid = "xinstall-rewind-1"

    # Prime the cache with the full 2-turn transcript, stamped at version (4, 40).
    full = sdk_entries([("Hi", "Hello"), ("Bye", "Goodbye")])  # 4 messages
    full_loader, full_calls = _counting_loader(full)
    version_loader, vstate = _mutable_version_loader((4, 40))

    primed, _ = jp.parse_session_history_from_store(
        sid, "/workspace", full_loader, use_cache=True,
        store_version_loader=version_loader,
    )
    assert len(primed) == 4
    assert full_calls["n"] == 1  # cache miss -> loader ran once

    # A rewind on another install removes the 2nd turn: fewer rows, lower max_id.
    vstate["version"] = (2, 20)
    rewound = sdk_entries([("Hi", "Hello")])  # 2 messages
    rewound_loader, rewound_calls = _counting_loader(rewound)

    served, _ = jp.parse_session_history_from_store(
        sid, "/workspace", rewound_loader, use_cache=True,
        store_version_loader=version_loader,
    )

    # Stale entry dropped: the rewound loader ran and produced the FRESH parse.
    assert rewound_calls["n"] == 1, "stale cache served; loader was not consulted"
    assert len(served) == 2
    assert not any("Goodbye" in str(m.get("content")) for m in served), (
        "pre-rewind transcript served from stale store cache (the bug)"
    )

    # The re-primed entry now carries the NEW version and is served without a
    # re-parse while the store is unchanged.
    rewound_calls["n"] = 0
    served2, _ = jp.parse_session_history_from_store(
        sid, "/workspace", rewound_loader, use_cache=True,
        store_version_loader=version_loader,
    )
    assert rewound_calls["n"] == 0, "expected a store-cache hit after re-prime"
    assert len(served2) == 2


# ---------------------------------------------------------------------------
# (2) unchanged version -> cached entry served WITHOUT a full re-parse
# ---------------------------------------------------------------------------


def test_unchanged_store_version_serves_cache_without_reparse():
    """When the store version is unchanged, the cache is served verbatim."""
    sid = "xinstall-hit-1"
    msgs = sdk_entries([("Q", "A")])
    loader, calls = _counting_loader(msgs)
    version_loader, _ = _mutable_version_loader((2, 20))

    first, _ = jp.parse_session_history_from_store(
        sid, "/workspace", loader, use_cache=True,
        store_version_loader=version_loader,
    )
    assert calls["n"] == 1  # cache miss -> loader ran

    # A sentinel loader carrying DIFFERENT data must not be consulted on the hit.
    sentinel_loader, sentinel_calls = _counting_loader(sdk_entries([("X", "SENTINEL")]))
    served, _ = jp.parse_session_history_from_store(
        sid, "/workspace", sentinel_loader, use_cache=True,
        store_version_loader=version_loader,
    )
    assert sentinel_calls["n"] == 0, "expected a store-cache hit; loader should not run"
    assert served == first
    assert not any("SENTINEL" in str(m.get("content")) for m in served)


# ---------------------------------------------------------------------------
# (3) pre-change unstamped legacy entry -> treated stale
# ---------------------------------------------------------------------------


def test_legacy_unstamped_entry_is_treated_stale():
    """A 2-tuple entry primed before stamping existed must be re-parsed, not served.

    Entries in ``_store_parse_cache`` written by an older build carry no version
    stamp; they may hold a pre-rewind transcript and cannot be validated, so the
    read path must drop them and re-parse.
    """
    sid = "legacy-unstamped-1"

    # Hand-plant a legacy 2-tuple (old on-the-wire shape) holding stale data.
    stale = sdk_entries([("old", "STALE")])
    import json

    with jp._store_parse_cache_lock:
        jp._store_parse_cache[sid] = (
            json.dumps(stale, default=str),
            json.dumps({}, default=str),
        )

    fresh = sdk_entries([("new", "FRESH")])
    fresh_loader, fresh_calls = _counting_loader(fresh)
    version_loader, _ = _mutable_version_loader((2, 20))

    served, _ = jp.parse_session_history_from_store(
        sid, "/workspace", fresh_loader, use_cache=True,
        store_version_loader=version_loader,
    )

    assert fresh_calls["n"] == 1, "legacy unstamped entry was served instead of re-parsed"
    assert any("FRESH" in str(m.get("content")) for m in served)
    assert not any("STALE" in str(m.get("content")) for m in served)

    # The dropped legacy entry was replaced by a stamped one.
    with jp._store_parse_cache_lock:
        assert len(jp._store_parse_cache[sid]) == 3


# ---------------------------------------------------------------------------
# Unresolvable version -> conservative: never cache, never serve stale
# ---------------------------------------------------------------------------


def test_unresolvable_version_declines_to_cache():
    """If the store version can't be resolved, do not cache and never serve stale.

    A version-loader failure (or missing DB) means "cannot prove freshness", so
    every read re-parses from the store rather than risk a stale hit.
    """
    sid = "no-version-1"

    def _boom_version(_sid):
        raise RuntimeError("db down")

    msgs = sdk_entries([("Q", "A")])
    loader, calls = _counting_loader(msgs)

    jp.parse_session_history_from_store(
        sid, "/workspace", loader, use_cache=True,
        store_version_loader=_boom_version,
    )
    # Nothing cached (no version to stamp).
    with jp._store_parse_cache_lock:
        assert sid not in jp._store_parse_cache

    # A second read re-parses (loader runs again) rather than serving a stale hit.
    calls["n"] = 0
    jp.parse_session_history_from_store(
        sid, "/workspace", loader, use_cache=True,
        store_version_loader=_boom_version,
    )
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# pre_seed writes a stamped entry the read path trusts (post-turn coherence)
# ---------------------------------------------------------------------------


def test_pre_seed_stamps_store_cache_entry():
    """The post-turn pre-seed must stamp the store cache so a later read trusts it."""
    sid = "preseed-stamp-1"
    post = sdk_entries([("Hi", "Hello"), ("Bye", "Goodbye")])
    post_loader, _ = _counting_loader(post)
    version_loader, _ = _mutable_version_loader((4, 40))

    post_msgs, post_usage = jp.parse_session_history_from_store(
        "tmp-parse-only", "/workspace", post_loader, use_cache=False,
    )

    ok = jp.pre_seed_parse_cache(
        sid, post_msgs, post_usage, working_dir="/workspace",
        store_version_loader=version_loader,
    )
    assert ok is True

    # The seeded entry is stamped, so an unchanged-version read hits it without
    # consulting a (stale, different) loader.
    sentinel_loader, sentinel_calls = _counting_loader(sdk_entries([("X", "SENTINEL")]))
    served, _ = jp.parse_session_history_from_store(
        sid, "/workspace", sentinel_loader, use_cache=True,
        store_version_loader=version_loader,
    )
    assert sentinel_calls["n"] == 0, "pre-seeded entry was not trusted on read"
    assert served == post_msgs

    with jp._store_parse_cache_lock:
        assert len(jp._store_parse_cache[sid]) == 3

def test_pre_seed_with_matching_expected_version_writes_entry():
    """When the store hasn't moved since the caller snapshotted its messages,
    the seed is written and stamped as usual."""
    sid = "preseed-toctou-ok"
    post = sdk_entries([("Hi", "Hello")])
    post_loader, _ = _counting_loader(post)
    version_loader, _ = _mutable_version_loader((4, 40))

    post_msgs, post_usage = jp.parse_session_history_from_store(
        "tmp-parse-only", "/workspace", post_loader, use_cache=False,
    )

    ok = jp.pre_seed_parse_cache(
        sid, post_msgs, post_usage, working_dir="/workspace",
        store_version_loader=version_loader,
        expected_store_version=(4, 40),
    )
    assert ok is True
    with jp._store_parse_cache_lock:
        assert jp._store_parse_cache[sid][2] == (4, 40)


def test_pre_seed_drops_entry_when_store_moved_past_expected_version():
    """TOCTOU regression: the turn-end reseed snapshots the live messages, then
    resolves the store version at WRITE time. If the store mutated in between
    (next turn appended entries, or a rewind deleted rows), the old snapshot
    got stamped with the NEW version — a poisoned entry the read path's version
    check could never catch. With the caller's pre-snapshot version passed as
    ``expected_store_version``, the mismatch must DROP the entry instead."""
    sid = "preseed-toctou-stale"
    old = sdk_entries([("Hi", "Hello")])
    old_loader, _ = _counting_loader(old)
    version_loader, vstate = _mutable_version_loader((4, 40))

    old_msgs, old_usage = jp.parse_session_history_from_store(
        "tmp-parse-only", "/workspace", old_loader, use_cache=False,
    )

    # Warm the cache at the old version, as the previous turn's reseed would.
    assert jp.pre_seed_parse_cache(
        sid, old_msgs, old_usage, working_dir="/workspace",
        store_version_loader=version_loader,
        expected_store_version=(4, 40),
    )

    # The store moves on between the caller's snapshot and the write.
    vstate["version"] = (6, 60)
    jp.pre_seed_parse_cache(
        sid, old_msgs, old_usage, working_dir="/workspace",
        store_version_loader=version_loader,
        expected_store_version=(4, 40),
    )

    with jp._store_parse_cache_lock:
        assert sid not in jp._store_parse_cache, (
            "stale snapshot was seeded despite the store version moving on"
        )

    # And the next read re-parses fresh from the store, not the stale seed.
    fresh_loader, fresh_calls = _counting_loader(sdk_entries([("Hi", "Hello"), ("New", "Turn")]))
    served, _ = jp.parse_session_history_from_store(
        sid, "/workspace", fresh_loader, use_cache=True,
        store_version_loader=version_loader,
    )
    assert fresh_calls["n"] == 1
    assert len(served) == 4
