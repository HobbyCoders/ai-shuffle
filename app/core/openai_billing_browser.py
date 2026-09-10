"""Read OpenAI credit balances using provider-owned Platform browser tabs."""

import asyncio
import base64
import contextlib
import json
import math
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import websockets

from app.core import browser_panel_registry as registry
from app.core import native_chrome

BILLING_URL = "https://platform.openai.com/settings/organization/billing/overview"
CREDIT_URL = "https://api.openai.com/v1/dashboard/billing/credit_grants"
LOGIN_TIMEOUT = 600
REFRESH_TIMEOUT = 20
OBSERVATION_MAX_AGE = 120_000
_MAX_PENDING_INPUTS = 32
_CDP_REPLY_TIMEOUT = 8
_RAW_FIELDS = ("total_granted", "total_used", "total_available")


class BillingBrowserError(Exception):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


def _now() -> int:
    return int(time.time() * 1000)


def _organization(headers: dict) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == "openai-organization":
            if isinstance(value, str) and re.fullmatch(r"org-[A-Za-z0-9_-]{1,180}", value):
                return value
    return None


def _observation(payload: object, organization_id: str) -> dict:
    from app.core.provider_usage import normalize_openai_credit_grants

    if not isinstance(payload, dict):
        raise BillingBrowserError("billing_invalid_response")
    value = payload.get("total_available")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BillingBrowserError("billing_invalid_response")
    raw = {"source": "browser_platform", "organization_id": organization_id}
    for name in _RAW_FIELDS:
        value = payload.get(name)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise BillingBrowserError("billing_invalid_response")
            raw[name] = value
    credits = normalize_openai_credit_grants(raw)
    if not credits:
        raise BillingBrowserError("billing_invalid_response")
    return {"organization_id": organization_id, "credits": credits, "raw": raw, "fetched_at": _now()}


@dataclass
class _Session:
    provider_id: str
    flow_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "pending"
    error_kind: Optional[str] = None
    observation: Optional[dict] = None
    observation_id: Optional[str] = None
    expected_org: Optional[str] = None
    target_id: Optional[str] = None
    ws: object = None
    reader: Optional[asyncio.Task] = None
    expiry: Optional[asyncio.Task] = None
    body_tasks: set = field(default_factory=set)
    input_tasks: set = field(default_factory=set)
    replies: dict = field(default_factory=dict)
    requests: dict = field(default_factory=dict)
    extra_orgs: dict = field(default_factory=dict)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    command_id: int = 0
    generation: int = 0
    request_sequence: int = 0
    platform_page: bool = False
    closed: bool = False


