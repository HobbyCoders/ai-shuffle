"""Local OpenAI-compatible server serialization (llama.cpp llama-server, Ollama).

Request side: plain OpenAI chat/completions — model, messages, tools,
max_tokens, stream, ``stream_options.include_usage`` — and NOTHING else.
OpenRouter's ``usage: {include: true}`` and the ``reasoning`` param are never
sent (local servers reject or ignore unknown fields inconsistently); anything
server-specific (llama.cpp's ``cache_prompt``, samplers, grammar, …) goes
through ``ProviderRequest.extra``, which is merged into the body LAST so it can
override any default.

``base_url`` normalization rule (documented for embedders):

* ends with ``/chat/completions``      → used verbatim (full endpoint)
* ends with ``/v1``                    → ``/chat/completions`` appended
* anything else (server origin/prefix) → ``/v1/chat/completions`` appended

So ``http://127.0.0.1:8080``, ``http://127.0.0.1:8080/v1`` and
``http://127.0.0.1:8080/v1/chat/completions`` all reach the same endpoint, and
Ollama works with just ``base_url = "http://127.0.0.1:11434"`` (its
OpenAI-compatible surface lives at ``/v1/chat/completions``).
"""

from __future__ import annotations

from typing import Any

from fluid.providers._chat_completions import (
    system_message,
    translate_message,
    translate_tools,
)
from fluid.providers.base import ProviderRequest


def normalize_base_url(base_url: str) -> str:
    """Apply the base-url rule above. Trailing slashes are ignored."""
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def build_request_body(request: ProviderRequest) -> dict[str, Any]:
    """Plain OpenAI chat/completions body; ``request.extra`` merges last."""
    messages: list[dict[str, Any]] = []
    system = system_message(request.system)
    if system is not None:
        messages.append(system)
    for msg in request.messages:
        messages.extend(translate_message(msg))

    body: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens,
        "stream": True,
        # Ask for a usage object on the terminal chunk (finalize hook).
        "stream_options": {"include_usage": True},
    }
    if request.tools:
        body["tools"] = translate_tools(request.tools)
    # Escape hatch for server-specific knobs (llama.cpp cache_prompt, samplers,
    # grammar, Ollama options, …) — merged last, overrides anything above.
    body.update(request.extra)
    return body
