"""Route a worker's tool approvals to the PARENT session.

A Task-dispatched worker (:mod:`app.core.provider_agent`) runs its turn on its
OWN session, so its ``can_use_tool`` callback (built by
``query_engine._stream_to_websocket_impl``) is closed over the WORKER's session
id. Without this module its approval prompt would be raised on a session nobody
is looking at, and the worker would sit blocked until the TTL denied it.

The rule: the prompt surfaces on the **parent** session's UI, tagged with the
worker's label, through the existing ``pending_interactions`` machinery;
unanswered prompts fall to the existing TTL sweeper
(``permission_handler.PERMISSION_TTL_SECONDS``). One surface to watch.

Raising on another session's id is legitimate: ``pending_interactions`` has
**no ownership check** (``interactions.answer_interaction`` guards only on id,
``pending`` state and expiry), so an in-process caller may raise an interaction
on any session's behalf.

Correlation uses TWO ids. The waiter blocks on ``pending_interactions.id``; the
UI only sees ``payload["request_id"]``. The answer path
(``PermissionHandler._respond_v2``) re-derives the row by scanning
``(session_id, kind, payload.request_id)`` and calls
``interaction_wait.notify_changed(session_id)``, which wakes EVERY waiter on
that session; each re-reads its OWN row and resolves only on its own change. So
the one invariant with many workers prompting the same parent is that no two
live prompts on that parent share a ``request_id``. :func:`scoped_request_id`
guarantees it by namespacing every redirected id by the worker's session id (a
fresh uuid4). The id ``query_engine`` mints (``perm-<session_id>-<8 hex>``)
already contains it, so normalization is a no-op there.

Delivery to the parent's UI has two best-effort legs, because the parent may or
may not have a live socket:

1. the parent's own pooled ``callback_refs["broadcast_func"]`` — for a
   synchronous dispatch the parent is mid-turn (blocked in the Task tool
   call), so this is the socket that owns the tab the user is looking at;
2. a user-level fan-out (``sync_engine.broadcast_user_permission``) for every
   other device, mirroring ``sync_engine.broadcast_user_question``. Background
   and Explore-class dispatches return a receipt instead of blocking, so their
   parent may be parked or idle with no live socket, and this leg is what
   reaches the user then.

Neither leg is required for correctness: the durable row plus the parent's
``interaction.requested`` log event let any device restore the prompt.

Scope: the permission path only. ``AskUserQuestion`` (a different handler with
its own TTL) is not redirected; ``query_options`` withholds it from workers.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from app.core.agent_types import PermissionResultDeny

logger = logging.getLogger(__name__)


#: Separator used by :func:`scoped_request_id` when it has to namespace an id
#: that does not already carry the worker's session id. Chosen so it cannot
#: occur in the production ``perm-<uuid>-<hex>`` shape.
SCOPE_SEPARATOR = "@w:"

#: Returned to the worker's engine when the redirect itself fails (a vanished
#: parent, a DB error raising the row). The tool call must resolve, never hang.
ROUTE_FAILURE_MESSAGE = (
    "Approval could not be routed to the parent session, so the tool call was "
    "denied."
)


# ---------------------------------------------------------------------------
# Route registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerApprovalRoute:
    """Where a worker session's approvals go, and how they are tagged."""

    worker_session_id: str
    parent_session_id: str
    label: str = ""
    provider_id: Optional[str] = None
    model_id: Optional[str] = None

    @property
    def display_label(self) -> str:
        """A never-empty human label for the asking worker."""
        return (self.label or "").strip() or "ProviderAgent worker"

    @property
    def binding(self) -> str:
        """``provider · model``, or "" when neither is known."""
        return " · ".join(p for p in (self.provider_id, self.model_id) if p)

    def as_payload(self) -> Dict[str, Any]:
        """The structured tag persisted on the interaction row and broadcast.

        Carries the worker's label AND its session id, so a future card can link
        the prompt to the worker's transcript without a second lookup.
        """
        return {
            "worker_session_id": self.worker_session_id,
            "parent_session_id": self.parent_session_id,
            "label": self.display_label,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
        }


