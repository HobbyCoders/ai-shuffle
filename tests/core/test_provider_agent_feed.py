"""Unit tests for :mod:`app.core.provider_agent_feed` (chunk P3-A2).

These exercise the genuinely new mechanism — cross-session event forwarding —
against the REAL append path (the SQLite pg shim), because the things that can
go wrong here are all properties of the durable log:

* the contract triple actually lands on the PARENT with the right ``task_id``
  (a mocked recorder would prove nothing — ``record_drained_event`` silently
  DROPS types outside ``subagent_*``/``task_*``/``system``);
* the appends are UNFENCED, so they succeed while the parent holds a fenced run
  and leave that run usable;
* the WORKER's own log is untouched (it is the persisted full report);
* volume is bounded;
* a worker that settles after the parent's turn boundary still lands its
  terminal frames.

NOTHING here starts a session, a CLI, or a network call.
"""

import asyncio

import pytest

from app.core import provider_agent_feed as feed_mod
from app.db import runs
from app.db import session_events as se


PARENT = "parent-session"
WORKER = "worker-session"


@pytest.fixture
def db():
    """Route the DB layer at a fresh in-memory schema for this test."""
    from tests.pg_shim import new_sqlite_connection, route_db_to

    conn = new_sqlite_connection()
    with route_db_to(conn):
        yield conn
    conn.close()


def _feed(**overrides):
    kwargs = dict(
        parent_session_id=PARENT,
        worker_session_id=WORKER,
        label="reviewer",
        provider_id="prov-openai",
        model_id="gpt-5.5",
        prompt="review the diff",
    )
    kwargs.update(overrides)
    return feed_mod.WorkerFeed(**kwargs)


def _seed_worker(*events):
    """Append raw contract rows to the WORKER's log."""
    se.append_events(WORKER, list(events))


def _text(text):
    return {"type": "assistant.text_delta", "payload": {"text": text}}


def _tool_use(tool_id="t1", name="Read", **input_kwargs):
    return {
        "type": "tool.use",
        "payload": {"tool_use_id": tool_id, "name": name, "input": input_kwargs},
    }


def _tool_result(tool_id="t1", output="ok", is_error=False):
    return {
        "type": "tool.result",
        "payload": {
            "tool_use_id": tool_id,
            "is_error": is_error,
            "content_preview": output,
        },
    }


def _parent_rows():
    return se.get_events(PARENT)


def _types(rows):
    return [r["event_type"] for r in rows]


# ---------------------------------------------------------------------------
# The contract triple on the parent
# ---------------------------------------------------------------------------

