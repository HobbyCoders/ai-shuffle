"""MSTeamsConnector — Microsoft Teams integration via Bot Framework REST API."""

import asyncio
import json
import logging
import uuid
import os
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit
from typing import Any, Dict, Optional, Tuple

import httpx

from app.core.connectors.base import (
    ActionDef,
    BaseConnector,
    ConnectorCapabilities,
    ConnectorStatus,
    IncomingMessage,
    OutgoingMessage,
    PermissionPrompt,
    IncomingMessageCallback,
    PermissionResponseCallback,
    TriggerDef,
    chunk_text,
)
from app.core.connectors import msteams_actions

logger = logging.getLogger(__name__)

# Bot Framework OAuth endpoint
BF_LOGIN_URL = "https://login.microsoftonline.com"
BF_API_URL = "https://smba.trafficmanager.net/teams"


def validate_service_url(url: str) -> None:
    """Only public-cloud Teams Connector endpoints may receive bot tokens."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {
            "smba.trafficmanager.net", "smba.infra.teams.microsoft.com"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or "\\" in url
            or any(ord(char) < 33 for char in url)):
        raise ValueError("Unsupported Teams service URL")


class MSTeamsConnector(BaseConnector):
    """
    Connector for Microsoft Teams using the Bot Framework REST API.

    Uses OAuth2 client credentials flow (app_id + app_password) to
    authenticate with the Bot Framework and send proactive messages.
    Incoming messages are received via a webhook endpoint that the
    connector exposes.

    Config keys:
        app_id (str): Azure Bot / App Registration Application (Client) ID.
        app_password (str): Azure App Registration Client Secret.
        tenant_id (str, optional): Azure Tenant ID. Defaults to "botframework.com".
        _state_path (str, internal): Host-provided reference store, not user config.
        allowed_user_ids (list[str], optional): If set, only messages from
            these AAD user IDs will be processed.
    """

    MAX_MESSAGE_LENGTH = 4000
    CAPABILITIES = ConnectorCapabilities(
        buttons=True, rich_text=True,
    )

    TRIGGERS = [
        TriggerDef(
            id="message_received",
            name="Message Received",
            description="Fires when a new message is received",
            payload_schema={
                "type": "object",
                "properties": {
                    "platform_chat_id": {"type": "string"},
                    "platform_user_id": {"type": "string"},
                    "platform_user_name": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        ),
    ]

    # Full declarative action catalog (see msteams_actions.py). Exposed to the
    # AI through the generic msteams_actions/msteams_invoke gateway tool pair.
    ACTIONS = msteams_actions.MSTEAMS_ACTIONS

    # No per-tool MCP registration: the action gateway replaces the legacy
    # globally-exposed `msteams_send` tool.
    TOOLS: list = []

    SUPPORTS_ACTION_GATEWAY = True

    def __init__(
        self,
        connector_id: str,
        connector_type: str,
        config: Dict[str, Any],
        on_incoming_message: IncomingMessageCallback,
        on_permission_response: PermissionResponseCallback,
        **kwargs,
    ):
        super().__init__(
            connector_id, connector_type, config,
            on_incoming_message, on_permission_response,
            **kwargs,
        )
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._permission_sessions: Dict[str, str] = {}  # request_id -> session_id
        self._pending_action_approvals: Dict[str, asyncio.Future] = {}  # request_id -> Future[bool]
        # conversation_id -> ConversationReference (service_url, conversation, bot, etc.)
        self._conversation_refs: Dict[str, Dict[str, Any]] = {}
        self._token_lock = asyncio.Lock()
        self._reference_lock = asyncio.Lock()
        self._state_path = Path(config["_state_path"]) if config.get("_state_path") else None
        if self._state_path and self._state_path.exists():
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if state.get("app_id") == self._app_id and state.get("tenant_id") == self._tenant_id:
                self._conversation_refs = state["references"]

    def _save_references(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=self._state_path.parent, delete=False) as stream:
            scratch = Path(stream.name)
            json.dump({"app_id": self._app_id, "tenant_id": self._tenant_id,
                       "references": self._conversation_refs}, stream)
        try:
            os.replace(scratch, self._state_path)
        finally:
            scratch.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _app_id(self) -> str:
        return self.config["app_id"]

    @property
    def _app_password(self) -> str:
        return self.config["app_password"]

    @property
    def _tenant_id(self) -> str:
        return self.config.get("tenant_id") or "botframework.com"

    async def _ensure_token(self) -> str:
        """Serialize refreshes so simultaneous replies share the same token."""
        async with self._token_lock:
            return await self._refresh_token()

    async def _refresh_token(self) -> str:
        """Obtain or refresh the Bot Framework OAuth2 access token."""
        import time

        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        if self._tenant_id != "botframework.com":
            uuid.UUID(self._tenant_id)  # Tenant must not alter the token URL path.
        url = f"{BF_LOGIN_URL}/{self._tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._app_id,
            "client_secret": self._app_password,
            "scope": "https://api.botframework.com/.default",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, data=data)

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to get Bot Framework token: HTTP {response.status_code}"
            )

        token_data = response.json()
        self._access_token = token_data["access_token"]
        self._token_expires_at = time.time() + token_data.get("expires_in", 3600)
        return self._access_token

    def _auth_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _do_connect(self) -> None:
        """Validate credentials by obtaining a Bot Framework token."""
        await self._ensure_token()

    async def disconnect(self) -> None:
        """Mark as disconnected (webhook-based, no persistent connection)."""
        self._access_token = None
        self._token_expires_at = 0
        self._status = ConnectorStatus.DISCONNECTED
        self._status_message = None
        logger.info("MSTeamsConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def _bot_request(self, method: str, service_url: str, path: str,
                           json_body: Optional[Dict[str, Any]] = None) -> httpx.Response:
        validate_service_url(service_url)
        token = await self._ensure_token()
        url = f"{service_url.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            for attempt in range(4):
                response = await client.request(method, url, headers=self._auth_headers(token), json=json_body)
                if response.status_code == 401 and attempt == 0:
                    self._access_token = None
                    token = await self._ensure_token()
                    continue
                if response.status_code != 429 and not 500 <= response.status_code < 600:
                    return response
                if attempt == 3:
                    return response
                try:
                    delay = max(0, min(30, float(response.headers.get("Retry-After", 2 ** attempt))))
                except ValueError:
                    delay = 2 ** attempt
                await asyncio.sleep(delay)
        return response

    async def _send_activity(
        self, service_url: str, conversation_id: str, activity: Dict[str, Any]
    ) -> bool:
        """Send an activity to a Teams conversation via Bot Framework."""
        try:
            path = f"/v3/conversations/{quote(conversation_id, safe='')}/activities"
            if activity.get("replyToId"):
                path += f"/{quote(activity['replyToId'], safe='')}"
            response = await self._bot_request("POST", service_url, path, activity)

            if 200 <= response.status_code < 300:
                return True

            logger.error(
                "Teams send_activity failed (HTTP %s)", response.status_code,
            )
            return False

        except Exception as exc:
            logger.exception("Failed to send Teams activity: %s", exc)
            return False

    def _get_ref(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Look up stored conversation reference."""
        return self._conversation_refs.get(conversation_id)

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message to a Teams conversation."""
        ref = self._get_ref(message.platform_chat_id)
        if not ref:
            logger.error(
                "No conversation reference for %s — "
                "the bot must receive a message first before it can send proactively",
                message.platform_chat_id,
            )
            return False

        service_url = ref.get("service_url", BF_API_URL)

        for text in chunk_text(message.text, self.MAX_MESSAGE_LENGTH):
            activity = {
                "type": "message",
                "text": text,
                "conversation": ref["conversation"],
                "from": ref.get("bot"),
                "recipient": ref.get("user"),
                "replyToId": ref.get("activity_id"),
            }
            if not await self._send_activity(
                service_url, message.platform_chat_id, activity
            ):
                return False

        return True

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send an Adaptive Card with Approve / Deny buttons."""
        ref = self._get_ref(platform_chat_id)
        if not ref:
            logger.error(
                "No conversation reference for %s", platform_chat_id,
            )
            return False

        self._permission_sessions[prompt.request_id] = prompt.session_id
        service_url = ref.get("service_url", BF_API_URL)

        adaptive_card = {
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": "Permission Request",
                    "weight": "Bolder",
                    "size": "Medium",
                },
                {
                    "type": "TextBlock",
                    "text": f"**Tool:** {prompt.tool_name}",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "text": prompt.description,
                    "wrap": True,
                },
            ],
            "actions": [
                {
                    "type": "Action.Submit",
                    "title": "Approve",
                    "data": {
                        "action": f"perm:{prompt.request_id}:approve",
                    },
                    "style": "positive",
                },
                {
                    "type": "Action.Submit",
                    "title": "Deny",
                    "data": {
                        "action": f"perm:{prompt.request_id}:deny",
                    },
                    "style": "destructive",
                },
            ],
        }

        activity = {
            "type": "message",
            "text": f"Permission Request: {prompt.tool_name}",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": adaptive_card,
                }
            ],
        }

        return await self._send_activity(
            service_url, platform_chat_id, activity
        )

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a notification message to a Teams conversation."""
        ref = self._get_ref(platform_chat_id)
        if not ref:
            logger.error(
                "No conversation reference for %s", platform_chat_id,
            )
            return False

        service_url = ref.get("service_url", BF_API_URL)

        indicators = {
            "session.started": "\u25b6\ufe0f",
            "session.complete": "\u2705",
            "session.error": "\u274c",
            "permission.requested": "\ud83d\udd10",
        }
        icon = indicators.get(event_type, "\ud83d\udce3")

        if event_type == "session.complete":
            duration = data.get("duration_seconds", 0)
            cost = data.get("total_cost", 0)
            text = (
                f"{icon} **Session Complete**\n\n"
                f"Duration: {duration:.1f}s\n\n"
                f"Cost: ${cost:.4f}"
            )
        elif event_type == "session.error":
            error_msg = data.get("error_message", "Unknown error")
            text = f"{icon} **Session Error**\n\n{error_msg}"
        else:
            text = f"{icon} **{event_type}**\n\n{data}"

        activity = {
            "type": "message",
            "text": text,
            "textFormat": "markdown",
        }

        return await self._send_activity(
            service_url, platform_chat_id, activity
        )

    # ------------------------------------------------------------------
    # Webhook handling (inbound)
    # ------------------------------------------------------------------

    async def handle_webhook_activity(self, activity: Dict[str, Any]) -> None:
        """Internal RPC: caller MUST validate the channel JWT before dispatch.

        The public /api/v1/connectors/msteams/{id}/messages route is the sole
        ingress. Sender checks cover invokes too, before persisting references.
        """
        sender = activity.get("from", {})
        user_id = sender.get("aadObjectId") or sender.get("id", "")
        allowed = self.config.get("allowed_user_ids") or []
        if not user_id or user_id not in [str(user) for user in allowed]:
            return
        service_url = activity["serviceUrl"]
        validate_service_url(service_url)
        conversation = activity.get("conversation", {})
        conversation_id = conversation.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("Missing conversation id")
        async with self._reference_lock:
            self._conversation_refs[conversation_id] = {
                "service_url": service_url,
                "conversation": conversation,
                "bot": activity.get("recipient"),
                "user": sender,
                "activity_id": activity.get("id"),
            }
            # Persist before allowing an agent callback to reply, including after
            # worker/process restarts. Small atomic writes stay off the event loop.
            await asyncio.to_thread(self._save_references)
        if activity.get("type") == "invoke" or activity.get("value", {}).get("action", "").startswith(("perm:", "actreq:")):
            await self._handle_invoke_activity(activity)
        elif activity.get("type") == "message":
            await self._handle_message_activity(activity)

    async def _handle_message_activity(self, activity: Dict[str, Any]) -> None:
        """Process an incoming Teams message activity."""
        sender = activity.get("from", {})
        user_id = sender.get("aadObjectId") or sender.get("id", "")
        user_name = sender.get("name", user_id)

        # Filter by allowed user IDs
        allowed = self.config.get("allowed_user_ids")
        if allowed and user_id not in [str(u) for u in allowed]:
            logger.debug("Ignoring Teams message from non-allowed user %s", user_id)
            return

        conversation = activity.get("conversation", {})
        conversation_id = conversation.get("id", "")
        conversation_type = conversation.get("conversationType", "personal")

        # Strip mention tags: <at>Bot Name</at>
        text = activity.get("text", "")
        import re
        text = re.sub(r"<at>.*?</at>\s*", "", text).strip()

        msg = IncomingMessage(
            connector_id=self.connector_id,
            platform_chat_id=conversation_id,
            platform_user_id=user_id,
            platform_user_name=user_name,
            text=text,
            platform_message_id=activity.get("id"),
            metadata={
                "conversation_type": conversation_type,
                "tenant_id": conversation.get("tenantId"),
                "service_url": activity.get("serviceUrl"),
                "channel_id": activity.get("channelId"),
            },
        )
        await self._on_incoming_message(msg)

    async def _handle_invoke_activity(self, activity: Dict[str, Any]) -> None:
        """Handle invoke activities (Adaptive Card submit actions)."""
        value = activity.get("value", {})
        action_str = value.get("action", "")

        # Destructive-action approval buttons (action gateway)
        if action_str.startswith("actreq:"):
            parts = action_str.split(":")
            if len(parts) == 3:
                _, request_id, decision = parts
                fut = self._pending_action_approvals.get(request_id)
                if fut is not None and not fut.done():
                    fut.set_result(decision == "approve")
            return

        if not action_str.startswith("perm:"):
            return

        parts = action_str.split(":")
        if len(parts) != 3:
            logger.warning("Invalid Teams invoke action format: %s", action_str)
            return

        _, request_id, decision = parts
        approved = decision == "approve"

        session_id = self._permission_sessions.get(request_id, "")
        await self._on_permission_response(request_id, session_id, approved)

    # ------------------------------------------------------------------
    # Actions & Tools
    # ------------------------------------------------------------------

    async def execute_action(
        self,
        action_id: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a catalog action (see msteams_actions.py)."""
        return await msteams_actions.execute(self, action_id, params, context)

    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Legacy tool entry point — kept for back-compat with old callers."""
        if tool_id == "msteams_send":
            return await self.execute_action("send_message", params)
        return {"success": False, "error": f"Unknown tool: {tool_id}"}

    # ------------------------------------------------------------------
    # Action gateway hooks
    # ------------------------------------------------------------------

    def get_context_instructions(self, platform_chat_id: str) -> Optional[str]:
        return (
            "MICROSOFT TEAMS TOOLS: Call msteams_actions to discover everything "
            "you can do here (messages, Adaptive Cards, message edit/delete, "
            "typing indicator, member list) along with each action's input "
            "schema, then call msteams_invoke with {action, params} to act. "
            "Proactive actions only work in a conversation the bot has already "
            "received a message in. Destructive actions (message deletion) post "
            "an in-channel Adaptive Card Approve/Deny prompt before running."
        )

    async def request_action_approval(
        self,
        platform_chat_id: str,
        action: ActionDef,
        params: Dict[str, Any],
        timeout: float = 120.0,
    ) -> Tuple[bool, str]:
        """Post an Approve/Deny Adaptive Card for a destructive action."""
        ref = self._get_ref(platform_chat_id)
        if not ref:
            return False, "No conversation reference available for the approval prompt."

        request_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_action_approvals[request_id] = fut

        try:
            params_preview = json.dumps(params, default=str)[:900]
            adaptive_card = {
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "TextBlock",
                        "text": "Destructive Action Approval",
                        "weight": "Bolder",
                        "size": "Medium",
                    },
                    {
                        "type": "TextBlock",
                        "text": f"The AI wants to run **{action.name}** (`{action.id}`).",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": params_preview,
                        "wrap": True,
                    },
                ],
                "actions": [
                    {
                        "type": "Action.Submit",
                        "title": "Approve",
                        "data": {"action": f"actreq:{request_id}:approve"},
                        "style": "positive",
                    },
                    {
                        "type": "Action.Submit",
                        "title": "Deny",
                        "data": {"action": f"actreq:{request_id}:deny"},
                        "style": "destructive",
                    },
                ],
            }
            activity = {
                "type": "message",
                "text": f"Approval needed: {action.name}",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": adaptive_card,
                    }
                ],
            }
            service_url = ref.get("service_url", BF_API_URL)
            ok = await self._send_activity(service_url, platform_chat_id, activity)
            if not ok:
                return False, "Failed to post the approval card."

            try:
                approved = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                return False, f"No approval response within {timeout:.0f}s — action cancelled."
            return (True, "Approved by user.") if approved else (False, "Denied by user.")
        except Exception as exc:
            logger.exception("Failed to run Teams action approval flow")
            return False, f"Approval flow failed: {exc}"
        finally:
            self._pending_action_approvals.pop(request_id, None)

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        """Validate the app credentials by obtaining a Bot Framework token."""
        try:
            self._access_token = None
            await self._ensure_token()
            return {"success": True, "message": "Bot credentials accepted; send a Teams message to test delivery."}
        except Exception:
            return {"success": False, "message": "Bot authentication failed; check app ID, tenant and client secret."}
