"""Short-lived attestations for the native requests that produced owner replies."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any

from fluid.providers.base import TURN_MARKER_KEY, ProviderRequest, StreamChunk, WireState
from fluid.providers.openai_responses.client import OpenAIResponsesClient


@dataclass(frozen=True)
class Binding:
    provider_id: str
    account: str
    model: str


# No transcript, credentials, or attestations are written to storage. Restart,
# eviction, and expiry all mean unknown; current session settings cannot fill
# the gap. Preparation runs in a DB worker thread, settlement on the event loop.
MAX_SETTLEMENTS = 512
MAX_AGE_SECONDS = 3600
_recent: OrderedDict[tuple[str, str], tuple[float, Binding]] = OrderedDict()
_lock = Lock()


def record_settlement(session_id: str, turn_id: str, binding: Binding) -> None:
    with _lock:
        key = (session_id, turn_id)
        _recent[key] = (monotonic(), binding)
        _recent.move_to_end(key)
        while len(_recent) > MAX_SETTLEMENTS:
            _recent.popitem(last=False)


def settled_binding(session_id: str, turn_id: str) -> Binding | None:
    with _lock:
        receipt = _recent.get((session_id, turn_id))
        if receipt and monotonic() - receipt[0] <= MAX_AGE_SECONDS:
            return receipt[1]
    return None


@dataclass
class Execution:
    session_id: str
    submissions: int = 0
    native: bool = False
    uncertain: bool = False
    binding: Binding | None = None
    main_stream_completed: bool = False

    def submitted(self, session_id: str | None, *, native: bool) -> None:
        self.submissions += 1
        self.native = session_id == self.session_id and native

    def finish(self) -> Binding | None:
        if self.submissions == 1 and self.native and self.main_stream_completed and not self.uncertain:
            return self.binding
        return None


CURRENT: ContextVar[Execution | None] = ContextVar("behavioral_execution", default=None)


class SourceResponsesClient(OpenAIResponsesClient):
    """Attest the model and authenticated account at each source call's send seam."""

    def __init__(self, source_session_id: str, provider_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._source_session_id = source_session_id
        self._source_provider_id = provider_id

    def _execution(self) -> Execution | None:
        execution = CURRENT.get()
        if execution is not None and execution.session_id == self._source_session_id and execution.native:
            return execution
        return None

    async def _headers(
        self, body: dict | None = None, wire: WireState | None = None,
        request_id: str | None = None, model: str | None = None,
    ) -> dict[str, str]:
        # The base client has just loaded the current token and built the
        # headers for this POST, even after retrying or recreating its HTTP client.
        headers = await super()._headers(body, wire, request_id, model)
        execution = self._execution()
        if execution is not None:
            account = headers.get("chatgpt-account-id")
            wire_model = (body or {}).get("model")
            if not account or not isinstance(wire_model, str) or not wire_model:
                execution.uncertain = True
            else:
                observed = Binding(self._source_provider_id, account, wire_model)
                if execution.binding is not None and execution.binding != observed:
                    execution.uncertain = True
                execution.binding = observed
        return headers

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        execution = self._execution()
        async with aclosing(super().stream(request)) as chunks:
            async for chunk in chunks:
                if execution is not None and request.extra.get(TURN_MARKER_KEY):
                    if chunk.kind == "message_start":
                        served = (chunk.payload.get("message") or {}).get("model")
                    elif chunk.kind == "usage":
                        served = chunk.payload.get("served_model")
                    else:
                        served = None
                    if served and served != request.model:
                        # A provider-side model substitution is not a same-model exchange.
                        execution.uncertain = True
                    if chunk.kind == "message_stop":
                        execution.main_stream_completed = True
                yield chunk
