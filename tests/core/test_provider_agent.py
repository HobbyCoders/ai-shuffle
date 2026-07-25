"""Unit tests for :mod:`app.core.provider_agent` (multi-provider phase 3, P3-A).

Covers the three framework-free responsibilities:

* ``resolve_provider_instance`` — id match, name match, ambiguity, miss;
* ``validate_provider_model`` — every rejection reason plus the happy path;
* ``dispatch_worker`` — the worker lifecycle and its failure modes.

NOTHING here spawns a session, a CLI or a network call: ``turn_manager`` and the
DB layer are stubbed. The task registry is the REAL one — the ``kind="agent"``
registration is load-bearing (it is what pins the parent in the session pool),
so it is asserted against the real predicate rather than a mock.
"""

import asyncio

import pytest

from app.core import provider_agent as pa


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _provider(
    provider_id="prov-openai",
    name="My OpenAI",
    provider_type="openai",
    enabled=True,
    model_ids=("gpt-5.5", "gpt-5.5-mini"),
    default_model=None,
):
    return {
        "id": provider_id,
        "name": name,
        "provider_type": provider_type,
        "enabled": enabled,
        "default_model": default_model,
        "models_json": [{"id": m, "label": m} for m in model_ids],
    }


@pytest.fixture(autouse=True)
def _clean_worker_registry():
    pa.reset_worker_sessions()
    yield
    pa.reset_worker_sessions()


@pytest.fixture
def enabled_providers(monkeypatch):
    """Patch the enabled-provider listing; returns a mutable list."""
    rows = []

    from app.db import database as db

    monkeypatch.setattr(db, "get_enabled_model_providers", lambda *a, **k: list(rows))
    return rows


# ---------------------------------------------------------------------------
# resolve_provider_instance
# ---------------------------------------------------------------------------

class TestResolveProviderInstance:

    def test_exact_id_match(self, enabled_providers):
        enabled_providers.append(_provider())

        result = pa.resolve_provider_instance("prov-openai")

        assert result.ok
        assert result.provider["id"] == "prov-openai"

    def test_case_insensitive_name_match(self, enabled_providers):
        enabled_providers.append(_provider())

        result = pa.resolve_provider_instance("  my openai ")

        assert result.ok
        assert result.provider["id"] == "prov-openai"

    def test_id_match_wins_over_name_match(self, enabled_providers):
        """A row whose NAME collides with another row's ID must not shadow the
        id match — ids are the unambiguous key."""
        enabled_providers.append(_provider(provider_id="alpha", name="beta"))
        enabled_providers.append(_provider(provider_id="beta", name="Beta Corp"))

        result = pa.resolve_provider_instance("beta")

        assert result.ok
        assert result.provider["id"] == "beta"

    def test_ambiguous_name_is_an_error_listing_candidates(self, enabled_providers):
        enabled_providers.append(_provider(provider_id="p1", name="Codex"))
        enabled_providers.append(_provider(provider_id="p2", name="codex"))

        result = pa.resolve_provider_instance("Codex")

        assert not result.ok
        assert result.provider is None
        assert result.candidates == ("p1", "p2")
        assert "p1" in result.error and "p2" in result.error

    def test_miss_lists_known_providers(self, enabled_providers):
        enabled_providers.append(_provider(provider_id="p1", name="Codex"))

        result = pa.resolve_provider_instance("nope")

        assert not result.ok
        assert "nope" in result.error
        assert "p1" in result.error

    def test_no_enabled_providers(self, enabled_providers):
        result = pa.resolve_provider_instance("anything")

        assert not result.ok
        assert "No model providers are enabled" in result.error

    def test_blank_instance(self, enabled_providers):
        result = pa.resolve_provider_instance("   ")

        assert not result.ok
        assert "required" in result.error

    def test_lookup_failure_is_an_error_not_a_raise(self, monkeypatch):
        from app.db import database as db

        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "get_enabled_model_providers", _boom)

        result = pa.resolve_provider_instance("prov-openai")

        assert not result.ok
        assert "db down" in result.error

    def test_disabled_providers_are_invisible(self, enabled_providers):
        """The resolver reads only ENABLED rows, so a disabled provider is a
        miss here; validate_provider_model is what says 'disabled'."""
        # get_enabled_model_providers never returns it, so the list stays empty.
        result = pa.resolve_provider_instance("prov-openai")

        assert not result.ok


