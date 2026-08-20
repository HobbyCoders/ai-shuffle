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
        """A ``custom`` row: its leg comes from base_url + api_dialect, not from
        the type, so it is the kind that stays out of the servable set."""
        result = pa.validate_provider_model(
            "prov-custom", "gpt-5.5", provider_row=_provider(provider_type="custom")
        )

        assert not result.ok
        assert result.code == pa.PROVIDER_NOT_SERVABLE
        assert "not servable" in result.reason

    def test_openrouter_is_servable(self):
        """2026-08-17 ruling: OpenRouter rows are worker-target providers (fluid
        drives them natively via OpenRouterClient)."""
        result = pa.validate_provider_model(
            "prov-or", "gpt-5.5", provider_row=_provider(provider_type="openrouter")
        )

        assert result.ok

    def test_chatgpt_auth_mode_is_servable(self):
        """``auth_mode == 'chatgpt'`` is servable even with an odd type — mirrors
        is_provider_servable."""
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

    Returns a namespace with the fake turn manager, the recorded
    ``create_session`` kwargs, the recorded ``update_session`` calls (the
    worker's own status verdict) and the terminal frames handed to
    ``turn_scheduler.on_task_terminal`` (the caller wake).
    """
    from app.core import turn_manager as tm_mod
    from app.core import turn_scheduler as ts_mod
    from app.core import project_resolver
    from app.db import database as db

    created = {}
    updates = []
    terminal_frames = []
    fake_tm = _FakeTurnManager()

    def _update_session(session_id, **kwargs):
        updates.append({"session_id": session_id, **kwargs})
        return None

    monkeypatch.setattr(db, "update_session", _update_session)

    async def _on_task_terminal(session_id, frame):
        terminal_frames.append((session_id, dict(frame)))
        return None

    monkeypatch.setattr(ts_mod, "on_task_terminal", _on_task_terminal)

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
    monkeypatch.setattr(
        pa, "final_turn_outcome",
        lambda sid: {
            "usage": {"tokens_in": 5}, "error": None, "interrupted": False,
            "terminal_found": True,
        },
    )
    monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "the worker's report")

    class _Env:
        tm = fake_tm
        create_kwargs = created
        status_updates = updates
        wakes = terminal_frames

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


def _dispatch_bg(**overrides):
    """``_dispatch`` in BACKGROUND mode.

    The notification lanes (ownership registration + the proactive caller wake)
    are gated on this flag: a synchronous dispatch hands its report back inline
    as the tool result of a turn that is still running, so it has neither a
    parked caller to wake nor a detached outcome to chip.
    """
    overrides.setdefault("background", True)
    return _dispatch(**overrides)


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
        assert created["derive_provider_from_profile"] is False
        assert created["parent_session_id"] == "parent-1"
        assert created["profile_id"] == "default"
        assert created["project_id"] == "proj-1"
        assert created["worktree_id"] == "wt-1"
        assert created["session_id"] == outcome.session_id
        assert created["hidden"] is True

    def test_turn_carries_provider_and_model_overrides(self, dispatch_env):
        _dispatch()

        submitted = dispatch_env.tm.submitted[0]
        assert submitted["source"] == pa.WORKER_TURN_SOURCE
        assert submitted["prompt"] == "do the thing"
        assert submitted["overrides"]["provider"] == "prov-openai"
        assert submitted["overrides"]["model"] == "gpt-5.5"
        # cwd inherited from the parent's ALREADY-materialized checkout
        assert submitted["overrides"]["cwd"] == "/repo/feat-x"

    def test_worker_inherits_the_callers_profile_permission_mode(
        self, dispatch_env, monkeypatch
    ):
        """VM22 — the voice-session shape: no turns, so the caller PROFILE's
        configured mode is what the worker inherits."""
        from app.db import database as db
        from app.db import turns as turns_db

        monkeypatch.setattr(turns_db, "get_latest_turn", lambda sid: None)
        monkeypatch.setattr(
            db, "get_session",
            lambda sid: {"id": sid, "profile_id": "caller-prof"},
        )
        monkeypatch.setattr(
            db, "get_profile",
            lambda pid: {"id": pid, "config": {"permission_mode": "bypassPermissions"}},
        )

        _dispatch()
        submitted = dispatch_env.tm.submitted[0]
        assert submitted["overrides"]["permission_mode"] == "bypassPermissions"

    def test_the_callers_turn_override_wins_over_its_profile_config(
        self, dispatch_env, monkeypatch
    ):
        """VM22 — a composer-picked mode (per-turn override) is what actually
        governed the caller, so it beats the caller profile's config."""
        from app.db import database as db
        from app.db import turns as turns_db

        monkeypatch.setattr(
            turns_db, "get_latest_turn",
            lambda sid: {"overrides": {"permission_mode": "acceptEdits"}},
        )
        monkeypatch.setattr(
            db, "get_session",
            lambda sid: {"id": sid, "profile_id": "caller-prof"},
        )
        monkeypatch.setattr(
            db, "get_profile",
            lambda pid: {"id": pid, "config": {"permission_mode": "bypassPermissions"}},
        )

        _dispatch()
        submitted = dispatch_env.tm.submitted[0]
        assert submitted["overrides"]["permission_mode"] == "acceptEdits"

    def test_plan_mode_is_not_inherited(self, dispatch_env, monkeypatch):
        """VM22 — a worker forced into plan mode could only produce a plan, so
        `plan` never propagates; the worker's own agent config applies."""
        from app.db import database as db
        from app.db import turns as turns_db

        monkeypatch.setattr(
            turns_db, "get_latest_turn",
            lambda sid: {"overrides": {"permission_mode": "plan"}},
        )
        monkeypatch.setattr(
            db, "get_session",
            lambda sid: {"id": sid, "profile_id": "caller-prof"},
        )
        monkeypatch.setattr(db, "get_profile", lambda pid: {"id": pid, "config": {}})

        _dispatch()
        submitted = dispatch_env.tm.submitted[0]
        assert "permission_mode" not in submitted["overrides"]

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

    def test_a_connect_failure_is_recorded_on_the_registry_entry(
        self, dispatch_env, monkeypatch
    ):
        """A BACKGROUND dispatch already answered ok:true, so the registry row is
        the only place a worker that died at provider connect can still be told.
        It must carry the reason, not the "<provider> · <model>" summary it
        started with — otherwise TaskList shows an idle-looking dead card."""
        from app.core.task_registry import task_registry

        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {
                "usage": {},
                "error": (
                    "Model provider 'xAI' has no credentials configured — add an "
                    "API key for it before starting a session on it."
                ),
                "interrupted": False,
                "terminal_found": True,
            },
        )

        outcome = _dispatch(background=True)

        assert not outcome.ok
        record = task_registry.get_task("parent-1", outcome.session_id)
        assert record["status"] == "error"
        # No frames were ever forwarded, so this is a START failure, said so.
        assert record["summary"].startswith("worker failed to start: ")
        assert "no credentials configured" in record["summary"]
        task_registry.clear_session("parent-1")

    def test_a_clean_completion_keeps_its_provider_model_summary(self, dispatch_env):
        from app.core.task_registry import task_registry

        outcome = _dispatch()

        record = task_registry.get_task("parent-1", outcome.session_id)
        assert record["status"] == "completed"
        assert record["summary"] == "prov-openai · gpt-5.5"
        task_registry.clear_session("parent-1")

    def test_failure_summary_distinguishes_start_from_mid_run(self):
        assert pa._failure_summary("error", "boom", 0) == "worker failed to start: boom"
        assert pa._failure_summary("error", "boom", 3) == "worker failed: boom"
        assert pa._failure_summary("completed", "fine", 0) is None
        # Bounded, and a missing message still says something.
        assert pa._failure_summary("killed", "x" * 500, 1).endswith("…")
        assert "no error detail" in pa._failure_summary("error", None, 0)

    def test_failure_summary_puts_the_message_before_the_headers(self):
        """The live rate-limit incident: the provider's stringified error leads
        with a header dump, which ate the whole 240-char budget and left the
        actual reason invisible. The message must survive the cap."""
        raw = (
            "Error code: 429 - {'headers': {"
            + ", ".join(f"'x-ratelimit-{i}': 'value-{i}'" for i in range(40))
            + "}, 'error': {'type': 'rate_limit_error', "
            "'message': 'This request would exceed your organization rate limit.'}}"
        )
        summary = pa._failure_summary("error", raw, 2)
        assert summary.startswith(
            "worker failed: This request would exceed your organization rate limit."
        )
        assert len(summary) <= len("worker failed: ") + pa._FAILURE_SUMMARY_CHARS

    def test_message_first_is_a_no_op_without_a_message_field(self):
        """It may only ever re-order — never invent or drop information."""
        assert pa._message_first("plain failure text") == "plain failure text"
        assert pa._message_first("") == ""

    def test_provider_display_name_resolves_the_row_name(self, monkeypatch):
        """The registry summary showed a raw provider-row UUID."""
        import app.db.database as db

        monkeypatch.setattr(
            db, "get_model_provider",
            lambda pid, include_token=False: {"id": pid, "name": "My OpenRouter"},
        )
        assert pa._provider_display_name("abc-123") == "My OpenRouter"
        # The delegated leg has no row, and a lookup failure falls back to the id.
        assert pa._provider_display_name(None) == "agent"
        monkeypatch.setattr(
            db, "get_model_provider",
            lambda pid, include_token=False: (_ for _ in ()).throw(RuntimeError("x")),
        )
        assert pa._provider_display_name("abc-123") == "abc-123"

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
# Fail-closed outcomes + rate-limit fail-fast
# ---------------------------------------------------------------------------