class TestParentTriple:

    def test_started_progress_completed_land_on_the_parent(self, db):
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(
                _text("looking"), _text(" at it"),
                _tool_use("t1", "Read", file_path="/repo/x.py"),
                _tool_result("t1", "file contents"),
                _text("done"),
            )
            await f.drain()
            await f.completed("completed", result_preview="the verdict")

        asyncio.run(run())

        rows = _parent_rows()
        assert _types(rows) == [
            "subagent.started",
            "task.updated",        # running frame — the active-task chip
            "subagent.progress",   # coalesced text before the tool call
            "subagent.progress",   # tool_use
            "subagent.progress",   # tool_result
            "subagent.progress",   # trailing text
            "subagent.completed",
            "task.updated",        # terminal settle belt
        ]

    def test_every_frame_carries_the_same_task_id(self, db):
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(_text("hi"), _tool_use(), _tool_result())
            await f.drain()
            await f.completed("completed", result_preview="done")

        asyncio.run(run())

        for row in _parent_rows():
            assert row["payload"]["task_id"] == WORKER, row["event_type"]

    def test_started_payload_describes_the_worker(self, db):
        f = _feed()
        asyncio.run(f.started())

        payload = _parent_rows()[0]["payload"]
        assert payload["task_id"] == WORKER
        assert payload["agent_id"] == WORKER
        # Unknown agent types render as their raw string badge on the card;
        # agent_type keeps carrying the MODEL id for compatibility.
        assert payload["agent_type"] == "gpt-5.5"
        assert payload["description"] == "reviewer"
        assert payload["prompt_preview"] == "review the diff"

    def test_started_carries_the_agent_name_when_one_was_dispatched(self, db):
        """The card's IDENTITY (2026-08-17 ruling): the agent's display name
        rides ``subagent.started`` alongside — not instead of — the model id."""
        f = _feed(agent_name="Frontend Designer")
        asyncio.run(f.started())

        payload = _parent_rows()[0]["payload"]
        assert payload["agent_name"] == "Frontend Designer"
        assert payload["agent_type"] == "gpt-5.5"

    def test_completed_forwards_the_workers_usage(self, db):
        """The card's token/duration row rendered nothing for ProviderAgent
        workers because the terminal frame carried no usage at all."""
        f = _feed()

        async def run():
            await f.started()
            await f.completed(
                "completed", result_preview="done",
                usage={"input_tokens": 120, "output_tokens": 40},
            )

        asyncio.run(run())
        done = [r for r in _parent_rows() if r["event_type"] == "subagent.completed"]
        assert done[0]["payload"]["usage"] == {
            "input_tokens": 120, "output_tokens": 40,
        }

    def test_completed_without_usage_stays_null(self, db):
        f = _feed()

        async def run():
            await f.started()
            await f.completed("completed", result_preview="done", usage={})

        asyncio.run(run())
        done = [r for r in _parent_rows() if r["event_type"] == "subagent.completed"]
        assert done[0]["payload"].get("usage") is None

    def test_started_without_an_agent_name_leaves_the_field_empty(self, db):
        """A bare ProviderAgent worker has no agent behind it — the card falls
        back to the model badge exactly as it always did."""
        f = _feed()
        asyncio.run(f.started())

        assert _parent_rows()[0]["payload"].get("agent_name") in (None, "")

    def test_started_falls_back_to_provider_and_model_without_a_label(self, db):
        f = _feed(label=None)
        asyncio.run(f.started())

        assert _parent_rows()[0]["payload"]["description"] == "prov-openai · gpt-5.5"

    def test_progress_payloads_match_what_the_card_reads(self, db):
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(
                _text("thinking"),
                _tool_use("tu-9", "Grep", pattern="needle"),
                _tool_result("tu-9", "3 matches", is_error=True),
            )
            await f.drain()

        asyncio.run(run())

        # [0] subagent.started, [1] the running task.updated.
        chunk, tool_use, tool_result = _parent_rows()[2:5]
        assert chunk["payload"]["kind"] == "chunk"
        assert chunk["payload"]["payload"]["content"] == "thinking"

        assert tool_use["payload"]["kind"] == "tool_use"
        assert tool_use["payload"]["payload"]["id"] == "tu-9"
        assert tool_use["payload"]["payload"]["name"] == "Grep"
        assert tool_use["payload"]["payload"]["input"] == {"pattern": "needle"}

        assert tool_result["payload"]["kind"] == "tool_result"
        assert tool_result["payload"]["payload"]["tool_use_id"] == "tu-9"
        assert tool_result["payload"]["payload"]["output"] == "3 matches"
        assert tool_result["payload"]["payload"]["is_error"] is True

    def test_completed_carries_a_bounded_preview_and_status(self, db):
        f = _feed()

        async def run():
            await f.started()
            await f.completed(
                "completed",
                result_preview="x" * (feed_mod.MAX_RESULT_PREVIEW_CHARS + 500),
            )

        asyncio.run(run())

        done = [r for r in _parent_rows() if r["event_type"] == "subagent.completed"][0]
        assert done["payload"]["status"] == "completed"
        assert len(done["payload"]["result_preview"]) == feed_mod.MAX_RESULT_PREVIEW_CHARS

    @pytest.mark.parametrize(
        "status,expected",
        [("completed", "completed"), ("error", "failed"), ("killed", "failed")],
    )
    def test_failure_statuses_render_as_the_error_card(self, db, status, expected):
        f = _feed()
        asyncio.run(f.completed(status, result_preview="r"))

        done = [r for r in _parent_rows() if r["event_type"] == "subagent.completed"][0]
        assert done["payload"]["status"] == expected

    @pytest.mark.parametrize("status", ["completed", "error", "killed"])
    def test_settle_state_stays_in_the_reducers_terminal_set(self, db, status):
        """reducer.ts:1202 tests status against exactly completed/error/killed —
        anything else never settles the joined card on replay."""
        f = _feed()
        asyncio.run(f.completed(status, result_preview="r"))

        settle = [r for r in _parent_rows() if r["event_type"] == "task.updated"][0]
        assert settle["payload"]["state"] == status

    def test_settle_frame_joins_the_card_through_tool_use_id(self, db):
        """The `/live` overlay and the terminal chip both join on tool_use_id."""
        f = _feed()
        asyncio.run(f.completed("completed", result_preview="r"))

        settle = [r for r in _parent_rows() if r["event_type"] == "task.updated"][0]
        assert settle["payload"]["task_id"] == WORKER
        assert settle["payload"]["tool_use_id"] == WORKER
        assert settle["payload"]["state"] == "completed"

    def test_started_and_completed_are_idempotent(self, db):
        f = _feed()

        async def run():
            await f.started()
            await f.started()
            await f.completed("completed", result_preview="r")
            await f.completed("failed", result_preview="r2")

        asyncio.run(run())

        types = _types(_parent_rows())
        assert types.count("subagent.started") == 1
        assert types.count("subagent.completed") == 1
        # One running frame and one terminal settle — never a second of either.
        assert types.count("task.updated") == 2


