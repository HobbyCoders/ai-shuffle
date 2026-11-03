"""Adjacent native family consent through the real StartSession admission path."""
import json

import pytest

from app.core import model_override as mo
from tests.core.test_model_override import ARGS, _start, _turn, chats, world  # noqa: F401
from tests.core.test_model_override_memory import M260, _save


@pytest.mark.parametrize("text", [
    "Use Opus 4 as the reviewer", "Use Opus four", "use old opus",
    "use the previous opus", "use older opus", "use prior opus", "use legacy opus",
    "use last opus", "Use Opus version 4", "use opus one", "use opus 1m",
    "use 4.7 opus", "use claude-3-opus", "use opus-4-7", "use opus[1m]",
])
async def test_adjacent_qualification_requires_agreement(chats, text):
    _turn("caller", "user", text, profile="agent")
    refused = await _start({**ARGS, "request_id": "pin", "model": "claude-opus-4-7"})
    assert refused.is_error and not chats[1] and not chats[2]
    retry = await _start({**ARGS, "request_id": "family", "model": "opus"})
    assert retry.is_error and len(chats[2]) == 1 and not chats[1], retry.text


@pytest.mark.parametrize("text", [
    "use opus for PR 1040", "use opus for step 2", "Use opus instead of Sol 6.1",
    "use 4 opus agents", "use opus for the 2026 report", "run opus on port 8000",
    "Use opus. 3 files changed", "Opus please - 4 agents",
])
async def test_numbers_elsewhere_allow_start_and_receipt_names_actual_model(chats, text):
    _turn("caller", "user", text, profile="agent")
    result = await _start({**ARGS, "model": "opus"})
    assert not result.is_error, result.text
    assert json.loads(result.text)["model"] == "opus"
    assert not chats[2]


@pytest.mark.parametrize("memory", [
    M260,
    "Risky/cross-cutting code and all UI work: Opus. GetUsage before Opus work; 5h under ~30% left: new coding to Sol.",
    "Use Opus for UI. Opus version 4 is legacy.",
    "Opus 4 for old work. Use Opus for reviews.",
])
async def test_plain_memory_sentence_authorizes_despite_another_qualified_sentence(chats, memory):
    _save(memory)
    _turn("caller", "user", "start the review", profile="agent")
    result = await _start({**ARGS, "model": "opus"})
    assert not result.is_error, result.text
    receipt = json.loads(result.text)
    assert receipt["model"] == "opus" and receipt["model_override"]["source"] == mo.REMEMBERED
    assert not chats[2]


@pytest.mark.parametrize("text", [
    "Use Opus (4.7) for the review", "Opus/4.7 for the review", "Use Opus: 4.7 for the review",
    "Opus, 4.7", "Opus — 4.7", "Opus model 4.7", "opus at version 4.7",
    "opus build 4.7", "opus release 4.7", "opus with 1m context",
    "Use Claude 3 Opus for the review", "Use the 4.7 version of Opus", "Use 2024 Opus",
    "Use earlier Opus", "Use original Opus", "the earlier opus", "use the original opus",
    "Can you use Opus 4.7 for the review?", "Use Opus 4.7, ok?",
    "could we run the review on Opus 4?",
])
@pytest.mark.parametrize("older", ["memory", "chat"])
async def test_separator_qualifications_override_older_consent(chats, text, older):
    if older == "memory":
        _save("Risky/cross-cutting code and all UI work: Opus.")
    else:
        _turn("caller", "user", "use opus for the refactor", profile="agent")
    _turn("caller", "user", text, profile="agent")
    assert (await _start({**ARGS, "request_id": "pin", "model": "claude-opus-4-7"})).is_error
    result = await _start({**ARGS, "request_id": "retry", "model": "opus"})
    assert result.is_error and len(chats[2]) == 1 and not chats[1], result.text


@pytest.mark.parametrize("text", [
    "can you use opus?", "could you run this on opus?", "use opus please?",
])
async def test_polite_directive_question_restores_consent(chats, text):
    _turn("caller", "user", "Use Opus 4.7", profile="agent")
    _turn("caller", "user", text, profile="agent")
    result = await _start({**ARGS, "model": "opus"})
    assert not result.is_error, result.text
    assert json.loads(result.text)["model"] == "opus" and not chats[2]


@pytest.mark.parametrize("text", ["the opus agent stopped", "why did the opus agent stop?"])
async def test_incidental_mention_cannot_restore_consent(chats, text):
    _turn("caller", "user", "Use Opus 4.7", profile="agent")
    _turn("caller", "user", text, profile="agent")
    result = await _start({**ARGS, "model": "opus"})
    assert result.is_error and len(chats[2]) == 1 and not chats[1], result.text


async def test_a_question_alone_is_not_model_consent(chats):
    _turn("caller", "user", "why did the opus agent stop?", profile="agent")
    result = await _start({**ARGS, "model": "opus"})
    assert result.is_error and len(chats[2]) == 1 and not chats[1]


@pytest.mark.parametrize("text, stored", [
    ("Use opus\n\n2 things to check", "Use opus 2 things to check"),
    ("Reviews: Opus\n2 reviewers per PR", "Reviews: Opus 2 reviewers per PR"),
])
async def test_saved_memory_whitespace_normalization_makes_a_number_adjacent(chats, text, stored):
    memory = _save(text)
    assert memory.text == stored
    _turn("caller", "user", "start the review", profile="agent")
    result = await _start({**ARGS, "model": "opus"})
    assert result.is_error and len(chats[2]) == 1 and not chats[1]
    assert "saved memory mentioned Opus" in result.text and "plain wording" in result.text


async def test_question_does_not_replace_a_newer_qualified_owner_request(chats):
    _turn("caller", "user", "Use Opus for the refactor", profile="agent")
    assert not (await _start({**ARGS, "request_id": "earlier", "model": "opus"})).is_error
    chats[1].clear()
    _turn("caller", "user", "Use Opus 4.7 for the review", profile="agent")
    _turn("caller", "user", "why did the opus agent stop?", profile="agent")
    result = await _start({**ARGS, "request_id": "question", "model": "opus"})
    assert result.is_error and len(chats[2]) == 1 and not chats[1], result.text
    _turn("caller", "user", "Now use Opus", profile="agent")
    result = await _start({**ARGS, "request_id": "instruction", "model": "opus"})
    assert not result.is_error, result.text
    assert json.loads(result.text)["model"] == "opus" and len(chats[2]) == 1
