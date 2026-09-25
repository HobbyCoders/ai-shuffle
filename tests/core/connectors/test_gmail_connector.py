"""GmailConnector against a fake Gmail API service object.

The connector talks to googleapiclient's ``Resource`` (``service.users()
.messages().send(...).execute()``), so the fake here reproduces that call
chain, records every request and returns realistic Gmail v1 payloads.
Upstream failures are real ``googleapiclient.errors.HttpError`` instances.
No network, no OAuth. The gateway handlers in gmail_actions.py are covered
elsewhere; this file covers gmail.py itself.
"""

import asyncio
import base64
import email
from unittest.mock import AsyncMock, MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.core.connectors import gmail as gmail_module
from app.core.connectors.base import ConnectorStatus, OutgoingMessage
from app.core.connectors.gmail import GmailConnector

CONFIG = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec", "refresh_token": "1//rt"}


def http_error(status, message="error"):
    return HttpError(httplib2.Response({"status": str(status)}),
                     ('{"error": {"code": %d, "message": "%s"}}' % (status, message)).encode())


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def full_message(mid, sender="Alice Example <alice@example.com>", subject="Quarterly numbers",
                 body="Hi there", labels=("INBOX", "UNREAD")):
    return {
        "id": mid, "threadId": f"thr-{mid}", "labelIds": list(labels), "snippet": body[:40],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": f"<{mid}@mail.example.com>"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64(body)}},
                {"mimeType": "text/html", "body": {"data": b64(f"<p>{body}</p>")}},
            ],
        },
    }


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeGmail:
    """Minimal stand-in for ``build("gmail", "v1", ...)``."""

    def __init__(self, messages=None, email_address="me@example.com"):
        self.store = {m["id"]: m for m in (messages or [])}
        self.email_address = email_address
        self.unread = [m["id"] for m in (messages or [])]
        self.sent = []
        self.modified = []
        self.list_calls = []
        self.get_calls = []
        self.fail = {}  # op -> exception (or list of exceptions, consumed in order)

    def _maybe_fail(self, op):
        err = self.fail.get(op)
        if isinstance(err, list):
            if err:
                raise err.pop(0)
            return
        if err is not None:
            raise err

    # Resource chain -----------------------------------------------------
    def users(self):
        return self

    def messages(self):
        return self

    def getProfile(self, userId):
        def run():
            self._maybe_fail("profile")
            return {"emailAddress": self.email_address, "messagesTotal": 42, "historyId": "1"}
        return _Call(run)

    def list(self, **kwargs):
        def run():
            self.list_calls.append(kwargs)
            self._maybe_fail("list")
            ids = self.unread[: kwargs.get("maxResults", 100)]
            return {"messages": [{"id": i, "threadId": f"thr-{i}"} for i in ids], "resultSizeEstimate": len(ids)}
        return _Call(run)

    def get(self, userId, id, **kwargs):
        def run():
            self.get_calls.append((id, kwargs))
            self._maybe_fail("get")
            if id not in self.store:
                raise http_error(404, "Requested entity was not found.")
            return self.store[id]
        return _Call(run)

    def send(self, userId, body):
        def run():
            self._maybe_fail("send")
            self.sent.append(body)
            return {"id": f"sent-{len(self.sent)}", "threadId": body.get("threadId", "new-thread"), "labelIds": ["SENT"]}
        return _Call(run)

    def modify(self, userId, id, body):
        def run():
            self._maybe_fail("modify")
            self.modified.append((id, body))
            return {"id": id}
        return _Call(run)

    # helpers ------------------------------------------------------------
    def sent_mime(self, i=-1):
        return email.message_from_bytes(base64.urlsafe_b64decode(self.sent[i]["raw"]))


def make_gmail(service=None, *, config=None, on_trigger=None):
    conn = GmailConnector(
        connector_id="gm-1", connector_type="gmail", config={**CONFIG, **(config or {})},
        on_incoming_message=AsyncMock(), on_permission_response=AsyncMock(), on_trigger=on_trigger,
    )
    conn._service = service
    conn._email_address = "me@example.com"
    return conn


async def send(conn, text, chat="thr-1", reply_to=None):
    return await conn.send_message(OutgoingMessage(platform_chat_id=chat, text=text, reply_to_message_id=reply_to))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

