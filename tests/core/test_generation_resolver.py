"""Unified selection/spend contracts. All providers are fakes; no paid calls."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.core.generation_resolver import Candidate, Resolver, RunContext, UNKNOWN_PRICE


PRICE = {"currency": "USD", "unit": "item", "amount": "0.05", "total": "0.05", "status": "estimated"}
INTERACTIVE = RunContext(session_id="session", unattended=False, owner=True)


def candidate(source="fal", account="account-a", model="model-a", kind="image", **kw):
    return Candidate(source, account, "worker-a", model, kind, True, price=dict(PRICE), options={"size": "square"}, **kw)


class FakeBackend:
    def __init__(self, entries):
        self.entries = entries
        self.calls = []
        self.cap = True

    async def catalog(self, kind, **kw):
        return [c for c in self.entries if c.kind == kind]

    async def prepare(self, c, options, prompt):
        self.calls.append(("prepare", c.account, options))
        return replace(c, options=options)

    async def reserve(self, c, context):
        self.calls.append(("reserve", c.account))
        return self.cap

    async def execute(self, c, prompt, context):
        self.calls.append(("execute", c.account))
        return {"success": True, "output_url": "/fake-output"}


@pytest.mark.parametrize("kind", ["image", "video", "audio", "3d"])
@pytest.mark.parametrize("source", ["fal", "openai", "xai", "chatgpt", "supergrok"])
async def test_names_source_or_account(kind, source):
    first = candidate(source=source, kind=kind)
    other = candidate(source="other", account="account-b", kind=kind)
    backend = FakeBackend([first, other])
    for selection in (source, first.account):
        output = await Resolver(backend).generate(kind, {"prompt": "make it", "source": selection}, INTERACTIVE)
        assert output["status"] == "completed"
        assert output["choice"]["account"] == first.account
        assert output["price"] == PRICE


async def test_unspecified_choose_contract_and_last_first():
    a, b = candidate(), candidate(account="account-b", model="model-b")
    backend = FakeBackend([a, b])
    output = await Resolver(backend).generate("image", {"prompt": "make it"}, replace(INTERACTIVE, last_choice=b.choice()))
    assert output == {"status": "choose", "success": False, "price": UNKNOWN_PRICE, "kind": "image",
                      "options": [{**b.view(), "label": "Same as last time"}, a.view()]}
    assert backend.calls == []


async def test_choose_at_most_four_and_not_fake_extra_sources():
    backend = FakeBackend([candidate(account=f"a-{i}") for i in range(6)])
    output = await Resolver(backend).generate("image", {"prompt": "make it"}, INTERACTIVE)
    assert len(output["options"]) == 4
    backend.entries = backend.entries[:1]
    output = await Resolver(backend).generate("image", {"prompt": "make it"}, INTERACTIVE)
    assert len(output["options"]) == 1
    assert output["status"] == "choose"


@pytest.mark.parametrize("source", ["chatgpt", "supergrok"])
async def test_unverified_subscription_refused(source):
    entry = replace(candidate(source=source), verified=False)
    backend = FakeBackend([entry])
    output = await Resolver(backend).generate("image", {"prompt": "make it", "source": source}, INTERACTIVE)
    assert output["status"] == "refused"
    assert "verified" in output["error"]
    assert backend.calls == []


@pytest.mark.parametrize("stored_field", ["remembered_choice", "job_choice"])
@pytest.mark.parametrize("unattended", [False, True])
async def test_stored_choice_used_without_asking(stored_field, unattended):
    a, b = candidate(), candidate(account="account-b", model="model-b")
    context = replace(INTERACTIVE, unattended=unattended, **{stored_field: b.choice()})
    output = await Resolver(FakeBackend([a, b])).generate("image", {"prompt": "make it"}, context)
    assert output["status"] == "completed"
    assert output["choice"]["account"] == b.account


async def test_job_choice_wins_and_unattended_never_guesses_from_tool_args():
    a, b = candidate(), candidate(account="account-b", model="model-b")
    context = replace(INTERACTIVE, unattended=True, remembered_choice=a.choice(), job_choice=b.choice())
    resolver = Resolver(FakeBackend([a, b]))
    output = await resolver.generate("image", {"prompt": "make it", "source": a.account}, context)
    assert output["choice"]["account"] == b.account
    output = await resolver.generate("image", {"prompt": "make it", "source": a.account}, RunContext())
    assert output == {"status": "skipped", "success": False, "price": UNKNOWN_PRICE, "reason": "no_stored_choice", "kind": "image",
                      "message": "Generation skipped: this unattended run has no remembered or job-stored choice."}


async def test_missing_stored_source_refuses_instead_of_fallback():
    context = replace(INTERACTIVE, remembered_choice={"account": "missing"})
    output = await Resolver(FakeBackend([candidate()])).generate("image", {"prompt": "make it"}, context)
    assert output["status"] == "refused"


async def test_expensive_contract_confirmed_retry_and_no_replay():
    entry = replace(candidate(), price={**PRICE, "amount": "0.25", "total": "0.25"})
    backend = FakeBackend([entry])
    resolver = Resolver(backend)
    args = {"prompt": "make it", "source": entry.account}
    output = await resolver.generate("image", {**args, "confirmed": True}, INTERACTIVE)
    token = output["confirmation_token"]
    assert output == {"status": "needs-confirmation", "success": False, "price": entry.price, "kind": "image",
                      "choice": entry.choice(), "threshold_usd": "0.10", "confirmation_token": token,
                      "retry": {"source": entry.account, "model": entry.model, "worker": entry.worker, "options": entry.options,
                                "confirmed": True, "confirmation_token": token}}
    assert not any(c[0] in ("reserve", "execute") for c in backend.calls)
    retry = {**args, **output["retry"]}
    assert (await resolver.generate("image", retry, INTERACTIVE))["status"] == "completed"
    assert (await resolver.generate("image", retry, INTERACTIVE))["status"] == "needs-confirmation"
    assert [c[0] for c in backend.calls].count("execute") == 1


@pytest.mark.parametrize("change", ["prompt", "options", "session", "price"])
async def test_confirmation_bound_to_exact_request(change):
    entry = replace(candidate(), price={**PRICE, "amount": "0.25", "total": "0.25"})
    backend = FakeBackend([entry])
    resolver = Resolver(backend)
    args = {"prompt": "make it", "source": entry.account}
    output = await resolver.generate("image", args, INTERACTIVE)
    retry, context = {**args, **output["retry"]}, INTERACTIVE
    if change == "prompt":
        retry["prompt"] = "different"
    elif change == "options":
        retry["options"] = {"size": "large"}
    elif change == "session":
        context = replace(context, session_id="different")
    else:
        backend.entries = [replace(entry, price={**entry.price, "total": "0.30"})]
    assert (await resolver.generate("image", retry, context))["status"] == "needs-confirmation"
    assert not any(c[0] == "execute" for c in backend.calls)


async def test_daily_cap_is_enforced_even_after_confirmation():
    backend = FakeBackend([candidate()])
    backend.cap = False
    output = await Resolver(backend).generate("image", {"prompt": "make it", "source": "fal", "confirmed": True}, INTERACTIVE)
    assert output["reason"] == "daily_cap"
    assert not any(c[0] == "execute" for c in backend.calls)


@pytest.mark.parametrize("price", [UNKNOWN_PRICE, {**PRICE, "total": "NaN"}, {**PRICE, "amount": "-1"}, {**PRICE, "currency": "EUR"}, {**PRICE, "total": "0.01"}])
async def test_unknown_or_invalid_prices_cannot_spend_even_confirmed(price):
    backend = FakeBackend([replace(candidate(), price=price)])
    output = await Resolver(backend).generate("image", {"prompt": "make it", "source": "fal", "confirmed": True}, INTERACTIVE)
    assert output["reason"] == "price_unavailable"
    assert not any(c[0] in ("reserve", "execute") for c in backend.calls)


async def test_last_choice_preserves_its_actual_options_without_spending():
    a, b = candidate(), candidate(account="account-b", model="model-b")
    backend = FakeBackend([a, b])
    last = {**b.choice(), "options": {"size": "wide"}}
    output = await Resolver(backend).generate("image", {"prompt": "tree"}, replace(INTERACTIVE, last_choice=last))
    assert output["options"][0]["label"] == "Same as last time"
    assert output["options"][0]["options"] == {"size": "wide"}
    assert not any(c[0] in ("reserve", "execute") for c in backend.calls)


async def test_ambiguous_stored_choice_skips_instead_of_asking_unattended():
    backend = FakeBackend([candidate(), candidate(account="account-b", model="model-b")])
    output = await Resolver(backend).generate("image", {"prompt": "tree"}, RunContext(remembered_choice={"source": "fal"}))
    assert output["status"] == "skipped" and output["reason"] == "stored_choice_ambiguous"
    assert backend.calls == []


async def test_unattended_run_cannot_approve_its_own_spend():
    entry = replace(candidate(), price={**PRICE, "amount": "0.25", "total": "0.25"})
    backend = FakeBackend([entry])
    context = RunContext(session_id="session", remembered_choice=entry.choice())
    output = await Resolver(backend).generate("image", {"prompt": "make it", "confirmed": True}, context)
    assert output["status"] == "skipped" and output["reason"] == "needs_confirmation"
    assert "confirmation_token" not in output and "retry" not in output
    assert not any(c[0] in ("reserve", "execute") for c in backend.calls)


@pytest.mark.parametrize("returned", [{"success": False, "error": "Provider timed out."}, {"success": False}, None])
async def test_failed_or_unknown_outcome_says_do_not_retry(returned):
    from app.core.generation_resolver import DO_NOT_RETRY
    backend = FakeBackend([candidate()])
    backend.execute = AsyncMock(return_value=returned)
    output = await Resolver(backend).generate("image", {"prompt": "make it", "source": "fal"}, INTERACTIVE)
    assert output["status"] == "failed"
    assert output["result"]["error"].endswith(DO_NOT_RETRY)


async def test_budget_check_failure_never_submits():
    backend = FakeBackend([candidate()])
    backend.reserve = AsyncMock(side_effect=ValueError("The network founder is unavailable."))
    output = await Resolver(backend).generate("image", {"prompt": "make it", "source": "fal"}, INTERACTIVE)
    assert output["status"] == "refused" and output["reason"] == "budget_unavailable"
    assert not any(c[0] == "execute" for c in backend.calls)
