"""The ``fast_outcome`` system note: how a turn really ran against Fast mode.

Built from fluid's ``FastModeOutcome``. The note's ``content`` is the sentence a
client shows; its ``outcome`` says which kind of line it is:

``fast``      every request reported running on Fast
``partial``   some requests reported Fast, others did not
``standard``  no request reported Fast, and at least one reported standard
``unknown``   no request reported a speed

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


def fast_outcome_note(data: Dict[str, Any]) -> Dict[str, Any]:
    """The ``fast_outcome`` system note's data from a ``fast_outcome`` message."""
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
        parts.append("Fast mode was turned on during this turn, so it starts with your next message.")
    elif requested and not at_end:
        parts.append("Fast mode was turned off during this turn, so it stops with your next message.")
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
