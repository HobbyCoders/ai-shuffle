"""
Permission Handler for Tool Requests

Manages tool permission requests with a queue system that supports:
- Multiple concurrent permission requests per session
- Batch approval/denial when rules match multiple queued requests
- Persistent permission rules (session-level and profile-level)
- Pattern matching for tool inputs (e.g., "Bash:npm*")
"""

import asyncio
import logging
import fnmatch
from collections import defaultdict
from typing import Optional, Dict, Any, List, Literal, Union
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# V2 Phase 4 (Q4): permission prompts persist as ``pending_interactions`` rows
# of this kind, so any install answers them relay-free. TTL is the durable
# form of the legacy 300s auto-deny (OD-2/Q4-5).
PERMISSION_KIND = "permission"
PERMISSION_TTL_SECONDS = 300

class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    DEFER = "defer"


class _DeferSentinel:
    """Returned by request_permission when user defers; query_engine translates to SDK deferred output."""


class RememberScope(str, Enum):
    NONE = "none"           # Don't remember
    SESSION = "session"     # Remember for this session only
    PROFILE = "profile"     # Remember for all sessions with this profile


@dataclass
class PermissionRequest:
    """A pending permission request for a tool use"""
    request_id: str
    session_id: str
    profile_id: str
    tool_name: str
    tool_input: Dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # SDK context fields (SDK 0.1.52+) — enables disambiguating parallel requests
    tool_use_id: Optional[str] = None
    agent_id: Optional[str] = None

    # SDK 0.1.81 ToolPermissionContext enrichment fields (Slice D)
    title: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    decision_reason: Optional[str] = None
    blocked_path: Optional[str] = None

    # P3-A3: set ONLY when this prompt was raised by a ProviderAgent worker and
    # redirected onto its parent's session (see app/core/provider_agent_approvals).
    # Carries the asking worker's label + session id so the UI can tag it. None
    # on every ordinary permission request, and omitted from payloads/events when
    # None, so the non-worker path is unchanged.
    provider_agent: Optional[Dict[str, Any]] = None

    # Request lifecycle state: "pending" | "deferred"
    state: str = "pending"

    # Async event for waiting on response
    response_event: asyncio.Event = field(default_factory=asyncio.Event)
    response: Optional[Dict[str, Any]] = None


@dataclass
class PermissionRule:
    """A saved permission rule"""
    id: str
    session_id: Optional[str]  # None = applies to all sessions
    profile_id: Optional[str]  # None = applies to all profiles
    tool_name: str
    tool_pattern: Optional[str]  # Optional pattern for tool input matching
    decision: PermissionDecision
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def matches(self, tool_name: str, tool_input: Dict[str, Any]) -> bool:
        """Check if this rule matches the given tool use"""
        if self.tool_name != tool_name and self.tool_name != "*":
            return False

        if not self.tool_pattern:
            return True

        # Pattern matching based on tool type
        return self._match_pattern(tool_input)

    def _match_pattern(self, tool_input: Dict[str, Any]) -> bool:
        """Match pattern against tool input"""
        pattern = self.tool_pattern

        # Handle different tool types
        if self.tool_name == "Bash":
            command = tool_input.get("command", "")
            return fnmatch.fnmatch(command, pattern)

        elif self.tool_name in ("Read", "Write", "Edit", "Glob"):
            file_path = tool_input.get("file_path", "") or tool_input.get("path", "")
            return fnmatch.fnmatch(file_path, pattern)

        elif self.tool_name == "Grep":
            path = tool_input.get("path", "")
            return fnmatch.fnmatch(path, pattern)

        elif self.tool_name == "WebFetch":
            url = tool_input.get("url", "")
            return fnmatch.fnmatch(url, pattern)

        # Generic: try to match against any string value in input
        for value in tool_input.values():
            if isinstance(value, str) and fnmatch.fnmatch(value, pattern):
                return True

        return False