# ---------------------------------------------------------------------------
# The RUNNING task.updated — what puts the worker on the active-task strip
# ---------------------------------------------------------------------------

class TestRunningTaskFrame:
    """``deriveActiveTasks`` is built only from ``task.updated`` summaries in
    state ``running``, so without this frame a ProviderAgent worker never
    reached the background-tasks strip, the spinner overlay or the StopButton —
    only its terminal settle arrived, and a terminal summary adds no chip."""

    def _running(self):
        return [
            r for r in _parent_rows()
            if r["event_type"] == "task.updated"
            and r["payload"]["state"] == "running"
        ]

    def test_started_emits_exactly_one_running_frame(self, db):
        f = _feed()
        asyncio.run(f.started())

        running = self._running()
        assert len(running) == 1
        assert running[0]["payload"]["task_id"] == WORKER
        assert running[0]["payload"]["tool_use_id"] == WORKER

    def test_running_frame_describes_the_worker_by_its_label(self, db):
        """``description`` is what the reducer stores as the Task's subject."""
        f = _feed()
        asyncio.run(f.started())

        assert self._running()[0]["payload"]["description"] == "reviewer"

    def test_running_frame_falls_back_to_provider_and_model_without_a_label(self, db):
        f = _feed(label=None)
        asyncio.run(f.started())

        assert self._running()[0]["payload"]["description"] == "prov-openai · gpt-5.5"

    def test_running_and_terminal_frames_share_both_join_keys(self, db):
        """The terminal settle only clears the chip the running frame created if
        both land on the same task_id AND the same tool_use_id."""
        f = _feed()

        async def run():
            await f.started()
            await f.completed("completed", result_preview="r")

        asyncio.run(run())

        settles = [r for r in _parent_rows() if r["event_type"] == "task.updated"]
        assert [s["payload"]["state"] for s in settles] == ["running", "completed"]
        assert {s["payload"]["task_id"] for s in settles} == {WORKER}
        assert {s["payload"]["tool_use_id"] for s in settles} == {WORKER}

    def test_running_frame_precedes_the_first_progress_frame(self, db):
        """The chip must exist before any progress arrives, or the strip flickers
        in only once the worker happens to say something."""
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(_text("thinking"))
            await f.drain()

        asyncio.run(run())

        rows = _parent_rows()
        running_seq = self._running()[0]["seq"]
        first_progress = [
            r for r in rows if r["event_type"] == "subagent.progress"
        ][0]
        assert running_seq < first_progress["seq"]


# ---------------------------------------------------------------------------
# Fencing
# ---------------------------------------------------------------------------