class TestParsing:
    def test_extract_body_simple_and_multipart(self):
        assert GmailConnector._extract_body({"mimeType": "text/plain", "body": {"data": b64("plain")}}) == "plain"
        assert GmailConnector._extract_body({"mimeType": "text/plain", "body": {}}) == ""
        assert GmailConnector._extract_body(full_message("m")["payload"]) == "Hi there"

    def test_extract_body_nested_and_html_only(self):
        nested = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/html", "body": {"data": b64("<b>x</b>")}},
                {"mimeType": "text/plain", "body": {"data": b64("nested plain")}},
            ]},
            {"mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
        ]}
        assert GmailConnector._extract_body(nested) == "nested plain"
        html_only = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<b>x</b>")}},
        ]}
        assert GmailConnector._extract_body(html_only) == ""

    def test_extract_body_tolerates_bad_utf8(self):
        data = base64.urlsafe_b64encode(b"caf\xe9").decode()
        assert GmailConnector._extract_body({"mimeType": "text/plain", "body": {"data": data}}) == "caf�"

    def test_headers_and_sender(self):
        headers = full_message("m")["payload"]["headers"]
        assert GmailConnector._get_header(headers, "subject") == "Quarterly numbers"
        assert GmailConnector._get_header(headers, "X-Missing") == ""
        assert GmailConnector._parse_sender('"Doe, John" <john@example.com>') == ("Doe, John", "john@example.com")
        assert GmailConnector._parse_sender("bob@example.com") == ("bob@example.com", "bob@example.com")

    def test_build_reply_threading_headers(self):
        conn = make_gmail()
        body = conn._build_reply("thr-9", "<abc@mail>", "alice@example.com", "Re: Lunch", "Sure")
        assert body["threadId"] == "thr-9"
        mime = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
        assert mime["Subject"] == "Re: Lunch"  # not doubled
        assert mime["In-Reply-To"] == "<abc@mail>" and mime["References"] == "<abc@mail>"
        assert mime["From"] == "me@example.com" and mime["To"] == "alice@example.com"
        other = conn._build_reply("t", "<x>", "a@b", "Lunch", "Sure")
        assert email.message_from_bytes(base64.urlsafe_b64decode(other["raw"]))["Subject"] == "Re: Lunch"


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------

class TestConnect:
    async def test_connect_without_watch_does_not_poll(self, monkeypatch):
        svc = FakeGmail([full_message("m1")])
        monkeypatch.setattr(gmail_module, "build", MagicMock(return_value=svc))
        conn = make_gmail()
        conn._email_address = ""
        assert await conn.connect() is True
        assert conn._email_address == "me@example.com"
        assert conn._poll_task is None and svc.list_calls == []
        gmail_module.build.assert_called_once()
        assert gmail_module.build.call_args.args == ("gmail", "v1")
        creds = gmail_module.build.call_args.kwargs["credentials"]
        assert creds.refresh_token == "1//rt" and creds.client_id == CONFIG["client_id"]

    async def test_connect_with_watch_seeds_and_polls(self, monkeypatch):
        svc = FakeGmail([full_message("m1"), full_message("m2")])
        monkeypatch.setattr(gmail_module, "build", MagicMock(return_value=svc))
        conn = make_gmail(config={"watch_inbox": "True", "labels": "INBOX, IMPORTANT ,", "max_results": "5",
                                  "poll_interval_seconds": "3600"})
        assert await conn.connect() is True
        assert conn._seen_message_ids == {"m1", "m2"}
        assert svc.list_calls[0] == {"userId": "me", "q": "is:unread", "labelIds": ["INBOX", "IMPORTANT"], "maxResults": 5}
        assert conn._poll_task is not None
        await conn.disconnect()
        assert conn._service is None and conn.status == ConnectorStatus.DISCONNECTED
        assert conn._poll_task.done()

    async def test_seed_failure_is_tolerated(self, monkeypatch):
        svc = FakeGmail()
        svc.fail["list"] = http_error(503, "Backend Error")
        monkeypatch.setattr(gmail_module, "build", MagicMock(return_value=svc))
        conn = make_gmail(config={"watch_inbox": "true", "poll_interval_seconds": "3600"})
        assert await conn.connect() is True
        assert conn._seen_message_ids == set()
        await conn.disconnect()

    async def test_revoked_refresh_token_fails_connect(self, monkeypatch):
        svc = FakeGmail()
        svc.fail["profile"] = http_error(401, "Request had invalid authentication credentials.")
        monkeypatch.setattr(gmail_module, "build", MagicMock(return_value=svc))
        conn = make_gmail()
        assert await conn.connect() is False
        assert conn.status == ConnectorStatus.ERROR
        assert "401" in conn.status_message

    async def test_missing_sdk(self, monkeypatch):
        monkeypatch.setattr(GmailConnector, "_SDK_AVAILABLE", False)
        conn = make_gmail()
        assert await conn.connect() is False
        assert "google-api-python-client" in conn.status_message


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