class PermissionHandler:
    """
    Handles permission requests for tool uses with queue support.

    Key features:
    - Queue multiple permission requests per session
    - When a rule is created (e.g., "Allow Always"), automatically resolve
      all queued requests that match the new rule
    - Support session-level and profile-level rules
    - WebSocket integration for real-time permission UI
    """

    def __init__(self):
        # Pending requests by session_id
        self._pending_requests: Dict[str, Dict[str, PermissionRequest]] = {}

        # Deferred requests by session_id (Slice B)
        self._deferred: Dict[str, Dict[str, PermissionRequest]] = defaultdict(dict)

        # In-memory session rules (cleared when session ends)
        self._session_rules: Dict[str, List[PermissionRule]] = {}

        # Lock for thread-safe operations
        self._lock = asyncio.Lock()

    async def request_permission(
        self,
        request_id: str,
        session_id: str,
        profile_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
        broadcast_func: callable,
        tool_use_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        title: Optional[str] = None,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        decision_reason: Optional[str] = None,
        blocked_path: Optional[str] = None,
        provider_agent: Optional[Dict[str, Any]] = None,
    ) -> Union[PermissionResultAllow, PermissionResultDeny, "_DeferSentinel"]:
        """
        Request permission for a tool use.

        This will:
        1. Check saved rules first (session and profile level)
        2. If no rule matches, queue the request and notify frontend
        3. Wait for user response

        Args:
            request_id: Unique ID for this request
            session_id: The chat session ID
            profile_id: The profile ID for this session
            tool_name: Name of the tool being requested
            tool_input: Tool input parameters
            broadcast_func: Async function to broadcast to WebSocket
            tool_use_id: SDK tool_use_id from ToolPermissionContext (SDK 0.1.52+)
            agent_id: SDK agent_id from ToolPermissionContext (SDK 0.1.52+)
            title: SDK 0.1.81 ToolPermissionContext.title
            display_name: SDK 0.1.81 ToolPermissionContext.display_name
            description: SDK 0.1.81 ToolPermissionContext.description
            decision_reason: SDK 0.1.81 ToolPermissionContext.decision_reason
            blocked_path: SDK 0.1.81 ToolPermissionContext.blocked_path
            provider_agent: P3-A3 worker tag ({worker_session_id, label, ...})
                when this prompt was raised by a ProviderAgent worker and
                redirected onto its parent's session. None otherwise, and then
                invisible in every payload and WS event.

        Returns:
            PermissionResultAllow, PermissionResultDeny, or _DeferSentinel
        """
        return await self._request_permission_v2(
            request_id=request_id,
            session_id=session_id,
            profile_id=profile_id,
            tool_name=tool_name,
            tool_input=tool_input,
            broadcast_func=broadcast_func,
            tool_use_id=tool_use_id,
            agent_id=agent_id,
            title=title,
            display_name=display_name,
            description=description,
            decision_reason=decision_reason,
            blocked_path=blocked_path,
            provider_agent=provider_agent,
        )

    # ------------------------------------------------------------------
    # V2 Phase 4 (Q4) — pending_interactions-backed permission flow
    #
    # The durable ``pending_interactions`` row is the single source of truth:
    # the executing runner blocks on it via
    # ``interaction_wait.await_interaction`` while ANY install answers it
    # directly against the shared table (no cloud_relay). The in-memory
    # ``_pending_requests`` / ``_deferred`` dicts are NOT used on this path;
    # ``_session_rules`` (a purely local convenience) still short-circuits
    # future prompts on the executing runner exactly as before. All frontend
    # WebSocket shapes are byte-identical to the legacy path.
    # ------------------------------------------------------------------

    @staticmethod
    def _build_permission_payload(
        request_id: str,
        profile_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
        tool_use_id: Optional[str],
        agent_id: Optional[str],
        title: Optional[str],
        display_name: Optional[str],
        description: Optional[str],
        decision_reason: Optional[str],
        blocked_path: Optional[str],
        provider_agent: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Assemble the JSONB payload persisted on a permission interaction row.

        Carries everything the answer/broadcast/rule-match paths need: the FE
        ``request_id`` (matched Python-side, Q4-3), the rule-match keys
        (``profile_id``/``tool_name``/``tool_input``), and the SDK enrichment
        fields. ``profile_id`` is internal-only and never surfaced in a WS event.

        ``provider_agent`` (P3-A3) rides the SAME ``is not None`` guard as every
        enrichment field, so an ordinary prompt's payload is byte-identical to
        what it was before that parameter existed.
        """
        payload: Dict[str, Any] = {
            "request_id": request_id,
            "profile_id": profile_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
        for key, value in (
            ("tool_use_id", tool_use_id),
            ("agent_id", agent_id),
            ("title", title),
            ("display_name", display_name),
            ("description", description),
            ("decision_reason", decision_reason),
            ("blocked_path", blocked_path),
            ("provider_agent", provider_agent),
        ):
            if value is not None:
                payload[key] = value
        return payload

    @staticmethod
    def _v2_pending_permissions(session_id: str) -> List[Dict[str, Any]]:
        """This session's pending, unexpired permission interaction rows."""
        from app.db import interactions

        return [
            r
            for r in interactions.get_pending_interactions(session_id)
            if r.get("kind") == PERMISSION_KIND
        ]

    def _v2_pending_count(self, session_id: str) -> int:
        return len(self._v2_pending_permissions(session_id))


    def _payload_row_to_dict(self, row: Dict[str, Any], state: str) -> Dict[str, Any]:
        """Serialize a permission interaction row to the legacy request dict
        shape (matches :meth:`_request_to_dict`)."""
        payload = row.get("payload") or {}
        created = row.get("created_at")
        if hasattr(created, "isoformat"):
            created_at = created.isoformat()
        elif created is not None:
            created_at = str(created)
        else:
            created_at = ""
        d: Dict[str, Any] = {
            "session_id": row.get("session_id"),
            "request_id": payload.get("request_id"),
            "tool_name": payload.get("tool_name"),
            "tool_input": payload.get("tool_input"),
            "created_at": created_at,
            "state": state,
        }
        for key in (
            "tool_use_id",
            "agent_id",
            "title",
            "display_name",
            "description",
            "decision_reason",
            "blocked_path",
            # P3-A3 worker tag — present only on a redirected worker prompt.
            "provider_agent",
        ):
            if payload.get(key) is not None:
                d[key] = payload[key]
        return d

    async def _request_permission_v2(
        self,
        request_id: str,
        session_id: str,
        profile_id: str,
        tool_name: str,
        tool_input: Dict[str, Any],
        broadcast_func: "callable",
        tool_use_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        title: Optional[str] = None,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        decision_reason: Optional[str] = None,
        blocked_path: Optional[str] = None,
        provider_agent: Optional[Dict[str, Any]] = None,
    ) -> Union[PermissionResultAllow, PermissionResultDeny, "_DeferSentinel"]:
        """Table-backed request_permission (V2 Phase 4)."""
        from app.core import interaction_wait
        from app.db import interactions

        # Saved rules still short-circuit on the executing runner (identical to
        # legacy — profile rules are shared-DB, session rules are local).
        rule = self._check_rules(session_id, profile_id, tool_name, tool_input)
        if rule:
            logger.debug(f"Permission for {tool_name} auto-resolved by rule: {rule.decision}")
            if rule.decision == PermissionDecision.ALLOW:
                return PermissionResultAllow(updated_input=tool_input)
            else:
                return PermissionResultDeny(message="Denied by saved rule")

        payload = self._build_permission_payload(
            request_id, profile_id, tool_name, tool_input, tool_use_id,
            agent_id, title, display_name, description, decision_reason,
            blocked_path, provider_agent,
        )
        row = await asyncio.to_thread(
            interactions.create_interaction,
            session_id, PERMISSION_KIND, payload, PERMISSION_TTL_SECONDS,
        )
        interaction_id = row["id"]

        # Queue position counts this session's pending permission rows (the row
        # just created is included) — matches legacy len(_pending_requests).
        queue_position = self._v2_pending_count(session_id)

        # Broadcast the SAME permission_request event as legacy (transient
        # request object, never registered in _pending_requests).
        request = PermissionRequest(
            request_id=request_id,
            session_id=session_id,
            profile_id=profile_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            agent_id=agent_id,
            title=title,
            display_name=display_name,
            description=description,
            decision_reason=decision_reason,
            blocked_path=blocked_path,
            provider_agent=provider_agent,
        )
        await self._broadcast_permission_request(request, queue_position, broadcast_func)

        # Connector funnel: sessions owned by a connector conversation have
        # their prompts delivered to the platform (Discord buttons, ...) —
        # best-effort, AFTER the durable row exists so the normal web/mobile
        # answer paths stay authoritative even when delivery fails.
        try:
            from app.core.connector_permissions import deliver_permission_prompt

            await deliver_permission_prompt(
                session_id=session_id,
                request_id=request_id,
                tool_name=tool_name,
                tool_input=tool_input,
                description=description or title or f"Allow {display_name or tool_name}?",
            )
        except Exception:  # noqa: BLE001 — the funnel must never break the flow
            logger.debug(
                "connector permission funnel failed", exc_info=True
            )

        # Durable lifecycle event (#653): emit AFTER the WS broadcast, swallowed
        # on failure so a log append can never break the permission flow.
        from app.core import interaction_events

        await interaction_events.emit_interaction_requested(
            session_id, interaction_id, request_id, PERMISSION_KIND, payload,
        )

        # Notify watching mobile devices (verbatim from the legacy path).
        try:
            from app.services.push_notification import fire_and_forget_notify

            fire_and_forget_notify(
                title="Permission needed",
                body=f"Approve {display_name or tool_name}?",
                data={
                    "type": "permission_request",
                    "session_id": session_id,
                    "request_id": request_id,
                },
            )
        except Exception:
            pass

        logger.debug(f"Permission request persisted (v2): {request_id} for {tool_name}")

        # Block on the durable row: a peer install's answer wakes us via the
        # gateway NOTIFY; a local answer wakes us in-process. The adapter also
        # honours its own local expires_at deadline so a dead sweeper never
        # wedges the turn (Q4-4).
        result_row = await interaction_wait.await_interaction(
            interaction_id, session_id, float(PERMISSION_TTL_SECONDS)
        )

        state = (result_row or {}).get("state")
        if result_row is None or state in ("expired", "pending"):
            # Timed out — durably expired, or the local deadline lapsed with the
            # row still pending. Dismiss the modal + deny (legacy timeout arm).
            try:
                await broadcast_func({
                    "type": "permission_timeout",
                    "session_id": session_id,
                    "request_id": request_id,
                })
            except Exception:
                pass
            return PermissionResultDeny(message="Permission request timed out")
        if state == "cancelled":
            return PermissionResultDeny(message="Request cancelled")

        answer = result_row.get("answer") or {}
        # A14: a ``remember: "session"`` answered on ANY install stamps
        # remember/pattern onto the shared row; replay it into THIS runner's local
        # session rules (BEFORE returning the decision) so identical follow-up
        # requests short-circuit via _check_rules. Idempotent, and a no-op unless
        # a pattern rode along (profile-remember is DB-shared, needs no replay).
        if answer.get("remember") == "session" and answer.get("pattern"):
            payload_row = result_row.get("payload") or {}
            self._add_session_rule_idempotent(
                session_id,
                payload_row.get("tool_name", ""),
                answer.get("pattern"),
                answer.get("decision", "allow"),
            )
        decision = answer.get("decision")
        if decision == "defer":
            return _DeferSentinel()
        if decision == "allow" or answer.get("behavior") == "allow":
            return PermissionResultAllow(
                updated_input=answer.get("updatedInput", tool_input)
            )
        return PermissionResultDeny(
            message=answer.get("message", "Permission denied")
        )

    async def _respond_v2(
        self,
        request_id: str,
        session_id: str,
        decision: Literal["allow", "deny", "defer"],
        remember: Optional[Literal["none", "session", "profile"]],
        pattern: Optional[str],
        broadcast_func: Optional["callable"],
        answered_by_device: Optional[str],
    ) -> Dict[str, Any]:
        """Table-backed respond (V2 Phase 4). Resolves against the shared row so
        any install can answer relay-free (Q4-3/Q4-6)."""
        from app.core import interaction_wait
        from app.db import interactions

        row = next(
            (
                r
                for r in self._v2_pending_permissions(session_id)
                if (r.get("payload") or {}).get("request_id") == request_id
            ),
            None,
        )
        if row is None:
            return {"error": "Request not found"}
        payload = row.get("payload") or {}

        # Defer (Q4-2, option a): answer THIS row {decision: defer}; the requeue
        # path later mints a fresh pending row with the same request_id. The
        # awaiting request_permission translates the answered-defer row to a
        # _DeferSentinel, exactly as the legacy in-memory defer did.
        if decision == "defer":
            if not interactions.answer_interaction(
                row["id"], {"decision": "defer"}, answered_by_device
            ):
                # A15: lost the TOCTOU — a concurrent responder already answered
                # this row. A defer must not report success or broadcast when it
                # did not actually resolve anything.
                return {"error": "Request not found"}
            interaction_wait.notify_changed(session_id)
            if broadcast_func:
                await broadcast_func({
                    "type": "permission_queue_update",
                    "session_id": session_id,
                    "resolved_ids": [request_id],
                    "deferred_ids": [request_id],
                    "remaining_count": self._v2_pending_count(session_id),
                })
            # Durable lifecycle event (#653): the guarded UPDATE won above, so
            # emit exactly one interaction.answered. AFTER the WS broadcast.
            from app.core import interaction_events

            await interaction_events.emit_interaction_answered(
                session_id, row["id"], request_id, {"decision": "defer"},
            )
            return {
                "resolved": True,
                "deferred": True,
                "auto_resolved_count": 0,
                "auto_resolved_ids": [],
            }

        tool_input = payload.get("tool_input") or {}
        if decision == "allow":
            answer = {"decision": "allow", "behavior": "allow", "updatedInput": tool_input}
        else:
            answer = {"decision": "deny", "behavior": "deny", "message": "User denied permission"}
        # A14: ride a session-remember hint on the shared answer JSONB so the
        # EXECUTING runner (possibly a different install than the one answering)
        # learns the rule and short-circuits identical follow-ups. Stamp BEFORE
        # the guarded update so A15's False branch discards it with every other
        # side effect. (profile rules are DB-persisted and already shared.)
        if remember == "session":
            answer["remember"] = "session"
            answer["pattern"] = pattern
        if not interactions.answer_interaction(row["id"], answer, answered_by_device):
            # A15: lost the TOCTOU — a concurrent responder already answered this
            # row, or it expired between the pending read and here. Report the
            # not-resolved shape and leave NO side effects: no remember rule, no
            # defer requeue, no wake.
            return {"error": "Request not found"}
        interaction_wait.notify_changed(session_id)

        resolved_ids: List[str] = []
        resolved_emits: List[tuple] = []
        if remember and remember != "none":
            profile_id = payload.get("profile_id") or ""
            rule = PermissionRule(
                id=f"rule-{datetime.now().timestamp()}",
                session_id=session_id if remember == "session" else None,
                profile_id=profile_id if remember == "profile" else None,
                tool_name=payload.get("tool_name", ""),
                tool_pattern=pattern,
                decision=PermissionDecision(decision),
            )
            if remember == "session":
                # Session rules are executing-runner-local (as in legacy); a
                # rule created on a peer install does not reach the owner.
                self._session_rules.setdefault(session_id, []).append(rule)
            else:
                # Awaited, not fire-and-forget: the rule must be durable before
                # the next permission check consults it.
                await run_sync(
                    database.add_permission_rule,
                    profile_id=profile_id,
                    tool_name=rule.tool_name,
                    tool_pattern=rule.tool_pattern,
                    decision=rule.decision.value,
                )
            resolved_ids, resolved_emits = self._v2_resolve_matching(
                session_id, rule, answered_by_device
            )
            if resolved_ids:
                interaction_wait.notify_changed(session_id)
                if broadcast_func:
                    await broadcast_func({
                        "type": "permission_queue_update",
                        "session_id": session_id,
                        "resolved_ids": resolved_ids,
                        "remaining_count": self._v2_pending_count(session_id),
                    })

        # Durable lifecycle events (#653): each guarded UPDATE that won emits
        # exactly one interaction.answered, AFTER the notify_changed + WS
        # broadcast above. The primary row plus every sibling row auto-resolved
        # by a remember rule.
        from app.core import interaction_events

        await interaction_events.emit_interaction_answered(
            session_id, row["id"], request_id, answer,
        )
        for emit_interaction_id, emit_request_id, emit_answer in resolved_emits:
            await interaction_events.emit_interaction_answered(
                session_id, emit_interaction_id, emit_request_id, emit_answer,
            )

        return {
            "resolved": True,
            "auto_resolved_count": len(resolved_ids),
            "auto_resolved_ids": resolved_ids,
        }

    def _v2_resolve_matching(
        self,
        session_id: str,
        rule: PermissionRule,
        answered_by_device: Optional[str],
    ) -> tuple:
        """Answer every OTHER pending permission row matching ``rule`` (table
        analogue of :meth:`_resolve_matching_requests`).

        Returns ``(resolved_request_ids, emits)`` where ``emits`` is a list of
        ``(interaction_id, request_id, answer)`` tuples for every row whose
        guarded UPDATE won — the async caller emits the durable
        ``interaction.answered`` events (#653), one per winning transition.
        """
        from app.db import interactions

        resolved: List[str] = []
        emits: List[tuple] = []
        for r in self._v2_pending_permissions(session_id):
            payload = r.get("payload") or {}
            if not rule.matches(payload.get("tool_name", ""), payload.get("tool_input") or {}):
                continue
            if rule.decision == PermissionDecision.ALLOW:
                answer = {
                    "decision": "allow",
                    "behavior": "allow",
                    "updatedInput": payload.get("tool_input") or {},
                }
            else:
                answer = {"decision": "deny", "behavior": "deny", "message": "Denied by rule"}
            # A14: when the triggering rule is a session-remember (session_id set;
            # profile rules leave it None), stamp the same hint onto each
            # batch-resolved row so the executing runner installs the rule when
            # that row's waiter wakes — matching the direct-answer stamping.
            if rule.session_id is not None:
                answer["remember"] = "session"
                answer["pattern"] = rule.tool_pattern
            if interactions.answer_interaction(r["id"], answer, answered_by_device):
                rid = payload.get("request_id")
                if rid is not None:
                    resolved.append(rid)
                emits.append((r["id"], rid, answer))
        return resolved, emits

    def _add_session_rule_idempotent(
        self,
        session_id: str,
        tool_name: str,
        pattern: Optional[str],
        decision: str,
    ) -> None:
        """Install a session-scoped rule locally if an identical one is absent (A14).

        A ``remember: "session"`` answer stamps its rule hint into the shared
        interaction row; the EXECUTING runner — which need not be the install that
        answered — replays it here so identical follow-up requests short-circuit in
        :meth:`_check_rules`. Idempotent: repeated waiter wakes add no duplicate.
        The rule shape matches the local one stored by the legacy/respond path.
        """
        rules = self._session_rules.setdefault(session_id, [])
        for existing in rules:
            if (
                existing.tool_name == tool_name
                and existing.tool_pattern == pattern
                and existing.decision.value == decision
            ):
                return
        rules.append(
            PermissionRule(
                id=f"rule-{datetime.now().timestamp()}",
                session_id=session_id,
                profile_id=None,
                tool_name=tool_name,
                tool_pattern=pattern,
                decision=PermissionDecision(decision),
            )
        )

    async def _requeue_deferred_v2(
        self,
        session_id: str,
        broadcast_func: Optional["callable"],
    ) -> List[str]:
        """Table-backed requeue (V2 Phase 4, Q4-2): mint a fresh pending row
        (same request_id, fresh TTL) for each not-yet-requeued deferred row."""
        from app.core import interaction_wait
        from app.db import interactions

        requeued_ids: List[str] = []
        new_payloads: List[Dict[str, Any]] = []
        # (new_interaction_id, request_id, payload) for each freshly minted row,
        # so the durable interaction.requested events (#653) fire after the WS
        # re-broadcast below.
        requested_emits: List[tuple] = []
        for r in interactions.get_answered_interactions(session_id, PERMISSION_KIND):
            answer = r.get("answer") or {}
            if answer.get("decision") != "defer" or answer.get("requeued"):
                continue
            payload = r.get("payload") or {}
            interactions.mark_interaction_requeued(r["id"])
            new_row = interactions.create_interaction(
                session_id, PERMISSION_KIND, payload, PERMISSION_TTL_SECONDS
            )
            rid = payload.get("request_id")
            if rid is not None:
                requeued_ids.append(rid)
            new_payloads.append(payload)
            requested_emits.append((new_row["id"], rid, payload))

        if requeued_ids:
            interaction_wait.notify_changed(session_id)

        # Re-broadcast each requeued request + the queue update (mirrors the
        # legacy requeue_deferred broadcasts exactly).
        if broadcast_func:
            for payload in new_payloads:
                queue_position = self._v2_pending_count(session_id)
                request = PermissionRequest(
                    request_id=payload.get("request_id", ""),
                    session_id=session_id,
                    profile_id=payload.get("profile_id", ""),
                    tool_name=payload.get("tool_name", ""),
                    tool_input=payload.get("tool_input") or {},
                    tool_use_id=payload.get("tool_use_id"),
                    agent_id=payload.get("agent_id"),
                    title=payload.get("title"),
                    display_name=payload.get("display_name"),
                    description=payload.get("description"),
                    decision_reason=payload.get("decision_reason"),
                    blocked_path=payload.get("blocked_path"),
                    provider_agent=payload.get("provider_agent"),
                )
                await self._broadcast_permission_request(request, queue_position, broadcast_func)

            if requeued_ids:
                await broadcast_func({
                    "type": "permission_queue_update",
                    "session_id": session_id,
                    "requeued_ids": requeued_ids,
                    "remaining_count": self._v2_pending_count(session_id),
                })

        # Durable lifecycle events (#653): a requeue mints a fresh pending row
        # with a NEW interaction_id (same FE request_id), so emitting a second
        # interaction.requested is correct. AFTER the WS re-broadcast.
        from app.core import interaction_events

        for new_interaction_id, rid, payload in requested_emits:
            await interaction_events.emit_interaction_requested(
                session_id, new_interaction_id, rid, PERMISSION_KIND, payload,
            )

        return requeued_ids

    async def _broadcast_permission_request(
        self,
        request: "PermissionRequest",
        queue_position: int,
        broadcast_func: callable,
    ) -> None:
        """Build and broadcast a permission_request event for the given request."""
        permission_event = {
            "type": "permission_request",
            "session_id": request.session_id,
            "request_id": request.request_id,
            "tool_name": request.tool_name,
            "tool_input": request.tool_input,
            "queue_position": queue_position,
            "queue_total": queue_position,
        }
        # Include SDK context fields when available (SDK 0.1.52+)
        if request.tool_use_id:
            permission_event["tool_use_id"] = request.tool_use_id
        if request.agent_id:
            permission_event["agent_id"] = request.agent_id
        # Include SDK 0.1.81 enrichment fields when available (Slice D)
        if request.title is not None:
            permission_event["title"] = request.title
        if request.display_name is not None:
            permission_event["display_name"] = request.display_name
        if request.description is not None:
            permission_event["description"] = request.description
        if request.decision_reason is not None:
            permission_event["decision_reason"] = request.decision_reason
        if request.blocked_path is not None:
            permission_event["blocked_path"] = request.blocked_path
        # P3-A3: structured worker tag, present ONLY on a ProviderAgent worker's
        # prompt redirected onto its parent. Absent on every ordinary request, so
        # the legacy event shape is unchanged.
        if request.provider_agent is not None:
            permission_event["provider_agent"] = request.provider_agent

        # ── Delivery leg 1: the origin socket that owns this query ───────────
        # Best-effort: a dead/closing socket must never propagate out of here.
        # Before P3-A4 this call was bare, so an origin-socket failure aborted
        # the whole broadcast — and (worse) left the caller's flow half-done
        # while a redirected ProviderAgent worker prompt, whose delivery is
        # already two-legged and individually swallowed, sailed through. That
        # asymmetry is what this block removes.
        try:
            await broadcast_func(permission_event)
        except Exception:  # noqa: BLE001 — delivery is best-effort
            logger.warning(
                "Permission prompt origin broadcast failed (session=%s request=%s)",
                request.session_id, request.request_id, exc_info=True,
            )

        # ── Delivery leg 2: user-wide device fan-out ─────────────────────────
        # Mirrors ``provider_agent_approvals.deliver_to_parent``: the origin
        # socket only reaches the tab that owns the query, which may be closed,
        # headless, or on another device. The frame carries its own session_id
        # and request_id, so the frontend routes and dedupes it.
        user_id = await self._resolve_user_id(request.session_id)
        if user_id:
            try:
                from app.core.sync_engine import sync_engine

                await sync_engine.broadcast_user_permission(user_id, permission_event)
            except Exception:  # noqa: BLE001 — delivery is best-effort
                logger.warning(
                    "Permission prompt device fan-out failed (session=%s request=%s)",
                    request.session_id, request.request_id, exc_info=True,
                )

    async def _resolve_user_id(self, session_id: str) -> Optional[str]:
        """Owning user for a session, keyed exactly as the sync engine registers.

        ``sync_engine._user_connections`` is keyed by the value /ws/chat computes
        at connect time — ``api_user_id or "admin"`` (app/api/websocket.py) — so
        the session row's ``api_user_id`` maps to it with the same fallback (the
        identical mapping already used by ``app/core/streaming_status.py``).
        Returns ``None`` when the row is missing or the lookup fails; leg 2 is
        then simply skipped, exactly as it is for an unknown user.
        """
        try:
            from app.db import sessions as sessions_db

            row = await asyncio.to_thread(sessions_db.get_session, session_id)
        except Exception:  # noqa: BLE001 — fan-out is best-effort
            logger.warning(
                "Permission prompt user lookup failed (session=%s)",
                session_id, exc_info=True,
            )
            return None
        if not row:
            return None
        return row.get("api_user_id") or "admin"

    async def respond(
        self,
        request_id: str,
        session_id: str,
        decision: Literal["allow", "deny", "defer"],
        remember: Optional[Literal["none", "session", "profile"]] = None,
        pattern: Optional[str] = None,
        broadcast_func: Optional[callable] = None,
        answered_by_device: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Respond to a permission request.

        If remember is set (and decision != defer), creates a rule and
        auto-resolves matching queued requests.

        Args:
            request_id: The request to respond to
            session_id: The session ID
            decision: "allow", "deny", or "defer"
            remember: Scope to remember the decision (ignored when decision == "defer")
            pattern: Optional pattern for the rule (ignored when decision == "defer")
            broadcast_func: Function to broadcast queue updates
            answered_by_device: Device id of the answering install (V2 Phase 4,
                Q4-6). Ignored on the legacy path.

        Returns:
            Info about resolved requests
        """
        return await self._respond_v2(
            request_id=request_id,
            session_id=session_id,
            decision=decision,
            remember=remember,
            pattern=pattern,
            broadcast_func=broadcast_func,
            answered_by_device=answered_by_device,
        )

    async def _resolve_matching_requests(
        self,
        session_id: str,
        rule: PermissionRule,
        broadcast_func: Optional[callable]
    ) -> List[str]:
        """Resolve all queued requests that match the new rule"""
        resolved_ids = []

        if session_id not in self._pending_requests:
            return resolved_ids

        # Find all matching requests
        requests_to_resolve = []
        for req_id, req in list(self._pending_requests[session_id].items()):
            if rule.matches(req.tool_name, req.tool_input):
                requests_to_resolve.append((req_id, req))

        # Resolve them
        for req_id, req in requests_to_resolve:
            if rule.decision == PermissionDecision.ALLOW:
                req.response = {"behavior": "allow", "updatedInput": req.tool_input}
            else:
                req.response = {"behavior": "deny", "message": "Denied by rule"}

            req.response_event.set()
            del self._pending_requests[session_id][req_id]
            resolved_ids.append(req_id)

            logger.debug(f"Auto-resolved request {req_id} for {req.tool_name} by new rule")

        # Broadcast updated queue if any were resolved
        if resolved_ids and broadcast_func:
            await broadcast_func({
                "type": "permission_queue_update",
                "session_id": session_id,
                "resolved_ids": resolved_ids,
                "remaining_count": len(self._pending_requests.get(session_id, {}))
            })

        return resolved_ids

    def _check_rules(
        self,
        session_id: str,
        profile_id: str,
        tool_name: str,
        tool_input: Dict[str, Any]
    ) -> Optional[PermissionRule]:
        """Check if any saved rule matches this tool use"""
        # Check session-level rules first (more specific)
        if session_id in self._session_rules:
            for rule in self._session_rules[session_id]:
                if rule.matches(tool_name, tool_input):
                    return rule

        # Check profile-level rules from database
        db_rules = database.get_permission_rules(profile_id=profile_id)
        for rule_data in db_rules:
            rule = PermissionRule(
                id=rule_data["id"],
                session_id=None,
                profile_id=rule_data["profile_id"],
                tool_name=rule_data["tool_name"],
                tool_pattern=rule_data.get("tool_pattern"),
                decision=PermissionDecision(rule_data["decision"])
            )
            if rule.matches(tool_name, tool_input):
                return rule

        return None

    def get_pending_requests(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all pending permission requests for a session"""
        return [
            self._payload_row_to_dict(row, "pending")
            for row in self._v2_pending_permissions(session_id)
        ]

    def get_deferred_requests(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all deferred permission requests for a session (Slice B)"""
        from app.db import interactions

        out: List[Dict[str, Any]] = []
        for row in interactions.get_answered_interactions(session_id, PERMISSION_KIND):
            answer = row.get("answer") or {}
            if answer.get("decision") != "defer" or answer.get("requeued"):
                continue
            out.append(self._payload_row_to_dict(row, "deferred"))
        return out

    def _request_to_dict(self, req: "PermissionRequest") -> Dict[str, Any]:
        """Serialize a PermissionRequest to a dict for WS broadcast."""
        d: Dict[str, Any] = {
            "session_id": req.session_id,
            "request_id": req.request_id,
            "tool_name": req.tool_name,
            "tool_input": req.tool_input,
            "created_at": req.created_at.isoformat(),
            "state": req.state,
        }
        if req.tool_use_id:
            d["tool_use_id"] = req.tool_use_id
        if req.agent_id:
            d["agent_id"] = req.agent_id
        if req.title is not None:
            d["title"] = req.title
        if req.display_name is not None:
            d["display_name"] = req.display_name
        if req.description is not None:
            d["description"] = req.description
        if req.decision_reason is not None:
            d["decision_reason"] = req.decision_reason
        if req.blocked_path is not None:
            d["blocked_path"] = req.blocked_path
        if req.provider_agent is not None:
            d["provider_agent"] = req.provider_agent
        return d

    async def requeue_deferred(
        self,
        session_id: str,
        broadcast_func: Optional[callable] = None,
    ) -> List[str]:
        """
        Move all deferred requests for a session back to pending and re-broadcast.

        Returns the list of requeued request_ids.
        """
        return await self._requeue_deferred_v2(session_id, broadcast_func)

    def get_queue_count(self, session_id: str) -> int:
        """Get count of pending requests for a session"""
        return self._v2_pending_count(session_id)

    async def cancel_request(self, request_id: str, session_id: str) -> bool:
        """Cancel a pending permission request"""
        from app.core import interaction_wait
        from app.db import interactions

        row = next(
            (
                r
                for r in self._v2_pending_permissions(session_id)
                if (r.get("payload") or {}).get("request_id") == request_id
            ),
            None,
        )
        if row is None:
            return False
        cancelled = interactions.cancel_interaction(row["id"])
        if cancelled:
            interaction_wait.notify_changed(session_id)
            # Durable lifecycle event (#653): guarded cancel won → emit
            # exactly one interaction.expired (reason="cancelled").
            from app.core import interaction_events

            await interaction_events.emit_interaction_expired(
                session_id, row["id"], request_id, "cancelled",
            )
        return cancelled

    async def cancel_all_requests(self, session_id: str) -> int:
        """Cancel all pending requests for a session"""
        from app.core import interaction_wait
        from app.db import interactions

        rows = self._v2_pending_permissions(session_id)
        count = 0
        cancelled_rows: List[tuple] = []
        for row in rows:
            if interactions.cancel_interaction(row["id"]):
                count += 1
                cancelled_rows.append(
                    (row["id"], (row.get("payload") or {}).get("request_id"))
                )
        if count:
            interaction_wait.notify_changed(session_id)
            # Durable lifecycle events (#653): one interaction.expired
            # (reason="cancelled") per row whose guarded cancel won.
            from app.core import interaction_events

            for cancelled_interaction_id, rid in cancelled_rows:
                await interaction_events.emit_interaction_expired(
                    session_id, cancelled_interaction_id, rid, "cancelled",
                )
        return count

    def clear_session_rules(self, session_id: str):
        """Clear session-level rules when session ends"""
        self._session_rules.pop(session_id, None)

    def get_session_rules(self, session_id: str) -> List[Dict[str, Any]]:
        """Get session-level rules"""
        if session_id not in self._session_rules:
            return []

        return [
            {
                "id": rule.id,
                "tool_name": rule.tool_name,
                "tool_pattern": rule.tool_pattern,
                "decision": rule.decision.value
            }
            for rule in self._session_rules[session_id]
        ]

    def reset_all(self) -> None:
        """Drop all pending/deferred requests and session rules.

        Used on server shutdown and between tests to prevent unbounded growth
        across sessions. Synchronous so it can run outside an event loop.
        """
        self._pending_requests.clear()
        self._deferred.clear()
        self._session_rules.clear()


# Global singleton instance
permission_handler = PermissionHandler()
