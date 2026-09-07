"""Builtin tools, registered with bare names (Read/Edit/Bash/Grep/Glob/Write/Monitor)."""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch

from fluid.tools.builtins import (
    ask_user,
    bash,
    edit,
    glob,
    grep,
    history_expand,
    history_search,
    ledger,
    list_agents,
    monitor,
    note,
    plan,
    read,
    recall,
    remember,
    send_message,
    session_search,
    session_view,
    skill,
    task,
    swarm,
    tool_search,
    web,
    write,
)
from fluid.tools.registry import ToolRegistry

_MODULES = (
    read, grep, glob, write, edit, bash, skill, note, task, monitor,
    tool_search, history_expand, history_search, ledger,
    session_search, session_view, ask_user, web, plan, remember, recall,
    list_agents, send_message, swarm,
)

# Tools whose descriptions specialize to the session's permitted surface —
# cross-tool routing clauses ("Prefer Read over cat", "use Monitor") are
# emitted only when the named tool exists, mirroring the base prompt's gated
# bullets (a description naming an absent tool is hallucinated-call bait).
_DESCRIBERS = {
    "Read": read.describe,
    "Grep": grep.describe,
    "Glob": glob.describe,
    "Write": write.describe,
    "Edit": edit.describe,
    "Bash": bash.describe,
    "Monitor": monitor.describe,
    "Task": task.describe,
    "AskUser": ask_user.describe,
    "HistorySearch": history_search.describe,
    "SessionSearch": session_search.describe,
    "SessionView": session_view.describe,
    "WebFetch": web.describe_fetch,
    "WebSearch": web.describe_search,
    "Remember": remember.describe,
    "Recall": recall.describe,
    "ListAgents": list_agents.describe,
    "SendMessage": send_message.describe,
}


def register_builtins(registry: ToolRegistry) -> None:
    """Register all builtin tools on ``registry``."""
    for mod in _MODULES:
        pairs = (
            mod.SPECS if hasattr(mod, "SPECS") else ((mod.SPEC, mod.handler),)
        )
        for spec, handler in pairs:
            registry.register_builtin(spec, handler)


def effective_surface(
    names: Iterable[str],
    allowed: Iterable[str] | None,
    disallowed: Iterable[str] | None = (),
) -> frozenset[str]:
    """The names surviving the session's allow/deny fnmatch patterns (CE12)."""
    out = set(names)
    if allowed is not None:
        patterns = list(allowed)
        out = {n for n in out if any(fnmatch(n, p) for p in patterns)}
    deny = list(disallowed or ())
    out -= {n for n in out if any(fnmatch(n, p) for p in deny)}
    return frozenset(out)


def refine_builtin_descriptions(
    registry: ToolRegistry,
    *,
    allowed: Iterable[str] | None = None,
    disallowed: Iterable[str] | None = (),
    monitor_budget_seconds: int | None = None,
) -> None:
    """Specialize registered builtin descriptions to this session.

    Run once at session build, after :func:`register_builtins` and after the
    tool restriction is known: cross-tool clauses collapse to the permitted
    surface. Idempotent — amending with the full surface reproduces the
    module-constant text.

    ``monitor_budget_seconds`` is the wall-clock budget the host enforces on a
    monitored command. Only the host knows it, and Monitor's prose about
    bounding a command is guesswork without it — pass the same number the host
    puts on ``ctx.extra["monitor_budget_seconds"]``.
    """
    builtin_names = {
        s.name
        for s in registry.list_tools(loaded_only=False)
        if s.source == "builtin"
    }
    surface = effective_surface(builtin_names, allowed, disallowed)
    for name, describe in _DESCRIBERS.items():
        if name not in builtin_names:
            continue
        if name == "Monitor":
            text = describe(surface, budget_seconds=monitor_budget_seconds)
        else:
            text = describe(surface)
        registry.amend_spec(name, description=text)


__all__ = [
    "effective_surface",
    "refine_builtin_descriptions",
    "register_builtins",
]
