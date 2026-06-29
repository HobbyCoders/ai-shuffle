"""
Unit tests for the pure rewind-slicing helpers in ``app.core.jsonl_rewind``.

``compute_rewind_keep_count`` is the single source of truth for *where* a
rewind truncates a transcript. Both the on-disk JSONL file
(:meth:`JSONLRewindService.truncate_to_checkpoint`) and the Postgres session
store (:func:`app.core.pg_session_store.truncate_session_store_to_checkpoint`)
slice at the count it returns, so its behavior is the regression guard for the
"rewind removes the wrong messages" / "rewind removes nothing" family of bugs.

These are pure functions over plain dicts — no filesystem, no DB.
"""

from app.core.jsonl_rewind import (
    _next_real_user_index,
    compute_rewind_keep_count,
)


# ─── Transcript builders ──────────────────────────────────────────────────────

def _user(uuid: str, text: str = "hi") -> dict:
    return {"uuid": uuid, "type": "user",
            "message": {"role": "user", "content": text}}


def _assistant(uuid: str, text: str = "ok") -> dict:
    return {"uuid": uuid, "type": "assistant",
            "message": {"role": "assistant", "content": text}}


def _tool_result_user(uuid: str, tool_use_id: str = "t1") -> dict:
    """A `user`-role entry that only carries a tool_result — the SDK echoing
    tool output, NOT a new human turn."""
    return {
        "uuid": uuid,
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "out"}
            ],
        },
    }


# A two-turn conversation, exactly the user's repro:
#   u1 "hello 1" / a1 reply / u2 "hello 2" / a2 reply
def _two_turn() -> list:
    return [
        _user("u1", "hello 1"),
        _assistant("a1", "hi 1"),
        _user("u2", "hello 2"),
        _assistant("a2", "hi 2"),
    ]


# ─── compute_rewind_keep_count: user targets ─────────────────────────────────

class TestUserRewind:
    def test_rewind_second_user_drops_it_and_after(self):
        """The reported bug: rewinding to 'hello 2' keeps turn 1 only."""
        entries = _two_turn()
        keep, err = compute_rewind_keep_count(
            entries, "u2", include_response=False, target_role="user"
        )
        assert err is None
        # Keep e0 (u1) and e1 (a1); drop u2 + its reply.
        assert keep == 2
        kept = entries[:keep]
        assert [e["uuid"] for e in kept] == ["u1", "a1"]

    def test_rewind_first_user_drops_everything(self):
        entries = _two_turn()
        keep, err = compute_rewind_keep_count(
            entries, "u1", include_response=False, target_role="user"
        )
        assert err is None
        assert keep == 0
        assert entries[:keep] == []

    def test_user_include_response_keeps_prompt_and_reply(self):
        """include_response=True on a user msg keeps the prompt + its assistant
        answer, dropping from the NEXT user prompt onward."""
        entries = _two_turn()
        keep, err = compute_rewind_keep_count(
            entries, "u1", include_response=True, target_role="user"
        )
        assert err is None
        assert keep == 2  # u1 + a1 kept; u2 onward dropped
        assert [e["uuid"] for e in entries[:keep]] == ["u1", "a1"]

    def test_user_include_response_on_last_turn_is_noop(self):
        """No user prompt follows the last turn, so include_response keeps all."""
        entries = _two_turn()
        keep, err = compute_rewind_keep_count(
            entries, "u2", include_response=True, target_role="user"
        )
        assert err is None
        assert keep == len(entries)  # nothing removed


# ─── compute_rewind_keep_count: assistant targets ────────────────────────────

class TestAssistantRewind:
    def test_rewind_first_assistant_keeps_through_it(self):
        entries = _two_turn()
        keep, err = compute_rewind_keep_count(
            entries, "a1", include_response=False, target_role="assistant"
        )
        assert err is None
        # Assistant target: keep the assistant message + everything before;
        # drop from the next user prompt (u2) onward.
        assert keep == 2
        assert [e["uuid"] for e in entries[:keep]] == ["u1", "a1"]

    def test_rewind_last_assistant_is_noop(self):
        entries = _two_turn()
        keep, err = compute_rewind_keep_count(
            entries, "a2", include_response=False, target_role="assistant"
        )
        assert err is None
        assert keep == len(entries)


# ─── tool_result echoes are not human turns ──────────────────────────────────

class TestToolResultSkipping:
    def _with_tool_roundtrip(self) -> list:
        # u1 / a1(tool_use) / user-tool_result / a2(follow-up) / u2 / a3
        return [
            _user("u1", "do a thing"),
            _assistant("a1", "calling tool"),
            _tool_result_user("utr"),
            _assistant("a2", "tool said X"),
            _user("u2", "next question"),
            _assistant("a3", "answer"),
        ]

    def test_next_real_user_index_skips_tool_result(self):
        entries = self._with_tool_roundtrip()
        # Searching after the first assistant (index 1) must skip the
        # tool_result user (index 2) and land on the real prompt u2 (index 4).
        assert _next_real_user_index(entries, 1) == 4

    def test_assistant_rewind_keeps_tool_roundtrip(self):
        entries = self._with_tool_roundtrip()
        keep, err = compute_rewind_keep_count(
            entries, "a1", include_response=False, target_role="assistant"
        )
        assert err is None
        # Keep u1, a1, tool_result, a2 — drop from the real next prompt u2.
        assert keep == 4
        assert [e["uuid"] for e in entries[:keep]] == ["u1", "a1", "utr", "a2"]


# ─── error path ──────────────────────────────────────────────────────────────

class TestTargetNotFound:
    def test_missing_uuid_returns_error(self):
        entries = _two_turn()
        keep, err = compute_rewind_keep_count(
            entries, "does-not-exist", include_response=False, target_role="user"
        )
        assert keep is None
        assert err is not None
        assert "does-not-exist" in err

    def test_empty_transcript_returns_error(self):
        keep, err = compute_rewind_keep_count(
            [], "anything", include_response=False, target_role="user"
        )
        assert keep is None
        assert err is not None
