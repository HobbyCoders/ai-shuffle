"""Regression: ``pre_seed_parse_cache`` must also refresh the STORE parse cache.

Bug (postgres_only "latest turn missing on reopen"):

* The live-store seed in ``run_query`` loads the transcript through
  ``parse_session_history_from_store`` at stream START — before the new turn
  exists — priming ``_store_parse_cache`` (keyed by ``sdk_session_id``) with the
  PRE-turn transcript.
* ``_store_parse_cache`` has no on-disk mtime to self-invalidate; it is served
  verbatim until an explicit invalidate or LRU eviction.
* ``pre_seed_parse_cache`` (run at stream END) historically refreshed ONLY the
  JSONL ``_parse_cache`` — never ``_store_parse_cache``. Whenever a JSONL file
  was present (it is, on the primary install) it took that branch and left the
  store cache holding the stale PRE-turn transcript.

Net effect: once the live session was cleaned up, ``/live`` fell through to the
stale ``_store_parse_cache`` and the just-completed exchange "disappeared" on the
next session open until the process restarted.

Fix: ``pre_seed_parse_cache`` overwrites ``_store_parse_cache`` with the
post-turn messages, independent of JSONL presence.

These tests fail on the pre-fix code (the refreshed read returns the stale
pre-turn transcript) and pass on the fixed code.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.core import jsonl_parser as jp


def setup_function(_func):
    jp.invalidate_parse_cache()


def teardown_function(_func):
    jp.invalidate_parse_cache()


def _entries(pairs):
    """Build minimal SDK user/assistant entries from (user_text, asst_text) pairs."""
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


def _counting_loader(entries):
    """A store_loader that counts how many times the main transcript is fetched."""
    calls = {"n": 0}

    def _loader(session_id, subpath=None):
        if subpath is None:
            calls["n"] += 1
            return list(entries)
        return []

    return _loader, calls


@contextlib.contextmanager
def _projects_with_jsonl(session_id, entries):
    """Patch settings so ``get_session_jsonl_path`` FINDS a JSONL for session_id.

    This forces ``pre_seed_parse_cache`` down its JSONL branch — the exact
    condition under which the pre-fix code left ``_store_parse_cache`` stale.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        projects = Path(tmpdir) / "projects"
        project_dir = projects / "-workspace"  # encodes working_dir "/workspace"
        project_dir.mkdir(parents=True)
        (project_dir / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n"
        )
        with patch("app.core.jsonl_parser.settings") as mock_settings:
            mock_settings.get_claude_projects_dir = projects
            yield


@contextlib.contextmanager
def _projects_empty():
    """Patch settings so ``get_session_jsonl_path`` finds NO JSONL (postgres_only)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        projects = Path(tmpdir) / "projects"
        projects.mkdir(parents=True)
        with patch("app.core.jsonl_parser.settings") as mock_settings:
            mock_settings.get_claude_projects_dir = projects
            yield


def test_pre_seed_refreshes_store_parse_cache_with_jsonl():
    """The post-turn pre-seed must overwrite a store cache primed pre-turn."""
    sid = "coherence-sess-1"
    pre = _entries([("Hi", "Hello")])  # 1 turn  -> 2 messages
    post = _entries([("Hi", "Hello"), ("Bye", "Goodbye")])  # 2 turns -> 4 messages

    # Canonical post-turn messages, exactly as the live store hands them to
    # pre_seed_parse_cache (clean parse, no caching side effects).
    post_loader, _ = _counting_loader(post)
    post_msgs, post_usage = jp.parse_session_history_from_store(
        "tmp-parse-only", "/workspace", post_loader, use_cache=False
    )
    assert len(post_msgs) == 4

    pre_loader, pre_calls = _counting_loader(pre)

    # 1. Stream START seed primes the store cache with the PRE-turn transcript.
    seeded, _ = jp.parse_session_history_from_store(
        sid, "/workspace", pre_loader, use_cache=True
    )
    assert len(seeded) == 2
    assert pre_calls["n"] == 1  # cache miss -> loader ran

    # 2. Stream END pre-seed, with a JSONL file present (the buggy branch).
    with _projects_with_jsonl(sid, post):
        ok = jp.pre_seed_parse_cache(sid, post_msgs, post_usage, working_dir="/workspace")
    assert ok is True

    # 3. Next read (same PRE-turn loader) must serve the REFRESHED post-turn
    #    transcript from the store cache, NOT re-run the now-stale loader.
    pre_calls["n"] = 0
    served, _ = jp.parse_session_history_from_store(
        sid, "/workspace", pre_loader, use_cache=True
    )
    assert pre_calls["n"] == 0, "expected a store-cache hit; loader should not run"
    assert served == post_msgs, "stale store cache: post-turn exchange missing (the bug)"
    assert len(served) == 4
    assert any("Goodbye" in str(m.get("content")) for m in served)


def test_pre_seed_seeds_store_cache_without_jsonl():
    """postgres_only machines may have no local JSONL — seed the store cache anyway."""
    sid = "coherence-sess-2"
    msgs_loader, _ = _counting_loader(_entries([("Q", "A")]))
    msgs, usage = jp.parse_session_history_from_store(
        "tmp-parse-only-2", "/workspace", msgs_loader, use_cache=False
    )
    assert len(msgs) == 2

    with _projects_empty():
        ok = jp.pre_seed_parse_cache(sid, msgs, usage, working_dir="/workspace")
    assert ok is True, "store cache must be seeded even when no JSONL file exists"

    # A subsequent read hits the seeded store cache; a sentinel loader carrying
    # DIFFERENT data must not be consulted.
    sentinel_loader, sentinel_calls = _counting_loader(_entries([("X", "SENTINEL")]))
    served, _ = jp.parse_session_history_from_store(
        sid, "/workspace", sentinel_loader, use_cache=True
    )
    assert sentinel_calls["n"] == 0, "expected a store-cache hit; loader should not run"
    assert len(served) == 2
    assert not any("SENTINEL" in str(m.get("content")) for m in served)
