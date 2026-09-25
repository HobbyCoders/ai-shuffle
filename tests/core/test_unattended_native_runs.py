"""Unattended runs end to end: automations, connector turns and REST, on a real native turn.

Nobody can answer a prompt on these paths. Each test drives the app's own
intake (``AutomationManager`` triggers, ``ConnectorManager.handle_incoming_message``,
``POST /conversation``) through the durable turn queue, ``SessionRuntime``,
``query_engine``, the pooled ``HarnessAdapter`` and the native fluid loop,
against the scripted loopback provider in
``fluid/tests/fixtures/fake_provider_server.py``. Only the provider client's
base URL (``_build_provider_client``) and the permission TTL are swapped.
Assertions read what the app recorded (run rows, permission rows, session
rows, the session event log), what the provider was sent, and what the
connector sent back. Hermetic: loopback only.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fluid.providers._retry import RetryPolicy
from fluid.providers.anthropic.client import AnthropicClient

import app.core.provider.harness_adapter as ha
from app.core import permission_handler as ph
from app.core import turn_manager, turn_scheduler
from app.core.automation_manager import AutomationManager
from app.core.config import settings
from app.core.connectors.base import BaseConnector, IncomingMessage, OutgoingMessage
from app.db import database
from tests.core.interaction_harness import (  # noqa: F401  (autouse fixtures)
    no_gateway_listener,
    no_push,
    reset_backfill_memo_on_setup,
    reset_interaction_wait_state,
)
from tests.shared_fixtures import clean_task_registry  # noqa: F401  (autouse fixture)

FIXTURES = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures"
FAST_RETRY = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05, jitter=0)
RUN_BUDGET = 60.0
# The unattended wait for a permission nobody answers (300 s in production).
PERMISSION_TTL = 0.5
MODEL = "claude-sonnet-4-5"


def _load_fake_server():
    name = "fake_provider_server"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "fake_provider_server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fps = _load_fake_server()
Messages = fps.Messages


# ─── harness ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    return root


@pytest.fixture
async def provider(mock_db, monkeypatch):
    """The fake provider behind the app's native Anthropic leg, plus the
    scheduler seams the app installs at boot."""
    server = fps.FakeProviderServer()

    async def _client(kind, options):
        assert kind == "anthropic"
        return AnthropicClient(
            api_key="sk-test", base_url=server.url + "/v1/messages", retry=FAST_RETRY,
        ), None

    monkeypatch.setattr(ha, "_build_provider_client", _client)
    monkeypatch.setenv("AIH_FLUID_PROVIDER", "anthropic")
    monkeypatch.setattr(ph, "PERMISSION_TTL_SECONDS", PERMISSION_TTL)
    database.register_install(
        settings.effective_instance_id, "test-runner", None, "local", "1", {},
        approve_new=True,
    )
    turn_scheduler.install()
    try:
        yield server
    finally:
        from app.core.session_pool import get_session_pool

        await get_session_pool().shutdown()
        turn_manager.register_kick(None)
        from app.core import session_runtime

        session_runtime.register_scheduler(None)
        server.close()


def _agent(project: Path, agent_id: str = "agent-1", mode: str = "default", **config) -> str:
    database.create_profile(
        agent_id, agent_id, None,
        {"model": MODEL, "cwd": str(project), "permission_mode": mode, **config},
    )
    return agent_id


def _write_call(project: Path, call_id: str = "toolu_w1") -> list:
    args = {"file_path": str(project / "out.txt"), "content": "hello"}
    return Messages.tool_calls([{"id": call_id, "name": "Write", "args": args}])


def _read_call(project: Path, call_id: str = "toolu_r1") -> list:
    args = {"file_path": str(project / "notes.txt")}
    return Messages.tool_calls([{"id": call_id, "name": "Read", "args": args}])


def _fed_back(server, index: int) -> List[dict]:
    body = server.requests[index].body
    return [b for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_result"]


def _interactions(test_db, session_id: str) -> List[Dict[str, Any]]:
    rows = test_db.execute(
        "SELECT kind, state, payload FROM pending_interactions WHERE session_id = ?", (session_id,)
    ).fetchall()
    return [{"kind": r[0], "state": r[1], "payload": json.loads(r[2] or "{}")} for r in rows]


class Automations:
    """A fresh AutomationManager whose background tasks the test can await."""

    def __init__(self) -> None:
        self.manager = AutomationManager()
        self.tasks: List[asyncio.Task] = []

        def _track(coro, *, name: str) -> asyncio.Task:
            task = asyncio.create_task(coro, name=name)
            self.tasks.append(task)
            return task

        self.manager._create_tracked_task = _track

    async def settle(self) -> None:
        while self.tasks:
            pending = list(self.tasks)
            self.tasks.clear()
            await asyncio.wait_for(asyncio.gather(*pending), RUN_BUDGET)


def _automation(agent_id: str, automation_id: str = "auto-1", **fields) -> Dict[str, Any]:
    fields.setdefault("trigger_type", "webhook")
    fields.setdefault("trigger_config", {})
    return database.create_automation(
        automation_id=automation_id, name="Nightly", profile_id=agent_id,
        prompt=fields.pop("prompt", "summarize {{payload.topic}}"), enabled=True, **fields,
    )


def _runs(automation_id: str = "auto-1") -> List[Dict[str, Any]]:
    return database.get_automation_runs(automation_id)


# ─── automations ────────────────────────────────────────────────────────────


async def test_webhook_run_completes_and_records_reply_cost_and_output(provider, project):
    agent = _agent(project)
    _automation(agent)
    provider.script(Messages.text("All green.", input_tokens=40, output_tokens=6))
    autos = Automations()

    assert await autos.manager.trigger_webhook("auto-1", {"topic": "builds"}) == {
        "automation_id": "auto-1", "status": "triggered",
    }
    await autos.settle()

    [run] = _runs()
    assert run["status"] == "completed", run
    assert run["response"] == "All green."
    assert run["output_status"] == "delivered"
    assert run["tokens_in"] == 40 and run["tokens_out"] == 6
    assert run["cost_usd"] and run["cost_usd"] > 0
    assert run["prompt_rendered"] == "summarize builds"
    session = database.get_session(run["session_id"])
    assert session["automation_id"] == "auto-1" and session["profile_id"] == agent
    automation = database.get_automation("auto-1")
    assert automation["total_cost_used"] == pytest.approx(run["cost_usd"])
    # The provider saw the rendered prompt, once.
    [request] = provider.requests
    assert "summarize builds" in json.dumps(request.body["messages"])


async def test_connector_event_run_with_a_read_completes(provider, project, test_db):
    agent = _agent(project)
    _automation(
        agent, trigger_type="connector_event",
        trigger_config={"event_type": "session.complete"}, max_turns=5,
        prompt="what changed in {{event.session_id}}?",
    )
    provider.script(_read_call(project), Messages.text("It lists alpha and beta."))
    autos = Automations()

    await autos.manager.handle_connector_event(
        "session.complete", {"session_id": "s-9", "connector_id": "conn-1"},
    )
    await autos.settle()

    [run] = _runs()
    assert run["status"] == "completed", run
    assert run["response"] == "It lists alpha and beta."
    [fed] = _fed_back(provider, 1)
    assert fed["tool_use_id"] == "toolu_r1" and not fed.get("is_error")
    assert "alpha" in json.dumps(fed["content"])
    assert _interactions(test_db, run["session_id"]) == []


async def test_default_max_turns_ends_a_tool_run_after_one_model_call(provider, project):
    """``max_turns`` defaults to 1 ("conversation turns per run"): on the
    native leg that is one model call. A run whose model asks for a tool gets
    the tool run but no second call to answer from it, and the run is stamped
    completed with an empty reply."""
    agent = _agent(project)
    _automation(agent)
    provider.script(_read_call(project), Messages.text("never sent"))
    autos = Automations()

    await autos.manager.trigger_webhook("auto-1", {"topic": "x"})
    await autos.settle()

    [run] = _runs()
    assert len(provider.requests) == 1
    assert (run["status"], run["error"], run["response"]) == ("completed", None, "")


async def test_ask_mode_write_is_put_to_the_app_and_denied_after_the_unattended_timeout(
    provider, project, test_db
):
    agent = _agent(project, mode="default")
    _automation(agent, trigger_type="cron", trigger_config={"expression": "0 9 * * *"},
                max_turns=5, prompt="write the report")
    provider.script(_write_call(project), Messages.text("Could not write it."))
    autos = Automations()

    await autos.manager._on_schedule_trigger("auto-1", "cron", {"expression": "0 9 * * *"})
    await autos.settle()

    [run] = _runs()
    assert run["status"] == "completed", run
    assert run["response"] == "Could not write it."
    assert not (project / "out.txt").exists()
    [fed] = _fed_back(provider, 1)
    assert fed["is_error"] is True and "timed out" in json.dumps(fed["content"])
    [prompt] = _interactions(test_db, run["session_id"])
    assert prompt["kind"] == "permission"
    assert prompt["payload"]["tool_name"] == "Write"


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
async def test_approving_modes_run_the_write_without_asking(provider, project, test_db, mode):
    agent = _agent(project, mode=mode)
    _automation(agent, max_turns=5, prompt="write the report")
    provider.script(_write_call(project), Messages.text("Written."))
    autos = Automations()

    await autos.manager.trigger_webhook("auto-1", {})
    await autos.settle()

    [run] = _runs()
    assert run["status"] == "completed", run
    assert (project / "out.txt").read_text(encoding="utf-8") == "hello"
    [fed] = _fed_back(provider, 1)
    assert not fed.get("is_error")
    assert _interactions(test_db, run["session_id"]) == []


async def test_a_rate_limited_provider_fails_the_run_with_the_providers_reason(provider, project):
    agent = _agent(project)
    _automation(agent)
    provider.script(*[
        fps.error_reply(429, "Number of request tokens has exceeded your rate limit",
                        retry_after="0")
        for _ in range(3)
    ])
    autos = Automations()

    await autos.manager.trigger_webhook("auto-1", {})
    await autos.settle()

    [run] = _runs()
    assert len(provider.requests) == 3  # the first try plus two retries
    # Not documented for automations: the run fails with the provider's
    # error body as its error, and is never delivered.
    assert (run["status"], run["output_status"]) == ("failed", "pending"), run
    assert "exceeded your rate limit" in run["error"]
    automation = database.get_automation("auto-1")
    assert automation["status"] == "error" and automation["failure_count"] == 1


async def test_a_provider_500_fails_the_run_and_caps_the_stored_error(provider, project):
    agent = _agent(project)
    _automation(agent)
    huge = "upstream exploded: " + "x" * 50_000
    provider.script(*[fps.error_reply(500, huge) for _ in range(3)])
    autos = Automations()

    await autos.manager.trigger_webhook("auto-1", {})
    await autos.settle()

    [run] = _runs()
    assert run["status"] == "failed", run
    assert "upstream exploded" in run["error"]
    # The provider client keeps the first 2,000 characters of an error body.
    assert len(run["error"]) <= 2000
    automation = database.get_automation("auto-1")
    assert automation["last_error"] == run["error"] and automation["status"] == "error"


@pytest.mark.parametrize(
    "limits, history, reason",
    [
        ({"max_cost_total": 1.0}, {"total_cost_used": 1.0}, "Lifetime budget exceeded"),
        ({"max_runs_per_day": 2}, {"runs_today": 2}, "Daily run limit reached (2 / 2)"),
        ({"max_cost_per_day": 0.5}, {"cost_today": 0.6}, "Daily cost limit reached"),
    ],
)
async def test_budget_stops_refuse_before_any_provider_call(
    provider, project, limits, history, reason
):
    agent = _agent(project)
    _automation(agent, **limits)
    if history.get("total_cost_used"):
        database.update_automation("auto-1", total_cost_used=history["total_cost_used"])
    for i in range(history.get("runs_today", 0)):
        database.create_automation_run(run_id=f"old-{i}", automation_id="auto-1",
                                       status="completed", trigger_data={})
    if history.get("cost_today"):
        database.create_automation_run(run_id="old-cost", automation_id="auto-1",
                                       status="completed", trigger_data={})
        database.update_automation_run("old-cost", cost_usd=history["cost_today"])
    autos = Automations()

    await autos.manager.trigger_webhook("auto-1", {})
    await autos.settle()

    assert provider.requests == []
    refused = [r for r in _runs() if not r["id"].startswith("old-")]
    assert [r["status"] for r in refused] == ["budget_exceeded"]
    assert refused[0]["session_id"] is None
    assert reason in database.get_automation("auto-1")["last_error"]


async def _run_once(autos: Automations, provider, text: str) -> Dict[str, Any]:
    provider.script(Messages.text(text))
    await autos.manager.trigger_webhook("auto-1", {})
    await autos.settle()
    return _runs()[0]


async def test_dedup_skips_a_repeat_of_a_recently_delivered_reply(provider, project):
    agent = _agent(project)
    _automation(agent, output_config={"type": "none", "deduplicate_window_seconds": 3600})
    autos = Automations()

    first = await _run_once(autos, provider, "Nothing new.")
    second = await _run_once(autos, provider, "Nothing new.")
    third = await _run_once(autos, provider, "Build 42 failed.")

    assert first["output_status"] == "delivered"
    assert second["output_status"] == "deduplicated"
    assert third["output_status"] == "delivered"
    assert [r["status"] for r in (first, second, third)] == ["completed"] * 3


async def test_dedup_compares_the_stored_2000_char_tail(provider, project):
    agent = _agent(project)
    _automation(agent, output_config={"type": "none", "deduplicate_window_seconds": 3600})
    autos = Automations()
    tail = "t" * 2000

    first = await _run_once(autos, provider, "A" + tail)
    second = await _run_once(autos, provider, "B" + tail)

    assert first["response"] == second["response"] == tail
    assert second["output_status"] == "deduplicated"


async def test_dedup_looks_back_only_four_runs(provider, project):
    agent = _agent(project)
    _automation(agent, output_config={"type": "none", "deduplicate_window_seconds": 3600})
    autos = Automations()

    await _run_once(autos, provider, "Same.")
    for i in range(4):
        await _run_once(autos, provider, f"Other {i}.")
    fifth_back = await _run_once(autos, provider, "Same.")

    # "Same." was delivered five runs back: outside the window of four.
    assert fifth_back["output_status"] == "delivered"


async def test_an_approved_run_left_by_a_restart_runs_once(provider, project):
    agent = _agent(project)
    _automation(agent, requires_approval=True)
    first_boot = Automations()
    await first_boot.manager.trigger_webhook("auto-1", {"topic": "deploys"})
    await first_boot.settle()
    [run] = _runs()
    [approval] = database.get_pending_automation_approvals()
    assert run["status"] == "pending" and provider.requests == []
    # Approved, then the process stopped before the run started.
    database.update_automation_approval(approval["id"], status="approved", decided_by="admin")
    database.update_automation_run(run["id"], status="approved")

    provider.script(Messages.text("Deploys look fine."))
    second_boot, other_install = Automations(), Automations()
    await second_boot.manager._requeue_approved_runs()
    await other_install.manager._requeue_approved_runs()
    await second_boot.settle()
    await other_install.settle()

    [run] = _runs()
    assert run["status"] == "completed", run
    assert run["response"] == "Deploys look fine."
    assert len(provider.requests) == 1
    assert "summarize deploys" in json.dumps(provider.requests[0].body["messages"])


async def test_no_run_starts_once_shutdown_began(provider, project):
    """Regression: a webhook or connector event landing after shutdown began
    minted a run and a session and queued a turn the suppressed scheduler
    never started, so the run waited out its timeout (or was cancelled with
    its row left ``running``) and the turn ran unwatched on the next boot."""
    agent = _agent(project)
    _automation(agent)
    _automation(agent, automation_id="auto-2", trigger_type="connector_event",
                trigger_config={"event_type": "session.complete"})
    autos = Automations()
    await autos.manager.shutdown()

    await autos.manager.trigger_webhook("auto-1", {})
    await autos.manager.handle_connector_event("session.complete", {"connector_id": "c"})
    await autos.manager._execute("auto-1", {"trigger": "manual"})
    await autos.settle()

    assert _runs("auto-1") == [] and _runs("auto-2") == []
    assert provider.requests == []


async def test_no_run_starts_while_the_app_is_shutting_down(provider, project, monkeypatch):
    """The app flags shutdown before it reaches the automation manager."""
    from app.core import startup

    agent = _agent(project)
    _automation(agent)
    monkeypatch.setattr(startup, "is_shutting_down", True)
    autos = Automations()

    await autos.manager.trigger_webhook("auto-1", {})
    await autos.settle()

    assert _runs() == [] and provider.requests == []


async def test_max_cost_per_run_only_logs(provider, project, caplog):
    agent = _agent(project)
    _automation(agent, max_cost_per_run=0.0000001)
    autos = Automations()

    with caplog.at_level(logging.WARNING, logger="app.core.automation_manager"):
        run = await _run_once(autos, provider, "Expensive answer.")

    assert run["status"] == "completed" and run["output_status"] == "delivered"
    assert run["cost_usd"] > 0.0000001
    assert any("exceeded per-run cost" in r.getMessage() for r in caplog.records)


# ─── connector turns ────────────────────────────────────────────────────────


class FakeChat(BaseConnector):
    """A messaging connector with no platform SDK: it records what it sends."""

    def __init__(self, connector_id: str, config: Dict[str, Any] | None = None) -> None:
        from app.core.connectors.manager import connector_manager

        super().__init__(
            connector_id, "fakechat", config or {},
            connector_manager.handle_incoming_message,
            connector_manager.handle_permission_response,
        )
        self.sent: List[OutgoingMessage] = []

    async def disconnect(self) -> None:
        return None

    async def send_message(self, message: OutgoingMessage) -> bool:
        self.sent.append(message)
        return True

    async def send_permission_prompt(self, platform_chat_id, prompt) -> bool:
        return True

    async def send_notification(self, platform_chat_id, event_type, data) -> bool:
        return True


@pytest.fixture
def connectors():
    """Register connector instances with the live manager for one test."""
    from app.core.connectors.manager import connector_manager

    added: List[str] = []

    def _register(instance: BaseConnector) -> BaseConnector:
        connector_manager._connectors[instance.connector_id] = instance
        added.append(instance.connector_id)
        return instance

    yield _register
    for connector_id in added:
        connector_manager._connectors.pop(connector_id, None)
        connector_manager._registered_at.pop(connector_id, None)


def _key(key_id: str, agent_id: str, **fields) -> str:
    database.create_api_user(key_id, key_id, f"hash-{key_id}", profile_ids=[agent_id], **fields)
    return key_id


def _connector(connector_id: str, key_id: str | None, connector_type: str = "fakechat",
               config: Dict[str, Any] | None = None) -> str:
    database.create_connector(
        connector_id, connector_type, connector_id, config or {}, [], api_user_id=key_id,
        is_active=True,
    )
    return connector_id


def _inbound(connector_id: str, text: str, chat: str = "chat-1") -> IncomingMessage:
    return IncomingMessage(
        connector_id=connector_id, platform_chat_id=chat, platform_user_id="u-1",
        platform_user_name="Ann", text=text, platform_message_id="m-1",
    )


def _linked_session(connector_id: str, chat: str = "chat-1") -> Dict[str, Any]:
    link = database.get_connector_session(connector_id, chat)
    assert link and link.get("session_id")
    return database.get_session(link["session_id"])


async def _deliver(message: IncomingMessage) -> None:
    from app.core.connectors.manager import connector_manager

    await asyncio.wait_for(connector_manager.handle_incoming_message(message), RUN_BUDGET)


@pytest.fixture
def pump_calls(monkeypatch):
    """What each turn's pump was started with (the runtime's own arguments)."""
    import app.core.query_engine as qe

    real = qe._stream_to_websocket_impl
    calls: List[Dict[str, Any]] = []

    async def _spy(**kwargs):
        calls.append(kwargs)
        async for event in real(**kwargs):
            yield event

    monkeypatch.setattr(qe, "_stream_to_websocket_impl", _spy)
    return calls


async def test_a_connector_message_is_answered_through_the_connector(
    provider, project, connectors
):
    agent = _agent(project)
    key = _key("key-chat", agent)
    chat = connectors(FakeChat(_connector("conn-1", key)))
    provider.script(Messages.text("Hi Ann, all quiet.", input_tokens=30, output_tokens=5))

    await _deliver(_inbound("conn-1", "anything new?"))

    assert [(m.platform_chat_id, m.text, m.reply_to_message_id) for m in chat.sent] == [
        ("chat-1", "Hi Ann, all quiet.", "m-1"),
    ]
    session = _linked_session("conn-1")
    assert session["api_user_id"] == key and session["profile_id"] == agent
    assert session["permission_mode"] == "default"
    assert "anything new?" in json.dumps(provider.requests[0].body["messages"])
    # The prompt tells the model where it is talking.
    assert "fakechat" in json.dumps(provider.requests[0].body.get("system"))


async def test_a_connector_turn_runs_as_the_connectors_key(
    provider, project, connectors, pump_calls
):
    """Connectors always run as their API key (defea7ce scopes tools, notes
    and credentials to the key the turn runs as)."""
    agent = _agent(project)
    key = _key("key-chat", agent)
    connectors(FakeChat(_connector("conn-1", key)))
    provider.script(Messages.text("ok"))

    await _deliver(_inbound("conn-1", "hi"))

    [call] = pump_calls
    assert call["turn_source"] == "connector"
    assert call["api_user_id"] == key


async def test_each_connector_runs_its_own_keys_agent(provider, project, connectors):
    first, second = _agent(project, "agent-a"), _agent(project, "agent-b",
                                                       model="claude-haiku-4-5")
    chat_a = connectors(FakeChat(_connector("conn-a", _key("key-a", first))))
    chat_b = connectors(FakeChat(_connector("conn-b", _key("key-b", second))))
    provider.script(Messages.text("from a"), Messages.text("from b"))

    await _deliver(_inbound("conn-a", "hello a"))
    await _deliver(_inbound("conn-b", "hello b"))

    assert [m.text for m in chat_a.sent] == ["from a"]
    assert [m.text for m in chat_b.sent] == ["from b"]
    assert _linked_session("conn-a")["profile_id"] == "agent-a"
    assert _linked_session("conn-b")["profile_id"] == "agent-b"
    assert [r.body["model"] for r in provider.requests] == [MODEL, "claude-haiku-4-5"]


async def test_a_connector_tool_prompt_shows_in_the_app_and_is_denied_at_the_timeout(
    provider, project, connectors, test_db
):
    agent = _agent(project, mode="bypassPermissions")  # the connector pins Ask anyway
    chat = connectors(FakeChat(_connector("conn-1", _key("key-chat", agent))))
    provider.script(_write_call(project), Messages.text("I was not allowed to write it."))

    await _deliver(_inbound("conn-1", "write the file"))

    session = _linked_session("conn-1")
    [prompt] = _interactions(test_db, session["id"])
    assert prompt["kind"] == "permission" and prompt["payload"]["tool_name"] == "Write"
    assert not (project / "out.txt").exists()
    [fed] = _fed_back(provider, 1)
    assert fed["is_error"] is True and "timed out" in json.dumps(fed["content"])
    # The chat hears that the owner has to answer, then the reply.
    assert len(chat.sent) == 2
    assert "Write" in chat.sent[0].text
    assert chat.sent[1].text == "I was not allowed to write it."


async def test_the_allowed_chat_rule_refuses_other_senders(provider, project, connectors):
    from app.core.connectors.manager import connector_manager
    from app.core.connectors.telegram import TelegramConnector

    class RecordingTelegram(TelegramConnector):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sent: List[OutgoingMessage] = []

        async def send_message(self, message: OutgoingMessage) -> bool:
            self.sent.append(message)
            return True

        async def send_typing_indicator(self, platform_chat_id: str) -> None:
            return None

        async def send_ack_reaction(self, platform_chat_id, platform_message_id) -> None:
            return None

        async def mark_as_read(self, platform_chat_id, platform_message_id) -> None:
            return None

    agent = _agent(project)
    config = {"allowed_chat_ids": ["100"]}
    connector_id = _connector("conn-tg", _key("key-tg", agent), "telegram", config)
    bot = connectors(RecordingTelegram(
        connector_id, "telegram", config,
        connector_manager.handle_incoming_message, connector_manager.handle_permission_response,
    ))
    provider.script(Messages.text("hello friend"))

    def _update(chat_id: int, text: str):
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="private"),
            from_user=SimpleNamespace(id=chat_id, full_name="Someone"),
            text=text, message_id=7,
        )

    await asyncio.wait_for(bot._handle_message(_update(999, "let me in")), RUN_BUDGET)
    assert bot.sent == [] and provider.requests == []
    assert database.get_connector_session(connector_id, "999") is None

    await asyncio.wait_for(bot._handle_message(_update(100, "hi")), RUN_BUDGET)
    assert [m.text for m in bot.sent] == ["hello friend"]
    assert len(provider.requests) == 1


async def test_a_connector_without_a_key_is_refused_before_any_model_call(
    provider, project, connectors
):
    from app.core.connectors.manager import CONNECTOR_NEEDS_API_USER_MESSAGE

    chat = connectors(FakeChat(_connector("conn-1", None)))

    await _deliver(_inbound("conn-1", "hello?"))

    assert [m.text for m in chat.sent] == [CONNECTOR_NEEDS_API_USER_MESSAGE]
    assert provider.requests == []


async def test_a_provider_failure_sends_the_fallback_reply(provider, project, connectors):
    agent = _agent(project)
    chat = connectors(FakeChat(_connector("conn-1", _key("key-chat", agent))))
    provider.script(*[fps.error_reply(500, "upstream exploded") for _ in range(3)])

    await _deliver(_inbound("conn-1", "hello?"))

    assert [m.text for m in chat.sent] == ["I couldn't generate a response."]
    assert len(provider.requests) == 3


# ─── REST ───────────────────────────────────────────────────────────────────


async def test_rest_conversation_denies_an_ask_prompt_at_the_timeout(
    provider, project, test_db, pump_calls
):
    from app.api.query import conversation
    from app.core.models import ConversationRequest

    agent = _agent(project, mode="default")
    key = _key("key-rest", agent)
    provider.script(_write_call(project), Messages.text("Not written: no approval."))

    result = await asyncio.wait_for(conversation(
        ConversationRequest(prompt="write the file", profile=agent),
        {"id": key, "profile_ids": [agent], "project_id": None},
    ), RUN_BUDGET)

    assert result["response"] == "Not written: no approval."
    assert not (project / "out.txt").exists()
    [prompt] = _interactions(test_db, result["session_id"])
    assert prompt["kind"] == "permission" and prompt["payload"]["tool_name"] == "Write"
    [fed] = _fed_back(provider, 1)
    assert fed["is_error"] is True and "timed out" in json.dumps(fed["content"])
    assert database.get_session(result["session_id"])["api_user_id"] == key
    # Regression: the turn ran as the owner (no key on the turn), so the key's
    # tool surface, credentials and note scope did not apply.
    [call] = pump_calls
    assert call["api_user_id"] == key and call["turn_source"] == "api"
