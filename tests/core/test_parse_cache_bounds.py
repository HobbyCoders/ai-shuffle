"""Byte-bounded parse caches + the negative JSONL memo (findings B5 and B9).

B5: ``_parse_cache`` / ``_store_parse_cache`` used to hold up to 50 whole
transcripts EACH, serialized as JSON strings — unbounded in bytes (a transcript
carries inline base64 screenshots and untruncated tool inputs), and a cache HIT
still ``json.loads``-ed the entire transcript per request. They are now
:class:`~app.core.jsonl_parser.ByteBoundedLRU`s holding the transformed message
list itself.

B9: ``get_session_jsonl_path`` falls back to stat-ing every Claude project
directory. Sessions whose transcript lives only in Postgres hit that fallback on
every open/poll, so a miss is memoized for a short TTL.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from app.core import jsonl_parser as jp
from app.core.jsonl_parser import ByteBoundedLRU, estimate_size


def setup_function(_func):
    jp.invalidate_parse_cache()


def teardown_function(_func):
    jp.invalidate_parse_cache()


# ---------------------------------------------------------------------------
# ByteBoundedLRU
# ---------------------------------------------------------------------------

def test_evicts_least_recently_used_on_count_overflow():
    cache = ByteBoundedLRU(max_entries=2, max_bytes=10**9)
    cache["a"] = "1"
    cache["b"] = "2"
    cache.get("a")  # "a" is now the most recent
    cache["c"] = "3"

    assert "b" not in cache
    assert "a" in cache and "c" in cache
    assert len(cache) == 2


def test_evicts_on_byte_budget_not_just_count():
    """The bug this class exists for: 50 entries is not a memory bound when one
    entry can be tens of megabytes."""
    payload = "x" * 100_000
    budget = estimate_size(payload) * 3
    cache = ByteBoundedLRU(max_entries=50, max_bytes=budget)

    for i in range(10):
        cache[f"k{i}"] = payload

    assert len(cache) <= 3
    assert cache.current_bytes <= budget
    # The most recent survives; the oldest are gone.
    assert "k9" in cache
    assert "k0" not in cache


def test_oversized_entry_is_not_cached_at_all():
    """A single screenshot-heavy transcript must not evict every other session
    on insert and then be evicted itself before it is ever read back."""
    cache = ByteBoundedLRU(max_entries=10, max_bytes=10**6, max_entry_bytes=1000)
    cache["small"] = "s"
    assert cache.put("huge", "y" * 100_000) is False

    assert "huge" not in cache
    assert "small" in cache, "an oversized insert evicted an unrelated entry"


def test_oversized_overwrite_drops_the_previous_value():
    """Refusing the new value must not leave the STALE one behind — a cache
    that keeps serving a superseded transcript is worse than a cache miss."""
    cache = ByteBoundedLRU(max_entries=10, max_bytes=10**6, max_entry_bytes=1000)
    cache["k"] = "old"
    assert cache.put("k", "y" * 100_000) is False
    assert "k" not in cache


def test_byte_accounting_survives_overwrite_pop_and_clear():
    cache = ByteBoundedLRU(max_entries=10, max_bytes=10**6)
    cache["k"] = "x" * 100
    after_first = cache.current_bytes
    assert after_first > 0

    cache["k"] = "x" * 100  # same size, overwritten
    assert cache.current_bytes == after_first
    assert len(cache) == 1

    cache["j"] = "y" * 100
    cache.pop("j")
    assert cache.current_bytes == after_first

    cache.clear()
    assert cache.current_bytes == 0
    assert len(cache) == 0


def test_estimate_size_tracks_payload_growth():
    small = [{"content": "a"}]
    big = [{"content": "a" * 100_000}]
    assert estimate_size(big) > estimate_size(small) + 90_000


# ---------------------------------------------------------------------------
# The store parse cache: a hit deserializes nothing and cannot be poisoned
# ---------------------------------------------------------------------------

def _entries(pairs):
    out = []
    for i, (u, a) in enumerate(pairs):
        out.append(
            {
                "type": "user",
                "message": {"role": "user", "content": u},
                "uuid": f"u{i}",
                "timestamp": f"2024-01-01T00:00:{i * 2:02d}Z",
            }
        )
        out.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": a}],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                    "model": "claude-x",
                },
                "uuid": f"a{i}",
                "timestamp": f"2024-01-01T00:00:{i * 2 + 1:02d}Z",
            }
        )
    return out


def _loader(entries):
    calls = {"n": 0}

    def load(_sid, subpath=None):
        if subpath is not None:
            return []
        calls["n"] += 1
        return entries

    return load, calls


def test_store_cache_hit_returns_equal_but_independent_messages():
    sid = "cache-shape-1"
    loader, calls = _loader(_entries([("Q", "A")]))

    first, _ = jp.parse_session_history_from_store(
        sid, "/workspace", loader, store_version_loader=lambda _s: (2, 2)
    )
    second, _ = jp.parse_session_history_from_store(
        sid, "/workspace", loader, store_version_loader=lambda _s: (2, 2)
    )

    assert calls["n"] == 1, "the second read re-parsed instead of hitting the cache"
    assert second == first
    # Independent top-level dicts: a caller re-keying a message must not write
    # through into the cached snapshot.
    assert all(a is not b for a, b in zip(first, second))


def test_mutating_a_returned_message_does_not_poison_the_cache():
    sid = "cache-poison-1"
    loader, _ = _loader(_entries([("Q", "A")]))
    version = lambda _s: (2, 2)  # noqa: E731

    served, _ = jp.parse_session_history_from_store(
        sid, "/workspace", loader, store_version_loader=version
    )
    served[0]["content"] = "MUTATED"
    served[0]["injected"] = True

    again, _ = jp.parse_session_history_from_store(
        sid, "/workspace", loader, store_version_loader=version
    )
    assert again[0]["content"] != "MUTATED"
    assert "injected" not in again[0]


def test_store_cache_value_holds_structures_not_json_strings():
    """A hit must cost no deserialization — the whole point of B5."""
    sid = "cache-struct-1"
    loader, _ = _loader(_entries([("Q", "A")]))
    jp.parse_session_history_from_store(
        sid, "/workspace", loader, store_version_loader=lambda _s: (2, 2)
    )

    messages, usage, version = jp._store_parse_cache[sid]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    assert isinstance(usage, dict)
    assert version == (2, 2)


def test_store_cache_is_byte_bounded(monkeypatch):
    """An oversized transcript is served but not cached, so the cache cannot
    grow without bound (and never serves a stale copy of it either)."""
    monkeypatch.setattr(jp._store_parse_cache, "max_entry_bytes", 2000)
    sid = "cache-huge-1"
    big = _entries([("Q", "A" * 50_000)])
    loader, calls = _loader(big)
    version = lambda _s: (2, 2)  # noqa: E731

    jp.parse_session_history_from_store(
        sid, "/workspace", loader, store_version_loader=version
    )
    assert sid not in jp._store_parse_cache

    jp.parse_session_history_from_store(
        sid, "/workspace", loader, store_version_loader=version
    )
    assert calls["n"] == 2  # re-parsed rather than served from an unbounded cache


# ---------------------------------------------------------------------------
# B9 — negative memo for "no local JSONL"
# ---------------------------------------------------------------------------

def test_missing_jsonl_is_memoized_and_skips_the_directory_scan():
    with tempfile.TemporaryDirectory() as tmpdir:
        projects_dir = Path(tmpdir) / "projects"
        (projects_dir / "-workspace").mkdir(parents=True)
        for i in range(5):
            (projects_dir / f"-other-{i}").mkdir()

        sid = "no-jsonl-anywhere"
        with patch("app.core.jsonl_parser.settings") as mock_settings:
            mock_settings.get_claude_projects_dir = projects_dir

            scans = {"n": 0}
            real_iterdir = Path.iterdir

            def counting_iterdir(self):
                if self == projects_dir:
                    scans["n"] += 1
                return real_iterdir(self)

            with patch.object(Path, "iterdir", counting_iterdir):
                assert jp.get_session_jsonl_path(sid, "/workspace") is None
                assert scans["n"] == 1
                for _ in range(10):
                    assert jp.get_session_jsonl_path(sid, "/workspace") is None
                assert scans["n"] == 1, "memoized miss still scanned every project dir"


def test_memo_is_dropped_when_the_session_cache_is_invalidated():
    """A transcript that appears on disk must become visible — invalidation is
    the signal that a session's history changed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        projects_dir = Path(tmpdir) / "projects"
        project_dir = projects_dir / "-workspace"
        project_dir.mkdir(parents=True)
        sid = "appears-later"

        with patch("app.core.jsonl_parser.settings") as mock_settings:
            mock_settings.get_claude_projects_dir = projects_dir

            assert jp.get_session_jsonl_path(sid, "/workspace") is None

            jsonl = project_dir / f"{sid}.jsonl"
            jsonl.write_text('{"test": true}')
            assert jp.get_session_jsonl_path(sid, "/workspace") is None  # memoized

            jp.invalidate_parse_cache(sid)
            assert jp.get_session_jsonl_path(sid, "/workspace") == jsonl


