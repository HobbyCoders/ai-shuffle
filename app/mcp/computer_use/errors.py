"""Coded computer-use errors.

Every refusal or failure the model sees carries a short message plus a stable
``code`` it can branch on, and ``outcome``: what happened on the desktop.

outcome
    ``rejected``  nothing was sent; safe to fix and retry.
    ``partial``   some input was sent (a batch stopped part way, typing was cut).
    ``unknown``   the call ended without knowing what took effect; observe first.

code
    STALE_OBSERVATION       the screenshot the coordinates came from is no longer current
    TARGET_NOT_FOUND        a window id is not open (call list_windows again)
    TARGET_CHANGED          a ui_tree element is gone or no longer matches its name and type
    FOCUS_BLOCKED           Windows kept another window in front
    POINTER_MISMATCH        the pointer did not stay where it was placed
    USER_INTERRUPTED        the user stopped control or is using the mouse
    TURN_INTERRUPTED        the app (not the user) interrupted the turn
    CONTROL_ENDED           the turn that held control ended
    DESKTOP_LOCKED          lock screen or UAC secure desktop owns input
    PERMISSION_REQUIRED     not granted, setting off, or an elevated window
    DESKTOP_BUSY            another action or chat owns the desktop
    UNSUPPORTED_CAPABILITY  not available on this platform
    INVALID_ARGUMENT        the arguments are wrong
    EXECUTION_OUTCOME_UNKNOWN  cancelled or failed after input may have been sent
    ACTION_FAILED           failed before any input was sent
"""

from __future__ import annotations

from typing import Any


class ActionError(ValueError):
    """A refusal or failure with a stable ``code``. A ValueError, so older
    callers that only read the message keep working."""

    def __init__(self, code: str, message: str, outcome: str = "rejected", **extra: Any):
        super().__init__(message)
        self.code, self.outcome, self.extra = code, outcome, extra

    def payload(self) -> dict:
        return {"error": str(self), "code": self.code, "outcome": self.outcome, **self.extra}


def stale(message: str) -> ActionError:
    return ActionError("STALE_OBSERVATION", message)