class TestUnfencedAppend:

    def test_forwarding_succeeds_while_the_parent_holds_a_fenced_run(self, db):
        run = runs.claim_run(PARENT, "runner-A", 60)
        se.append_events(
            PARENT, [{"type": "turn.started", "payload": {}}],
            run_id=run["id"], fence_token=run["fence_token"],
        )

        f = _feed()

        async def go():
            await f.started()
            _seed_worker(_tool_use())
            await f.drain()
            await f.completed("completed", result_preview="r")

        asyncio.run(go())

        rows = _parent_rows()
        assert "subagent.started" in _types(rows)
        # Forwarded rows are unfenced: no owning run.
        for row in rows:
            if row["event_type"].startswith("subagent."):
                assert row["run_id"] is None

        # The parent's fenced run is undisturbed and still appends.
        se.append_events(
            PARENT, [{"type": "turn.completed", "payload": {}}],
            run_id=run["id"], fence_token=run["fence_token"],
        )
        assert _types(_parent_rows())[-1] == "turn.completed"

    def test_a_stale_fence_still_raises_for_a_fenced_writer(self, db):
        """Sanity: the forwarding path is unfenced, not the fence disabled."""
        run = runs.claim_run(PARENT, "runner-A", 60)
        asyncio.run(_feed().started())

        with pytest.raises(se.FenceViolation):
            se.append_events(
                PARENT, [{"type": "turn.started", "payload": {}}],
                run_id=run["id"], fence_token=run["fence_token"] + 1,
            )


# ---------------------------------------------------------------------------
# The worker's own log is read-only to the tap
# ---------------------------------------------------------------------------

class TestWorkerLogIsUntouched:

    def test_worker_log_is_complete_and_unmodified(self, db):
        seeded = [
            _text("alpha"),
            _tool_use("t1", "Bash", command="ls"),
            _tool_result("t1", "a\nb"),
            _text("omega"),
            {"type": "turn.completed", "payload": {"usage": {"tokens_in": 3}}},
        ]
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(*seeded)
            await f.drain()
            await f.completed("completed", result_preview="omega")

        asyncio.run(run())

        worker_rows = se.get_events(WORKER)
        assert _types(worker_rows) == [e["type"] for e in seeded]
        assert [r["seq"] for r in worker_rows] == [1, 2, 3, 4, 5]
        # Full fidelity retained — the parent gets the bounded copy, not the log.
        assert worker_rows[2]["payload"]["content_preview"] == "a\nb"

    def test_parent_and_worker_allocate_seqs_independently(self, db):
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(_text("a"))
            await f.drain()

        asyncio.run(run())

        # started() writes two rows (subagent.started + the running task.updated),
        # the drain a third; the worker's own counter is untouched by all of it.
        assert [r["seq"] for r in _parent_rows()] == [1, 2, 3]
        assert [r["seq"] for r in se.get_events(WORKER)] == [1]


# ---------------------------------------------------------------------------
# Bounding
# ---------------------------------------------------------------------------

