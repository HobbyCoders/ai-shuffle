"""Over-threshold generation asks on the regular permission prompt."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Dict, Tuple
from unittest.mock import AsyncMock, patch

import pytest

from app.core import owner_confirm as confirm
from app.core import permission_handler as ph
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.core.generation_confirmations import SPEND_TOOL, ask_on_prompt
from app.core.generation_resolver import Resolver, continues_approved_generation
from app.core.query_options import _generation_context, _generation_is_interactive
from tests.core.test_approval_pending_message_delivery import (  # noqa: F401
    _pending, _steers, _wakes, assistant_fixture, clean_confirm, decide, home,
    no_connector_delivery, no_gateway_listener, no_push, owner_says, quiet_scheduler,
    reset_backfill_memo_on_setup, reset_interaction_wait_state,
)
from tests.core.test_generation_resolver import FakeBackend, candidate


CANDIDATE = SimpleNamespace(
    price={"currency": "USD", "total": "0.25"}, model="model-a", account="account-a",
    worker="worker-a", options={"size": "square"},
)


async def _ask(decision):
    calls = []

    async def route(kwargs, request_fn):
        calls.append(kwargs)
        return decision

    with patch("app.core.provider_agent_approvals.route_permission", new=route):
        return await ask_on_prompt("session-1", "profile-1", "image", CANDIDATE, "a tree"), calls[0]


async def test_approval_on_the_prompt_lets_it_run():
    outcome, request = await _ask(PermissionResultAllow())
    assert outcome is None
    assert request["tool_name"] == SPEND_TOOL and request["session_id"] == "session-1"
    assert "$0.25" in request["title"] and "model-a" in request["title"]
    # Asked whatever the mode or saved rules say: spending always asks.
    assert request["skip_saved_rules"] is True and request["safety_check"] is True
    assert request["tool_input"]["prompt"] == "a tree"


async def test_decline_returns_why():
    outcome, _ = await _ask(PermissionResultDeny(message="No thanks"))
    assert outcome == "No thanks"


async def test_a_decline_with_no_message_still_reads_as_declined():
    outcome, _ = await _ask(PermissionResultDeny(message=""))
    assert "did not approve" in outcome


COSTLY = {"currency": "USD", "unit": "item", "amount": "0.25", "total": "0.25", "status": "estimated"}
SESSION_ROW = {"id": "row", "profile_id": "assistant-agent"}
Home = Tuple[str, str]


def _context_for(home_id: str, overrides: Dict[str, Any]) -> Any:
    """The turn's real generation context, as ``query_options`` builds it."""
    return _generation_context(
        {}, SESSION_ROW, None, session_id=home_id,
        unattended=not _generation_is_interactive(overrides, SESSION_ROW),
        turn_source=overrides["turn_source"],
    )


def _executed(backend: FakeBackend) -> int:
    return sum(call[0] == "execute" for call in backend.calls)


async def test_a_generation_approved_after_its_turn_ended_runs_exactly_once(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    home_id, _ = home
    _steers(monkeypatch)
    costly = replace(candidate(), price=dict(COSTLY))
    backend = FakeBackend([costly])
    resolver = Resolver(backend)
    args = {"prompt": "a tree", "source": costly.account, "model": costly.model}

    # The prompt waits; the owner sends another message; the call is let go.
    first = asyncio.create_task(resolver.generate("image", args, _context_for(home_id, {"turn_source": "user"})))
    [request] = await _pending(home_id)
    await owner_says(home_id, "and also check CI")
    parked = await asyncio.wait_for(first, 12.0)
    assert parked["status"] == "refused" and parked["reason"] == "declined"
    assert _executed(backend) == 0

    # That turn has ended by now. Approve wakes the agent with a one-use pass.
    await decide(home_id, request, "allow")
    [wake] = _wakes(home_id)
    assert wake["source"] == "wakeup"
    wake_context = _context_for(home_id, {
        "turn_source": "wakeup",
        "continues_approved_generation": continues_approved_generation(wake["payload"]),
    })
    assert wake_context.unattended is False
    assert confirm.arm_pass(home_id, wake["payload"], wake["id"])

    # The retry is admitted without asking again, and is submitted once.
    retry = await asyncio.wait_for(resolver.generate("image", args, wake_context), 5.0)
    assert retry["status"] == "completed"
    assert _executed(backend) == 1
    assert confirm.pending_requests(home_id) == []

    # The same call again is a new spend: it asks, and a decline submits nothing.
    again = asyncio.create_task(resolver.generate("image", args, wake_context))
    [asked] = await _pending(home_id)
    await decide(home_id, asked, "deny")
    assert (await asyncio.wait_for(again, 12.0))["reason"] == "declined"
    assert _executed(backend) == 1


async def test_a_wake_without_a_generation_approval_stays_unattended(
    home: Home,  # noqa: F811
) -> None:
    home_id, _ = home
    costly = replace(candidate(), price=dict(COSTLY))
    backend = FakeBackend([costly])
    context = _context_for(home_id, {
        "turn_source": "wakeup",
        "continues_approved_generation": continues_approved_generation(
            {confirm.PASS_KEY: {"tool_name": "Bash", "tool_input": {"command": "ls"}, "interaction_id": "x"}}),
    })
    assert context.unattended is True and context.spend_prompt is None
    output = await Resolver(backend).generate("image", {"prompt": "a tree", "source": costly.account}, context)
    assert output["status"] == "skipped" and _executed(backend) == 0


@pytest.mark.parametrize("payload,expected", [
    (None, False),
    ({}, False),
    ({confirm.PASS_KEY: {"tool_name": SPEND_TOOL, "interaction_id": "x"}}, True),
    ({confirm.PASS_KEY: {"tool_name": "mcp__ai-tools__generateImage", "interaction_id": "x"}}, True),
    ({confirm.PASS_KEY: {"tool_name": "Bash", "interaction_id": "x"}}, False),
])
def test_only_an_approved_generation_continues(payload, expected):
    assert continues_approved_generation(payload) is expected


def test_a_worker_or_automation_never_becomes_interactive():
    overrides = {"turn_source": "wakeup", "continues_approved_generation": True}
    assert _generation_is_interactive(overrides, SESSION_ROW)
    for blocked in ({"is_worker": True}, {"automation_id": "a"}, {"hidden": True}, {"mode": "voice"}):
        assert not _generation_is_interactive(overrides, {**SESSION_ROW, **blocked})


async def test_a_prompt_answered_while_the_call_still_waits_needs_no_wake(
    home: Home,  # noqa: F811
) -> None:
    home_id, _ = home
    costly = replace(candidate(), price=dict(COSTLY))
    backend = FakeBackend([costly])
    args = {"prompt": "a tree", "source": costly.account, "model": costly.model}
    task = asyncio.create_task(Resolver(backend).generate(
        "image", args, _context_for(home_id, {"turn_source": "user"})))
    [request] = await _pending(home_id)
    await ph.permission_handler.respond(request["request_id"], home_id, "allow", broadcast_func=AsyncMock())
    assert (await asyncio.wait_for(task, 12.0))["status"] == "completed"
    assert _executed(backend) == 1 and _wakes(home_id) == []
