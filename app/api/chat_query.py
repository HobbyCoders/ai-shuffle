"""HTTP transport for the existing chat query intake (no synthetic socket)."""
from fastapi import HTTPException, Request

from app.api.auth import get_api_user_from_request
from app.api.chat_ws_handler import ChatWebSocketHandler


def _request_query_principal(request: Request):
    """The already-authenticated REST identity, in the WS intake's shape."""
    from app.api.auth import get_api_key
    from app.core import mobile_tool_identity as identity
    from app.core.config import settings

    paired = identity.request_principal(request)
    if paired:
        return paired
    user = get_api_user_from_request(request)
    if user:
        return identity.principal("api_user", user["id"], get_api_key(request))
    if identity.request_is_preview(request):
        return identity.preview_principal()
    token = getattr(request.state, "auth_token", None)
    if token == "local-owner":
        token = "local-transport:" + settings.effective_instance_id
    return identity.principal("admin", "install", token) if token else None


class HTTPQuerySender(ChatWebSocketHandler):
    """Use the same validation, attachments, placement and queueing as a send.

    A REST request has no device connection to register or write to. Query
    errors become HTTP errors so the composer can restore its pending draft.
    Session/queue broadcasts still reach all real watchers normally.
    """

    def __init__(self, request: Request, session_id: str, principal):
        user = get_api_user_from_request(request)
        user_id = ("paired:" + principal["principal_id"]
                   if isinstance(principal, dict) and principal.get("kind") == "paired"
                   else (user or {}).get("id") or "admin")
        super().__init__(None, None, user_id, (user or {}).get("id"), principal)
        # Prevent handle_query from registering a nonexistent WebSocket.
        self.current_session_id = session_id

    async def send_json(self, data: dict) -> None:
        if data.get("type") in ("error", "runner_unreachable"):
            raise HTTPException(status_code=400, detail=data.get("message") or "Could not send message")


async def submit_http_query(request: Request, session_id: str, data: dict, *, compact_first: bool) -> dict:
    # A move can start at any await. Intake treats held messages as accepted,
    # just like a normal send, rather than trying to preflight that race here.
    from app.db.async_database import run_sync
    principal = await run_sync(_request_query_principal, request)
    sender = HTTPQuerySender(request, session_id, principal)
    result = await sender.handle_query({**data, "session_id": session_id}, compact_first=compact_first)
    if not result or not result.get("turn_id"):
        raise HTTPException(status_code=409, detail="The message could not be queued. Try again.")
    return result
