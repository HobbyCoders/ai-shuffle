"""#483/#484: the REST transcript fold shows each approval asked in words once,
as a card keyed by request id at the point it was raised, settled in place by
its answer or expiry, with the outcome row after the owner's answer.

It mirrors the frontend reducer (``frontend/src/lib/stores/session/v2``), so a
reload and every client read the same transcript.
"""

from app.api import sessions_helpers
from app.db import session_events


def _question(command, job):
    return f"The background job “{job}” asks: do you authorize it to run `{command}`? Please answer yes or no."


def _requested(request_id, command, job):
    return "interaction.requested", {
        "interaction_id": f"i-{request_id}", "request_id": request_id, "kind": "permission",
        "request": {"request_id": request_id, "tool_name": "Bash", "tool_input": {"command": command},
                    "assistant_confirm": {"question": _question(command, job), "action": f"run `{command}`",
                                          "asker": job, "chat_session_id": f"chat-{job}"}},
    }


def _answered(request_id, decision):
    return "interaction.answered", {"interaction_id": f"i-{request_id}", "request_id": request_id,
                                    "answer": {"decision": decision, "behavior": decision}}


def _outcome(request_id, decision):
    return "system.note", {"subtype": "assistant_confirm",
                           "data": {"request_id": request_id, "decision": decision}}


def _user(text):
    return "message.user", {"content": text}


def _fold(monkeypatch, events):
    rows = [{"id": f"e{seq}", "session_id": "home", "seq": seq, "run_id": None, "turn_id": None,
             "command_id": None, "event_type": kind, "payload": payload, "created_at": None}
            for seq, (kind, payload) in enumerate(events, start=1)]
    monkeypatch.setattr(session_events, "get_events",
                        lambda _sid, cursor, limit=500: [r for r in rows if r["seq"] > cursor][:limit])
    messages, has_events = sessions_helpers.load_event_messages("home")
    assert has_events
    return messages


def _shape(message):
    data = message.get("systemData") or {}
    if message.get("systemSubtype") == "assistant_confirm_request":
        return ("card", data["request_id"], data["status"], data["attempt"])
    if message.get("systemSubtype") == "assistant_confirm":
        return ("outcome", data["request_id"], data["decision"])
    return (message["role"], message["content"])


def test_two_jobs_interleaved_read_request_answer_outcome(monkeypatch):
    messages = _fold(monkeypatch, [
        _user("start both jobs"),
        _requested("perm-a", "git push", "Release"),
        _requested("perm-b", "rm -rf build", "Cleanup"),
        _answered("perm-a", "allow"), _user("yes"), _outcome("perm-a", "approved"),
        _answered("perm-b", "deny"), _user("no"), _outcome("perm-b", "declined"),
    ])
    assert [_shape(m) for m in messages] == [
        ("user", "start both jobs"),
        ("card", "perm-a", "approved", 1),
        ("card", "perm-b", "declined", 1),
        ("user", "yes"), ("outcome", "perm-a", "approved"),
        ("user", "no"), ("outcome", "perm-b", "declined"),
    ]
    card = messages[1]
    assert card["id"] == "assistant-confirm-perm-a"
    assert card["systemData"]["action"] == "run `git push`" and card["systemData"]["asker"] == "Release"


def test_a_retry_is_a_new_numbered_card_and_an_expired_one_keeps_its_place(monkeypatch):
    messages = _fold(monkeypatch, [
        _requested("perm-a", "git push", "Release"),
        ("interaction.expired", {"interaction_id": "i-perm-a", "request_id": "perm-a", "reason": "ttl"}),
        _requested("perm-a2", "git push", "Release"),
        _answered("perm-a2", "deny"),
        _requested("perm-a3", "git push", "Release"),
    ])
    assert [_shape(m) for m in messages] == [
        ("card", "perm-a", "expired", 1),
        ("card", "perm-a2", "declined", 2),
        ("card", "perm-a3", "pending", 3),
    ]


def test_repeating_an_approved_or_cancelled_request_is_not_asked_again(monkeypatch):
    messages = _fold(monkeypatch, [
        _requested("perm-a", "git push", "Release"),
        _answered("perm-a", "allow"),
        _requested("perm-a2", "git push", "Release"),  # the job runs it again
        ("interaction.expired", {"interaction_id": "i-perm-a2", "request_id": "perm-a2", "reason": "cancelled"}),
        _requested("perm-a3", "git push", "Release"),
    ])
    assert [_shape(m) for m in messages] == [
        ("card", "perm-a", "approved", 1),
        ("card", "perm-a2", "cancelled", 1),
        ("card", "perm-a3", "pending", 1),
    ]


def test_one_card_per_request_id_and_ordinary_prompts_add_none(monkeypatch):
    messages = _fold(monkeypatch, [
        _requested("perm-a", "ls", "Job"),
        _answered("perm-a", "defer"),
        _requested("perm-a", "ls", "Job"),  # a deferred request re-asked under its id
        ("interaction.requested", {"interaction_id": "i-x", "request_id": "perm-x", "kind": "permission",
                                   "request": {"request_id": "perm-x", "tool_name": "Bash"}}),
        ("interaction.requested", {"interaction_id": "i-q", "request_id": "q-1", "kind": "question",
                                   "request": {"assistant_confirm": {"question": "?"}}}),
    ])
    assert [_shape(m) for m in messages] == [("card", "perm-a", "pending", 1)]


# A provider safety check (docs/internal/guardrail-contract.md) asked in words
# in an owner chat: the card keeps the safety-check fields so the chat can show
# the check instead of "use ModelGuardrail with {…}". Mirrors
# ``approvalGuardrail`` in frontend/src/lib/utils/assistant-confirm.ts.
_GUARDRAIL_INPUT = {
    "guardrail": True, "provider": "anthropic", "provider_label": "Anthropic",
    "category": "cyber", "explanation": "Flagged by  the\ncyber classifier.",
    "requested_model": "claude-opus-4-8", "suggested_model": "claude-opus-4-7",
    "suggested_by": "provider", "request_id": "req_1", "partial_output_discarded": True,
    "message": "ignored",
}


def test_a_safety_check_card_keeps_its_guardrail_fields(monkeypatch):
    messages = _fold(monkeypatch, [
        ("interaction.requested", {
            "interaction_id": "i-g", "request_id": "guardrail-t1", "kind": "permission",
            "request": {"request_id": "guardrail-t1", "tool_name": "ModelGuardrail",
                        "tool_input": _GUARDRAIL_INPUT,
                        "assistant_confirm": {"question": "Continue?", "action": "continue"}}}),
        _answered("guardrail-t1", "allow"),
    ])
    assert [_shape(m) for m in messages] == [("card", "guardrail-t1", "approved", 1)]
    assert messages[0]["systemData"]["guardrail"] == {
        "provider": "anthropic", "provider_label": "Anthropic", "category": "cyber",
        "explanation": "Flagged by the cyber classifier.",
        "requested_model": "claude-opus-4-8", "suggested_model": "claude-opus-4-7",
        "suggested_by": "provider", "partial_output_discarded": True,
    }


def test_an_ordinary_card_has_no_guardrail_fields(monkeypatch):
    messages = _fold(monkeypatch, [_requested("perm-a", "ls", "Job")])
    assert "guardrail" not in messages[0]["systemData"]
