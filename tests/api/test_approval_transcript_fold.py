"""#483/#484: the REST transcript fold shows each approval asked in words once,
as a card keyed by request id at the point it was raised, settled in place by
its answer or expiry, with the outcome row after the owner's answer.

It mirrors the frontend reducer (``frontend/src/lib/stores/session/v2``), so a
reload and every client read the same transcript.
"""

import pytest

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
    rows = [{"id": f"e{seq}", "session_id": "home", "seq": seq, "run_id": None,
             "turn_id": payload.get("turn_id"), "command_id": None, "event_type": kind, "payload": payload, "created_at": None}
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


def _assistant_shape(messages):
    return [(m["type"], m.get("content") or m.get("toolName"))
            for m in messages if m["role"] == "assistant" and m["type"] in ("text", "thinking", "tool_use")]


def _blocked_attempt():
    return [
        ("turn.started", {}),
        _user("go"),
        ("assistant.text_delta", {"text": "Before the tools."}),
        ("tool.use", {"tool_use_id": "done", "name": "Read", "input": {"file_path": "a"}}),
        ("tool.result", {"tool_use_id": "done", "content_preview": "ok"}),
        # The call a safety check declines: thinking, text, a tool call.
        ("assistant.thinking_delta", {"text": "hmm"}),
        ("assistant.text_delta", {"text": "Partial answer"}),
        ("tool.use", {"tool_use_id": "cut", "name": "Bash", "input": {"command": "ls"}}),
        ("system.note", {"subtype": "attempt_discarded", "data": {"reason": "guardrail"}}),
    ]


def test_a_reload_drops_a_declined_attempt_on_stop(monkeypatch):
    messages = _fold(monkeypatch, [*_blocked_attempt(), ("turn.completed", {"stop_reason": "refusal"})])
    assert _assistant_shape(messages) == [("text", "Before the tools."), ("tool_use", "Read")]
    assert not any(m.get("systemSubtype") == "attempt_discarded" for m in messages)


def test_a_reload_after_continue_shows_only_the_replacement(monkeypatch):
    messages = _fold(monkeypatch, [*_blocked_attempt(), ("assistant.text_delta", {"text": "Full answer"})])
    assert _assistant_shape(messages) == [
        ("text", "Before the tools."), ("tool_use", "Read"), ("text", "Full answer"),
    ]


# A voice exchange or a brief is written beside the agent's model call: it neither
# ends that call nor belongs to it. reducer.test.ts holds the live side of these.
def _aside(turn_id):
    return [
        ("turn.started", {"turn_id": turn_id}),
        ("message.user", {"turn_id": turn_id, "content": "Aside question", "source": "voice"}),
        ("message.assistant", {"turn_id": turn_id, "content": "Aside reply"}),
        ("turn.completed", {"turn_id": turn_id, "stop_reason": "end_turn"}),
    ]


def _start():
    return [("turn.started", {"turn_id": "t1"}), ("message.user", {"turn_id": "t1", "content": "go"})]


def _chunk(text):
    return "assistant.text_delta", {"turn_id": "t1", "text": text}


_DISCARD = ("system.note", {"subtype": "attempt_discarded", "data": {"reason": "guardrail"}})


@pytest.mark.parametrize("turn_id", ["voice-1", "brief-1"])
@pytest.mark.parametrize("where", ["before", "between", "after"])
def test_a_discard_keeps_a_transcript_only_turn_and_drops_every_blocked_chunk(monkeypatch, turn_id, where):
    aside = _aside(turn_id)
    events = {
        "before": [*_start(), *aside, _chunk("Blocked one"), _chunk(" Blocked two")],
        "between": [*_start(), _chunk("Blocked one"), *aside, _chunk(" Blocked two")],
        "after": [*_start(), _chunk("Blocked one"), _chunk(" Blocked two"), *aside],
    }[where]
    stopped = _fold(monkeypatch, [*events, _DISCARD, ("turn.completed", {"turn_id": "t1", "stop_reason": "refusal"})])
    assert _assistant_shape(stopped) == [("text", "Aside reply")]
    assert [m["content"] for m in stopped if m["role"] == "user"] == ["go", "Aside question"]
    continued = _fold(monkeypatch, [*events, _DISCARD, _chunk("Full answer")])
    assert _assistant_shape(continued) == [("text", "Aside reply"), ("text", "Full answer")]


def test_a_discard_keeps_a_voice_research_card(monkeypatch):
    voice = "voice-tool-1"
    messages = _fold(monkeypatch, [
        *_start(), _chunk("Blocked one"),
        ("turn.started", {"turn_id": voice}),
        ("tool.use", {"turn_id": voice, "tool_use_id": "said", "name": "web_search", "input": {"query": "q"}}),
        ("tool.result", {"turn_id": voice, "tool_use_id": "said", "content_preview": "found"}),
        ("turn.completed", {"turn_id": voice, "stop_reason": "tool_result"}),
        _chunk("Blocked two"), _DISCARD,
    ])
    assert _assistant_shape(messages) == [("tool_use", "web_search")]
    assert messages[-1]["toolStatus"] == "complete"


def test_a_transcript_only_turn_does_not_protect_the_output_before_it(monkeypatch):
    """The boundary stays at the agent's tool result, not the voice turn after it."""
    messages = _fold(monkeypatch, [
        *_start(), _chunk("Kept."),
        ("tool.use", {"turn_id": "t1", "tool_use_id": "done", "name": "Read", "input": {}}),
        ("tool.result", {"turn_id": "t1", "tool_use_id": "done", "content_preview": "ok"}),
        _chunk("Blocked"), *_aside("voice-1"), _DISCARD,
    ])
    assert _assistant_shape(messages) == [("text", "Kept."), ("tool_use", "Read"), ("text", "Aside reply")]


def test_a_safety_check_marked_by_an_earlier_build_gets_no_card_in_words(monkeypatch):
    def marked(request_id, tool_name, tool_input):
        return "interaction.requested", {
            "kind": "permission", "request_id": request_id,
            "request": {"tool_name": tool_name, "tool_input": tool_input,
                        "assistant_confirm": {"question": "May I? Yes or no."}},
        }
    messages = _fold(monkeypatch, [
        _user("go"),
        marked("g1", "ModelGuardrail", {}),
        marked("g2", "Other", {"guardrail": True}),
        marked("b1", "Bash", {"command": "ls"}),
    ])
    assert [m["id"] for m in messages if m["role"] != "user"] == ["assistant-confirm-b1"]