def test_memo_expires(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        projects_dir = Path(tmpdir) / "projects"
        project_dir = projects_dir / "-workspace"
        project_dir.mkdir(parents=True)
        sid = "expiring-miss"

        monkeypatch.setattr(jp, "_JSONL_MISS_TTL_SECONDS", 0.0)
        with patch("app.core.jsonl_parser.settings") as mock_settings:
            mock_settings.get_claude_projects_dir = projects_dir
            assert jp.get_session_jsonl_path(sid, "/workspace") is None

            jsonl = project_dir / f"{sid}.jsonl"
            jsonl.write_text('{"test": true}')
            assert jp.get_session_jsonl_path(sid, "/workspace") == jsonl


def test_finding_the_file_clears_any_memo():
    """A positive result must not leave a stale negative memo behind."""
    with tempfile.TemporaryDirectory() as tmpdir:
        projects_dir = Path(tmpdir) / "projects"
        project_dir = projects_dir / "-workspace"
        project_dir.mkdir(parents=True)
        sid = "found-clears-memo"
        jsonl = project_dir / f"{sid}.jsonl"
        jsonl.write_text('{"test": true}')

        with patch("app.core.jsonl_parser.settings") as mock_settings:
            mock_settings.get_claude_projects_dir = projects_dir
            assert jp.get_session_jsonl_path(sid, "/workspace") == jsonl
            assert not jp._jsonl_miss_is_fresh(sid, "/workspace", str(projects_dir))
