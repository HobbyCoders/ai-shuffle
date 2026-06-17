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

logger = logging.getLogger(__name__)


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

        Returns:
            PermissionResultAllow, PermissionResultDeny, or _DeferSentinel
        """
        # Check saved rules first
        rule = self._check_rules(session_id, profile_id, tool_name, tool_input)
        if rule:
            logger.debug(f"Permission for {tool_name} auto-resolved by rule: {rule.decision}")
            if rule.decision == PermissionDecision.ALLOW:
                return PermissionResultAllow(updated_input=tool_input)
            else:
                return PermissionResultDeny(message="Denied by saved rule")

        # Create pending request
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
        )

        async with self._lock:
            if session_id not in self._pending_requests:
                self._pending_requests[session_id] = {}
            self._pending_requests[session_id][request_id] = request

        # Get current queue position
        queue_position = len(self._pending_requests.get(session_id, {}))

        # Broadcast permission request to frontend
        await self._broadcast_permission_request(request, queue_position, broadcast_func)

        logger.debug(f"Permission request queued: {request_id} for {tool_name}")

        # Wait for response (with timeout)
        try:
            await asyncio.wait_for(request.response_event.wait(), timeout=300.0)  # 5 min timeout
        except asyncio.TimeoutError:
            # Remove from queue on timeout
            async with self._lock:
                if session_id in self._pending_requests:
                    self._pending_requests[session_id].pop(request_id, None)
            # Notify frontend to dismiss the modal
            try:
                await broadcast_func({
                    "type": "permission_timeout",
                    "session_id": session_id,
                    "request_id": request_id
                })
            except Exception:
                pass  # Best-effort notification
            return PermissionResultDeny(message="Permission request timed out")

        # Check if user deferred (Slice B)
        if isinstance(request.response, _DeferSentinel):
            return request.response

        # Return the response - convert from stored dict to proper type
        if request.response:
            if request.response.get("behavior") == "allow":
                return PermissionResultAllow(updated_input=request.response.get("updatedInput", tool_input))
            else:
                return PermissionResultDeny(message=request.response.get("message", "Permission denied"))
        else:
            return PermissionResultDeny(message="No response received")

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
        await broadcast_func(permission_event)

    async def respond(
        self,
        request_id: str,
        session_id: str,
        decision: Literal["allow", "deny", "defer"],
        remember: Optional[Literal["none", "session", "profile"]] = None,
        pattern: Optional[str] = None,
        broadcast_func: Optional[callable] = None
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

        Returns:
            Info about resolved requests
        """
        async with self._lock:
            if session_id not in self._pending_requests:
                return {"error": "Session not found"}

            request = self._pending_requests[session_id].get(request_id)
            if not request:
                return {"error": "Request not found"}

            # Handle defer decision (Slice B)
            if decision == "defer":
                request.state = "deferred"
                request.response = _DeferSentinel()
                request.response_event.set()
                del self._pending_requests[session_id][request_id]
                self._deferred[session_id][request_id] = request

                # Broadcast that this request moved to deferred state
                if broadcast_func:
                    await broadcast_func({
                        "type": "permission_queue_update",
                        "session_id": session_id,
                        "resolved_ids": [request_id],
                        "deferred_ids": [request_id],
                        "remaining_count": len(self._pending_requests.get(session_id, {})),
                    })

                return {
                    "resolved": True,
                    "deferred": True,
                    "auto_resolved_count": 0,
                    "auto_resolved_ids": [],
                }

            # Set the response
            if decision == "allow":
                request.response = {"behavior": "allow", "updatedInput": request.tool_input}
            else:
                request.response = {"behavior": "deny", "message": "User denied permission"}

            # Signal the waiting coroutine
            request.response_event.set()

            # Remove from pending
            del self._pending_requests[session_id][request_id]

            # Create rule if remembering
            resolved_requests = []
            if remember and remember != "none":
                rule = PermissionRule(
                    id=f"rule-{datetime.now().timestamp()}",
                    session_id=session_id if remember == "session" else None,
                    profile_id=request.profile_id if remember == "profile" else None,
                    tool_name=request.tool_name,
                    tool_pattern=pattern,
                    decision=PermissionDecision(decision)
                )

                # Save rule
                if remember == "session":
                    if session_id not in self._session_rules:
                        self._session_rules[session_id] = []
                    self._session_rules[session_id].append(rule)
                else:
                    # Save to database for profile-level persistence
                    database.add_permission_rule(
                        profile_id=request.profile_id,
                        tool_name=rule.tool_name,
                        tool_pattern=rule.tool_pattern,
                        decision=rule.decision.value
                    )

                # Auto-resolve matching queued requests
                resolved_requests = await self._resolve_matching_requests(
                    session_id, rule, broadcast_func
                )

        return {
            "resolved": True,
            "auto_resolved_count": len(resolved_requests),
            "auto_resolved_ids": resolved_requests
        }

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
        if session_id not in self._pending_requests:
            return []

        return [
            self._request_to_dict(req)
            for req in self._pending_requests[session_id].values()
        ]

    def get_deferred_requests(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all deferred permission requests for a session (Slice B)"""
        return [
            self._request_to_dict(req)
            for req in self._deferred.get(session_id, {}).values()
        ]

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
        requeued_ids: List[str] = []

        async with self._lock:
            deferred_snapshot = dict(self._deferred.get(session_id, {}))
            self._deferred[session_id].clear()

            if session_id not in self._pending_requests:
                self._pending_requests[session_id] = {}

            for req_id, req in deferred_snapshot.items():
                req.state = "pending"
                # Reset the event so request_permission can wait again
                req.response_event = asyncio.Event()
                req.response = None
                self._pending_requests[session_id][req_id] = req
                requeued_ids.append(req_id)

        # Re-broadcast each request (outside lock)
        if broadcast_func:
            for req_id in requeued_ids:
                req = self._pending_requests[session_id].get(req_id)
                if req:
                    queue_position = len(self._pending_requests.get(session_id, {}))
                    await self._broadcast_permission_request(req, queue_position, broadcast_func)

            if requeued_ids:
                await broadcast_func({
                    "type": "permission_queue_update",
                    "session_id": session_id,
                    "requeued_ids": requeued_ids,
                    "remaining_count": len(self._pending_requests.get(session_id, {})),
                })

        return requeued_ids

    def get_queue_count(self, session_id: str) -> int:
        """Get count of pending requests for a session"""
        return len(self._pending_requests.get(session_id, {}))

    async def cancel_request(self, request_id: str, session_id: str) -> bool:
        """Cancel a pending permission request"""
        async with self._lock:
            if session_id not in self._pending_requests:
                return False

            request = self._pending_requests[session_id].get(request_id)
            if not request:
                return False

            # Set denial response and signal
            request.response = {"behavior": "deny", "message": "Request cancelled"}
            request.response_event.set()
            del self._pending_requests[session_id][request_id]

            return True

    async def cancel_all_requests(self, session_id: str) -> int:
        """Cancel all pending requests for a session"""
        async with self._lock:
            if session_id not in self._pending_requests:
                return 0

            count = len(self._pending_requests[session_id])
            for request in self._pending_requests[session_id].values():
                request.response = {"behavior": "deny", "message": "Session cancelled"}
                request.response_event.set()

            del self._pending_requests[session_id]
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