class TestProgressBounding:

    def test_consecutive_text_is_coalesced_into_one_frame(self, db):
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(*[_text(f"delta-{i} ") for i in range(50)])
            await f.drain()

        asyncio.run(run())

        progress = [r for r in _parent_rows() if r["event_type"] == "subagent.progress"]
        assert len(progress) == 1
        assert progress[0]["payload"]["payload"]["content"].startswith("delta-0 ")

    def test_frame_count_is_capped_with_a_single_note(self, db, monkeypatch):
        monkeypatch.setattr(feed_mod, "MAX_PROGRESS_FRAMES", 4)
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(*[_tool_use(f"t{i}", "Read") for i in range(40)])
            await f.drain()
            # A second drain after the cap forwards nothing more.
            _seed_worker(*[_tool_use(f"u{i}", "Read") for i in range(10)])
            await f.drain()

        asyncio.run(run())

        progress = [r for r in _parent_rows() if r["event_type"] == "subagent.progress"]
        # 4 forwarded frames + exactly one cap note.
        assert len(progress) == 5
        assert f.capped is True
        assert progress[-1]["payload"]["payload"]["content"] == feed_mod._CAP_NOTE

    def test_total_forwarded_text_is_capped(self, db, monkeypatch):
        monkeypatch.setattr(feed_mod, "MAX_PROGRESS_TEXT_CHARS", 40)
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(_text("y" * 500), _tool_use("t1"), _text("z" * 500))
            await f.drain()

        asyncio.run(run())

        chunks = [
            r for r in _parent_rows()
            if r["event_type"] == "subagent.progress"
            and r["payload"]["kind"] == "chunk"
        ]
        notes = (feed_mod._CAP_NOTE, feed_mod._TEXT_CAP_NOTE)
        forwarded = "".join(
            c["payload"]["payload"]["content"] for c in chunks
            if c["payload"]["payload"]["content"] not in notes
        )
        assert len(forwarded) <= 40

    def test_text_cap_is_soft_tool_frames_keep_streaming(self, db, monkeypatch):
        """Exhausting the text budget silences TEXT only — tool activity keeps
        the card live, with exactly one text-cap note."""
        monkeypatch.setattr(feed_mod, "MAX_PROGRESS_TEXT_CHARS", 10)
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(
                _text("x" * 50), _tool_use("t1", "Read"),
                _text("y" * 50), _tool_use("t2", "Bash"),
            )
            await f.drain()

        asyncio.run(run())

        progress = [r for r in _parent_rows() if r["event_type"] == "subagent.progress"]
        kinds = [r["payload"]["kind"] for r in progress]
        assert kinds.count("tool_use") == 2  # both tool frames survived the cap
        notes = [
            r for r in progress
            if r["payload"]["kind"] == "chunk"
            and r["payload"]["payload"]["content"] == feed_mod._TEXT_CAP_NOTE
        ]
        assert len(notes) == 1
        assert f.capped is False  # the feed itself is NOT latched quiet
        assert f.wants_rows is True

    def test_long_coalesced_text_splits_into_chunk_frames(self, db, monkeypatch):
        """Text beyond one frame's clip is chunked, not discarded."""
        monkeypatch.setattr(feed_mod, "MAX_CHUNK_CHARS", 30)
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(_text("q" * 100))
            await f.drain()

        asyncio.run(run())

        chunks = [
            r["payload"]["payload"]["content"]
            for r in _parent_rows() if r["event_type"] == "subagent.progress"
        ]
        assert "".join(chunks) == "q" * 100
        assert all(len(c) <= 30 for c in chunks)

    def test_a_single_chunk_frame_is_capped(self, db, monkeypatch):
        monkeypatch.setattr(feed_mod, "MAX_CHUNK_CHARS", 30)
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(_text("q" * 5000))
            await f.drain()

        asyncio.run(run())

        chunk = [r for r in _parent_rows() if r["event_type"] == "subagent.progress"][0]
        assert len(chunk["payload"]["payload"]["content"]) == 30

    def test_long_tool_input_strings_are_clipped(self, db, monkeypatch):
        monkeypatch.setattr(feed_mod, "MAX_TOOL_INPUT_CHARS", 20)
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(_tool_use("t1", "Bash", command="echo " + "x" * 900))
            await f.drain()

        asyncio.run(run())

        tu = [r for r in _parent_rows() if r["event_type"] == "subagent.progress"][0]
        assert len(tu["payload"]["payload"]["input"]["command"]) == 20

    def test_worker_lifecycle_and_nested_rows_are_not_mirrored(self, db):
        """Only text / tool activity is summarised; the rest stays worker-side."""
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(
                {"type": "turn.started", "payload": {}},
                {"type": "assistant.thinking_delta", "payload": {"text": "hmm"}},
                {"type": "turn.usage", "payload": {"usage": {}}},
                {"type": "subagent.progress", "payload": {"task_id": "nested"}},
                {"type": "turn.completed", "payload": {}},
            )
            await f.drain()

        asyncio.run(run())

        assert _types(_parent_rows()) == ["subagent.started", "task.updated"]

    def test_the_cursor_advances_so_rows_are_never_forwarded_twice(self, db):
        f = _feed()

        async def run():
            await f.started()
            _seed_worker(_tool_use("t1"))
            await f.drain()
            await f.drain()
            _seed_worker(_tool_use("t2"))
            await f.drain()

        asyncio.run(run())

        ids = [
            r["payload"]["payload"]["id"] for r in _parent_rows()
            if r["event_type"] == "subagent.progress"
        ]
        assert ids == ["t1", "t2"]


# ---------------------------------------------------------------------------
# Follow lifecycle / teardown
# ---------------------------------------------------------------------------