class TestFinalTurnOutcomeTerminalMarker:
    """``terminal_found`` is what distinguishes "finished with no usage" from
    "we know nothing about this worker" — the two states the rest of the dict
    renders identically."""

    def test_no_events_reports_no_terminal(self, monkeypatch):
        monkeypatch.setattr(pa, "_load_session_events", lambda sid: [])

        outcome = pa.final_turn_outcome("w")

        assert outcome["terminal_found"] is False
        # Additive only — every pre-existing key keeps its meaning.
        assert outcome["usage"] == {} and outcome["error"] is None
        assert outcome["interrupted"] is False

    def test_a_terminal_event_reports_terminal(self, monkeypatch):
        monkeypatch.setattr(pa, "_load_session_events", lambda sid: [
            {"event_type": "turn.completed", "payload": {"usage": {"tokens_in": 3}}},
        ])

        outcome = pa.final_turn_outcome("w")

        assert outcome["terminal_found"] is True
        assert outcome["usage"] == {"tokens_in": 3}

    def test_a_parked_turn_is_still_a_terminal_event(self, monkeypatch):
        monkeypatch.setattr(pa, "_load_session_events", lambda sid: [
            {"event_type": "turn.parked", "payload": {}},
        ])

        assert pa.final_turn_outcome("w")["terminal_found"] is True


