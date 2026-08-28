"""GmailConnector — Gmail integration via Google API Python client."""

import asyncio
import base64
import logging
import re
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Set

from app.core.connectors.base import (
    BaseConnector,
    ConnectorCapabilities,
    ConnectorStatus,
    IncomingMessage,
    IncomingMessageCallback,
    OutgoingMessage,
    PermissionPrompt,
    PermissionResponseCallback,
    TriggerCallback,
    TriggerDef,
    chunk_text,
)
from app.core.connectors import gmail_actions

logger = logging.getLogger(__name__)

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False


class GmailConnector(BaseConnector):
    """
    Connector for Gmail using the Google API Python client.

    Uses OAuth2 refresh token credentials to authenticate with the
    Gmail API.  Incoming emails are discovered by polling for unread
    messages.  Outgoing messages are sent via the Gmail API with
    proper threading support.

    Config keys:
        client_id (str): Google OAuth2 client ID.
        client_secret (str): Google OAuth2 client secret.
        refresh_token (str): Google OAuth2 refresh token.
        labels (str, optional): Comma-separated label IDs to poll.
            Defaults to "INBOX".
        max_results (str, optional): Maximum messages per poll.
            Defaults to "10".
        poll_interval_seconds (str, optional): Seconds between polls.
            Defaults to "60".
    """

    MAX_MESSAGE_LENGTH = 4000
    CAPABILITIES = ConnectorCapabilities(
        read_receipts=True, file_upload=True, is_messaging=False,
    )

    TRIGGERS = [
        TriggerDef(
            id="email_received",
            name="Email Received",
            description="Fires when a new email is received",
            payload_schema={
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "subject": {"type": "string"},
                    "snippet": {"type": "string"},
                    "message_id": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
    ]

    # Full declarative action catalog (see gmail_actions.py), exposed through
    # the gmail_actions/gmail_invoke gateway tool pair. The legacy send_email
    # action id is preserved for existing automations.
    ACTIONS = gmail_actions.GMAIL_ACTIONS

    # No per-tool MCP registration: the action gateway replaces the legacy
    # gmail_send tool.
    TOOLS: List[Any] = []

    SUPPORTS_ACTION_GATEWAY = True

    def __init__(
        self,
        connector_id: str,
        connector_type: str,
        config: Dict[str, Any],
        on_incoming_message: IncomingMessageCallback,
        on_permission_response: PermissionResponseCallback,
        on_trigger: Optional[TriggerCallback] = None,
    ):
        super().__init__(
            connector_id, connector_type, config,
            on_incoming_message, on_permission_response,
            on_trigger=on_trigger,
        )
        self._service: Optional[Any] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._seen_message_ids: Set[str] = set()
        self._email_address: str = ""
        self._last_thread_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_credentials(self) -> Any:
        """Build Google OAuth2 credentials from config."""
        return Credentials(
            token=None,
            refresh_token=self.config["refresh_token"],
            client_id=self.config["client_id"],
            client_secret=self.config["client_secret"],
            token_uri="https://oauth2.googleapis.com/token",
        )

    def _build_service(self, creds: Any) -> Any:
        """Build the Gmail API service client."""
        return build("gmail", "v1", credentials=creds)

    @staticmethod
    def _extract_body(payload: Dict[str, Any]) -> str:
        """Extract text/plain body from a Gmail message payload.

        Handles both simple and multipart message structures by
        recursively searching for the first ``text/plain`` part.
        """
        # Simple (non-multipart) message
        mime_type = payload.get("mimeType", "")
        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return ""

        # Multipart — recurse into parts
        parts = payload.get("parts", [])
        for part in parts:
            part_mime = part.get("mimeType", "")
            if part_mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            # Nested multipart
            if part_mime.startswith("multipart/"):
                result = GmailConnector._extract_body(part)
                if result:
                    return result

        return ""

    @staticmethod
    def _get_header(headers: List[Dict[str, str]], name: str) -> str:
        """Get a header value by name from Gmail message headers."""
        for header in headers:
            if header.get("name", "").lower() == name.lower():
                return header.get("value", "")
        return ""

    @staticmethod
    def _parse_sender(from_header: str) -> tuple:
        """Parse a From header into (name, email).

        Handles formats like:
            "John Doe <john@example.com>"
            "john@example.com"
        """
        match = re.match(r"^(.+?)\s*<([^>]+)>$", from_header)
        if match:
            name = match.group(1).strip().strip('"')
            email = match.group(2).strip()
            return name, email
        # Bare email address
        return from_header.strip(), from_header.strip()

    def _build_reply(
        self,
        thread_id: str,
        message_id: str,
        to: str,
        subject: str,
        body: str,
    ) -> Dict[str, Any]:
        """Build a reply MIME message with proper threading headers.

        Returns a dict suitable for ``users().messages().send()``.
        """
        mime_msg = MIMEText(body)
        mime_msg["To"] = to
        mime_msg["From"] = self._email_address
        mime_msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
        mime_msg["In-Reply-To"] = message_id
        mime_msg["References"] = message_id

        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("ascii")
        return {"raw": raw, "threadId": thread_id}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    _SDK_AVAILABLE = GMAIL_AVAILABLE
    _SDK_NAME = "google-api-python-client and google-auth"

    async def _do_connect(self) -> None:
        """Build credentials, initialise the Gmail service, and start polling."""
        creds = self._build_credentials()
        self._service = self._build_service(creds)

        # Verify credentials by fetching user profile
        profile = self._service.users().getProfile(userId="me").execute()
        self._email_address = profile.get("emailAddress", "")
        logger.info(
            "GmailConnector %s authenticated as %s",
            self.connector_id, self._email_address,
        )

        # Seed _seen_message_ids with current inbox messages to avoid
        # processing old emails on first poll
        await self._seed_seen_messages()

        # Start the polling background task
        self._poll_task = asyncio.create_task(self._poll_inbox())

    async def _seed_seen_messages(self) -> None:
        """Populate _seen_message_ids with current unread message IDs."""
        try:
            labels = self.config.get("labels", "INBOX").split(",")
            label_ids = [lbl.strip() for lbl in labels if lbl.strip()]
            max_results = int(self.config.get("max_results", "10"))

            response = self._service.users().messages().list(
                userId="me",
                q="is:unread",
                labelIds=label_ids,
                maxResults=max_results,
            ).execute()

            messages = response.get("messages", [])
            for msg in messages:
                self._seen_message_ids.add(msg["id"])

            logger.debug(
                "GmailConnector %s seeded %d existing message IDs",
                self.connector_id, len(messages),
            )
        except Exception as exc:
            logger.warning(
                "GmailConnector %s failed to seed seen messages: %s",
                self.connector_id, exc,
            )

    async def disconnect(self) -> None:
        """Cancel the polling task and mark as disconnected."""
        try:
            if self._poll_task is not None and not self._poll_task.done():
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass
        except Exception as exc:
            logger.warning("Error during GmailConnector disconnect: %s", exc)
        finally:
            self._service = None
            self._status = ConnectorStatus.DISCONNECTED
            self._status_message = None
            logger.info("GmailConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_inbox(self) -> None:
        """Background task that polls Gmail for new unread messages."""
        poll_interval = int(self.config.get("poll_interval_seconds", "60"))
        consecutive_failures = 0
        max_consecutive_failures = 5

        try:
            while True:
                try:
                    await self._check_for_new_messages()
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    logger.exception(
                        "GmailConnector %s poll error (%d/%d): %s", self.connector_id, consecutive_failures, max_consecutive_failures, exc
                    )
                    if consecutive_failures >= max_consecutive_failures:
                        self._status = ConnectorStatus.ERROR
                        self._status_message = f"Repeated poll failures: {exc}"
                        logger.error("GmailConnector %s entering ERROR state after %d consecutive failures", self.connector_id, consecutive_failures)
                        try:
                            from app.db import database
                            from app.db.async_database import run_sync
                            await run_sync(database.update_connector_status, self.connector_id, "error", error=str(exc))
                        except Exception:
                            logger.debug("Failed to update DB status", exc_info=True)
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            logger.info("GmailConnector %s polling cancelled", self.connector_id)
        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception("GmailConnector %s polling fatal error", self.connector_id)

    async def _check_for_new_messages(self) -> None:
        """List unread messages and process any new ones."""
        if self._service is None:
            return

        labels = self.config.get("labels", "INBOX").split(",")
        label_ids = [lbl.strip() for lbl in labels if lbl.strip()]
        max_results = int(self.config.get("max_results", "10"))

        response = self._service.users().messages().list(
            userId="me",
            q="is:unread",
            labelIds=label_ids,
            maxResults=max_results,
        ).execute()

        messages = response.get("messages", [])

        for msg_stub in messages:
            msg_id = msg_stub["id"]
            if msg_id in self._seen_message_ids:
                continue

            self._seen_message_ids.add(msg_id)

            try:
                await self._process_message(msg_id)
            except Exception as exc:
                logger.exception(
                    "GmailConnector %s error processing message %s: %s",
                    self.connector_id, msg_id, exc,
                )

    async def _process_message(self, msg_id: str) -> None:
        """Fetch a full message and emit an IncomingMessage."""
        msg_data = self._service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        payload = msg_data.get("payload", {})
        headers = payload.get("headers", [])

        from_header = self._get_header(headers, "From")
        subject = self._get_header(headers, "Subject")
        sender_name, sender_email = self._parse_sender(from_header)

        # Extract body — fall back to snippet if no text/plain part
        body = self._extract_body(payload)
        if not body:
            body = msg_data.get("snippet", "")

        thread_id = msg_data.get("threadId", msg_id)
        self._last_thread_id = thread_id

        text = f"From: {from_header}\nSubject: {subject}\n\n{body}"

        msg = IncomingMessage(
            connector_id=self.connector_id,
            platform_chat_id=thread_id,
            platform_user_id=sender_email,
            platform_user_name=sender_name,
            text=text,
            platform_message_id=msg_id,
            metadata={
                "thread_id": thread_id,
                "message_id": msg_id,
                "subject": subject,
            },
        )
        await self._on_incoming_message(msg)

        await self.emit_trigger("email_received", {
            "from": from_header,
            "subject": subject,
            "snippet": msg_data.get("snippet", ""),
            "message_id": msg_id,
            "labels": msg_data.get("labelIds", []),
        })

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send an email via the Gmail API.

        If ``reply_to_message_id`` is set, sends as a threaded reply.
        Otherwise, attempts to parse "To: ...\\nSubject: ...\\n\\n{body}"
        from the text, or sends directly to ``platform_chat_id`` as a
        new message.
        """
        if self._service is None:
            logger.error("Cannot send message: Gmail service is not initialised")
            return False

        try:
            thread_id = message.platform_chat_id
            text = message.text

            # --- Reply mode ---
            if message.reply_to_message_id:
                # Fetch the original message to get threading headers
                try:
                    original = self._service.users().messages().get(
                        userId="me", id=message.reply_to_message_id, format="metadata",
                        metadataHeaders=["From", "Subject", "Message-ID"],
                    ).execute()
                    orig_headers = original.get("payload", {}).get("headers", [])
                    orig_from = self._get_header(orig_headers, "From")
                    orig_subject = self._get_header(orig_headers, "Subject")
                    orig_message_id = self._get_header(orig_headers, "Message-ID")

                    _, to_email = self._parse_sender(orig_from)
                except Exception as exc:
                    logger.warning(
                        "GmailConnector: could not fetch original message for reply: %s",
                        exc,
                    )
                    to_email = thread_id
                    orig_subject = ""
                    orig_message_id = ""

                for chunk in chunk_text(text, self.MAX_MESSAGE_LENGTH):
                    if orig_message_id:
                        body = self._build_reply(
                            thread_id=thread_id,
                            message_id=orig_message_id,
                            to=to_email,
                            subject=orig_subject,
                            body=chunk,
                        )
                    else:
                        mime_msg = MIMEText(chunk)
                        mime_msg["To"] = to_email
                        mime_msg["From"] = self._email_address
                        mime_msg["Subject"] = (
                            f"Re: {orig_subject}" if orig_subject else "Re:"
                        )
                        raw = base64.urlsafe_b64encode(
                            mime_msg.as_bytes()
                        ).decode("ascii")
                        body = {"raw": raw, "threadId": thread_id}

                    self._service.users().messages().send(
                        userId="me", body=body
                    ).execute()

                return True

            # --- New message mode ---
            # Try to parse "To: ...\nSubject: ...\n\n{body}" from text
            to_addr: Optional[str] = None
            subject: str = ""
            email_body: str = text

            match = re.match(
                r"^To:\s*(.+)\nSubject:\s*(.*)\n\n(.*)",
                text,
                re.DOTALL,
            )
            if match:
                to_addr = match.group(1).strip()
                subject = match.group(2).strip()
                email_body = match.group(3)
            else:
                # platform_chat_id might be an email address
                if "@" in thread_id:
                    to_addr = thread_id
                    subject = ""
                else:
                    logger.error(
                        "GmailConnector: cannot determine recipient from "
                        "platform_chat_id=%s and message text",
                        thread_id,
                    )
                    return False

            for chunk in chunk_text(email_body, self.MAX_MESSAGE_LENGTH):
                mime_msg = MIMEText(chunk)
                mime_msg["To"] = to_addr
                mime_msg["From"] = self._email_address
                mime_msg["Subject"] = subject

                raw = base64.urlsafe_b64encode(
                    mime_msg.as_bytes()
                ).decode("ascii")
                send_body: Dict[str, Any] = {"raw": raw}
                # If thread_id looks like a Gmail thread ID, include it
                if thread_id and "@" not in thread_id:
                    send_body["threadId"] = thread_id

                self._service.users().messages().send(
                    userId="me", body=send_body
                ).execute()

            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Gmail message to %s: %s",
                message.platform_chat_id, exc,
            )
            return False

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Gmail does not support interactive permission prompts."""
        return False

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a notification email to the last active thread."""
        if self._service is None:
            logger.error("Cannot send notification: Gmail service is not initialised")
            return False

        try:
            indicators = {
                "session.started": "[Started]",
                "session.complete": "[Complete]",
                "session.error": "[Error]",
                "permission.requested": "[Permission]",
            }
            label = indicators.get(event_type, f"[{event_type}]")

            if event_type == "session.complete":
                duration = data.get("duration_seconds", 0)
                cost = data.get("total_cost", 0)
                text = (
                    f"{label} Session Complete\n\n"
                    f"Duration: {duration:.1f}s\n"
                    f"Cost: ${cost:.4f}"
                )
            elif event_type == "session.error":
                error_msg = data.get("error_message", "Unknown error")
                text = f"{label} Session Error\n\n{error_msg}"
            else:
                text = f"{label} {event_type}\n\n{data}"

            # Use the provided chat_id (thread) or fall back to last active
            thread_id = platform_chat_id or self._last_thread_id
            if not thread_id:
                logger.warning(
                    "GmailConnector: no thread ID available for notification"
                )
                return False

            outgoing = OutgoingMessage(
                platform_chat_id=thread_id,
                text=text,
                reply_to_message_id=None,
            )
            return await self.send_message(outgoing)

        except Exception as exc:
            logger.exception(
                "Failed to send Gmail notification to %s: %s",
                platform_chat_id, exc,
            )
            return False

    # ------------------------------------------------------------------
    # Mark as read
    # ------------------------------------------------------------------

    async def mark_as_read(
        self, platform_chat_id: str, platform_message_id: str
    ) -> None:
        """Mark an email as read by removing the UNREAD label."""
        if self._service is None:
            return

        try:
            self._service.users().messages().modify(
                userId="me",
                id=platform_message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception as exc:
            logger.warning(
                "GmailConnector: failed to mark message %s as read: %s",
                platform_message_id, exc,
            )

    # ------------------------------------------------------------------
    # Actions & Tools
    # ------------------------------------------------------------------

    async def execute_action(
        self,
        action_id: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a catalog action (see gmail_actions.py)."""
        return await gmail_actions.execute(self, action_id, params, context)

    def get_context_instructions(self, platform_chat_id: str) -> Optional[str]:
        return (
            "GMAIL TOOLS: Call gmail_actions to discover what you can do "
            "(send/reply/draft email, search and read mail, manage labels, "
            "mark read/unread, archive, trash/untrash) with each action's "
            "input schema, then call gmail_invoke with {action, params} to "
            "act. Use Gmail query syntax with search_messages (e.g. "
            "'from:alice is:unread newer_than:7d'). trash_message is "
            "reversible via untrash_message; nothing permanently deletes mail."
        )

    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a Gmail tool call by routing to the appropriate action."""
        if tool_id != "gmail_send":
            return {"success": False, "error": f"Unknown tool: {tool_id}"}

        return await self.execute_action("send_email", params)

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        """Validate credentials by building the service and fetching the profile."""
        if not GMAIL_AVAILABLE:
            return {
                "success": False,
                "message": "google-api-python-client and google-auth are not installed",
            }

        try:
            creds = self._build_credentials()
            service = self._build_service(creds)
            profile = service.users().getProfile(userId="me").execute()
            email = profile.get("emailAddress", "Unknown")
            return {
                "success": True,
                "message": f"Authenticated as {email}",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}
