"""Public API producers claim their own run before the registered Plan tool writes."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.core import query_background, sdk_options
from app.core.agent_types import AgentOptions
from app.core.config import settings
from app.core.provider import harness_adapter as ha
from app.core.session_plan import CURRENT_PLAN_RUN
from app.core.session_pool import get_session_pool
from app.db import database, runs, session_events, session_plans
from fluid.providers.base import ProviderCaps, StreamChunk


class PlanProvider:
    name = "controlled"
    caps = ProviderCaps()

    def __init__(self):
        self.calls = 0

    async def stream(self, request):
        self.calls += 1
        yield StreamChunk("message_start", {"usage": {"input_tokens": 5}})
        if self.calls == 1:
            args = {"action": "update", "expected_revision": 0, "request_id": "api-write",
                    "goal": "API-produced plan", "done_when": "Durable event and read API agree",
                    "tasks": [{"id": "one", "title": "Write through real handler", "status": "completed"}]}
            yield StreamChunk("block_start", {"index": 0, "block_type": "tool_use", "tool_use_id": "api-plan", "tool_name": "Plan"})
            yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps(args)})
            reason = "tool_use"
        else:
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "Saved through Plan"})
            reason = "end_turn"
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 4}, "stop_reason": reason})


@pytest.fixture
def plan_api(mock_db, monkeypatch, tmp_path):
    from app.api import auth, query, sessions
    from app.core import credential_gate

    # Only external provider/config discovery is controlled. The API route,
    # admission, recorder claim/publication, real adapter/loop and Plan store run.
    def options(**kwargs):
        options = AgentOptions(model="controlled", cwd=str(tmp_path), permission_mode="bypassPermissions",
                               mcp_servers={}, disallowed_tools=[])
        options.aih_session_id = kwargs["session_id"]
        return options, {}

    monkeypatch.setattr(sdk_options, "build_session_options", options)
    monkeypatch.setattr(ha, "_decide_provider_kind", lambda options: "openai_responses")
    monkeypatch.setattr(ha, "_build_provider_client", AsyncMock(side_effect=lambda *args: (PlanProvider(), None)))
    monkeypatch.setattr(settings, "fluid_code_mode", False)
    monkeypatch.setattr(settings, "fluid_continual", False)
    monkeypatch.setattr(credential_gate, "turn_refusal_async", AsyncMock(return_value=None))
    monkeypatch.setattr(credential_gate, "refusal_for_options_async", AsyncMock(return_value=None))
    database.create_profile("api-plan", "API plan", None, {"model": "controlled"})
    database.create_api_user("plan-user", "Plan caller", auth.hash_api_key("test-only-plan-key"), profile_ids=["api-plan"])
    app = FastAPI()
    app.include_router(query.router)
    app.include_router(sessions.router)
    return app


@pytest.mark.parametrize("cancelled", [False, True])
async def test_background_exception_clears_context_and_releases_claim(mock_db, monkeypatch, cancelled):
    sid = "background-cleanup"
    database.create_session(sid, "default")

    async def fail(**kwargs):
        proof = CURRENT_PLAN_RUN.get()
        active = runs.get_active_run(sid)
        assert proof == (sid, active["id"], active["fence_token"])
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("controlled failure")

    monkeypatch.setattr(query_background, "_run_background_query_impl", fail)
    exception = asyncio.CancelledError if cancelled else RuntimeError
    with pytest.raises(exception):
        await query_background._run_background_query(sid, "Fail", {"id": "default"}, None, None, None, None, None)
    assert CURRENT_PLAN_RUN.get() is None
    assert runs.get_active_run(sid) is None


@pytest.mark.parametrize("path", ["/conversation/start", "/query", "/query/stream"])
async def test_public_api_claim_to_plan_event_and_readback(plan_api, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=plan_api), base_url="http://test",
                                headers={"Authorization": "Bearer test-only-plan-key"}) as client:
        response = await client.post("/api/v1" + path, json={"prompt": "Make a plan", "profile": "api-plan"})
        assert response.status_code == 200, response.text
        if path.endswith("/stream"):
            frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert not [f for f in frames if f["type"] == "error"], frames
            sid = next(f["session_id"] for f in frames if f["type"] == "init")
        else:
            sid = response.json()["session_id"]
        try:
            if path == "/conversation/start":
                task = query_background._pending_background_tasks.get(sid)
                if task is None:
                    task = get_session_pool().peek(sid).background_task
                await asyncio.wait_for(asyncio.shield(task), 10)
            assert session_plans.read(sid)["plan"]["revision"] == 1
            assert runs.get_active_run(sid) is None
            assert CURRENT_PLAN_RUN.get() is None
            events = session_events.get_events(sid)
            written = [event for event in events if event["event_type"] == "plan.updated"]
            assert len(written) == 1
            result = await client.get(f"/api/v1/sessions/{sid}/plan")
            assert result.status_code == 200, result.text
            assert result.json()["plan"] == written[0]["payload"]["plan"]
            assert any(event["event_type"] == "assistant.text_delta" for event in events)
        finally:
            await get_session_pool().remove(sid)