class TestDispatchWorkerFailsClosed:

    def _latch(self, reason):
        """Latch the supervisor record exactly the way ``teardown_worker`` does,
        from inside the wait — i.e. while the outcome is still unresolved."""
        from app.core import provider_agent_supervisor as sup

        def _on_wait(worker_session_id):
            record = sup.get_worker("parent-1", worker_session_id)
            assert record is not None
            record.torn_down = True
            record.torn_down_reason = reason

        return _on_wait

    def test_a_stop_all_teardown_fails_the_outcome(self, dispatch_env):
        from app.core import provider_agent_supervisor as sup

        dispatch_env.tm.on_wait = self._latch(sup.STOP_ALL_REASON)

        outcome = _dispatch()

        assert not outcome.ok
        assert "stop-all" in outcome.error
        assert outcome.session_id in outcome.error
        assert outcome.report == ""
        sup.reset()

    def test_a_turn_end_teardown_fails_the_outcome(self, dispatch_env):
        from app.core import provider_agent_supervisor as sup

        dispatch_env.tm.on_wait = self._latch(sup.TURN_END_REASON)

        outcome = _dispatch()

        assert not outcome.ok
        assert "turn end" in outcome.error
        sup.reset()

    def test_a_torn_down_worker_never_reads_the_event_log(self, dispatch_env, monkeypatch):
        """The short-circuit is the point: a killed worker has no terminal event,
        and deriving an outcome from that log is what produced a false SUCCESS."""
        from app.core import provider_agent_supervisor as sup

        def _boom(sid):
            raise AssertionError("final_turn_outcome must not run for a torn-down worker")

        monkeypatch.setattr(pa, "final_turn_outcome", _boom)
        dispatch_env.tm.on_wait = self._latch(sup.STOP_ALL_REASON)

        assert not _dispatch().ok
        sup.reset()

    def test_a_timed_out_worker_that_was_torn_down_reports_the_teardown(
        self, dispatch_env
    ):
        from app.core import provider_agent_supervisor as sup

        dispatch_env.tm.idle = False
        dispatch_env.tm.on_wait = self._latch(sup.STOP_ALL_REASON)

        outcome = _dispatch(timeout_seconds=5)

        assert not outcome.ok
        assert "stop-all" in outcome.error
        assert "did not finish within" not in outcome.error
        sup.reset()

    def test_no_terminal_event_fails_with_the_turns_row(self, dispatch_env, monkeypatch):
        from app.db import turns as turns_db

        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {
                "usage": {}, "error": None, "interrupted": False,
                "terminal_found": False,
            },
        )
        monkeypatch.setattr(
            turns_db, "get_turn",
            lambda tid: {"id": tid, "state": "aborted", "stop_reason": "lease_reaped",
                         "error": "runner died"},
        )

        outcome = _dispatch()

        assert not outcome.ok
        assert "no terminal turn event" in outcome.error
        assert "state=aborted" in outcome.error
        assert "stop_reason=lease_reaped" in outcome.error
        assert "runner died" in outcome.error

    def test_no_terminal_event_still_fails_when_the_turns_row_is_unreadable(
        self, dispatch_env, monkeypatch
    ):
        from app.db import turns as turns_db

        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {
                "usage": {}, "error": None, "interrupted": False,
                "terminal_found": False,
            },
        )

        def _boom(tid):
            raise RuntimeError("db down")

        monkeypatch.setattr(turns_db, "get_turn", _boom)

        outcome = _dispatch()

        assert not outcome.ok
        assert "no terminal turn event" in outcome.error


