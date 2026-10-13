"""Saved permission rules for calls no person is asked about.

The watched approver (``query_engine``'s ``can_use_tool`` callback) applies a
session's saved rules before its permission mode: a remembered Deny beats
Accept Edits and Auto, a remembered Allow skips the prompt. Two paths decide
tool calls without that approver and use this lookup to keep the same order:

* ``HarnessAdapter._answer_without_approver`` / ``_allow_without_approver``:
  a prompt raised on a path with no approver (REST, background, automation).
* the delegated leg's PreToolUse policy: calls the claude CLI approves on its
  own (in-cwd edits under Accept Edits, classifier approvals under Auto) never
  raise a prompt, so a saved Deny is enforced there. That covers the CLI's
  built-in tools only: ``DelegatedLoop._cli_pre_tool_use`` skips the
  bridge-served ``mcp__fluid_bundled__*`` app tools. The bridge runs fluid's
  hooks on those, but no permission engine (the adapter builds DelegatedLoop
  without one), so a saved Deny on a bridged app tool is not enforced when
  the CLI runs it without a prompt.

Session rules are keyed by the app session id, the agent's rules by its
profile id; the lookup is ``permission_handler.saved_rule``.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

SavedDecision = Literal["allow", "deny"]

#: The denial text for a call a saved Deny rule names, on every path.
SAVED_RULE_DENIAL = "Denied by saved rule"


def saved_rule_decision(
    app_session_id: Optional[str],
    profile_id: Optional[str],
    tool_name: str,
    tool_input: Optional[Dict[str, Any]],
) -> Optional[SavedDecision]:
    """``"allow"`` or ``"deny"`` when a saved rule decides this call, else None.

    Blocking: the agent's rules are a DB read, so async callers run this in a
    thread. Errors propagate; each caller decides what a failed lookup means
    (the adapter denies, because the CLI blocks until a call is answered).
    A Defer rule decides nothing here and returns None.
    """
    from app.core.permission_handler import PermissionDecision, permission_handler

    rule = permission_handler.saved_rule(
        app_session_id, profile_id, tool_name, dict(tool_input or {}),
    )
    if rule is None:
        return None
    if rule.decision == PermissionDecision.DENY:
        return "deny"
    if rule.decision == PermissionDecision.ALLOW:
        return "allow"
    return None
