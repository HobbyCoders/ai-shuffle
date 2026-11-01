"""The dev loop's fake AI: stateless scenario selection over real HTTP."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.devloop import fake_wire
from scripts.devloop.fake_ai import (
    SCENARIOS_DIR,
    FakeAI,
    ScenarioError,
    Scenarios,
    conversation,
    parse_tag,
    prompt_and_round,
)

BASH = [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}]
CODE_MODE = [{"type": "function", "function": {"name": "run_python", "parameters": {}}}]


@pytest.fixture
def fake(tmp_path: Path):
    with FakeAI(log_path=tmp_path / "requests.jsonl") as server:
        yield server


def _sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[len("data: "):]))
    return events


def _chat(fake: FakeAI, messages: list[dict], tools: list | None = None, **extra) -> httpx.Response:
    body = {"model": "fake-ai", "stream": True, "messages": messages, **extra}
    if tools is not None:
        body["tools"] = tools
    return httpx.post(fake.url + "/v1/chat/completions", json=body, timeout=10)


def _chat_text(response: httpx.Response) -> str:
    return "".join(choice["delta"].get("content") or ""
                   for event in _sse(response.text) for choice in event.get("choices", []))


def _chat_calls(response: httpx.Response) -> list[dict]:
    calls: dict[int, dict] = {}
    for event in _sse(response.text):
        for choice in event.get("choices", []):
            for call in choice["delta"].get("tool_calls") or []:
                entry = calls.setdefault(call["index"], {"name": "", "arguments": ""})
                function = call.get("function") or {}
                entry["name"] += function.get("name") or ""
                entry["arguments"] += function.get("arguments") or ""
    return [calls[index] for index in sorted(calls)]


def _log(fake: FakeAI) -> list[dict]:
    return [json.loads(line) for line in fake.log_path.read_text().splitlines()]


def test_models_lists_the_fake_model_and_is_logged(fake: FakeAI) -> None:
    response = httpx.get(fake.url + "/v1/models", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert [model["id"] for model in response.json()["data"]] == ["fake-ai"]
    entry = _log(fake)[0]
    assert (entry["method"], entry["path"], entry["reply"]) == ("GET", "/v1/models", "models")
    headers = {key.lower(): value for key, value in entry["headers"].items()}
    assert headers["authorization"] == "<redacted>"
    assert "secret" not in fake.log_path.read_text()


def test_default_reply_streams_an_echo_of_the_prompt(fake: FakeAI) -> None:
    response = _chat(fake, [{"role": "system", "content": "be nice"},
                            {"role": "user", "content": "Please   summarise\nthis text"}])
    assert response.status_code == 200
    assert response.text.rstrip().endswith("data: [DONE]")
    assert len([e for e in _sse(response.text) if e.get("choices")]) > 3  # really streamed
    assert "Please summarise this text" in _chat_text(response)
    assert _log(fake)[0]["scenario"] == "default"


def test_script_tag_picks_the_scenario_and_unknown_names_list_the_choices(fake: FakeAI) -> None:
    long = _chat_text(_chat(fake, [{"role": "user", "content": "go [script:long]"}]))
    assert "```python" in long and long.count("### ") == 10
    unknown = _chat_text(_chat(fake, [{"role": "user", "content": "[script:nope]"}]))
    assert "Unknown scenario 'nope'" in unknown and "tool-bash" in unknown


def test_tag_parameters_and_last_tag_wins() -> None:
    assert parse_tag("a [script:long] b [script:slow tps=4 latency_ms=800]") == (
        "slow", {"tps": "4", "latency_ms": "800"})
    assert parse_tag("no tag here") == (None, {})


def test_tool_scenario_runs_across_the_tool_loop(fake: FakeAI) -> None:
    prompt = {"role": "user", "content": "do it [script:tool-bash]"}
    first = _chat(fake, [prompt], BASH)
    calls = _chat_calls(first)
    assert [call["name"] for call in calls] == ["Bash"]
    assert json.loads(calls[0]["arguments"]) == {"command": "echo hello from fake ai"}
    assert '"finish_reason":"tool_calls"' in first.text.replace(" ", "")

    # Turn 2 carries the tool result (and a reminder the app appends); the
    # prompt is still the user's, so the scenario moves to its final step.
    second = _chat(fake, [
        prompt,
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "hello from fake ai"},
        {"role": "user", "content": "<system-reminder>time passed</system-reminder>"},
    ], BASH)
    assert _chat_calls(second) == []
    assert "All done" in _chat_text(second)
    assert [(e["round"], e["reply"]) for e in _log(fake)] == [(0, "tool_calls"), (1, "text")]


def test_multi_round_scenario_steps_by_tool_rounds(fake: FakeAI) -> None:
    prompt = {"role": "user", "content": "[script:tool-multi]"}

    def round_trip(count: int) -> list[dict]:
        messages = [prompt]
        for number in range(count):
            call = {"id": f"c{number}", "type": "function",
                    "function": {"name": "Bash", "arguments": "{}"}}
            messages += [{"role": "assistant", "tool_calls": [call]},
                         {"role": "tool", "tool_call_id": f"c{number}", "content": "ok"}]
        return messages

    assert len(_chat_calls(_chat(fake, round_trip(0), BASH))) == 2
    assert len(_chat_calls(_chat(fake, round_trip(1), BASH))) == 1
    assert "two rounds" in _chat_text(_chat(fake, round_trip(2), BASH))


@pytest.mark.asyncio
async def test_background_stream_has_a_real_running_monitor_task(fake: FakeAI, tmp_path: Path) -> None:
    import time

    from app.core.monitor_spawn import make_fluid_monitor_spawner
    from app.core.query_session_lifecycle import stop_all_background_tasks
    from app.core.task_registry import task_registry
    from app.db import database
    from app.mcp import builtin_tools as bt
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolCtx, ToolRegistry

    session_id = "background-stream"
    database.create_session(session_id, "default")
    prompt = {"role": "user", "content": "[script:tool-background-stream]"}
    tools = [{"type": "function", "function": {"name": "Monitor", "parameters": {}}}]
    calls = _chat_calls(_chat(fake, [prompt], tools))
    assert len(calls) == 1 and calls[0]["name"] == "Monitor"
    call = calls[0]
    arguments = json.loads(call["arguments"])
    assert arguments == {"command": "sleep 120", "wait_seconds": 0}
    registry = ToolRegistry()
    register_builtins(registry)
    task = None
    try:
        outcome = await registry.dispatch(call["name"], arguments, ToolCtx(
            cwd=str(tmp_path), extra={"monitor_spawn": make_fluid_monitor_spawner(session_id)},
        ))
        assert not outcome.is_error
        task_id = outcome.background_task.task_id
        task = bt._background_tasks[task_id]
        assert task.session_id == session_id and task.is_running()
        assert task_registry.is_monitor_shell_task(session_id, task_id)
        messages = [prompt, {"role": "assistant", "tool_calls": [
            {"id": "monitor-1", "type": "function", "function": call}]},
            {"role": "tool", "tool_call_id": "monitor-1", "content": outcome.text}]
        deltas, times = [], []
        async with httpx.AsyncClient(timeout=10) as client:
            async with client.stream("POST", fake.url + "/v1/chat/completions", json={
                "model": "fake-ai", "stream": True, "messages": messages, "tools": tools,
            }) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    for choice in json.loads(line[len("data: "):]).get("choices", []):
                        delta = choice["delta"]
                        assert not delta.get("tool_calls")
                        if delta.get("content"):
                            deltas.append(delta["content"])
                            times.append(time.monotonic())
                            assert task.is_running()
                            # This is the wire view consumed by /live's activeTasks.
                            visible = task_registry.get_visible(session_id)
                            assert [(t["id"], t["kind"], t["status"]) for t in visible] == [
                                (task_id, "shell", "running")]
        assert len(deltas) > 10
        assert times[-1] - times[0] >= 3
        assert "switch chats" in "".join(deltas)
        assert [(e["round"], e["reply"]) for e in _log(fake)] == [(0, "tool_calls"), (1, "text")]
        # Archive uses this Stop sweep, ignoring any keep-shells preference.
        await stop_all_background_tasks(session_id, user_stop=True, respect_shell_preference=False)
        assert not task.is_running()
        assert task_registry.get_active(session_id) == []
    finally:
        # Reap the real shell even on assertion failure; never wait 120s.
        await bt.stop_session_background_shells(session_id, require_stopped=True)
    assert task is not None and task.process.returncode is not None
    assert task.task_id not in bt._background_tasks
    assert task_registry.get_active(session_id) == []


def test_question_answer_continues_the_scenario_that_asked(fake: FakeAI) -> None:
    ask = [{"type": "function", "function": {"name": "AskUser", "parameters": {}}}]
    prompt = {"role": "user", "content": "pick one [script:ask-question]"}
    calls = _chat_calls(_chat(fake, [prompt], ask))
    assert [call["name"] for call in calls] == ["AskUser"]
    assert len(json.loads(calls[0]["arguments"])["questions"]) == 1
    history = [prompt,
               {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                     "function": {"name": "AskUser",
                                                                  "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c1", "content": '{"status": "queued"}'}]
    assert "wait for your answer" in _chat_text(_chat(fake, history, ask))
    # The app queues the tray's answer as a user message: it is round 2 of the
    # scenario that asked, not a new prompt, and {answer} carries it.
    answer = {"role": "user", "content": "Answer to your question:\n"
              "Which colour should the banner use?\nUser skipped this question."}
    reply = _chat_text(_chat(fake, history + [
        {"role": "assistant", "content": "I'll wait."}, answer], ask))
    assert "Which colour should the banner use?\nUser skipped this question." in reply
    assert [e["round"] for e in _log(fake)] == [0, 1, 2]
    assert _log(fake)[-1]["scenario"] == "ask-question"
    # Two questions in one request.
    two = _chat_calls(_chat(fake, [{"role": "user", "content": "[script:ask-questions]"}], ask))
    assert len(json.loads(two[0]["arguments"])["questions"]) == 2


@pytest.mark.parametrize("scenario,tool,arguments", [
    ("tool-bash", "Bash", "command='echo hello from fake ai'"),
    ("tool-background-stream", "Monitor", "command='sleep 120', wait_seconds=0"),
])
def test_code_mode_requests_get_one_python_cell(fake: FakeAI, scenario, tool, arguments) -> None:
    prompt = {"role": "user", "content": f"[script:{scenario} tps=0]"}
    calls = _chat_calls(_chat(fake, [prompt], CODE_MODE))
    assert [call["name"] for call in calls] == ["run_python"]
    code = json.loads(calls[0]["arguments"])["code"]
    assert f"from tools.{tool} import {tool}" in code
    assert f"{tool}({arguments})" in code
    compile(code, "<cell>", "exec")
    assert _log(fake)[0]["code_mode"] is True


def test_side_calls_without_tools_get_text(fake: FakeAI) -> None:
    response = _chat(fake, [{"role": "user", "content": "title this [script:tool-bash]"}])
    assert _chat_calls(response) == [] and _chat_text(response)


def test_anthropic_messages_dialect_plays_the_same_scenario(fake: FakeAI) -> None:
    tools = [{"name": "Bash", "input_schema": {"type": "object"}}]
    prompt = {"role": "user", "content": [{"type": "text", "text": "[script:tool-bash]"}]}
    first = httpx.post(fake.url + "/v1/messages", json={
        "model": "fake-ai", "stream": True, "tools": tools, "messages": [prompt]}, timeout=10)
    assert '"name": "Bash"' in first.text or '"name":"Bash"' in first.text
    assert "tool_use" in first.text
    second = httpx.post(fake.url + "/v1/messages", json={
        "model": "fake-ai", "stream": False, "tools": tools, "messages": [
            prompt,
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                               "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": "hello"}]},
        ]}, timeout=10).json()
    assert second["stop_reason"] == "end_turn"
    assert "All done" in second["content"][0]["text"]


def test_responses_dialect_counts_function_call_rounds() -> None:
    entries = conversation("responses", {"input": [
        {"role": "user", "content": [{"type": "input_text", "text": "[script:tool-bash]"}]},
        {"type": "function_call", "call_id": "a"}, {"type": "function_call", "call_id": "b"},
        {"type": "function_call_output", "call_id": "a"},
        {"type": "function_call_output", "call_id": "b"},
    ]})
    assert prompt_and_round(entries) == ("[script:tool-bash]", 1)


def test_responses_endpoint_streams_text(fake: FakeAI) -> None:
    response = httpx.post(fake.url + "/v1/responses", json={
        "model": "fake-ai", "stream": True, "input": "hello there"}, timeout=10)
    assert "response.output_text.delta" in response.text
    assert "response.completed" in response.text


@pytest.mark.parametrize("name,status,retry_after", [("error-500", 500, None),
                                                      ("rate-limit-429", 429, "1")])
def test_error_scenarios(fake: FakeAI, name: str, status: int, retry_after: str | None) -> None:
    response = _chat(fake, [{"role": "user", "content": f"[script:{name}]"}])
    assert response.status_code == status
    assert response.headers.get("retry-after") == retry_after
    assert _log(fake)[0]["reply"] == "error"


def test_cut_scenario_drops_the_stream_without_a_terminal_frame(fake: FakeAI) -> None:
    with pytest.raises(httpx.HTTPError):
        # A cut chunked stream is a protocol error for a strict client...
        _chat(fake, [{"role": "user", "content": "[script:cut tps=0]"}])
    entry = _log(fake)[0]
    assert (entry["scenario"], entry["reply"]) == ("cut", "cut")


def test_timing_knobs_come_from_tag_scenario_then_globals(tmp_path: Path) -> None:
    with FakeAI(log_path=tmp_path / "log.jsonl", tps=99, latency_ms=5) as fake:
        def plan(text: str):
            recorded = fake_wire.Recorded("POST", "/v1/chat/completions", {},
                                          {"messages": [{"role": "user", "content": text}]})
            return fake.plan(recorded)[0]
        assert (plan("hi").tps, plan("hi").latency_ms) == (99, 5)
        assert (plan("[script:slow]").tps, plan("[script:slow]").latency_ms) == (6, 600)
        slow = plan("[script:slow tps=2 latency_ms=0]")
        assert (slow.tps, slow.latency_ms) == (2, 0)


def test_shipped_scenarios_all_load() -> None:
    assert set(Scenarios(SCENARIOS_DIR).load()) >= {
        "default", "long", "slow", "tool-bash", "tool-multi", "tool-background-stream",
        "error-500", "rate-limit-429", "cut"}


@pytest.mark.parametrize("data,message", [
    ({"steps": []}, "non-empty"),
    ({"steps": [{"text": "x", "error": {"status": 500}}]}, "needs text"),
    ({"steps": [{"tool_calls": [{"input": {}}]}]}, "needs a name"),
    ({"steps": [{"error": {"status": "500"}}]}, "integer status"),
])
def test_bad_scenarios_are_refused(tmp_path: Path, data: dict, message: str) -> None:
    (tmp_path / "default.json").write_text(json.dumps({"steps": [{"text": "ok"}]}))
    (tmp_path / "bad.json").write_text(json.dumps(data))
    with pytest.raises(ScenarioError, match=message):
        Scenarios(tmp_path)


def test_a_default_scenario_is_required(tmp_path: Path) -> None:
    (tmp_path / "other.json").write_text(json.dumps({"steps": [{"text": "ok"}]}))
    with pytest.raises(ScenarioError, match="default.json"):
        Scenarios(tmp_path)


def test_test_fixture_reexports_the_one_implementation() -> None:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "fluid/tests/fixtures/fake_provider_server.py"
    spec = importlib.util.spec_from_file_location("fps_shim_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    shared = __import__(module._NAME)
    for name in ("FakeProviderServer", "Chat", "Messages", "Responses", "Reply", "error_reply"):
        assert getattr(module, name) is getattr(shared, name)
    assert Path(shared.__file__).resolve() == Path(fake_wire.__file__).resolve()


def test_queue_mode_get_does_not_eat_scripted_replies() -> None:
    with fake_wire.FakeProviderServer(fake_wire.Chat.text("only")) as server:
        assert httpx.get(server.url + "/v1/models").status_code == 200
        assert httpx.get(server.url + "/elsewhere").status_code == 404
        reply = httpx.post(server.url + "/v1/chat/completions", json={})
        assert _chat_text(reply) == "only"


# ─── provider safety checks (docs/internal/guardrail-contract.md) ──────────


def _refusal_prompt() -> list[dict]:
    return [{"role": "user", "content": "[script:guardrail-refusal] audit this"}]


def test_refusal_on_messages_carries_anthropic_stop_details(fake: FakeAI) -> None:
    response = httpx.post(fake.url + "/v1/messages", json={
        "model": "fake-ai", "stream": True, "max_tokens": 64, "messages": _refusal_prompt()},
        timeout=10)
    events = _sse(response.text)
    delta = next(e for e in events if e.get("type") == "message_delta")
    assert delta["delta"]["stop_reason"] == "refusal"
    assert delta["delta"]["stop_details"] == {
        "type": "refusal", "category": "cyber",
        "explanation": "This request looks like offensive security work.",
    }
    assert not any(e.get("type") == "content_block_start" for e in events)
    assert _log(fake)[-1]["reply"] == "refusal"


def test_refusal_spares_a_retry_on_another_model(fake: FakeAI) -> None:
    response = httpx.post(fake.url + "/v1/messages", json={
        "model": "fake-ai-safe", "stream": False, "max_tokens": 64,
        "messages": _refusal_prompt()}, timeout=10)
    body = response.json()
    assert body["stop_reason"] == "end_turn"
    assert body["content"][0]["text"] == "Answered on the model you approved."


def test_refusal_on_chat_is_a_content_filter_stop(fake: FakeAI) -> None:
    from fluid.providers._chat_completions import ChatCompletionsNormalizer

    response = _chat(fake, _refusal_prompt())
    norm = ChatCompletionsNormalizer()
    chunks = [c for event in _sse(response.text) for c in norm.consume("message", event)]
    assert _chat_text(response) == ""
    [error] = [c for c in chunks if c.kind == "error"]
    assert error.payload["native_stop_reason"] == "content_filter"


def test_refusal_on_responses_is_incomplete_content_filter(fake: FakeAI) -> None:
    response = httpx.post(fake.url + "/v1/responses", json={
        "model": "fake-ai", "stream": True, "input": "[script:guardrail-refusal] audit this"},
        timeout=10)
    [terminal] = [e for e in _sse(response.text) if e.get("type") == "response.incomplete"]
    assert terminal["response"]["incomplete_details"] == {"reason": "content_filter"}
    assert terminal["response"]["output"] == []


def test_served_model_is_stamped_on_every_dialect(fake: FakeAI) -> None:
    from fluid.providers._chat_completions import ChatCompletionsNormalizer

    chat = _chat(fake, [{"role": "user", "content": "[script:guardrail-reroute] hi"}])
    norm = ChatCompletionsNormalizer()
    chunks = [c for event in _sse(chat.text) for c in norm.consume("message", event)]
    usage = next(c for c in chunks if c.kind == "usage")
    assert usage.payload["served_model"] == "fake-ai-safe"

    messages = _sse(httpx.post(fake.url + "/v1/messages", json={
        "model": "fake-ai", "stream": True, "max_tokens": 64,
        "messages": [{"role": "user", "content": "[script:default served_model=other-ai] hi"}]},
        timeout=10).text)
    assert messages[0]["message"]["model"] == "other-ai"

    responses = _sse(httpx.post(fake.url + "/v1/responses", json={
        "model": "fake-ai", "stream": True, "input": "[script:guardrail-reroute] hi"},
        timeout=10).text)
    assert responses[-1]["response"]["model"] == "fake-ai-safe"


def test_refusal_step_shape_is_validated(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text(json.dumps({
        "steps": [{"refusal": {"refuse_models": "fake-ai"}, "text": "x"}]}))
    with pytest.raises(ScenarioError, match="refuse_models"):
        Scenarios(tmp_path).load()


@pytest.mark.parametrize("dialect", ["anthropic", "openrouter", "responses"])
async def test_fluid_pauses_on_the_fake_refusal_and_continues_on_the_approved_model(
    fake: FakeAI, dialect: str,
) -> None:
    """The whole native path against the fake: block → prompt → Continue →
    the retry on fake-ai-safe answers → the next turn is back on fake-ai."""
    import asyncio

    from fluid.engine.guardrail import GUARDRAIL_TOOL
    from fluid.engine.native import NativeLoop
    from fluid.events.model import GuardrailBlocked, PermissionRequest, TurnComplete
    from fluid.events.ops import RespondPermission, UserTurn
    from fluid.providers.anthropic.client import AnthropicClient
    from fluid.providers.openai_responses.client import OpenAIResponsesClient
    from fluid.providers.openrouter.client import OpenRouterClient
    from fluid.store.base import MemoryStore

    provider = {
        "anthropic": lambda: AnthropicClient(api_key="k", base_url=fake.url + "/v1/messages"),
        "openrouter": lambda: OpenRouterClient(api_key="k",
                                               base_url=fake.url + "/v1/chat/completions"),
        "responses": lambda: OpenAIResponsesClient(api_key="k",
                                                   base_url=fake.url + "/v1/responses"),
    }[dialect]()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="fake-ai")

    async def answer() -> PermissionRequest:
        while True:
            log = loop.bus.log(loop.session_id) if loop._session is not None else []
            prompt = next((e for e in log if isinstance(e, PermissionRequest)), None)
            if prompt is not None:
                await loop.submit(RespondPermission(
                    request_id=prompt.request_id, decision="allow",
                    updated_input={**prompt.input, "model": "fake-ai-safe"}))
                return prompt
            await asyncio.sleep(0.01)

    watcher = asyncio.create_task(answer())
    try:
        await loop.submit(UserTurn(content="[script:guardrail-refusal] audit this"))
        prompt = await asyncio.wait_for(watcher, 20)
        await asyncio.wait_for(loop.wait_turn(), 20)
        events = loop.bus.log(loop.session_id)
        assert prompt.name == GUARDRAIL_TOOL
        expected = "cyber" if dialect == "anthropic" else "content_filter"
        assert prompt.input["category"] == expected
        [blocked] = [e for e in events if isinstance(e, GuardrailBlocked)]
        assert blocked.outcome == "continued" and blocked.retry_model == "fake-ai-safe"
        [done] = [e for e in events if isinstance(e, TurnComplete)]
        assert done.stop_reason == "end_turn"
        assert loop.model == "fake-ai"  # this turn only
        models = [r["model"] for r in _log(fake) if r.get("method") == "POST"]
        assert models[-2:] == ["fake-ai", "fake-ai-safe"]
    finally:
        watcher.cancel()
        await loop.aclose()