#: worker session id -> route, newest last. In-process and bounded on purpose,
#: exactly like ``provider_agent._WORKER_SESSION_IDS``: a restart kills every
#: live worker anyway, so a forgotten entry can only matter for a session a
#: human later resumes by hand. Never unregistered on worker completion — the
#: worker's turn can still be draining tool calls when ``dispatch_worker``
#: returns, and a stale entry is inert (a route only matters when a session with
#: that id actually raises a permission prompt).
_ROUTES: "OrderedDict[str, WorkerApprovalRoute]" = OrderedDict()
_ROUTE_LIMIT = 10_000


def register_route(
    *,
    worker_session_id: str,
    parent_session_id: str,
    label: str = "",
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> Optional[WorkerApprovalRoute]:
    """Record that ``worker_session_id``'s approvals belong to its parent.

    Idempotent (a re-register replaces and promotes). Returns the stored route,
    or ``None`` when either id is missing — a worker with no parent simply keeps
    the default behaviour rather than raising here.
    """
    if not worker_session_id or not parent_session_id:
        return None
    route = WorkerApprovalRoute(
        worker_session_id=worker_session_id,
        parent_session_id=parent_session_id,
        label=label or "",
        provider_id=provider_id,
        model_id=model_id,
    )
    _ROUTES.pop(worker_session_id, None)
    _ROUTES[worker_session_id] = route
    while len(_ROUTES) > _ROUTE_LIMIT:
        _ROUTES.popitem(last=False)
    return route


def get_route(session_id: Optional[str]) -> Optional[WorkerApprovalRoute]:
    """The approval route for ``session_id``, or ``None`` for a normal session."""
    if not session_id:
        return None
    return _ROUTES.get(session_id)


def unregister_route(worker_session_id: Optional[str]) -> None:
    """Drop a route (not required — the registry is bounded and self-trimming)."""
    if worker_session_id:
        _ROUTES.pop(worker_session_id, None)


def reset_routes() -> None:
    """Drop every route (tests only)."""
    _ROUTES.clear()


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def scoped_request_id(route: WorkerApprovalRoute, request_id: Optional[str]) -> str:
    """A request id that cannot collide with another worker on the same parent.

    The answer path matches on ``(session_id, kind, payload.request_id)``, and
    every worker prompting one parent now shares that ``session_id``. Namespacing
    by the worker's session id (a fresh uuid4) is what keeps the scan
    single-valued.

    Idempotent, and a NO-OP on the production id (``query_engine`` mints
    ``perm-<worker_session_id>-<8 hex>``, which already contains the worker id) so
    the redirected id stays the one the engine/logs already reference.
    """
    rid = (request_id or "").strip()
    if not rid:
        rid = f"perm-{uuid.uuid4().hex[:8]}"
    if SCOPE_SEPARATOR in rid:
        return rid
    if route.worker_session_id and route.worker_session_id in rid:
        return rid
    return f"{rid}{SCOPE_SEPARATOR}{route.worker_session_id}"


# ---------------------------------------------------------------------------
# Tagging (rides EXISTING payload fields — no frontend change needed)
# ---------------------------------------------------------------------------

def tagged_title(route: WorkerApprovalRoute, title: Optional[str]) -> str:
    """The prompt heading, prefixed with the asking worker's label.

    ``PermissionRequest.svelte`` renders ``title || display_name || <tool label>``
    as the modal heading (with the tool name on the line beneath), and
    ``PermissionQueue.svelte`` uses the same precedence for queued items — so a
    label here tags BOTH surfaces with no frontend change. A caller-provided
    title is kept, never replaced.
    """
    base = (title or "").strip()
    label = route.display_label
    return f"{label} — {base}" if base else label


def tagged_description(route: WorkerApprovalRoute, description: Optional[str]) -> str:
    """The supporting subhead, prefixed with who is asking and on what.

    Kept short deliberately: the frontend collapses descriptions past 120 chars
    behind a "Show more" toggle, and the tag must be readable without expanding.
    A caller-provided description is preserved after a blank line.
    """
    binding = route.binding
    tag = f"Requested by ProviderAgent worker “{route.display_label}”"
    if binding:
        tag = f"{tag} ({binding})"
    tag += "."
    base = (description or "").strip()
    return f"{tag}\n\n{base}" if base else tag


# ---------------------------------------------------------------------------
# Delivery to the parent's UI
# ---------------------------------------------------------------------------

def _parent_refs(parent_session_id: str) -> Dict[str, Any]:
    """The parent's live callback refs, or ``{}`` when it is not pooled."""
    try:
        from app.core.session_pool import get_session_pool

        state = get_session_pool().peek(parent_session_id)
    except Exception:  # noqa: BLE001 — delivery is best-effort
        logger.debug(
            "ProviderAgent approvals: pool lookup failed for parent %s",
            parent_session_id, exc_info=True,
        )
        return {}
    refs = getattr(state, "callback_refs", None)
    return refs if isinstance(refs, dict) else {}


async def deliver_to_parent(parent_session_id: str, event: Dict[str, Any]) -> None:
    """Push one permission WS frame at the parent's UI. Never raises.

    Both legs are best-effort and independent; the durable interaction row is
    what actually guarantees the prompt is answerable.
    """
    refs = _parent_refs(parent_session_id)

    origin = refs.get("broadcast_func")
    if callable(origin):
        try:
            await origin(event)
        except Exception:  # noqa: BLE001
            logger.debug(
                "ProviderAgent approvals: parent origin broadcast failed for %s",
                parent_session_id, exc_info=True,
            )

    user_id = refs.get("user_id")
    if user_id:
        try:
            from app.core.sync_engine import sync_engine

            await sync_engine.broadcast_user_permission(user_id, event)
        except Exception:  # noqa: BLE001
            logger.debug(
                "ProviderAgent approvals: parent device fan-out failed for %s",
                parent_session_id, exc_info=True,
            )


def parent_broadcast(route: WorkerApprovalRoute):
    """A ``broadcast_func`` that targets the PARENT instead of the worker.

    The worker's own broadcast reaches devices watching the WORKER session,
    which is nobody — this replaces it wholesale for the redirected prompt.
    """
    async def _broadcast(event: Dict[str, Any]) -> None:
        await deliver_to_parent(route.parent_session_id, event)

    return _broadcast


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def redirect_kwargs(
    route: WorkerApprovalRoute, kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Rewrite ``request_permission`` kwargs to raise on the parent.

    ``profile_id`` is deliberately left alone: a worker inherits the parent's
    profile, so profile rules already agree. Note the consequence of moving the
    row: ``_check_rules`` and any ``remember: "session"`` rule now key off the
    PARENT's session id for worker prompts too — which is the coherent reading of
    "the prompt lives on the parent".
    """
    out = dict(kwargs)
    out["session_id"] = route.parent_session_id
    out["request_id"] = scoped_request_id(route, kwargs.get("request_id"))
    out["broadcast_func"] = parent_broadcast(route)
    out["title"] = tagged_title(route, kwargs.get("title"))
    out["description"] = tagged_description(route, kwargs.get("description"))
    out["provider_agent"] = route.as_payload()
    return out


async def route_permission(kwargs: Dict[str, Any], request_fn: Callable) -> Any:
    """Raise a permission prompt, on the parent when the caller is a worker.

    ``request_fn`` is the caller's own ``permission_handler.request_permission``.
    A NON-worker session gets the unmodified call — the SAME function with the
    SAME kwargs — so this is a pure pass-through for every ordinary session.

    A worker's prompt is redirected and failure-contained: a vanished parent or a
    DB error raising the row resolves the worker's tool call as a DENY rather
    than propagating into the engine, where it would surface as a broken turn.
    Deny, TTL expiry and cancellation already resolve through
    ``request_permission``'s own arms.
    """
    route = get_route(kwargs.get("session_id"))
    if route is None:
        return await request_fn(**kwargs)

    try:
        return await request_fn(**redirect_kwargs(route, kwargs))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a tool call must always resolve
        logger.exception(
            "ProviderAgent approvals: redirect to parent %s failed for worker %s",
            route.parent_session_id, route.worker_session_id,
        )
        return PermissionResultDeny(message=ROUTE_FAILURE_MESSAGE)


# ---------------------------------------------------------------------------
# Teardown: sweeping a killed worker's prompts off the parent
# ---------------------------------------------------------------------------

def worker_id_of(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """The worker session id a pending-interaction row was redirected FROM.

    ``None`` for an ordinary (non-worker) prompt. Reads the same
    ``payload.provider_agent`` tag :meth:`WorkerApprovalRoute.as_payload` writes
    and ``permission_handler._build_permission_payload`` persists.
    """
    payload = (row or {}).get("payload")
    if not isinstance(payload, dict):
        return None
    tag = payload.get("provider_agent")
    if not isinstance(tag, dict):
        return None
    worker = tag.get("worker_session_id")
    return worker if isinstance(worker, str) and worker else None


def cancel_worker_prompts(worker_session_id: str, parent_session_id: str) -> int:
    """Cancel a killed worker's still-pending prompts on its PARENT. Never raises.

    Worker approvals live on the parent's session id (that is the whole point of
    this module), so killing the worker's session does NOT touch them: the row
    stays pending on a surface the user is still looking at, for a prompt whose
    asker no longer exists, until the 5-minute TTL sweeps it. Worse, the worker's
    ``await_interaction`` waiter runs in a DETACHED task that only the row
    flipping out of ``pending`` can unblock.

    Selection is by the ``payload.provider_agent.worker_session_id`` tag rather
    than by the route registry, deliberately: routes are never unregistered (the
    registry is bounded and self-trimming — see ``_ROUTES``), so a lookup there
    would tell us nothing about which ROWS belong to this worker, and a sibling
    worker prompting the same parent must be left strictly alone.

    ONE ``notify_changed`` for the whole sweep: it wakes every waiter registered
    on the parent and each re-reads its own row, so per-row notifies would be
    pure duplicate work.

    Returns the number of rows actually cancelled (0 when there were none).
    """
    if not worker_session_id or not parent_session_id:
        return 0

    from app.core import interaction_wait
    from app.core.permission_handler import PERMISSION_KIND
    from app.db import interactions

    try:
        rows = interactions.get_pending_interactions(parent_session_id)
    except Exception:  # noqa: BLE001 — teardown must never raise
        logger.warning(
            "ProviderAgent approvals: pending-prompt read failed for parent %s",
            parent_session_id, exc_info=True,
        )
        return 0

    cancelled = 0
    for row in rows:
        if row.get("kind") != PERMISSION_KIND:
            continue
        if worker_id_of(row) != worker_session_id:
            continue
        try:
            if interactions.cancel_interaction(row["id"]):
                cancelled += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "ProviderAgent approvals: cancel failed for interaction %s",
                row.get("id"), exc_info=True,
            )

    if cancelled:
        try:
            interaction_wait.notify_changed(parent_session_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "ProviderAgent approvals: notify_changed failed for parent %s",
                parent_session_id, exc_info=True,
            )
        logger.info(
            "ProviderAgent approvals: cancelled %d pending prompt(s) of worker %s "
            "on parent %s",
            cancelled, worker_session_id, parent_session_id,
        )
    return cancelled