# ---------------------------------------------------------------------------
# validate_provider_model
# ---------------------------------------------------------------------------

class TestValidateProviderModel:

    def test_happy_path(self):
        result = pa.validate_provider_model(
            "prov-openai", "gpt-5.5", provider_row=_provider()
        )

        assert result.ok
        assert result.code is None
        assert result.provider_label == "My OpenAI"
        assert result.curated_ids == ("gpt-5.5", "gpt-5.5-mini")

    def test_missing_provider(self):
        result = pa.validate_provider_model("ghost", "gpt-5.5", provider_row=None)

        assert not result.ok
        assert result.code == pa.PROVIDER_MISSING
        assert result.reason == "The provider 'ghost' does not exist."

    def test_disabled_provider(self):
        result = pa.validate_provider_model(
            "prov-openai", "gpt-5.5", provider_row=_provider(enabled=False)
        )

        assert not result.ok
        assert result.code == pa.PROVIDER_DISABLED
        assert "disabled" in result.reason

    def test_non_servable_provider(self):
        result = pa.validate_provider_model(
            "prov-or", "gpt-5.5", provider_row=_provider(provider_type="openrouter")
        )

        assert not result.ok
        assert result.code == pa.PROVIDER_NOT_SERVABLE
        assert "gateway" in result.reason

    def test_chatgpt_auth_mode_is_servable(self):
        """``auth_mode == 'chatgpt'`` is servable even with an odd type — mirrors
        is_gateway_servable."""
        row = _provider(provider_type="custom")
        row["auth_mode"] = "chatgpt"

        assert pa.validate_provider_model("p", "gpt-5.5", provider_row=row).ok

    def test_model_not_curated(self):
        result = pa.validate_provider_model(
            "prov-openai", "gpt-nope", provider_row=_provider()
        )

        assert not result.ok
        assert result.code == pa.MODEL_NOT_CURATED
        assert "curated" in result.reason

    def test_empty_curated_list_rejects_everything(self):
        result = pa.validate_provider_model(
            "prov-openai", "gpt-5.5", provider_row=_provider(model_ids=())
        )

        assert not result.ok
        assert result.code == pa.MODEL_NOT_CURATED

    def test_models_json_as_raw_string_is_parsed(self):
        import json

        row = _provider()
        row["models_json"] = json.dumps(row["models_json"])

        assert pa.validate_provider_model("p", "gpt-5.5", provider_row=row).ok

    def test_unparseable_models_json_rejects(self):
        row = _provider()
        row["models_json"] = "{not json"

        result = pa.validate_provider_model("p", "gpt-5.5", provider_row=row)
        assert result.code == pa.MODEL_NOT_CURATED

    def test_row_is_looked_up_when_not_supplied(self, monkeypatch):
        from app.db import database as db

        seen = {}

        def _get(pid, include_token=False):
            seen["pid"] = pid
            seen["include_token"] = include_token
            return _provider()

        monkeypatch.setattr(db, "get_model_provider", _get)

        assert pa.validate_provider_model("prov-openai", "gpt-5.5").ok
        assert seen == {"pid": "prov-openai", "include_token": False}

    def test_lookup_failure_reads_as_missing(self, monkeypatch):
        from app.db import database as db

        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "get_model_provider", _boom)

        result = pa.validate_provider_model("prov-openai", "gpt-5.5")
        assert result.code == pa.PROVIDER_MISSING

    def test_module_is_framework_free(self):
        """Framework-free by contract: the module imports no web framework, so it
        cannot raise ``HTTPException`` and stays reusable by non-API callers."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(pa))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
                imported.update(alias.name for alias in node.names)

        assert "fastapi" not in imported
        assert "starlette" not in imported
        assert "HTTPException" not in imported


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_default_model_prefers_curated_default(self):
        row = _provider(default_model="gpt-5.5-mini")
        assert pa.default_model_for_provider(row) == "gpt-5.5-mini"

    def test_default_model_falls_back_to_first_curated(self):
        row = _provider(default_model="not-curated")
        assert pa.default_model_for_provider(row) == "gpt-5.5"

    def test_default_model_none_without_curated_models(self):
        assert pa.default_model_for_provider(_provider(model_ids=())) is None

    def test_bound_report_keeps_the_tail(self):
        """Truncation keeps the END — the conclusion is the part the parent needs."""
        text = "x" * (pa.MAX_REPORT_CHARS + 500) + "CONCLUSION"
        body, truncated = pa.bound_report(text)

        assert truncated is True
        assert len(body) <= pa.MAX_REPORT_CHARS
        assert body.endswith("CONCLUSION")

    def test_bound_report_short_text_untouched(self):
        body, truncated = pa.bound_report("  hello  ")
        assert (body, truncated) == ("hello", False)

    def test_worker_session_registry_roundtrip(self):
        assert not pa.is_worker_session("s1")
        pa.register_worker_session("s1")
        assert pa.is_worker_session("s1")
        assert not pa.is_worker_session(None)
        pa.reset_worker_sessions()
        assert not pa.is_worker_session("s1")

    def test_worker_session_registry_is_bounded(self, monkeypatch):
        monkeypatch.setattr(pa, "_WORKER_SESSION_LIMIT", 3)
        for i in range(5):
            pa.register_worker_session(f"s{i}")

        assert not pa.is_worker_session("s0")
        assert not pa.is_worker_session("s1")
        assert pa.is_worker_session("s4")

    def test_timeout_is_configurable_and_floored(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "provider_agent_timeout_seconds", 1234)
        assert pa.worker_timeout_seconds() == 1234.0

        monkeypatch.setattr(settings, "provider_agent_timeout_seconds", 1)
        assert pa.worker_timeout_seconds() == 30.0


# ---------------------------------------------------------------------------
# dispatch_worker
# ---------------------------------------------------------------------------

class _FakeTurnManager:
    def __init__(self, idle=True):
        self.idle = idle
        self.submitted = []
        self.interrupts = []
        self.on_wait = None

    def submit(self, session_id, source, **kwargs):
        self.submitted.append({"session_id": session_id, "source": source, **kwargs})
        return "turn-1"

    async def wait_session_idle(self, session_id, timeout=300.0):
        if self.on_wait is not None:
            self.on_wait(session_id)
        return self.idle

    def interrupt(self, session_id, mode, **kwargs):
        self.interrupts.append((session_id, mode))
        return None


@pytest.fixture
def dispatch_env(monkeypatch):
    """Stub every out-of-process dependency of dispatch_worker.

    Returns a namespace with the fake turn manager and the recorded
    ``create_session`` kwargs.
    """
    from app.core import turn_manager as tm_mod
    from app.core import project_resolver
    from app.db import database as db

    created = {}
    fake_tm = _FakeTurnManager()

    monkeypatch.setattr(tm_mod, "submit", fake_tm.submit)
    monkeypatch.setattr(tm_mod, "wait_session_idle", fake_tm.wait_session_idle)
    monkeypatch.setattr(tm_mod, "interrupt", fake_tm.interrupt)

    monkeypatch.setattr(
        db, "get_session",
        lambda sid: {
            "id": sid,
            "project_id": "proj-1",
            "worktree_id": "wt-1",
            "worktree_branch": "feat/x",
            "worktree_path": "/repo/feat-x",
        },
    )

    def _create_session(**kwargs):
        created.update(kwargs)
        return {"id": kwargs.get("session_id")}

    monkeypatch.setattr(db, "create_session", _create_session)
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: "/repo/feat-x")
    monkeypatch.setattr(pa, "final_turn_outcome", lambda sid: {"usage": {"tokens_in": 5}, "error": None, "interrupted": False})
    monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "the worker's report")

    class _Env:
        tm = fake_tm
        create_kwargs = created

    return _Env()


def _dispatch(**overrides):
    kwargs = dict(
        parent_session_id="parent-1",
        profile_id="default",
        provider_id="prov-openai",
        model_id="gpt-5.5",
        prompt="do the thing",
    )
    kwargs.update(overrides)
    return asyncio.run(pa.dispatch_worker(**kwargs))


class TestDispatchWorker:

    def test_happy_path(self, dispatch_env):
        outcome = _dispatch(label="reviewer")

        assert outcome.ok
        assert outcome.report == "the worker's report"
        assert outcome.truncated is False
        assert outcome.usage == {"tokens_in": 5}
        assert outcome.session_id

    def test_worker_session_is_bound_to_the_provider(self, dispatch_env):
        outcome = _dispatch()

        created = dispatch_env.create_kwargs
        assert created["provider_id"] == "prov-openai"
        assert created["parent_session_id"] == "parent-1"
        assert created["profile_id"] == "default"
        assert created["project_id"] == "proj-1"
        assert created["worktree_id"] == "wt-1"
        assert created["session_id"] == outcome.session_id

    def test_turn_carries_provider_and_model_overrides(self, dispatch_env):
        _dispatch()

        submitted = dispatch_env.tm.submitted[0]
        assert submitted["source"] == pa.WORKER_TURN_SOURCE
        assert submitted["prompt"] == "do the thing"
        assert submitted["overrides"]["provider"] == "prov-openai"
        assert submitted["overrides"]["model"] == "gpt-5.5"
        # cwd inherited from the parent's ALREADY-materialized checkout
        assert submitted["overrides"]["cwd"] == "/repo/feat-x"

    def test_worker_is_registered_for_the_nesting_guard(self, dispatch_env):
        outcome = _dispatch()

        assert pa.is_worker_session(outcome.session_id)

    def test_registry_entry_is_kind_agent_while_running(self, dispatch_env):
        """The registry entry is what pins the parent in the session pool; it must
        exist with kind=="agent" WHILE the worker runs, and be terminal after."""
        from app.core.task_registry import task_registry

        seen = {}

        def _on_wait(_sid):
            seen["pinned"] = task_registry.has_active_background_work("parent-1")
            seen["agent"] = task_registry.has_active_background_agent("parent-1")

        dispatch_env.tm.on_wait = _on_wait

        outcome = _dispatch()

        assert seen == {"pinned": True, "agent": True}
        record = task_registry.get_task("parent-1", outcome.session_id)
        assert record["kind"] == "agent"
        assert record["status"] == "completed"
        task_registry.clear_session("parent-1")

    def test_truncated_report_is_flagged(self, dispatch_env, monkeypatch):
        monkeypatch.setattr(
            pa, "final_assistant_text",
            lambda sid: "y" * (pa.MAX_REPORT_CHARS + 100) + "END",
        )

        outcome = _dispatch()

        assert outcome.ok
        assert outcome.truncated is True
        assert outcome.report.endswith("END")

    # -- failure modes ----------------------------------------------------

    def test_timeout_fails_and_interrupts(self, dispatch_env):
        from app.core.task_registry import task_registry

        dispatch_env.tm.idle = False

        outcome = _dispatch(timeout_seconds=5)

        assert not outcome.ok
        assert "did not finish within 5s" in outcome.error
        assert dispatch_env.tm.interrupts == [(outcome.session_id, "stop")]
        assert task_registry.get_task("parent-1", outcome.session_id)["status"] == "error"
        task_registry.clear_session("parent-1")

    def test_worker_turn_error_fails(self, dispatch_env, monkeypatch):
        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {"usage": {}, "error": "provider returned 429", "interrupted": False},
        )

        outcome = _dispatch()

        assert not outcome.ok
        assert outcome.error == "provider returned 429"
        assert outcome.session_id

    def test_worker_interrupted_fails(self, dispatch_env, monkeypatch):
        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {"usage": {}, "error": None, "interrupted": True},
        )

        outcome = _dispatch()

        assert not outcome.ok
        assert "interrupted" in outcome.error

    def test_session_creation_failure_fails_fast(self, dispatch_env, monkeypatch):
        from app.db import database as db

        def _boom(**kwargs):
            raise RuntimeError("insert failed")

        monkeypatch.setattr(db, "create_session", _boom)

        outcome = _dispatch()

        assert not outcome.ok
        assert "insert failed" in outcome.error
        # Nothing was submitted and no worker id was registered.
        assert dispatch_env.tm.submitted == []
        assert not pa.is_worker_session(outcome.session_id)

    def test_submit_failure_fails_without_raising(self, dispatch_env, monkeypatch):
        from app.core import turn_manager as tm_mod
        from app.core.task_registry import task_registry

        def _boom(*a, **k):
            raise RuntimeError("scheduler down")

        monkeypatch.setattr(tm_mod, "submit", _boom)

        outcome = _dispatch()

        assert not outcome.ok
        assert "scheduler down" in outcome.error
        assert task_registry.get_task("parent-1", outcome.session_id)["status"] == "error"
        task_registry.clear_session("parent-1")

    def test_cancellation_stops_the_worker_and_propagates(self, dispatch_env, monkeypatch):
        """The handler runs as a cancellable child of the parent's SDK client; a
        cancel must stop the worker rather than strand it."""
        from app.core.task_registry import task_registry

        async def _cancelled(session_id, timeout=300.0):
            raise asyncio.CancelledError()

        monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "")
        from app.core import turn_manager as tm_mod
        monkeypatch.setattr(tm_mod, "wait_session_idle", _cancelled)

        with pytest.raises(asyncio.CancelledError):
            _dispatch()

        assert len(dispatch_env.tm.interrupts) == 1
        assert dispatch_env.tm.interrupts[0][1] == "stop"
        killed = [
            r for r in task_registry.get_all("parent-1")
            if r["status"] == "killed"
        ]
        assert killed
        task_registry.clear_session("parent-1")


# ---------------------------------------------------------------------------
# Parent-feed forwarding (chunk P3-A2)
# ---------------------------------------------------------------------------

@pytest.fixture
def feed_db():
    """Real (SQLite-shimmed) event log so forwarded rows can be read back."""
    from tests.pg_shim import new_sqlite_connection, route_db_to

    conn = new_sqlite_connection()
    with route_db_to(conn):
        yield conn
    conn.close()


def _parent_events():
    from app.db import session_events as se

    return se.get_events("parent-1")


class TestDispatchWorkerParentFeed:
    """``dispatch_worker`` must make the EXISTING subagent card render.

    The card is built from contract events, not from the task registry
    (reducer.ts:996/:1026/:1094), so these assert the events — and the ONE join
    key that ties the card, the ``/live`` spinner overlay and the terminal
    settle together.
    """

    def test_the_event_triple_lands_on_the_parent(self, dispatch_env, feed_db):
        from app.core.task_registry import task_registry
        from app.db import session_events as se

        outcome = _dispatch(label="reviewer")
        worker = outcome.session_id

        types = [e["event_type"] for e in _parent_events()]
        assert types[0] == "subagent.started"
        assert "subagent.completed" in types
        assert types[-1] == "task.updated"
        assert se.get_events(worker) == []  # nothing written to the worker log

        for row in _parent_events():
            assert row["payload"]["task_id"] == worker
        task_registry.clear_session("parent-1")

    def test_one_join_key_across_card_registry_and_settle(self, dispatch_env, feed_db):
        """card.toolId == registry.task_id == registry.tool_use_id.

        The `/live` overlay matches ``activeTasks[].toolUseId`` against the
        card's ``toolId`` (SubagentMessage.svelte:37-44), and the terminal
        ``task.updated`` settle joins the same way (reducer.ts:1203) — a
        registry entry with no ``tool_use_id`` could never settle its card.
        """
        from app.core.task_registry import task_registry

        outcome = _dispatch()
        worker = outcome.session_id

        record = task_registry.get_task("parent-1", worker)
        assert record["id"] == worker
        # `/live` serves this as activeTasks[].toolUseId — the card join key.
        assert record["toolUseId"] == worker
        assert task_registry.get_task_tool_use_id("parent-1", worker) == worker

        started = _parent_events()[0]
        assert started["event_type"] == "subagent.started"
        assert started["payload"]["task_id"] == worker

        settle = [e for e in _parent_events() if e["event_type"] == "task.updated"][0]
        assert settle["payload"]["tool_use_id"] == worker
        assert settle["payload"]["state"] == "completed"
        task_registry.clear_session("parent-1")

    def test_an_explicit_tool_use_id_still_wins(self, dispatch_env, feed_db):
        from app.core.task_registry import task_registry

        outcome = _dispatch(tool_use_id="toolu_abc")

        record = task_registry.get_task("parent-1", outcome.session_id)
        assert record["toolUseId"] == "toolu_abc"
        task_registry.clear_session("parent-1")

    def test_worker_progress_is_summarised_onto_the_parent(
        self, dispatch_env, feed_db, monkeypatch
    ):
        """A worker that produces tool activity renders children on the card."""
        from app.core.task_registry import task_registry
        from app.db import session_events as se

        def _submit(session_id, source, **kwargs):
            se.append_events(session_id, [
                {"type": "assistant.text_delta", "payload": {"text": "working"}},
                {"type": "tool.use", "payload": {
                    "tool_use_id": "t1", "name": "Read", "input": {"file_path": "/a"},
                }},
                {"type": "tool.result", "payload": {
                    "tool_use_id": "t1", "is_error": False, "content_preview": "body",
                }},
            ])
            return "turn-1"

        from app.core import turn_manager as tm_mod
        monkeypatch.setattr(tm_mod, "submit", _submit)

        outcome = _dispatch()

        kinds = [
            e["payload"]["kind"] for e in _parent_events()
            if e["event_type"] == "subagent.progress"
        ]
        assert kinds == ["chunk", "tool_use", "tool_result"]
        # The worker's own log keeps its three rows, untouched.
        assert len(se.get_events(outcome.session_id)) == 3
        task_registry.clear_session("parent-1")

    def test_the_card_settles_even_when_the_parent_turn_is_cancelled(
        self, dispatch_env, feed_db, monkeypatch
    ):
        """Cancellation is the case with no reducer safety net.

        ``settleStaleBackgroundWork`` speculatively settles running cards on
        ``turn.completed`` only — ``turn.aborted`` (reducer.ts:609) does not
        touch subagent cards — so the terminal frame MUST land here or the card
        spins forever on replay.
        """
        from app.core.task_registry import task_registry

        async def _cancelled(session_id, timeout=300.0):
            raise asyncio.CancelledError()

        from app.core import turn_manager as tm_mod
        monkeypatch.setattr(tm_mod, "wait_session_idle", _cancelled)

        async def _run():
            with pytest.raises(asyncio.CancelledError):
                await pa.dispatch_worker(
                    parent_session_id="parent-1",
                    profile_id="default",
                    provider_id="prov-openai",
                    model_id="gpt-5.5",
                    prompt="do the thing",
                )
            # Let the shielded terminal append finish.
            for _ in range(20):
                await asyncio.sleep(0)

        asyncio.run(_run())

        types = [e["event_type"] for e in _parent_events()]
        assert "subagent.started" in types
        assert "subagent.completed" in types
        done = [
            e for e in _parent_events() if e["event_type"] == "subagent.completed"
        ][0]
        assert done["payload"]["status"] == "failed"
        settle = [e for e in _parent_events() if e["event_type"] == "task.updated"][0]
        assert settle["payload"]["state"] == "killed"
        assert settle["payload"]["tool_use_id"] == done["payload"]["task_id"]
        task_registry.clear_session("parent-1")

    def test_a_failed_worker_settles_the_card_as_an_error(
        self, dispatch_env, feed_db, monkeypatch
    ):
        from app.core.task_registry import task_registry

        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {
                "usage": {}, "error": "provider rate limited", "interrupted": False,
            },
        )

        outcome = _dispatch()

        assert not outcome.ok
        done = [
            e for e in _parent_events() if e["event_type"] == "subagent.completed"
        ][0]
        assert done["payload"]["status"] == "failed"
        assert done["payload"]["result_preview"] == "provider rate limited"
        task_registry.clear_session("parent-1")

    def test_forwarding_failure_never_breaks_the_dispatch(self, dispatch_env):
        """No DB routed at all — the worker still returns its report."""
        from app.core.task_registry import task_registry

        outcome = _dispatch()

        assert outcome.ok
        assert outcome.report == "the worker's report"
        task_registry.clear_session("parent-1")
