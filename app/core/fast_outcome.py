"""The ``fast_outcome`` system note: how a turn really ran against Fast mode.

Built from fluid's ``FastModeOutcome``. The note's ``content`` is the sentence a
client shows; its ``outcome`` says which kind of line it is:

``fast``      every request reported running on Fast
``partial``   some requests reported Fast, others did not
``standard``  no request reported Fast, and at least one reported standard
``unknown``   no request reported a speed

A turn that asked for Ultrafast (the OpenAI Responses lane: ``speed`` or
``speed_at_end`` of ``ultrafast``, any ``ultrafast_calls``, or the
``ultrafast_unavailable`` refusal) reads its own counts instead:

``ultrafast``  every request reported Ultrafast
``partial``    some requests reported Ultrafast, others did not
``standard``   no request reported Ultrafast (for instance the provider
               refused it: ``disabled_reason`` ``ultrafast_unavailable``)
``unknown``    no request reported a speed

The reason wording follows what Claude Code itself says for each value of
``fast_mode_disabled_reason``. A value this app has never seen is shown verbatim.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ["fast_outcome_note"]

_REASONS = {
    "free": "Fast mode needs a paid plan or purchased credits.",
    "preference": "Fast mode has been turned off by your organization.",
    "extra_usage_disabled": "Fast mode needs usage credits, and they aren't available on this account.",
    "network_error": "Claude Code couldn't confirm Fast mode is available because of a network problem.",
    "unknown": "Claude Code says Fast mode is unavailable right now.",
    "not_first_party": "Fast mode only works when Claude Code uses the Anthropic API directly.",
    "disabled_by_env": "Fast mode is turned off in this environment.",
    "model_not_allowed": "The model Fast mode needs isn't in your organization's allowed models.",
    "sdk_opt_in_required": "Claude Code says Fast mode hasn't been enabled for this session.",
    "pending": "Claude Code was still checking whether Fast mode is available.",
}
_COOLDOWN = "Fast mode is paused for now after a rate limit."
_ULTRAFAST_UNAVAILABLE = "Ultrafast isn't available for this account/model; ran on Standard."
_SPEEDS = ("standard", "fast", "ultrafast")


def _count(data: Dict[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _requests(count: int) -> str:
    return f"{count} request" if count == 1 else f"{count} requests"


def _ran(fast: int, standard: int, unknown: int) -> str:
    """What the requests reported. A request with no speed is never counted as either."""
    total = fast + standard + unknown
    silent = f"{unknown} didn't report a speed"
    if fast and not standard and not unknown:
        return "Ran on Fast."
    if fast:
        rest = []
        if standard:
            rest.append(f"{'the other ' if not unknown else ''}{standard} ran at standard speed")
        if unknown:
            rest.append(silent)
        return f"Ran on Fast for {fast} of {_requests(total)}; {' and '.join(rest)}."
    if standard and not unknown:
        return "Ran at standard speed."
    if standard:
        return f"Ran at standard speed for {standard} of {_requests(total)}; {silent}."
    return "Claude Code didn't report what speed this turn ran at."


def _reason_sentence(reason: Optional[str], state: Optional[str]) -> Optional[str]:
    if state == "cooldown":
        return _COOLDOWN
    if not reason:
        return None
    return _REASONS.get(reason) or f"Claude Code's reason: {reason}."


def _speed(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in _SPEEDS else None


def _ran_ultrafast(ultrafast: int, fast: int, standard: int, unknown: int) -> str:
    """What an Ultrafast turn's requests reported, Fast counted as its own tier."""
    total = ultrafast + fast + standard + unknown
    if ultrafast and ultrafast == total:
        return "Ran on Ultrafast."
    rest = []
    if fast:
        rest.append(f"{fast} ran on Fast")
    if standard:
        rest.append(f"{standard} ran at standard speed")
    if unknown:
        rest.append(f"{unknown} didn't report a speed")
    if ultrafast:
        return f"Ran on Ultrafast for {ultrafast} of {_requests(total)}; {' and '.join(rest)}."
    if fast and not standard and not unknown:
        return "Ran on Fast."
    if standard and not fast and not unknown:
        return "Ran at standard speed."
    if not total:
        return "The provider didn't report what speed this turn ran at."
    return f"Of {_requests(total)}, {' and '.join(rest)}."


