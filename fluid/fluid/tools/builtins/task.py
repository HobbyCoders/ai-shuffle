"""Task — dispatch work to a subagent (agents-program rulings A6/A8/A13).

Fluid owns the tool's SHAPE (bare name, schema, description travel with the
harness, Claude Code-parity); the actual dispatch machinery — agent registry,
session creation, approval routing, nesting/budget enforcement — is host-app
territory. The embedding application injects an async dispatcher as
``ctx.extra["task_dispatch"]``; without one the tool reports itself
unavailable instead of pretending to work. Fluid's own CLI is a host too:
:class:`fluid.cli.host.CliHost` injects a dispatcher that runs the subagent as
a child NativeLoop, so ``fluid chat`` / ``fluid run`` get working Task out of
the box.

The dispatcher receives ``(input, ctx)`` and must return a ``ToolOutcome`` (or
a plain string, which is wrapped). Contract for ``input``:
  * agent   — target subagent id or unambiguous name (required)
  * prompt  — the task for the subagent (required)
  * mode    — "sync" (default): block until the worker finishes and return its
              report; "background": atomically detach and return a
              ``BackgroundTaskReceipt`` (or ``ToolOutcome.background_task``).
              The host later submits ``BackgroundTaskFinished`` to the parent
              engine; Fluid persists the result and wakes the parked session.
              A host that writes the worker's report to a file should set
              ``BackgroundTaskFinished.report_path``: the wake turn then
              carries the report head plus that path, which is the pointer
              contract the synchronous result already has. Without it a summary
              the host elides has no recovery route at all — HistoryExpand
              cannot return text that was never in the conversation.
  * label   — optional short display label

The host may replace this fallback spec per session with an eligible subagent
roster. Provider and model selection remain host-owned. A host may expose a
validated model override in its replacement schema; this fallback does not.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Any

from fluid.tools.fragments import DONT_PREDICT_PENDING, END_TURN_ON_BACKGROUND, SUBAGENT_WORKTREE_GUIDANCE, SWARM_ROUTING
from fluid.tools.registry import (
    BackgroundTaskReceipt,
    ToolCtx,
    ToolOutcome,
    ToolSpec,
)

def describe(tools: frozenset[str] | None = None) -> str:
    """Task's description, specialized to the session's tool surface."""
    has = (lambda name: tools is None or name in tools)
    monitor_bullet = (
        "- A long-running shell command — that is Monitor's job, not a "
        "subagent's.\n"
        if has("Monitor")
        else ""
    )
    return (
        "Delegate a self-contained task to a configured subagent. The subagent "
        "runs in an isolated session with its own configuration, memory, and "
        "tools — it does not see this conversation and cannot ask you a "
        "follow-up question, so the prompt you send is the entire brief it "
        "will ever get.\n"
        "\n" + SUBAGENT_WORKTREE_GUIDANCE + "\n\n" +
        "Available agent types are listed under '# Available agent types' in "
        "your system prompt. Use only names listed there; when no such section "
        "exists, do not call Task.\n"
        "\n"
        "## When to Use This Tool\n"
        "\n"
        "- Broad searches — \"where is X handled\", \"which files reference "
        "Y\" — where the hunt costs many tool calls but the answer is a "
        "paragraph\n"
        "- Multi-file investigations whose intermediate reading you do not "
        "need to keep\n"
        + (f"- {SWARM_ROUTING}\n" if has("Swarm") else
           "- Independent work that can run in parallel: send the Task calls "
           "together rather than waiting between independent assignments\n") +
        "- Context-heavy work you want kept out of this conversation\n"
        "\n"
        "## When NOT to Use This Tool\n"
        "\n"
        "- A single file read, a single edit, or a lookup whose location you "
        "already know — you are better off just doing it directly\n"
        "- Work that turns on context the subagent cannot see and you cannot "
        "economically write down\n"
        + monitor_bullet +
        "- Anything you have already started yourself. Never duplicate "
        "delegated work by also doing it.\n"
        "\n"
        "## Fields\n"
        "\n"
        "- **agent**: the subagent id or unambiguous name, from the roster in "
        "your system prompt\n"
        "- **prompt**: the complete brief — objective, relevant paths and "
        "context, constraints, and the report you expect back\n"
        "- **mode**: `sync` (default) blocks and returns the subagent's "
        "report; `background` returns the worker session id at once and the "
        "completion is pushed to you in a later turn\n"
        "- **label**: short display label for the progress UI\n"
        "\n"
        "## Tips\n"
        "\n"
        "- In background mode, keep doing other useful work instead of "
        f"polling. {DONT_PREDICT_PENDING} {END_TURN_ON_BACKGROUND}\n"
        "- A subagent's report is a SELF-REPORT, not a verified fact. For any "
        "external side effect it claims (a file written, a push, an upload), "
        "verify the handle it returns — stat the path, fetch the URL, read the "
        "content — before telling the user the operation succeeded.\n"
        "- Only the report comes back; the subagent's intermediate work never "
        "enters your context. Ask for the specific findings you need rather "
        "than \"investigate X\"."
    )


SPEC = ToolSpec(
    name="Task",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "Target subagent id (or unambiguous name).",
            },
            "prompt": {
                "type": "string",
                "description": "The task for the subagent. Self-contained — "
                "the subagent does not see this conversation.",
            },
            "mode": {
                "type": "string",
                "enum": ["sync", "background"],
                "description": (
                    "sync (default) waits; background returns at once. The "
                    "host may run a target in background regardless of what "
                    "you ask — trust the receipt you get back, not the mode "
                    "you sent."
                ),
            },
            "label": {
                "type": "string",
                "description": "Short display label for progress UI.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Name the subagent is listed under in ListAgents and "
                    "addressed by in SendMessage. Derived from label/prompt "
                    "when omitted (a short slug); the task id in the receipt "
                    "addresses it too."
                ),
            },
        },
        "required": ["agent", "prompt"],
    },
    risk="write",
    # Host-injected dispatcher (see module docstring). Without it the engine
    # drops Task from the request schemas — an undeliverable delegation tool
    # must be invisible, not advertised.
    requires_extra=("task_dispatch",),
)


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_WORDS = 4
_SLUG_MAX = 40


def agent_name_for(input: dict[str, Any]) -> str:
    """A short, addressable name for the agent a Task starts.

    Naming an agent after its TYPE would make two same-type agents
    indistinguishable to SendMessage. The name is the explicit
    ``name`` when given, else the first few words of the label or prompt as
    a slug (``fix-flaky-login-test``). The host appends the task id's short
    ref when two agents collide, so the slug does not have to be unique.
    """
    explicit = str(input.get("name") or "").strip()
    if explicit:
        return explicit[:_SLUG_MAX]
    source = str(input.get("label") or "").strip() or str(input.get("prompt") or "").strip()
    first_line = source.splitlines()[0] if source else ""
    words = [w for w in _SLUG_RE.split(first_line.lower()) if w][:_SLUG_WORDS]
    slug = "-".join(words)[:_SLUG_MAX].strip("-")
    return slug or str(input.get("agent") or "subagent")


def _spawn_receipt_text(receipt: BackgroundTaskReceipt, name: str) -> str:
    return (
        f"Background task {receipt.task_id} started as agent '{name}'. "
        f"Address it with SendMessage(to='{name}') or by its task id; "
        "its report arrives in a later turn."
    )


def _background_outcome(result: Any, input: dict[str, Any]) -> ToolOutcome:
    name = agent_name_for(input)
    if isinstance(result, ToolOutcome):
        return result
    if isinstance(result, BackgroundTaskReceipt):
        return ToolOutcome(
            text=_spawn_receipt_text(result, name),
            background_task=result,
        )
    if isinstance(result, dict) and result.get("task_id"):
        receipt = BackgroundTaskReceipt(
            task_id=str(result["task_id"]),
            worker_session_id=(
                str(result["worker_session_id"])
                if result.get("worker_session_id") is not None
                else None
            ),
            description=result.get("description") or input.get("label"),
        )
        return ToolOutcome(
            text=str(
                result.get("text")
                or result.get("message")
                or _spawn_receipt_text(receipt, name)
            ),
            ui=result.get("ui", result),
            is_error=bool(result.get("is_error", False)),
            background_task=receipt,
        )
    return ToolOutcome(text=str(result))


async def _dispatch_and_register(
    dispatch: Any, input: dict[str, Any], ctx: ToolCtx
) -> ToolOutcome:
    """Dispatch, then register whatever the HOST actually detached.

    The decision is read off the returned receipt, never off the requested
    ``mode``: a host may detach a sync request (ai-shuffle backgrounds its
    read-only agent classes), and that worker must still be registered via
    ``background_task_started`` so the turn parks on it instead of ending.
    """
    # The host receives the derived name so its roster and its SendMessage
    # resolution can address the agent by what it is doing.
    input = {**input, "name": agent_name_for(input)}
    outcome = _background_outcome(await dispatch(input, ctx), input)
    receipt = outcome.background_task
    if receipt is None or outcome.is_error:
        return outcome
    started = (ctx.extra or {}).get("background_task_started")
    if started is not None:
        notified = started(receipt)
        if inspect.isawaitable(notified):
            await notified
    return outcome


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    dispatch = (ctx.extra or {}).get("task_dispatch")
    if dispatch is None:
        return ToolOutcome(
            text=(
                "Task dispatch is not available in this environment — no "
                "dispatcher was injected by the host application."
            ),
            is_error=True,
        )
    if input.get("mode", "sync") == "background":
        # The spawn acknowledgement is an atomic detach boundary. Interrupting
        # the parent turn may stop consuming this tool result, but must not cancel
        # a worker the host has already started or lose its durable task id.
        spawn = asyncio.create_task(_dispatch_and_register(dispatch, input, ctx))
        tracked = (ctx.extra or {}).get("background_spawn_tasks")
        if isinstance(tracked, set):
            tracked.add(spawn)
            spawn.add_done_callback(tracked.discard)
        return await asyncio.shield(spawn)
    # A sync REQUEST can still come back detached — the host decides. Same
    # registration path either way; a plain report simply carries no receipt.
    return await _dispatch_and_register(dispatch, input, ctx)
