"""Tool-access resolution — ``tool_selection_mode`` is the runtime authority.

The runtime does not infer intent from list emptiness: that made "Disable All
Tools" (``allowed_tools: []``) read as *unrestricted* on every path that wrote
``config.get("allowed_tools") or []``. The mode says what it means:

===========  ==========================  =====================
mode         allowed (SURFACE)           disallowed
===========  ==========================  =====================
``all``      None                        None
``none``     None                        ``["*"]``
``allow``    the saved list              None
``disallow`` None                        the saved list
===========  ==========================  =====================

``allow`` with an empty list is treated as ``none`` — "allow only these" with
nothing selected is a request for no tools, never for all of them. A missing or
unrecognized mode falls back to the legacy inference the old UI used, so a
profile saved before the v82 migration still resolves the way its author saw it
in the editor (the migration stamps every row, so this branch is for configs
that never went through the DB, e.g. import payloads and tests).

Entries on either list may be exact tool names or fnmatch patterns; the picker
saves ``mcp__<server>__*`` when a whole MCP group is selected. Both fluid legs
match patterns the same way (``NativeLoop(allowed_tools=/disallowed_tools=)``),
and ``tool_permitted`` below is the app-side equivalent used by the permission
callback and by the delegated-leg subtraction.

Kept deliberately free of app imports (no db, no fluid) so the v82 migration in
``app.db.database`` and the API layer can both use it without an import cycle.
``app.core.query_options`` re-exports ``resolve_tool_access`` and ``ToolAccess``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

# The four modes the editor offers. Anything else is treated as absent.
TOOL_SELECTION_MODES = ("all", "none", "allow", "disallow")

# The deny-everything pattern. fluid matches it with fnmatch on both engines;
# the Claude Code CLI matches it too (its deny rules are glob-matched — see
# ``harness_adapter._delegated_disallowed``).
DENY_ALL = "*"


# Back-compat aliases for tool names that were renamed. Applied at config-read
# time only (never migrated in the DB) so saved profiles keep working. The
# builtin ``PushNotification`` leaf tool was renamed to ``NotifyDevices`` to
# resolve a collision with a bare stub of the same name.
LEGACY_TOOL_ALIASES: Dict[str, str] = {
    "PushNotification": "mcp__builtins__NotifyDevices",
    "mcp__builtins__PushNotification": "mcp__builtins__NotifyDevices",
}


def apply_legacy_tool_aliases(tool_names: Iterable[Any]) -> List[str]:
    """Translate legacy tool names to their current equivalents, in order,
    without introducing duplicates. Blank/non-string entries are dropped."""
    out: List[str] = []
    for name in tool_names or ():
        text = str(name).strip()
        if not text:
            continue
        mapped = LEGACY_TOOL_ALIASES.get(text, text)
        if mapped not in out:
            out.append(mapped)
    return out


@dataclass(frozen=True)
class ToolAccess:
    """The resolved answer for one agent config.

    ``allowed`` is a SURFACE restriction (the session's entire toolset when set)
    and ``disallowed`` is a blocklist that subtracts afterwards — deny beats
    allow, on both legs. ``None`` on either side means "no restriction from this
    side".
    """

    allowed: Optional[List[str]] = None
    disallowed: Optional[List[str]] = None

    @property
    def restricted(self) -> bool:
        return bool(self.allowed) or bool(self.disallowed)


def infer_tool_selection_mode(config: Dict[str, Any]) -> str:
    """The mode an unstamped config was implicitly in, exactly as the old editor
    inferred it: ``[]`` → ``none``, a non-empty allow list → ``allow``, a
    non-empty deny list → ``disallow``, otherwise ``all``.

    Used by the v82 migration to stamp every existing agent, and as the runtime
    fallback for a config that was never stamped.
    """
    allowed = config.get("allowed_tools")
    disallowed = config.get("disallowed_tools")
    if isinstance(allowed, (list, tuple)) and len(allowed) == 0:
        return "none"
    if isinstance(allowed, (list, tuple)) and len(allowed) > 0:
        return "allow"
    if isinstance(disallowed, (list, tuple)) and len(disallowed) > 0:
        return "disallow"
    return "all"


def tool_selection_mode(config: Dict[str, Any]) -> str:
    """This config's effective mode: the stamped value when it is one of the
    four, otherwise the legacy inference."""
    raw = config.get("tool_selection_mode")
    if isinstance(raw, str) and raw in TOOL_SELECTION_MODES:
        return raw
    return infer_tool_selection_mode(config)


def resolve_tool_access(config: Optional[Dict[str, Any]]) -> ToolAccess:
    """Resolve one agent config into ``ToolAccess``.

    The single seam both legs read. Legacy aliases are applied to whichever
    list survives.
    """
    config = config or {}
    mode = tool_selection_mode(config)

    if mode == "none":
        return ToolAccess(allowed=None, disallowed=[DENY_ALL])

    if mode == "allow":
        allowed = apply_legacy_tool_aliases(config.get("allowed_tools") or [])
        if not allowed:
            # "Allow only selected" with nothing selected is a request for no
            # tools. Degrading to unrestricted here would silently grant every
            # tool.
            return ToolAccess(allowed=None, disallowed=[DENY_ALL])
        return ToolAccess(allowed=allowed, disallowed=None)

    if mode == "disallow":
        disallowed = apply_legacy_tool_aliases(config.get("disallowed_tools") or [])
        return ToolAccess(allowed=None, disallowed=disallowed or None)

    # "all" (and anything unrecognized that inferred to it)
    return ToolAccess(allowed=None, disallowed=None)


def tool_permitted(
    name: str,
    allowed: Optional[Iterable[str]] = None,
    disallowed: Optional[Iterable[str]] = None,
) -> bool:
    """Whether ``name`` survives an allow surface and/or a deny list.

    Exact match first, then fnmatch, so ``mcp__browser__*`` covers a whole
    server and a bare ``*`` covers everything. Deny wins.
    """
    if disallowed and matches_any(name, disallowed):
        return False
    if allowed:
        return matches_any(name, allowed)
    return True


def matches_any(name: str, patterns: Iterable[str]) -> bool:
    """Whether ``name`` matches any exact name or fnmatch pattern in the list."""
    for pattern in patterns or ():
        text = str(pattern)
        if text == name:
            return True
        if ("*" in text or "?" in text or "[" in text) and fnmatch.fnmatchcase(name, text):
            return True
    return False


def intersect_tool_surfaces(
    first: Optional[Iterable[str]], second: Optional[Iterable[str]]
) -> Optional[List[str]]:
    """Compose two tool SURFACES: the result is the narrower one.

    Either side may be ``None`` ("unrestricted"), in which case the other is
    returned unchanged. When both name something, an entry survives if the
    other side permits it — checked in both directions so a pattern on one side
    (``mcp__x__*``) and an exact name on the other (``mcp__x__foo``) intersect
    to the exact name rather than to nothing.

    An empty result is a real answer ("these two restrictions have no overlap")
    and is returned as ``[]``; callers must NOT treat that as unrestricted —
    ``query_options`` converts it into a ``["*"]`` deny instead.
    """
    if first is None and second is None:
        return None
    left = [str(t).strip() for t in (first or ()) if str(t).strip()]
    right = [str(t).strip() for t in (second or ()) if str(t).strip()]
    if not left:
        return right or ([] if second is not None else None)
    if not right:
        return left
    out: List[str] = []
    for entry in left:
        if matches_any(entry, right) and entry not in out:
            out.append(entry)
    for entry in right:
        if matches_any(entry, left) and entry not in out:
            out.append(entry)
    return out


def fold_legacy_tool_switches(config: Dict[str, Any]) -> Dict[str, Any]:
    """Fold the retired ``ai_tools`` / ``app_config_tools`` switches into the
    lists, and stamp ``tool_selection_mode``. Returns a NEW dict;
    the caller decides whether anything changed.

    * ``ai_tools`` was a per-category enable map. In ``all``/``disallow`` mode
      the categories the user turned OFF become disallowed tool names; in
      ``allow`` mode only the ON categories' tools may stay/appear in the allow
      list.
    * ``app_config_tools: False`` becomes a disallow of ``mcp__app-config__*``
      in ``all``/``disallow`` mode; in ``allow`` mode the group simply is not in
      the allow list, so nothing is written.

    Both source fields are LEFT IN PLACE — they stay on the model for import
    compatibility — but the runtime no longer reads them.
    """
    out = dict(config or {})
    mode = tool_selection_mode(out)
    out["tool_selection_mode"] = mode

    allowed = list(out.get("allowed_tools") or [])
    disallowed = list(out.get("disallowed_tools") or [])

    ai_tools = out.get("ai_tools")
    if isinstance(ai_tools, dict):
        from app.core.tool_catalog import ai_tool_names_for_config

        enabled_names = set(ai_tool_names_for_config(ai_tools))
        every_name = set(ai_tool_names_for_config(None, every=True))
        if mode == "allow":
            allowed = [t for t in allowed if t not in (every_name - enabled_names)]
        else:
            for name in sorted(every_name - enabled_names):
                if name not in disallowed:
                    disallowed.append(name)

    if not out.get("app_config_tools") and mode in ("all", "disallow"):
        pattern = "mcp__app-config__*"
        if pattern not in disallowed:
            disallowed.append(pattern)

    if mode == "allow":
        out["allowed_tools"] = allowed
        # An allow-mode fold that emptied the list means "nothing survives",
        # which resolve_tool_access already reads as ``none``.
    elif mode == "disallow":
        out["disallowed_tools"] = disallowed
    elif mode == "all" and disallowed:
        # Folding a switch OFF turns an "all" agent into a "disallow" one —
        # that is the only honest translation of "everything except these".
        out["tool_selection_mode"] = "disallow"
        out["disallowed_tools"] = disallowed

    return out