def _ultrafast_note(data: Dict[str, Any], start: Optional[str], end: Optional[str]) -> Dict[str, Any]:
    ultrafast = _count(data, "ultrafast_calls")
    fast = _count(data, "fast_calls")
    standard = _count(data, "standard_calls")
    unknown = _count(data, "unknown_calls")
    reason = data.get("disabled_reason") if isinstance(data.get("disabled_reason"), str) else None
    refused = reason == "ultrafast_unavailable"
    if ultrafast and not (fast or standard or unknown):
        outcome = "ultrafast"
    elif ultrafast:
        outcome = "partial"
    elif fast or standard:
        outcome = "standard"
    else:
        outcome = "unknown"

    if refused and not ultrafast:
        parts = [_ULTRAFAST_UNAVAILABLE]
    else:
        parts = [_ran_ultrafast(ultrafast, fast, standard, unknown)]
        if refused:
            parts.append("Ultrafast isn't available for this account/model, so the rest ran on Standard.")
    if end == "ultrafast" and start != "ultrafast":
        parts.append("Ultrafast was turned on during this turn.")
    elif start == "ultrafast" and end != "ultrafast":
        parts.append("Ultrafast was turned off during this turn.")
    return {
        "content": " ".join(parts),
        "outcome": outcome,
        "requested": start not in (None, "standard"),
        "requested_at_end": end not in (None, "standard"),
        "fast_calls": fast,
        "standard_calls": standard,
        "unknown_calls": unknown,
        "ultrafast_calls": ultrafast,
        "speed": start,
        "speed_at_end": end,
        "state": None,
        "disabled_reason": reason,
    }


def fast_outcome_note(data: Dict[str, Any]) -> Dict[str, Any]:
    """The ``fast_outcome`` system note's data from a ``fast_outcome`` message."""
    start, end = _speed(data.get("speed")), _speed(data.get("speed_at_end"))
    # Any turn that tried Ultrafast reads the Ultrafast counts, even when it
    # neither started nor ended on it (picked midway, then refused).
    if ("ultrafast" in (start, end) or _count(data, "ultrafast_calls")
            or data.get("disabled_reason") == "ultrafast_unavailable"):
        return _ultrafast_note(data, start, end)
    fast = _count(data, "fast_calls")
    standard = _count(data, "standard_calls")
    unknown = _count(data, "unknown_calls")
    requested = data.get("requested") is True
    at_end = data.get("requested_at_end") is True
    state = data.get("state") if isinstance(data.get("state"), str) else None
    reason = data.get("disabled_reason") if isinstance(data.get("disabled_reason"), str) else None

    if fast and not standard and not unknown:
        outcome = "fast"
    elif fast:
        outcome = "partial"
    elif standard:
        outcome = "standard"
    else:
        outcome = "unknown"

    parts = [_ran(fast, standard, unknown)]
    if at_end and not requested:
        parts.append("Fast mode was turned on during this turn, so it applies from your next message.")
    elif requested and not at_end:
        parts.append("Fast mode was turned off during this turn, so it applies from your next message.")
    # Only for a turn that asked for Fast throughout. With the setting off,
    # Claude Code reports a reason on every turn, which refuses nothing; and a
    # reason on a turn that only turned it on midway says nothing about the
    # next message, which will report its own.
    why = _reason_sentence(reason, state) if requested and at_end and outcome != "fast" else None
    if why:
        parts.append(why)
    elif requested and at_end and outcome in ("partial", "standard"):
        parts.append("Claude Code gave no reason.")

    return {
        "content": " ".join(parts),
        "outcome": outcome,
        "requested": requested,
        "requested_at_end": at_end,
        "fast_calls": fast,
        "standard_calls": standard,
        "unknown_calls": unknown,
        "state": state,
        "disabled_reason": reason,
    }
