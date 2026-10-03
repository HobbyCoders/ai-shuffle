"""AskUser posts persistent questions without waiting for human answers."""

from __future__ import annotations

from typing import Any

import json

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

MAX_QUESTIONS = 4
MAX_OPTIONS = 4

def describe(tools: frozenset[str] | None = None) -> str:
    """AskUser's description, specialized to the session's tool surface."""
    return (
        "Use this tool only when you are blocked on a decision that is "
        "genuinely the user's to make: one you cannot resolve from the "
        "request, the code, or sensible defaults. Reserve it for answers that "
        "change what you do next — when a choice has an obvious conventional "
        "reversible default, take it, say so, and keep going.\n"
        "For a genuinely blocking user decision, call AskUser and wait for "
        "the answer before dependent work; do not substitute a question in "
        "prose or guess the answer. This does not grant permission for a "
        "tool call or replace the harness permission prompt.\n"
        "\n"
        "This tool posts persistent questions and immediately returns request ids. "
        "Continue independent work, or end your turn when blocked. Answers arrive "
        "automatically during work or in a new turn; never poll or wait.\n"
        "Usage notes:\n"
        f"- Ask 1–{MAX_QUESTIONS} questions per call. Batch independent "
        "questions into ONE call instead of a chain of single-question calls. "
        "All questions in the call share one form and one submitted response. "
        "When a task needs several owner decisions, gather them up front into "
        "that one call rather than stopping for each in turn.\n"
        "- Write for a non-specialist: one plain-language sentence per "
        "question (or option description) on what it changes, no internal "
        "jargon. Check a gate or term against the current code before asking "
        "about it; do not present retired features as blockers.\n"
        '- Each question is {"question": "...", "header": "Topic", '
        '"options": [{"label": "Choice A", "description": "What it means"}, '
        '{"label": "Choice B"}], "multiSelect": false}. '
        "Use selectable options whenever there are concrete choices, rather "
        "than requiring the user to type one.\n"
        "- Put each choice ONLY in the options array — never enumerate the "
        "options inside the question text. The UI renders options as "
        "selectable rows, so options written into the question are dead "
        "prose.\n"
        "- If you recommend a specific option, make that the first option in "
        "the list and add \"(recommended)\" at the end of the label.\n"
        "- Use multiSelect: true to allow multiple answers to be selected for "
        "a question.\n"
        "- Users can always answer with their own text instead of picking an "
        "option; treat a free-text answer as overriding the options.\n"
        f"- Offer 2–{MAX_OPTIONS} options, or omit options entirely for an "
        "open-ended question — one option is not a choice.\n"
        "- Do not use this for facts a tool could establish, or for permission "
        "the harness already gates."
    )


SPEC = ToolSpec(
    name="AskUser",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": f"1–{MAX_QUESTIONS} questions to ask together.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The complete question, ending with '?'.",
                        },
                        "header": {
                            "type": "string",
                            "description": "Short topic label (max ~12 chars).",
                        },
                        "options": {
                            "type": "array",
                            "description": (
                                f"2–{MAX_OPTIONS} mutually exclusive choices; "
                                "omit for open-ended."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {
                                        "type": "string",
                                        "description": "What choosing this means.",
                                    },
                                },
                                "required": ["label"],
                            },
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "description": "Allow choosing several options.",
                        },
                    },
                    "required": ["question"],
                },
            },
        },
        "required": ["questions"],
    },
    risk="safe",
)


def _render_message(q: dict[str, Any]) -> str:
    """Plain-text rendering for hosts without a structured question UI."""
    lines = [str(q.get("question", "")).strip()]
    options = q.get("options") or []
    for i, opt in enumerate(options, 1):
        label = str(opt.get("label", "")).strip()
        desc = str(opt.get("description", "")).strip()
        lines.append(f"  {i}. {label}" + (f" — {desc}" if desc else ""))
    if options:
        lines.append(
            "(answer with a number"
            + (", several comma-separated" if q.get("multiSelect") else "")
            + ", or your own text)"
        )
    return "\n".join(lines)


def _validate(questions: Any) -> str | None:
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        return f"questions must be an array of 1–{MAX_QUESTIONS} items"
    for i, q in enumerate(questions):
        if not isinstance(q, dict) or not str(q.get("question", "")).strip():
            return f"questions[{i}] needs non-empty question text"
        options = q.get("options")
        if options is not None:
            if not isinstance(options, list) or len(options) > MAX_OPTIONS:
                return f"questions[{i}]: options must be a list of at most {MAX_OPTIONS}"
            if len(options) == 1:
                return (
                    f"questions[{i}]: one option is not a choice — offer at "
                    "least two, or ask open-ended"
                )
    return None


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    questions = input.get("questions")
    error = _validate(questions)
    if error is not None:
        return ToolOutcome(text=error, is_error=True)

    # A host may answer at once (an absent owner's recommended defaults)
    # instead of posting; ``None`` means post as usual.
    auto = ctx.extra.get("auto_answer_questions")
    if auto is not None:
        answered = await auto(questions)
        if answered is not None:
            return ToolOutcome(text=answered)
    post = ctx.extra.get("post_questions")
    if post is None:
        return ToolOutcome(text="AskUser requires a persistent question host.", is_error=True)
    request_ids = await post(questions)
    return ToolOutcome(text=json.dumps({
        "status": "queued", "request_ids": request_ids,
        "message": "Questions posted. Continue independent work, or end your turn "
        "if blocked. Answers will arrive automatically; do not poll.",
    }))


def _resolve_answer(raw: str, q: dict[str, Any]) -> str:
    options = q.get("options") or []
    if not options or not raw:
        return raw
    picks: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(options):
            picks.append(str(options[int(part) - 1].get("label", part)))
        else:
            return raw  # free text (or mixed input) overrides the options
    return ", ".join(picks)
