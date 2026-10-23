"""Unified selection/spend contracts. All providers are fakes; no paid calls."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.core.generation_resolver import CHOOSE_MESSAGE, Candidate, INCLUDED_PRICE, Resolver, RunContext, UNKNOWN_PRICE


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
                      "options": [{**b.view(), "label": "Same as last time"}, a.view()], "message": CHOOSE_MESSAGE}
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
    backend = FakeBackend([candidate()])
    for context in (replace(INTERACTIVE, unattended=True, remembered_choice={"account": "missing"}),
                    replace(INTERACTIVE, job_choice={"account": "missing"})):
        output = await Resolver(backend).generate("image", {"prompt": "make it"}, context)
        assert output["status"] == "refused"
    assert backend.calls == []


async def test_missing_remembered_choice_asks_again_interactively_without_spending():
    backend = FakeBackend([candidate()])
    context = replace(INTERACTIVE, remembered_choices={"image": {"account": "missing", "model": "gone"}})
    output = await Resolver(backend).generate("image", {"prompt": "make it"}, context)
    assert output["status"] == "choose" and output["remembered_unavailable"] is True
    assert [o["account"] for o in output["options"]] == ["account-a"]
    assert backend.calls == []


async def test_unpriced_remembered_choice_is_never_submitted():
    unpriced = replace(candidate(), price=dict(UNKNOWN_PRICE))
    priced = candidate(account="account-b", model="model-b")
    remembered = {"image": {**unpriced.choice(), "origin": "migrated"}}
    backend = FakeBackend([unpriced, priced])
    # Interactive: the card again, so the user re-picks a priced option.
    output = await Resolver(backend).generate("image", {"prompt": "make it"}, replace(INTERACTIVE, remembered_choices=remembered))
    assert output["status"] == "choose" and output["remembered_unpriced"] is True
    assert {o["account"] for o in output["options"]} == {"account-a", "account-b"}
    # Unattended: skipped and reported, pointing at Settings.
    output = await Resolver(backend).generate("image", {"prompt": "make it"},
                                              replace(INTERACTIVE, unattended=True, remembered_choices=remembered))
    assert output["status"] == "skipped" and output["reason"] == "stored_choice_unpriced"
    assert "no known price" in output["message"] and "Settings > Default models" in output["message"]
    assert output["choice"]["account"] == "account-a"
    assert not any(call[0] in ("reserve", "execute") for call in backend.calls)
    # Naming it explicitly still refuses truthfully, as any unpriced request does.
    output = await Resolver(backend).generate("image", {"prompt": "make it", "source": "account-a", "model": "model-a"},
                                              replace(INTERACTIVE, remembered_choices=remembered))
    assert output["status"] == "refused" and output["reason"] == "price_unavailable"


async def test_unusable_remembered_choice_asks_again_interactively():
    class Strict(FakeBackend):
        async def prepare(self, c, options, prompt):
            if options.get("steps") == 99:
                raise ValueError("steps is out of range")
            return await super().prepare(c, options, prompt)

    offline = replace(candidate(), available=False, reason="Worker offline")
    other = candidate(account="account-b", model="model-b")
    for entries, remembered in (([offline, other], offline.choice()),
                                ([candidate(), other], {**candidate().choice(), "options": {"steps": 99}})):
        backend = Strict(entries)
        output = await Resolver(backend).generate("image", {"prompt": "make it"},
                                                  replace(INTERACTIVE, remembered_choices={"image": remembered}))
        assert output["status"] == "choose" and output["remembered_unavailable"] is True
        output = await Resolver(backend).generate("image", {"prompt": "make it"},
                                                  replace(INTERACTIVE, unattended=True, remembered_choices={"image": remembered}))
        assert output["status"] == "refused"
        assert not any(call[0] in ("reserve", "execute") for call in backend.calls)

    # Saved options are fine, but this call's own options are not: a refusal
    # with the real message, not a silent re-offer of the card.
    backend = Strict([candidate(), other])
    output = await Resolver(backend).generate("image", {"prompt": "make it", "options": {"steps": 99}},
                                              replace(INTERACTIVE, remembered_choices={"image": candidate().choice()}))
    assert output["status"] == "refused" and output["reason"] == "invalid_request"
    assert output["error"] == "steps is out of range"
    assert not any(call[0] in ("reserve", "execute") for call in backend.calls)


@pytest.mark.parametrize("raw_schema", [False, True])
@pytest.mark.parametrize("saved_options", [{}, {"steps": 6}])
async def test_remembered_required_input_model_refuses_bad_call_and_runs_good_input(monkeypatch, raw_schema, saved_options):
    from app.services.fal_schema import ModelSchema, ParamField, schema_service
    from app.services.generation_sources import Sources

    schema = ModelSchema("image-to-video", fields=[
        ParamField("prompt", required=True, is_primary_text=True, enum=["animate it"]),
        ParamField("image_url", required=True, is_file=True),
        ParamField("steps", type="integer", default=4, minimum=1, maximum=10),
    ], primary_text_field="prompt", input_schema={
        "type": "object", "required": ["prompt", "image_url"],
        "properties": {"prompt": {"type": "string"}, "image_url": {"type": "string"},
                       "steps": {"type": "integer", "minimum": 1, "maximum": 10}},
    } if raw_schema else None)
    entry = replace(candidate(kind="video", model=schema.endpoint_id), options={"steps": 4}, schema=schema.to_dict())
    backend = FakeBackend([entry])
    sources = Sources(None, INTERACTIVE)
    monkeypatch.setattr(sources, "local_worker", lambda: entry.worker)
    monkeypatch.setattr(sources, "fal_key", AsyncMock(return_value="unused"))
    monkeypatch.setattr(sources, "fal_price", AsyncMock(return_value=dict(PRICE)))
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    # Exercise production preparation, not a fake that merely imitates the error.
    backend.prepare = sources.prepare
    context = replace(INTERACTIVE, remembered_choices={"video": {**entry.choice(), "options": saved_options}})
    resolver = Resolver(backend)
    steps = saved_options.get("steps", 4)

    for supplied in ({}, {"image_url": 123}):
        with pytest.raises(ValueError) as error:
            schema.validate_options({"steps": steps, **supplied}, "animate it")
        output = await resolver.generate("video", {"prompt": "animate it", "options": supplied}, context)
        assert output["status"] == "refused" and output["reason"] == "invalid_request"
        assert output["error"] == str(error.value)
        assert "remembered_unavailable" not in output
        assert backend.calls == []

    output = await resolver.generate("video", {"prompt": "animate it", "options": {
        "image_url": "https://example.com/input.png",
    }}, context)
    assert output["status"] == "completed" and output["remembered"] is True
    assert output["choice"]["model"] == schema.endpoint_id
    assert output["choice"]["options"] == {"steps": steps, "image_url": "https://example.com/input.png"}
    assert [call[0] for call in backend.calls] == ["reserve", "execute"]


@pytest.mark.parametrize("saved_options, expected_status", [
    ({"weights": ["bad"]}, "choose"),
    ({"image_size": {"width": 128, "height": 128}}, "refused"),
])
async def test_remembered_options_keep_original_nested_constraints(monkeypatch, saved_options, expected_status):
    from app.services.fal_schema import normalize_openapi, schema_service
    from app.services.generation_sources import Sources
    from tests.services.test_fal_schema import _openapi_with_input

    schema = normalize_openapi("image-to-video", _openapi_with_input({
        "prompt": {"type": "string"},
        "image_url": {"type": "string"},
        "weights": {"type": "array", "items": {"type": "integer"}},
        "image_size": {"type": "object", "required": ["width", "height"],
                       "properties": {"width": {"type": "integer"}, "height": {"type": "integer"}}},
    }, ["prompt", "image_url"]))
    entry = replace(candidate(kind="video", model=schema.endpoint_id), options={}, schema=schema.to_dict())
    backend = FakeBackend([entry])
    sources = Sources(None, INTERACTIVE)
    monkeypatch.setattr(sources, "local_worker", lambda: entry.worker)
    monkeypatch.setattr(sources, "fal_key", AsyncMock(return_value="unused"))
    monkeypatch.setattr(schema_service, "get_schema", AsyncMock(return_value=schema))
    backend.prepare = sources.prepare
    context = replace(INTERACTIVE, remembered_choices={"video": {**entry.choice(), "options": saved_options}})
    output = await Resolver(backend).generate("video", {"prompt": "animate it", "options": {"image_url": 123}}, context)
    assert output["status"] == expected_status
    if expected_status == "choose":
        assert output["remembered_unavailable"] is True
    else:
        with pytest.raises(ValueError) as error:
            schema.validate_options({**saved_options, "image_url": 123}, "animate it")
        assert output["reason"] == "invalid_request" and output["error"] == str(error.value)
        assert "remembered_unavailable" not in output
    assert backend.calls == []


@pytest.mark.parametrize("saved_options", [{"unknown": 1}, {"steps": 99}, {"steps": "4"}, {"size": "gone"}])
async def test_saved_fal_options_still_reoffer_unknown_or_invalid_fields(saved_options):
    from app.services.fal_schema import ModelSchema, ParamField

    schema = ModelSchema("image-to-video", fields=[
        ParamField("prompt", required=True, is_primary_text=True),
        ParamField("image_url", required=True, is_file=True),
        ParamField("steps", type="integer", minimum=1, maximum=10),
        ParamField("size", enum=["square", "wide"]),
    ], primary_text_field="prompt")
    entry = candidate(kind="video", model=schema.endpoint_id, schema=schema.to_dict())

    class Strict(FakeBackend):
        async def prepare(self, c, options, prompt):
            return replace(c, options=schema.validate_options(options, prompt))

    backend = Strict([entry])
    context = replace(INTERACTIVE, remembered_choices={"video": {**entry.choice(), "options": saved_options}})
    output = await Resolver(backend).generate("video", {"prompt": "animate it"}, context)
    assert output["status"] == "choose" and output["remembered_unavailable"] is True
    assert backend.calls == []


async def test_unreadable_choices_skip_unattended_and_ask_interactively():
    backend = FakeBackend([candidate(), candidate(account="account-b", model="model-b")])
    output = await Resolver(backend).generate("image", {"prompt": "make it"}, RunContext(choices_unreadable=True))
    assert output["status"] == "skipped" and output["reason"] == "choices_unreadable"
    assert "could not be read" in output["message"]
    output = await Resolver(backend).generate("image", {"prompt": "make it"}, replace(INTERACTIVE, choices_unreadable=True))
    assert output["status"] == "choose" and output["choices_unreadable"] is True
    assert "could not be read" in output["message"]
    assert backend.calls == []


async def test_remembered_choices_are_per_kind():
    a, b = candidate(), candidate(account="account-b", model="model-b")
    video = candidate(account="account-v", model="model-v", kind="video")
    backend = FakeBackend([a, b, video])
    context = replace(INTERACTIVE, remembered_choices={"video": video.choice()}, last_choices={"image": b.choice()})
    image = await Resolver(backend).generate("image", {"prompt": "make it"}, context)
    assert image["status"] == "choose" and image["options"][0]["label"] == "Same as last time"
    assert image["options"][0]["account"] == "account-b"
    output = await Resolver(backend).generate("video", {"prompt": "make it"}, context)
    assert output["status"] == "completed" and output["choice"]["account"] == "account-v"
    assert output["remembered"] is True and "Settings" in output["message"]


async def test_named_request_outranks_remembered_choice_interactively():
    a, b = candidate(), candidate(account="account-b", model="model-b")
    context = replace(INTERACTIVE, remembered_choices={"image": a.choice()})
    output = await Resolver(FakeBackend([a, b])).generate("image", {"prompt": "make it", "source": "account-b"}, context)
    assert output["choice"]["account"] == "account-b" and "remembered" not in output


async def test_remembered_options_apply_and_a_used_choice_is_reported():
    a = candidate()
    backend = FakeBackend([a])
    context = replace(INTERACTIVE, remembered_choices={"image": {**a.choice(), "options": {"size": "wide"}, "origin": "card"}})
    output = await Resolver(backend).generate("image", {"prompt": "make it"}, context)
    assert output["status"] == "completed" and output["remembered"] is True
    assert ("prepare", "account-a", {"size": "wide"}) in backend.calls


async def test_included_plan_price_runs_without_confirmation():
    entry = replace(candidate(source="supergrok", account="grok-1"), price=dict(INCLUDED_PRICE))
    backend = FakeBackend([entry])
    output = await Resolver(backend).generate("image", {"prompt": "make it", "source": "supergrok"}, INTERACTIVE)
    assert output["status"] == "completed" and output["price"]["status"] == "included"
    assert [c[0] for c in backend.calls] == ["prepare", "reserve", "execute"]


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
