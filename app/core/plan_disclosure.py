"""Show the plan when the HOST approves leaving plan mode, not a person.

Under ``bypassPermissions`` a ``StopPlanning`` gate is approved on policy, in
about seven milliseconds, with no panel and no one to read it. The plan
markdown only ever existed inside fluid's ``QuestionRequest`` event — the app's
mapper isolates it as ``question_input["plan"]`` precisely so the Plan Approval
panel can render it (``harness_adapter`` :2039-2049) — and on the bypass path
that panel never opens. The audit found the predictable consequence: a session
whose whole first round was a bet on one design decision, and a user who never
saw the decision because the model's follow-up paragraph left it out. fluid's
``Plan`` tool result does ask the model to write the plan out afterwards; that
is an aside inside a longer result and it was ignored.

So the host says it instead. When policy closes the gate this queues a
``system`` message carrying the plan verbatim plus the provenance — approved by
policy, no user review recorded — onto the adapter's plan-mode note queue, the
same queue ``PlanModeControl`` uses for "Switched to plan mode." It reaches the
live stream on the next engine event and is persisted with the turn's other
system messages, so it survives a reload.

That message is also the app's durable answer to the gate. Fluid emits the
``QuestionRequest`` before the host is consulted and persists it in its own
event log, so the app cannot suppress it from this side; what the app CAN do is
make sure the same transcript records how the question was settled, in a shape
no client renders as a modal (an ordinary system entry — the app's own
question/permission surfaces are driven by ``pending_interactions`` rows, and
the bypass path deliberately creates none).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: ``system`` subtype for the disclosure. Not in ``HIDDEN_SYSTEM_SUBTYPES`` and
#: not one of the frontend's special cases, so it renders through the generic
#: system-card path with ``data["content"]`` as its body.
PLAN_DISCLOSURE_SUBTYPE = "plan_auto_approved"

#: Trailing question fluid appends to the plan inside the ``QuestionRequest``
#: message. Mirrors ``harness_adapter``'s own suffix strip so the fallback path
#: (a ``question`` with no isolated ``plan``) is not left carrying it.
_APPROVAL_SUFFIX = (
    "\n\nApprove this plan and leave plan mode? "
    "(1 = Approve, anything else = feedback)"
)

_PROVENANCE = (
    "Plan mode ended by host policy (bypassPermissions). No approval was "
    "collected from you — this is the plan the model wrote, recorded here so "
    "it is not lost."
)


def plan_markdown(tool_input: Optional[Dict[str, Any]]) -> str:
    """The plan text out of a ``StopPlanning`` tool input, or ``""``.

    ``plan`` is what the adapter isolates for the approval panel. ``question``
    is the raw ``QuestionRequest`` message, kept as a fallback for inputs that
    predate that isolation; its trailing approval prompt is stripped so the
    transcript never shows a question nobody was asked.
    """
    if not isinstance(tool_input, dict):
        return ""
    text = tool_input.get("plan")
    if not isinstance(text, str) or not text.strip():
        text = tool_input.get("question")
    if not isinstance(text, str):
        return ""
    if text.endswith(_APPROVAL_SUFFIX):
        text = text[: -len(_APPROVAL_SUFFIX)]
    return text.strip()


def build_disclosure(plan: str) -> Dict[str, Any]:
    """The ``system`` message payload for one auto-approved plan."""
    body = f"{_PROVENANCE}\n\n{plan}" if plan else _PROVENANCE
    return {
        "subtype": PLAN_DISCLOSURE_SUBTYPE,
        "data": {
            "content": body,
            "plan": plan,
            # Who settled the gate. A replay consumer that pairs this with the
            # engine's QuestionRequest reads "answered by policy" rather than a
            # prompt still waiting for someone.
            "answeredBy": "host_policy",
            "approved": True,
            "userReviewed": False,
        },
    }


def disclose_auto_approved_plan(
    session_id: str, tool_input: Optional[Dict[str, Any]], adapter: Any
) -> bool:
    """Queue the disclosure onto ``adapter``'s plan-mode note queue.

    Returns True when a message was queued. Never raises: a missing transcript
    note must not turn into a failed plan exit. Answers False for an adapter
    with no such queue (the SDK leg, which reaches plan approval through
    ``ExitPlanMode`` and its own panel).
    """
    queue = getattr(adapter, "_pending_plan_mode_events", None)
    if not isinstance(queue, list):
        return False
    try:
        from app.core.provider.events import SystemMessage

        payload = build_disclosure(plan_markdown(tool_input))
        queue.append(
            SystemMessage(subtype=payload["subtype"], data=payload["data"])
        )
    except Exception:  # noqa: BLE001 — never fail the plan exit over a note.
        logger.warning(
            "[plan_disclosure] could not queue the plan disclosure for %s",
            session_id, exc_info=True,
        )
        return False
    logger.info(
        "[plan_disclosure] recorded the auto-approved plan for %s "
        "(%d chars) — the approval panel never opened on this path",
        session_id, len(payload["data"].get("plan") or ""),
    )
    return True