class TestSendMessage:
    async def test_no_service(self):
        assert await send(make_gmail(None), "hi") is False

    async def test_reply_mode_threads_to_original_sender(self):
        svc = FakeGmail([full_message("m1")])
        conn = make_gmail(svc)
        assert await send(conn, "Thanks!", chat="thr-m1", reply_to="m1") is True
        assert svc.get_calls == [("m1", {"format": "metadata", "metadataHeaders": ["From", "Subject", "Message-ID"]})]
        assert svc.sent[0]["threadId"] == "thr-m1"
        mime = svc.sent_mime()
        assert mime["To"] == "alice@example.com"
        assert mime["Subject"] == "Re: Quarterly numbers"
        assert mime["In-Reply-To"] == "<m1@mail.example.com>"
        assert mime.get_payload(decode=True).decode() == "Thanks!"

    async def test_reply_mode_original_missing_falls_back(self):
        svc = FakeGmail()
        conn = make_gmail(svc)
        assert await send(conn, "ok", chat="bob@example.com", reply_to="gone") is True
        mime = svc.sent_mime()
        assert mime["To"] == "bob@example.com" and mime["Subject"] == "Re:"
        assert mime["In-Reply-To"] is None

    @pytest.mark.xfail(strict=True, reason=(
        "bug: when the original can't be fetched, reply mode sends threadId=platform_chat_id "
        "even when that is an email address (new-message mode guards this); Gmail rejects it"
    ))
    async def test_reply_fallback_does_not_use_email_as_thread_id(self):
        svc = FakeGmail()
        conn = make_gmail(svc)
        await send(conn, "ok", chat="bob@example.com", reply_to="gone")
        assert "threadId" not in svc.sent[0]

    async def test_reply_long_text_is_chunked(self):
        svc = FakeGmail([full_message("m1")])
        conn = make_gmail(svc)
        assert await send(conn, "word " * 2000, chat="thr-m1", reply_to="m1") is True
        assert len(svc.sent) == 3
        assert all(b["threadId"] == "thr-m1" for b in svc.sent)

    async def test_new_message_with_headers_in_text(self):
        svc = FakeGmail()
        conn = make_gmail(svc)
        assert await send(conn, "To: carol@example.com\nSubject: Status\n\nAll green.", chat="thr-7") is True
        mime = svc.sent_mime()
        assert (mime["To"], mime["Subject"], mime["From"]) == ("carol@example.com", "Status", "me@example.com")
        assert mime.get_payload(decode=True).decode() == "All green."
        assert svc.sent[0]["threadId"] == "thr-7"

    async def test_new_message_to_chat_email_has_no_thread(self):
        svc = FakeGmail()
        conn = make_gmail(svc)
        assert await send(conn, "hello", chat="dave@example.com") is True
        assert svc.sent_mime()["To"] == "dave@example.com"
        assert "threadId" not in svc.sent[0]

    async def test_no_recipient_is_refused(self):
        svc = FakeGmail()
        assert await send(make_gmail(svc), "hello", chat="thr-1") is False
        assert svc.sent == []

    async def test_header_injection_is_not_sent(self):
        svc = FakeGmail()
        text = "To: carol@example.com\nBcc: attacker@evil.test\nSubject: hi\n\nbody"
        assert await send(make_gmail(svc), text) is False
        assert svc.sent == []

    @pytest.mark.parametrize("status", [400, 403, 429, 500])
    async def test_upstream_error_returns_false(self, status):
        svc = FakeGmail()
        svc.fail["send"] = http_error(status)
        assert await send(make_gmail(svc), "hi", chat="erin@example.com") is False

    async def test_timeout_returns_false(self):
        svc = FakeGmail()
        svc.fail["send"] = TimeoutError("The read operation timed out")
        assert await send(make_gmail(svc), "hi", chat="erin@example.com") is False

    @pytest.mark.xfail(strict=True, reason=(
        "bug: the To/Subject parser uses re.DOTALL with greedy (.*), so a body with a "
        "blank line is folded into the Subject and the send fails"
    ))
    async def test_multi_paragraph_body(self):
        svc = FakeGmail()
        text = "To: carol@example.com\nSubject: Update\n\nFirst paragraph.\n\nSecond paragraph."
        assert await send(make_gmail(svc), text) is True
        mime = svc.sent_mime()
        assert mime["Subject"] == "Update"
        assert mime.get_payload(decode=True).decode() == "First paragraph.\n\nSecond paragraph."


# ---------------------------------------------------------------------------
# Notifications / mark_as_read
# ---------------------------------------------------------------------------

