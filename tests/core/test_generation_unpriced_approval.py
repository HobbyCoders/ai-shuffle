"""INTERACTIVE_UNPRICED_APPROVAL: the one owner-approved run of an unpriced fal
model. Every other context keeps refusing. Fakes only; nothing is submitted."""
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core import generation_resolver
from app.core.generation_resolver import Candidate, Resolver, RunContext, UNKNOWN_PRICE

UNPRICED = {**UNKNOWN_PRICE, "reason": "billed_by_usage", "detail": "fal bills this model by what each run uses.",
            "rate": {"amount": "0.30", "unit": "seconds"}}


class Backend:
    def __init__(self, price=UNPRICED, worker="here"):
        self.entry = Candidate("fal", "fal", worker, "vendor/model", "video", True, price=dict(price))
        self.calls = []
        self.cap = True

    def local_worker(self):
        return "here"

    async def catalog(self, kind, **kw):
        return [self.entry]

    async def prepare(self, c, options, prompt):
        return replace(c, options=options)

    async def reserve(self, c, context):
        raise AssertionError("an unpriced run never reserves a quote")

    async def reserve_amount(self, amount):
        self.calls.append(("reserve", amount))
        return self.cap

    async def execute(self, c, prompt, context):
        self.calls.append(("execute", c.model))
        return {"success": True, "video_url": "/fake"}


ARGS = {"prompt": "a fox", "source": "fal", "model": "vendor/model"}


def chat(prompt):
    return RunContext(session_id="s", unattended=False, owner=True, spend_prompt=prompt)


async def test_owner_approves_one_run_and_one_unit_is_reserved():
    backend, asked = Backend(), AsyncMock(return_value=None)
    output = await Resolver(backend).generate("video", ARGS, chat(asked))
    assert output["status"] == "completed" and output["unpriced_approved"] is True
    assert output["price"]["status"] == "unknown" and output["price"]["total"] is None
    assert backend.calls == [("reserve", Decimal("0.30")), ("execute", "vendor/model")]
    asked.assert_awaited_once()


async def test_declined_or_capped_runs_nothing():
    backend = Backend()
    output = await Resolver(backend).generate("video", ARGS, chat(AsyncMock(return_value="No.")))
    assert output["status"] == "refused" and output["reason"] == "declined" and backend.calls == []
    backend.cap = False
    output = await Resolver(backend).generate("video", ARGS, chat(AsyncMock(return_value=None)))
    assert output["reason"] == "daily_cap" and ("execute", "vendor/model") not in backend.calls


@pytest.mark.parametrize("case", ["unattended", "no_prompt", "remote", "no_rate", "flag_off", "agent_confirmed"])
async def test_every_other_context_still_refuses(case, monkeypatch):
    price = {k: v for k, v in UNPRICED.items() if k != "rate"} if case == "no_rate" else UNPRICED
    backend = Backend(price, worker="elsewhere" if case == "remote" else "here")
    asked = AsyncMock(return_value=None)
    context = chat(asked)
    args = dict(ARGS)
    if case == "unattended":
        context = replace(context, unattended=True, job_choice={"source": "fal", "model": "vendor/model"})
    if case in ("no_prompt", "agent_confirmed"):
        context = replace(context, spend_prompt=None)
    if case == "agent_confirmed":
        args.update(confirmed=True, confirmation_token="x" * 32)
    if case == "flag_off":
        monkeypatch.setattr(generation_resolver, "INTERACTIVE_UNPRICED_APPROVAL", False)
    output = await Resolver(backend).generate("video", args, context)
    assert output["status"] in ("refused", "skipped") and backend.calls == []
    assert output["reason"] in ("price_unavailable", "stored_choice_unpriced")
    asked.assert_not_awaited()
    if output["reason"] == "price_unavailable":
        assert "bills this model by what each run uses" in output["error"]


def test_permission_prompt_states_the_rate_and_no_bound():
    from app.core.generation_confirmations import spend_request, unpriced_question
    entry = Backend().entry
    request = spend_request("video", entry, "a fox")
    assert request["price"] == "not bounded" and request["rate"] == "$0.30 per second"
    question = unpriced_question("video", entry)
    assert "$0.30 per second" in question and "not bounded" in question