class TestDispatchWorkerRateLimitFailFast:
    """A worker whose provider is refusing everything must die in seconds, not
    at the (minutes-long) worker timeout."""

    @pytest.fixture(autouse=True)
    def _clean_breaker(self):
        from app.core import rate_limit_breaker as circuit_breaker

        circuit_breaker.reset()
        yield
        circuit_breaker.reset()

    @staticmethod
    def _hanging_wait(monkeypatch):
        from app.core import turn_manager as tm_mod

        async def _never_idle(session_id, timeout=300.0):
            await asyncio.sleep(timeout)
            return False

        monkeypatch.setattr(tm_mod, "wait_session_idle", _never_idle)

    def test_a_tripped_breaker_kills_the_worker_and_fails_fast(
        self, dispatch_env, feed_db, monkeypatch
    ):
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.core import provider_agent_supervisor as sup
        from app.core.task_registry import task_registry
        from app.core import rate_limit_breaker as circuit_breaker

        monkeypatch.setattr(pa, "WAIT_SLICE_SECONDS", 0.01)
        self._hanging_wait(monkeypatch)
        circuit_breaker.record_rate_limited("prov-openai", "120")

        pool = MagicMock(name="session_pool")
        pool.remove_broken = AsyncMock(return_value=object())
        with patch("app.core.session_pool.get_session_pool", return_value=pool):
            outcome = _dispatch(timeout_seconds=600)

        assert not outcome.ok
        assert "prov-openai" in outcome.error
        assert "rate limited" in outcome.error
        assert "circuit breaker open" in outcome.error

        # Killed through the supervisor's OWN per-worker teardown: the session
        # is evicted, the registry row is terminal with the rate-limit reason,
        # and the card is settled.
        pool.remove_broken.assert_awaited_once_with(outcome.session_id)
        record = task_registry.get_task("parent-1", outcome.session_id)
        assert record["status"] == "killed"
        assert record["reason"] == sup.RATE_LIMITED_REASON
        assert sup.live_workers("parent-1") == []

        types = [e["event_type"] for e in _parent_events()]
        assert "subagent.completed" in types
        # [-1]: the first task.updated is the RUNNING frame from feed.started().
        settle = [e for e in _parent_events() if e["event_type"] == "task.updated"][-1]
        assert settle["payload"]["state"] == "killed"

        task_registry.clear_session("parent-1")
        sup.reset()

    def test_the_workers_prompts_are_swept_off_the_parent(
        self, dispatch_env, feed_db, monkeypatch
    ):
        """The kill path must cancel the parent-keyed approval rows too."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.core import provider_agent_supervisor as sup
        from app.core.task_registry import task_registry
        from app.core import rate_limit_breaker as circuit_breaker

        swept = []
        monkeypatch.setattr(pa, "WAIT_SLICE_SECONDS", 0.01)
        self._hanging_wait(monkeypatch)
        from app.core import provider_agent_approvals as approvals

        monkeypatch.setattr(
            approvals, "cancel_worker_prompts",
            lambda worker, parent: swept.append((worker, parent)) or 1,
        )
        circuit_breaker.record_rate_limited("prov-openai", "120")

        pool = MagicMock(name="session_pool")
        pool.remove_broken = AsyncMock(return_value=object())
        with patch("app.core.session_pool.get_session_pool", return_value=pool):
            outcome = _dispatch(timeout_seconds=600)

        assert swept == [(outcome.session_id, "parent-1")]
        task_registry.clear_session("parent-1")
        sup.reset()

    def test_an_untripped_breaker_preserves_the_timeout_semantics(
        self, dispatch_env, monkeypatch
    ):
        """The breaker never trips → byte-identical behaviour to before: one
        ``wait_session_idle`` with the SAME overall timeout, and the ordinary
        timeout failure when it lapses."""
        from app.core import turn_manager as tm_mod
        from app.core.task_registry import task_registry

        seen = []

        async def _wait(session_id, timeout=300.0):
            seen.append(timeout)
            return False

        monkeypatch.setattr(pa, "WAIT_SLICE_SECONDS", 0.01)
        monkeypatch.setattr(tm_mod, "wait_session_idle", _wait)

        outcome = _dispatch(timeout_seconds=42)

        assert seen == [42.0]
        assert not outcome.ok
        assert "did not finish within 42s" in outcome.error
        task_registry.clear_session("parent-1")

    def test_another_providers_trip_is_ignored(self, dispatch_env, monkeypatch):
        from app.core.task_registry import task_registry
        from app.core import rate_limit_breaker as circuit_breaker

        circuit_breaker.record_rate_limited("prov-someone-else", "120")

        outcome = _dispatch()

        assert outcome.ok
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

    def test_the_worker_is_running_before_it_is_terminal(self, dispatch_env, feed_db):
        """A full dispatch writes a RUNNING ``task.updated`` and only later the
        terminal one. The v2 background-tasks strip, the spinner overlay and the
        StopButton all read ``activeTasks``, which is derived from ``running``
        task summaries alone — with only the terminal frame a provider-lane
        worker was invisible to every one of them.
        """
        from app.core.task_registry import task_registry

        outcome = _dispatch(label="reviewer")
        worker = outcome.session_id

        settles = [
            e for e in _parent_events() if e["event_type"] == "task.updated"
        ]
        assert [s["payload"]["state"] for s in settles] == ["running", "completed"]
        assert {s["payload"]["task_id"] for s in settles} == {worker}
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

        # [-1]: the first task.updated is the RUNNING frame from feed.started().
        settle = [e for e in _parent_events() if e["event_type"] == "task.updated"][-1]
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
        # [-1]: the first task.updated is the RUNNING frame from feed.started().
        settle = [e for e in _parent_events() if e["event_type"] == "task.updated"][-1]
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


# ---------------------------------------------------------------------------
# Failed-status verdict + proactive caller wake
# ---------------------------------------------------------------------------

@pytest.fixture
def _clean_parent_registry():
    from app.core.task_registry import task_registry
    task_registry.clear_session("parent-1")
    yield
    task_registry.clear_session("parent-1")


@pytest.mark.usefixtures("_clean_parent_registry")
class TestWorkerFailedStatus:
    """FEATURE 1a — a worker whose dispatch ends in the registry status
    ``"error"`` stamps ``sessions.status = 'failed'`` on its OWN row."""

    def test_errored_worker_marks_its_session_failed(
        self, dispatch_env, monkeypatch
    ):
        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {
                "usage": {}, "error": "provider blew up", "interrupted": False,
                "terminal_found": True,
            },
        )

        outcome = _dispatch()

        assert not outcome.ok
        assert dispatch_env.status_updates == [
            {"session_id": outcome.session_id, "status": "failed"}
        ]

    def test_completed_worker_never_touches_its_status(self, dispatch_env):
        outcome = _dispatch()

        assert outcome.ok
        assert dispatch_env.status_updates == []

    def test_timed_out_worker_marks_its_session_failed(self, dispatch_env):
        """A worker that never went idle keeps the initial ``"error"`` status,
        so its row is stamped too."""
        dispatch_env.tm.idle = False

        outcome = _dispatch()

        assert not outcome.ok
        assert dispatch_env.status_updates == [
            {"session_id": outcome.session_id, "status": "failed"}
        ]

    def test_status_write_failure_never_breaks_the_dispatch(
        self, dispatch_env, monkeypatch
    ):
        from app.db import database as db

        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "update_session", _boom)
        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {
                "usage": {}, "error": "provider blew up", "interrupted": False,
                "terminal_found": True,
            },
        )

        outcome = _dispatch()

        assert not outcome.ok
        assert outcome.error == "provider blew up"


@pytest.mark.usefixtures("_clean_parent_registry")
class TestWorkerWakesItsCaller:
    """FEATURE 2 — an MCP-dispatched worker has no SDK task, so nothing in the
    notification pipeline ever fires for it. A BACKGROUND dispatch therefore
    (a) claims OWNERSHIP of the worker's terminal for the parent's notification
    lanes and (b) calls the scheduler seam itself on every terminal status.

    Both halves are gated on the background mode — see
    :class:`TestSyncDispatchNeedsNoWake` for the synchronous counterpart."""

    def test_dispatch_claims_ownership_of_the_worker_task(self, dispatch_env):
        """``is_own_background_task`` is the gate on BOTH the terminal chip and
        the scheduler wake. Before this feature it was False for every worker
        (no SDK task, no recorded launch), so both lanes dropped it."""
        from app.core.task_registry import task_registry

        outcome = _dispatch_bg()

        assert task_registry.is_own_background_task("parent-1", outcome.session_id)

    def test_launch_key_matches_the_registry_rows_tool_use_id(self, dispatch_env):
        """Consistency check — ownership resolves via the registry record's
        ``tool_use_id``, and the park gate resolves a launch via that SAME id.
        A mismatch would either drop the wake or park the session forever."""
        from app.core.task_registry import task_registry

        outcome = _dispatch_bg(tool_use_id="toolu-abc")

        record = task_registry.get_task("parent-1", outcome.session_id)
        assert record["toolUseId"] == "toolu-abc"
        assert task_registry.was_background_launch("parent-1", "toolu-abc")
        (_sid, frame), = dispatch_env.wakes
        assert frame["tool_use_id"] == "toolu-abc"

    def test_launch_key_defaults_to_the_worker_session_id(self, dispatch_env):
        from app.core.task_registry import task_registry

        outcome = _dispatch_bg()

        assert task_registry.was_background_launch("parent-1", outcome.session_id)
        (_sid, frame), = dispatch_env.wakes
        assert frame["tool_use_id"] == outcome.session_id

    def test_park_gate_resolves_once_the_worker_is_terminal(self, dispatch_env):
        """#803 ruling: ``complete_task`` must NOT prune the cumulative launch
        set — the GATE instead stops counting a launch whose registry record is
        terminal. Verify the two agree, or the caller parks forever."""
        from app.core.task_registry import task_registry

        _dispatch_bg()

        assert not task_registry.has_unresolved_background_launches("parent-1")
        assert not task_registry.has_active_background_work("parent-1")

    def test_completed_worker_wakes_the_caller(self, dispatch_env):
        outcome = _dispatch_bg()

        (sid, frame), = dispatch_env.wakes
        assert sid == "parent-1"
        # task_id is MANDATORY — a frame without it is dropped by the
        # contentless-frame choke before any lane sees it.
        assert frame["task_id"] == outcome.session_id
        assert frame["status"] == "completed"
        assert frame["summary"] == "the worker's report"
        assert frame["origin"] == "provider_agent"

    def test_errored_worker_wakes_the_caller_with_the_failure_summary(
        self, dispatch_env, monkeypatch
    ):
        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {
                "usage": {}, "error": "provider blew up", "interrupted": False,
                "terminal_found": True,
            },
        )

        outcome = _dispatch_bg()

        (sid, frame), = dispatch_env.wakes
        assert sid == "parent-1"
        assert frame["task_id"] == outcome.session_id
        assert frame["status"] == "error"
        # The registry row's failure summary, not the bare preview.
        assert frame["summary"].startswith("worker failed")
        assert "provider blew up" in frame["summary"]

    def test_torn_down_worker_does_not_wake_the_caller(
        self, dispatch_env, monkeypatch
    ):
        """Supervisor teardown coalesces its kills into ONE system note by
        ruling P12 — the non-torn_down branch is the only one that wakes."""
        from app.core import provider_agent_supervisor as sup

        real_register = sup.register_worker

        def _register_and_tear_down(record):
            supervised = real_register(record)
            supervised.torn_down = True
            supervised.torn_down_reason = "turn_end"
            return supervised

        monkeypatch.setattr(sup, "register_worker", _register_and_tear_down)

        outcome = _dispatch_bg()

        assert not outcome.ok
        assert dispatch_env.wakes == []
        assert dispatch_env.status_updates == []

    def test_wake_failure_never_breaks_the_dispatch_bg(
        self, dispatch_env, monkeypatch
    ):
        from app.core import turn_scheduler as ts_mod

        async def _boom(session_id, frame):
            raise RuntimeError("scheduler down")

        monkeypatch.setattr(ts_mod, "on_task_terminal", _boom)

        outcome = _dispatch_bg()

        assert outcome.ok
        assert outcome.report == "the worker's report"


@pytest.mark.usefixtures("_clean_parent_registry")
class TestSyncDispatchNeedsNoWake:
    """The gate (2026-08-17 ruling). A SYNCHRONOUS dispatch returns its report
    inline as the tool result, inside the caller's own still-running turn — the
    caller is neither parked nor uninformed. Registering a background launch or
    firing a terminal frame there would be pure noise, and the launch key would
    arm the park gate for a turn that never parks."""

    def test_sync_dispatch_does_not_wake_the_caller(self, dispatch_env):
        outcome = _dispatch()

        assert outcome.ok
        assert dispatch_env.wakes == []

    def test_sync_dispatch_registers_no_background_launch(self, dispatch_env):
        from app.core.task_registry import task_registry

        outcome = _dispatch()

        assert not task_registry.was_background_launch(
            "parent-1", outcome.session_id
        )
        assert not task_registry.is_own_background_task(
            "parent-1", outcome.session_id
        )

    def test_errored_sync_dispatch_still_marks_the_worker_failed(
        self, dispatch_env, monkeypatch
    ):
        """The failed-status verdict is about the WORKER's own row and is NOT
        gated on the dispatch mode — only the caller-notification lanes are."""
        monkeypatch.setattr(
            pa, "final_turn_outcome",
            lambda sid: {
                "usage": {}, "error": "provider blew up", "interrupted": False,
                "terminal_found": True,
            },
        )

        outcome = _dispatch()

        assert not outcome.ok
        assert dispatch_env.status_updates == [
            {"session_id": outcome.session_id, "status": "failed"}
        ]
        assert dispatch_env.wakes == []

    def test_background_dispatch_is_the_only_one_that_wakes(self, dispatch_env):
        """Direct A/B on the one flag."""
        _dispatch()
        assert dispatch_env.wakes == []

        _dispatch_bg()
        assert len(dispatch_env.wakes) == 1