class OpenAIBillingBrowser:
    def __init__(self):
        self._sessions: dict[str, _Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def operation_lock(self, provider_id: str) -> asyncio.Lock:
        return self._locks.setdefault(provider_id, asyncio.Lock())

    @staticmethod
    def panel_key(provider_id: str) -> str:
        return "openai-billing:" + provider_id

    def status(self, provider_id: str) -> dict:
        session = self._sessions.get(provider_id)
        observation = session.observation if session else None
        return {
            "status": session.status if session else "not_connected",
            "flow_id": session.flow_id if session else None,
            "observation_id": session.observation_id if session else None,
            "organization_id": observation.get("organization_id") if observation else None,
            "credits": deepcopy(observation.get("credits")) if observation else None,
            "fetched_at": observation.get("fetched_at") if observation else None,
            "error_kind": session.error_kind if session else None,
        }

    def _current(self, session: _Session) -> bool:
        return not session.closed and self._sessions.get(session.provider_id) is session

    def _fail(self, session: _Session, kind: str) -> None:
        if not self._current(session):
            return
        session.error_kind = kind
        if not session.expected_org and kind == "billing_sign_in_required":
            session.status = "pending"
        else:
            session.status = "unavailable" if kind == "billing_browser_unavailable" else "reconnect_required"
        session.changed.set()

    async def _send(self, session: _Session, method: str, params: dict | None = None) -> tuple[int, asyncio.Future]:
        async with session.send_lock:
            session.command_id += 1
            command_id = session.command_id
            future = asyncio.get_running_loop().create_future()
            session.replies[command_id] = future
            try:
                await session.ws.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
            except (Exception, asyncio.CancelledError) as error:
                session.replies.pop(command_id, None)
                future.cancel()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise BillingBrowserError("billing_browser_unavailable") from None
        return command_id, future

    async def _reply(self, session: _Session, command_id: int, future: asyncio.Future) -> dict:
        try:
            return await asyncio.wait_for(future, _CDP_REPLY_TIMEOUT)
        except asyncio.TimeoutError:
            raise BillingBrowserError("billing_timeout") from None
        finally:
            session.replies.pop(command_id, None)

    async def _call(self, session: _Session, method: str, params: dict | None = None) -> dict:
        command_id, future = await self._send(session, method, params)
        return await self._reply(session, command_id, future)

    async def _input_reply(self, session: _Session, command_id: int, future: asyncio.Future) -> None:
        try:
            await self._reply(session, command_id, future)
        except BillingBrowserError:
            # A late reply must not invalidate a confirmed connection.
            if session.status in {"pending", "ready"} and session.expected_org is None:
                self._fail(session, "billing_browser_unavailable")

    async def _send_input(self, session: _Session, method: str, params: dict) -> None:
        # Keep wire order, but do not serialize input on renderer ACKs. Mouse
        # moves arrive every animation frame and otherwise hold up clicks/keys.
        # Bound outstanding replies so an unresponsive renderer cannot grow
        # an unlimited queue. Replies still have timeouts and propagate failure.
        while len(session.input_tasks) >= _MAX_PENDING_INPUTS:
            await asyncio.wait(session.input_tasks, return_when=asyncio.FIRST_COMPLETED)
            if not self._current(session) or session.status not in {"pending", "ready"}:
                raise BillingBrowserError("billing_browser_unavailable")
        command_id, future = await self._send(session, method, params)
        if future.done():
            await self._reply(session, command_id, future)
            return
        task = asyncio.create_task(self._input_reply(session, command_id, future))
        session.input_tasks.add(task)

        def finished(task: asyncio.Task) -> None:
            session.input_tasks.discard(task)
            session.replies.pop(command_id, None)
            # Also clean up if disposal cancels the task before it starts.
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

        task.add_done_callback(finished)

    async def dispatch_input(self, provider_id: str, flow_id: str, command: dict) -> None:
        """Send translated login controls to this flow's owned CDP session.

        The shared screencast hub is read-only. A viewer from an old flow must
        never send input to a replacement tab or a background credit refresh.
        """
        async with self.operation_lock(provider_id):
            session = self._sessions.get(provider_id)
            if (
                session is None or not self._current(session)
                or session.flow_id != flow_id
                or session.status not in {"pending", "ready"}
                or session.expected_org is not None or session.ws is None
            ):
                raise BillingBrowserError("billing_browser_unavailable")
            method = command.get("method")
            if method not in {
                "Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText",
                "Emulation.setDeviceMetricsOverride", "Page.reload", "Page.stopLoading",
                "Page.getNavigationHistory",
            }:
                raise BillingBrowserError("billing_invalid_input")
            if method.startswith("Input."):
                await self._send_input(session, method, command.get("params", {}))
                return
            result = await self._call(session, method, command.get("params", {}))
            if method == "Page.getNavigationHistory":
                offset = command.get("__history_offset__")
                if offset not in (-1, 1):
                    raise BillingBrowserError("billing_invalid_input")
                entries = result.get("entries", [])
                index = result.get("currentIndex", 0) + offset
                if 0 <= index < len(entries):
                    await self._call(session, "Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})

    async def _open(self, session: _Session) -> None:
        target = await native_chrome.create_page("about:blank")
        if not target or not target.get("id"):
            raise BillingBrowserError("billing_browser_unavailable")
        session.target_id = target["id"]
        url = target.get("webSocketDebuggerUrl", "")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/devtools/page/" + session.target_id
            or parsed.port != native_chrome._cdp_port()
        ):
            raise BillingBrowserError("billing_browser_unavailable")
        session.ws = await websockets.connect(url, max_size=2 * 1024 * 1024, open_timeout=5, close_timeout=2)
        session.reader = asyncio.create_task(self._read(session))
        await self._call(session, "Network.enable", {"maxResourceBufferSize": 1024 * 1024})
        await self._call(session, "Page.enable")
        registry.register(self.panel_key(session.provider_id), session.target_id, None, BILLING_URL)
        await self._navigate(session)

    async def _navigate(self, session: _Session) -> None:
        session.generation += 1
        session.requests.clear()
        session.extra_orgs.clear()
        session.changed.clear()
        session.error_kind = None
        result = await self._call(session, "Page.navigate", {"url": BILLING_URL})
        if result.get("errorText"):
            raise BillingBrowserError("billing_browser_unavailable")

    async def start(self, provider_id: str) -> dict:
        await self.close(provider_id)
        session = _Session(provider_id)
        self._sessions[provider_id] = session
        try:
            async with asyncio.timeout(REFRESH_TIMEOUT):
                await self._open(session)
        except asyncio.CancelledError:
            await self.close(provider_id)
            raise
        except Exception:
            await self._dispose(session)
            self._fail(session, "billing_browser_unavailable")
            raise BillingBrowserError("billing_browser_unavailable") from None
        session.expiry = asyncio.create_task(self._expire(session))
        return self.status(provider_id)

    async def _expire(self, session: _Session) -> None:
        await asyncio.sleep(LOGIN_TIMEOUT)
        if self._current(session) and not session.expected_org:
            self._fail(session, "billing_timeout")
            await self._dispose(session)

    def confirm(self, provider_id: str, flow_id: str, observation_id: str, organization_id: str) -> dict:
        session = self._sessions.get(provider_id)
        if (
            not session or session.status != "ready" or session.flow_id != flow_id
            or session.observation_id != observation_id or not session.observation
            or session.observation["organization_id"] != organization_id
        ):
            raise BillingBrowserError("billing_flow_changed")
        if _now() - session.observation["fetched_at"] > OBSERVATION_MAX_AGE:
            self._fail(session, "billing_stale_observation")
            raise BillingBrowserError("billing_stale_observation")
        session.expected_org = organization_id
        session.status = "connected"
        session.error_kind = None
        if session.expiry:
            session.expiry.cancel()
        return deepcopy(session.observation)

    async def refresh(self, provider_id: str, expected_org_id: str) -> dict:
        session = self._sessions.get(provider_id)
        if session and not session.expected_org and session.status in {"pending", "ready"}:
            raise BillingBrowserError("billing_flow_changed")
        try:
            async with asyncio.timeout(REFRESH_TIMEOUT):
                if session is None or session.closed or session.reader is None or session.reader.done():
                    await self.close(provider_id)
                    session = _Session(provider_id, expected_org=expected_org_id, status="connected")
                    self._sessions[provider_id] = session
                    await self._open(session)
                else:
                    session.expected_org = expected_org_id
                    await self._navigate(session)
                while not session.changed.is_set():
                    await session.changed.wait()
                if session.error_kind:
                    raise BillingBrowserError(session.error_kind)
                if not session.observation or session.observation["organization_id"] != expected_org_id:
                    raise BillingBrowserError("billing_organization_mismatch")
                return deepcopy(session.observation)
        except BillingBrowserError as error:
            if session:
                self._fail(session, error.kind)
            raise
        except asyncio.TimeoutError:
            if session:
                self._fail(session, "billing_timeout")
            raise BillingBrowserError("billing_timeout") from None
        except Exception:
            if session:
                self._fail(session, "billing_browser_unavailable")
            raise BillingBrowserError("billing_browser_unavailable") from None

    async def _read(self, session: _Session) -> None:
        try:
            async for raw in session.ws:
                message = json.loads(raw)
                if "id" in message:
                    future = session.replies.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(BillingBrowserError("billing_browser_unavailable"))
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    self._event(session, message.get("method"), message.get("params") or {})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            for future in tuple(session.replies.values()):
                if not future.done():
                    future.set_exception(BillingBrowserError("billing_browser_unavailable"))
            if not asyncio.current_task().cancelling():
                self._fail(session, "billing_browser_unavailable")

    def _event(self, session: _Session, method: str, params: dict) -> None:
        if not self._current(session):
            return
        if method == "Page.frameNavigated" and not params.get("frame", {}).get("parentId"):
            url = urlsplit(params.get("frame", {}).get("url", ""))
            session.platform_page = url.scheme == "https" and url.hostname == "platform.openai.com"
            return
        request_id = params.get("requestId")
        if not request_id:
            return
        if method == "Network.requestWillBeSent":
            request = params.get("request", {})
            if (
                not session.platform_page or request.get("method") != "GET"
                or request.get("url") != CREDIT_URL or params.get("redirectResponse")
            ):
                session.requests.pop(request_id, None)
                return
            if len(session.requests) >= 32:
                session.requests.clear()
            session.request_sequence += 1
            session.requests[request_id] = {
                "org": _organization(request.get("headers") or {}), "status": None,
                "generation": session.generation, "sequence": session.request_sequence,
            }
        elif method == "Network.requestWillBeSentExtraInfo":
            org = _organization(params.get("headers") or {})
            if org:
                if len(session.extra_orgs) >= 128:
                    session.extra_orgs.clear()
                session.extra_orgs[request_id] = org
        elif method == "Network.responseReceived" and request_id in session.requests:
            response = params.get("response") or {}
            if response.get("url") != CREDIT_URL:
                session.requests.pop(request_id, None)
                return
            session.requests[request_id]["status"] = response.get("status")
        elif method == "Network.loadingFinished" and request_id in session.requests:
            record = session.requests.pop(request_id)
            record["org"] = record["org"] or session.extra_orgs.pop(request_id, None)
            task = asyncio.create_task(self._capture(session, request_id, record))
            session.body_tasks.add(task)
            task.add_done_callback(session.body_tasks.discard)
        elif method == "Network.loadingFailed" and request_id in session.requests:
            record = session.requests.pop(request_id)
            if self._current_capture(session, record):
                self._fail(session, "billing_browser_unavailable")

    def _current_capture(self, session: _Session, record: dict) -> bool:
        return (self._current(session) and record["generation"] == session.generation
                and record["sequence"] == session.request_sequence)

    async def _capture(self, session: _Session, request_id: str, record: dict) -> None:
        try:
            if not self._current_capture(session, record):
                return
            if record["status"] in {401, 403}:
                raise BillingBrowserError("billing_sign_in_required")
            if record["status"] != 200:
                raise BillingBrowserError("billing_invalid_response")
            org = record["org"]
            if not org:
                raise BillingBrowserError("billing_organization_required")
            if session.expected_org and session.expected_org != org:
                raise BillingBrowserError("billing_organization_mismatch")
            result = await self._call(session, "Network.getResponseBody", {"requestId": request_id})
            body = result.get("body", "")
            if not isinstance(body, str) or len(body) > 1024 * 1024:
                raise BillingBrowserError("billing_invalid_response")
            if result.get("base64Encoded"):
                body = base64.b64decode(body, validate=True).decode("utf-8")
            observation = _observation(json.loads(body), org)
            if not self._current_capture(session, record):
                return
            if session.expected_org and session.expected_org != org:
                raise BillingBrowserError("billing_organization_mismatch")
            session.observation = observation
            session.observation_id = uuid.uuid4().hex
            session.error_kind = None
            session.status = "connected" if session.expected_org else "ready"
            session.changed.set()
        except BillingBrowserError as error:
            if self._current_capture(session, record):
                self._fail(session, error.kind)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._current_capture(session, record):
                self._fail(session, "billing_invalid_response")

    async def _dispose(self, session: _Session) -> None:
        tasks = [session.reader, session.expiry, *session.body_tasks, *session.input_tasks]
        tasks = [task for task in tasks if task and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        session.reader = None
        if session.ws:
            with contextlib.suppress(Exception):
                await session.ws.close()
            session.ws = None
        if session.target_id:
            with contextlib.suppress(Exception):
                await native_chrome.cdp_get("/json/close/" + session.target_id)
            session.target_id = None
        key = self.panel_key(session.provider_id)
        registry.register(key, None, None, None)
        registry.clear(key)

    async def close(self, provider_id: str) -> None:
        session = self._sessions.pop(provider_id, None)
        if session:
            session.closed = True
            await self._dispose(session)

    async def reconcile(self, active_ids: set[str]) -> None:
        for provider_id in set(self._sessions) - set(active_ids):
            async with self.operation_lock(provider_id):
                await self.close(provider_id)

    async def shutdown(self) -> None:
        for provider_id in list(self._sessions):
            async with self.operation_lock(provider_id):
                await self.close(provider_id)


openai_billing_browser = OpenAIBillingBrowser()