class TestNotifications:
    async def test_formats(self):
        svc = FakeGmail()
        conn = make_gmail(svc)
        assert await conn.send_notification("ops@example.com", "session.complete",
                                            {"duration_seconds": 2.5, "total_cost": 0.0123}) is True
        assert await conn.send_notification("ops@example.com", "session.error", {}) is True
        assert await conn.send_notification("ops@example.com", "permission.requested", {"tool": "Bash"}) is True
        bodies = [svc.sent_mime(i).get_payload(decode=True).decode() for i in range(3)]
        assert bodies[0] == "[Complete] Session Complete\n\nDuration: 2.5s\nCost: $0.0123"
        assert bodies[1] == "[Error] Session Error\n\nUnknown error"
        assert bodies[2].startswith("[Permission] permission.requested")

    async def test_no_target_or_service(self):
        assert await make_gmail(FakeGmail()).send_notification("", "e", {}) is False
        assert await make_gmail(None).send_notification("a@b.c", "e", {}) is False

    async def test_permission_prompt_unsupported(self):
        assert await make_gmail(FakeGmail()).send_permission_prompt("x", MagicMock()) is False

    async def test_mark_as_read(self):
        svc = FakeGmail()
        conn = make_gmail(svc)
        await conn.mark_as_read("thr", "m9")
        assert svc.modified == [("m9", {"removeLabelIds": ["UNREAD"]})]
        svc.fail["modify"] = http_error(404)
        await conn.mark_as_read("thr", "m10")  # swallowed
        await make_gmail(None).mark_as_read("thr", "m11")  # no service: no-op


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

class TestPolling:
    async def test_new_unread_emits_trigger_once(self):
        on_trigger = AsyncMock()
        svc = FakeGmail([full_message("m1"), full_message("m2", sender="bob@example.com", subject="Hey")])
        conn = make_gmail(svc, config={"labels": "INBOX", "max_results": "10"}, on_trigger=on_trigger)
        conn._seen_message_ids = {"m1"}
        await conn._check_for_new_messages()
        await conn._check_for_new_messages()  # m2 now seen: no duplicate
        on_trigger.assert_awaited_once()
        _, trigger_id, payload = on_trigger.await_args.args
        assert trigger_id == "email_received"
        assert payload == {"from": "bob@example.com", "subject": "Hey", "snippet": "Hi there",
                           "message_id": "m2", "labels": ["INBOX", "UNREAD"]}
        # Mail content is data: it never becomes an incoming chat message.
        conn._on_incoming_message.assert_not_awaited()

    async def test_one_bad_message_does_not_block_the_rest(self):
        on_trigger = AsyncMock()
        svc = FakeGmail([full_message("m1"), full_message("m2")])
        svc.fail["get"] = [http_error(500)]
        conn = make_gmail(svc, on_trigger=on_trigger)
        await conn._check_for_new_messages()
        assert [c.args[2]["message_id"] for c in on_trigger.await_args_list] == ["m2"]
        assert conn._seen_message_ids == {"m1", "m2"}

    async def test_max_results_limits_page(self):
        svc = FakeGmail([full_message(f"m{i}") for i in range(5)])
        conn = make_gmail(svc, config={"max_results": "2"}, on_trigger=AsyncMock())
        await conn._check_for_new_messages()
        assert svc.list_calls[0]["maxResults"] == 2
        assert conn._seen_message_ids == {"m0", "m1"}

    async def test_no_service_is_noop(self):
        await make_gmail(None)._check_for_new_messages()

    async def test_repeated_failures_escalate_then_cancel(self, monkeypatch):
        from app.db import database

        update = MagicMock()
        monkeypatch.setattr(database, "update_connector_status", update)
        svc = FakeGmail()
        svc.fail["list"] = [http_error(500)] * 5 + [asyncio.CancelledError()]
        conn = make_gmail(svc, config={"poll_interval_seconds": "0"})
        await conn._poll_inbox()
        assert conn.status == ConnectorStatus.ERROR
        assert conn.status_message.startswith("Repeated poll failures:")
        update.assert_called_once()
        assert update.call_args.args[:2] == ("gm-1", "error")


# ---------------------------------------------------------------------------
# Tool shim / test_connection
# ---------------------------------------------------------------------------

class TestMisc:
    async def test_execute_tool(self):
        conn = make_gmail(FakeGmail())
        conn.execute_action = AsyncMock(return_value={"success": True})
        assert (await conn.execute_tool("gmail_read", {}))["error"] == "Unknown tool: gmail_read"
        await conn.execute_tool("gmail_send", {"to": "a@b.c"})
        conn.execute_action.assert_awaited_once_with("send_email", {"to": "a@b.c"})
        assert "gmail_invoke" in conn.get_context_instructions("x")

    async def test_test_connection(self, monkeypatch):
        monkeypatch.setattr(gmail_module, "build", MagicMock(return_value=FakeGmail(email_address="owner@example.com")))
        assert await make_gmail().test_connection() == {"success": True, "message": "Authenticated as owner@example.com"}

        bad = FakeGmail()
        bad.fail["profile"] = http_error(403, "insufficient scopes")
        monkeypatch.setattr(gmail_module, "build", MagicMock(return_value=bad))
        res = await make_gmail().test_connection()
        assert res["success"] is False and "403" in res["message"]

        monkeypatch.setattr(gmail_module, "GMAIL_AVAILABLE", False)
        res = await make_gmail().test_connection()
        assert res["success"] is False and "not installed" in res["message"]
