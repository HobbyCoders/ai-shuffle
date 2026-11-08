"""Over-threshold generation asks on the regular permission prompt."""
from types import SimpleNamespace
from unittest.mock import patch

from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.core.generation_confirmations import SPEND_TOOL, ask_on_prompt


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