class TestFollowLifecycle:

    def test_following_forwards_without_an_explicit_drain(self, db, monkeypatch):
        monkeypatch.setattr(feed_mod, "POLL_INTERVAL_SECONDS", 0.01)
        f = _feed()

        async def run():
            await f.started()
            f.start_following()
            _seed_worker(_tool_use("t1"))
            await asyncio.sleep(0.05)
            await f.stop_following()

        asyncio.run(run())

        assert "subagent.progress" in _types(_parent_rows())

    def test_stop_following_does_a_final_drain(self, db, monkeypatch):
        monkeypatch.setattr(feed_mod, "POLL_INTERVAL_SECONDS", 30)
        f = _feed()

        async def run():
            await f.started()
            f.start_following()
            _seed_worker(_tool_use("t-last"))
            # No tick can have fired at a 30s interval — only the final drain can
            # surface this row.
            await f.stop_following()

        asyncio.run(run())

        ids = [
            r["payload"]["payload"]["id"] for r in _parent_rows()
            if r["event_type"] == "subagent.progress"
        ]
        assert ids == ["t-last"]

    def test_start_following_without_a_loop_is_a_no_op(self, db):
        f = _feed()
        f.start_following()  # no running loop — must not raise
        assert f.frames_forwarded == 0


# ---------------------------------------------------------------------------
# The turn-boundary hazard
# ---------------------------------------------------------------------------

class TestTurnBoundarySettling:

    def test_terminal_frames_land_after_the_parent_turn_has_completed(self, db):
        """A worker settling past the parent's turn boundary still settles.

        `turn.completed` for the parent is emitted before `_finalize` runs, so
        the terminal frames can legitimately land after it. They must still be
        appended (unfenced, so the released run cannot reject them) and must
        still carry the join key.
        """
        f = _feed()

        async def run():
            await f.started()
            # The parent's turn ends while the worker is still live.
            se.append_events(PARENT, [{"type": "turn.completed", "payload": {}}])
            await f.completed("completed", result_preview="late but landed")

        asyncio.run(run())

        types = _types(_parent_rows())
        assert types == [
            "subagent.started", "task.updated",
            "turn.completed",
            "subagent.completed", "task.updated",
        ]
        done = _parent_rows()[3]
        assert done["payload"]["task_id"] == WORKER
        assert done["payload"]["result_preview"] == "late but landed"

    def test_emit_shielded_lands_the_frame_when_the_caller_is_cancelled(self, db):
        """`emit_shielded` must survive the cancellation that triggered it.

        The MCP handler is a tracked child of the parent's SDK client and is
        cancelled when that client closes; the reducer speculatively settles
        stale cards on `turn.completed` but NOT on `turn.aborted`, so a lost
        terminal frame there means a permanently spinning card.
        """
        f = _feed()
        outcome = {}

        async def run():
            await f.started()

            async def body():
                try:
                    await asyncio.sleep(10)
                finally:
                    await feed_mod.emit_shielded(
                        f.completed("killed", result_preview="stopped")
                    )


            task = asyncio.create_task(body())
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                outcome["cancelled"] = True
            # Let any still-shielded append finish.
            for _ in range(20):
                await asyncio.sleep(0)

        asyncio.run(run())

        assert outcome["cancelled"] is True
        types = _types(_parent_rows())
        assert "subagent.completed" in types
        assert "task.updated" in types

    def test_emit_shielded_swallows_a_failing_append(self, db, monkeypatch):
        async def boom():
            raise RuntimeError("nope")

        asyncio.run(feed_mod.emit_shielded(boom()))  # must not raise


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class TestRobustness:

    def test_a_worker_log_read_failure_degrades_to_no_progress(self, db, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(se, "get_events", _boom)
        f = _feed()

        async def run():
            await f.drain()

        asyncio.run(run())  # must not raise

    def test_an_append_failure_never_propagates(self, db, monkeypatch):
        from app.db import session_events as sedb

        def _boom(*a, **k):
            raise RuntimeError("append down")

        monkeypatch.setattr(sedb, "append_events", _boom)
        f = _feed()

        async def run():
            await f.started()
            await f.completed("completed", result_preview="r")

        asyncio.run(run())  # must not raise
