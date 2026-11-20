"""One stateless inference on an exact ChatGPT subscription, without an agent."""
from __future__ import annotations

import asyncio

import httpx
from fluid.providers._sse import sse_events
from fluid.providers.openai_responses.client import CODEX_RESPONSES_URL

from app.core.behavioral_review import MAX_OUTPUT_CHARS
from app.core import encryption
from app.db import database
from app.db.async_database import run_sync


def _access_token(provider_id: str, account: str) -> str:
    # One exact row, one snapshot of enabled/account/token, no provider alias
    # resolution and no refresh. Recheck after preparation, at dispatch.
    row = database.get_model_provider(provider_id, include_token=True)
    if (not row or row.get("id") != provider_id or not row.get("enabled")
            or row.get("auth_mode") != "chatgpt" or row.get("codex_account_id") != account
            or row.get("needs_reauth")):
        raise RuntimeError("Behavioral review account is unavailable")
    encrypted = row.get("codex_access_token")
    token = encryption.decrypt_value(encrypted) if encrypted else None
    if not token:
        raise RuntimeError("Behavioral review credentials are unavailable")
    return token


async def infer(prompt: str, route: dict, account: str, timeout_seconds: float) -> str:
    provider, model = route.get("provider"), route.get("model")
    if not all(isinstance(value, str) and value for value in (provider, model, account)):
        raise RuntimeError("Behavioral review is missing its exact binding")

    # Do not construct a Fluid session or traverse its normal transport: even a
    # one-shot turn can attach context, trace request bodies or book quota/usage.
    # No session/response IDs, cache key, tools, proxy, redirect, auth fallback,
    # user-configured request extras, or retries reach this HTTP client.
    body = {
        "model": model,
        "instructions": "Return only the requested JSON classification.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "store": False,
        "stream": True,
        "reasoning": {"effort": "low"},
    }
    async with asyncio.timeout(timeout_seconds):
        token = await run_sync(_access_token, provider, account)
        headers = {
            "Authorization": f"Bearer {token}",
            "chatgpt-account-id": account,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "accept": "text/event-stream",
        }
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False,
                                     follow_redirects=False) as client:
            async with client.stream("POST", CODEX_RESPONSES_URL, headers=headers, json=body) as response:
                if response.status_code != 200:
                    raise RuntimeError("Behavioral review request failed")
                return await _read_text(response)


async def _read_text(response: httpx.Response) -> str:
    chunks: list[str] = []
    total = 0
    async for name, event in sse_events(response.aiter_lines()):
        kind = event.get("type", name)
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise RuntimeError("Invalid behavioral review response")
            total += len(delta)
            if total > MAX_OUTPUT_CHARS:
                raise RuntimeError("Behavioral review response exceeded its limit")
            if delta:
                chunks.append(delta)
        elif kind in ("error", "response.failed", "response.incomplete", "response.refusal.delta"):
            raise RuntimeError("Behavioral review did not complete")
        elif kind == "response.completed":
            return "".join(chunks)
    raise RuntimeError("Behavioral review stream ended without a completion")
