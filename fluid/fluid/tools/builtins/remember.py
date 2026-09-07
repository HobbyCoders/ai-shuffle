"""Remember — the model's write channel into the continual learner.

The learner's memory list is written only by the background synthesizer
(``fluid.codemode.continual``), which keeps memory quality independent of the
session model's diligence. Before this tool the model had NO way to
participate: a user's "remember this" was answered with an acknowledgment and
a hope that the synthesizer would notice. Remember closes that gap with the
propose-then-curate shape: the call journals a high-salience ``remember``
entry and flags the synthesizer urgent, so the fact is distilled into
``memories.json`` on the next background pass (which the flag pulls forward
to "as soon as there is anything new"). The model proposes while the context
is hot; the curator with the global view decides what lands — a Remember
call never writes the memory file directly.

Wired through ``ctx.extra["continual_journal"]`` (a host callable set up by
the engine loader when continual learning is on); absent that injection the
engine drops the tool from the schema surface entirely.
"""

from __future__ import annotations

from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

_MAX_CHARS = 500  # one fact, not an essay — matches the memory store's cap


def describe(tools: frozenset[str] | None = None) -> str:
    """Remember's description, specialized to the session's surface."""
    has = (lambda name: tools is None or name in tools)
    note_route = (
        " Task progress and this session's working state belong in Note, "
        "not here."
        if has("Note")
        else " Task progress and this session's working state do not belong "
        "here."
    )
    return (
        "Save a durable fact to your cross-session memory — the # Memory "
        "list shown at the start of every future session. Call it the moment "
        "the user asks you to remember something, states a lasting "
        "preference, or corrects you on something worth never repeating; "
        "also for durable facts you establish yourself (a convention of this "
        "project, how this environment actually behaves). One call per fact; "
        "`text` is the fact itself in one or two plain sentences (\"user "
        "prefers X\", \"staging deploys go through Y\") — a statement about "
        "the world, not an instruction to yourself. A background learner "
        "distills it into the list shortly after (so the list itself updates "
        "next session, not mid-turn)." + note_route + " Never store secrets: "
        "record where a credential lives, not its value."
    )


SPEC = ToolSpec(
    name="Remember",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "The fact, in one or two plain sentences. State what is "
                    "true, not what to do."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["global", "project"],
                "description": (
                    "'project' when the fact is about this project's code, "
                    "layout, or conventions; 'global' (default) when it is "
                    "about the user or their machine in general."
                ),
            },
        },
        "required": ["text"],
    },
    risk="safe",
    # Loader-wired journal callable; absent (continual learning off) the
    # engine drops the tool from the request schemas.
    requires_extra=("continual_journal",),
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    text = " ".join(str(input.get("text") or "").split())
    if not text:
        return ToolOutcome(text="text is required — the fact to remember", is_error=True)
    if len(text) > _MAX_CHARS:
        return ToolOutcome(
            text=(
                f"that is {len(text)} chars; a memory is one or two sentences "
                f"(max {_MAX_CHARS}). Save the durable core of it, not the "
                "whole story."
            ),
            is_error=True,
        )
    from fluid.context.screening import looks_like_secret

    secret_kind = looks_like_secret(text)
    if secret_kind is not None:
        return ToolOutcome(
            text=(
                f"refused — this looks like it contains a credential "
                f"({secret_kind}). Remember where the credential lives, "
                "never its value."
            ),
            is_error=True,
        )
    journal = ctx.extra.get("continual_journal")
    if not callable(journal):
        return ToolOutcome(
            text="unavailable: continual learning is not wired in this session",
            is_error=True,
        )
    scope = input.get("scope")
    scope = scope if scope in ("global", "project") else None
    try:
        journal(text, scope)
    except Exception as exc:
        return ToolOutcome(text=f"failed to record: {exc}", is_error=True)
    return ToolOutcome(
        text=(
            "Recorded. The learner distills it into # Memory on its next "
            "pass; the updated list appears from the next session onward."
        )
    )
