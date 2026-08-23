"""AskUser — structured questions answered by the human, mid-turn.

Rides the engine's existing input-required round-trip (the MRTR seam): the
first dispatch returns ``input_required`` with one request per question; the
engine emits ``QuestionRequest`` events, parks until ``RespondQuestion`` ops
arrive (the REPL/TUI render the options and prompt), then re-dispatches this
tool with the collected answers, which become the tool result.

Each question carries its option list in the *schema* field so a UI can render
selectable choices; a plain-text host falls back to numbered options in the
message text.
"""

from __future__ import annotations

from typing import Any

from fluid.tools.registry import InputRequired, ToolCtx, ToolOutcome, ToolSpec

MAX_QUESTIONS = 4
MAX_OPTIONS = 4

SPEC = ToolSpec(
    name="AskUser",
    description=(
        "Ask the user one to four questions and wait for their answers. Use "
        "this ONLY when blocked on a decision that is genuinely the user's to "
        "make: materially different readings of the request, an "
        "expensive-to-reverse choice, a missing credential or approval — "
        "never for facts a tool could establish, and never for permission "
        "the harness already handles.\n"
        "\n"
        "Rules:\n"
        "- Put each choice ONLY in the options array — never enumerate "
        "options inside the question text (the UI renders options as "
        "selectable rows; options written into the question are dead prose).\n"
        "- List the option you recommend FIRST and mark its label with "
        "'(recommended)'.\n"
        "- Batch independent questions into ONE call instead of a chain of "
        "single-question calls.\n"
        "- The user can always answer with their own text instead of an "
        "option; treat a free-text answer as overriding the options.\n"
        "- Omit options entirely for an open-ended question."
    ),
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

    answers = input.get("__input_responses__")
    if answers is None:
        # First leg: hand the engine one request per question and park.
        requests = {
            f"q{i}": {"message": _render_message(q), "schema": q}
            for i, q in enumerate(questions)
        }
        return ToolOutcome(
            text="",
            input_required=InputRequired(
                server="builtin", tool="AskUser", request_state=None,
                requests=requests,
            ),
        )

    # Second leg: the collected answers are the result. Numeric answers map
    # back to option labels so the model reads the choice, not an index.
    lines: list[str] = []
    for i, q in enumerate(questions):
        header = str(q.get("header") or "").strip() or f"question {i + 1}"
        raw = str(answers.get(f"q{i}", "")).strip()
        lines.append(f"- {header}: {_resolve_answer(raw, q) or '(no answer)'}")
    return ToolOutcome(text="The user answered:\n" + "\n".join(lines))


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
