"""Native Messages gateways, not Claude subscription/OAuth clients.

Endpoint roots from the vendors' Messages docs (2026-11-05). A stored root
is an explicit override, never a model-name based routing hint.
"""
from __future__ import annotations

PROVIDERS = ("kimi", "minimax", "opencode")
BASE_URLS = {
    ("kimi", "plan_key"): "https://api.kimi.ai/coding",
    ("kimi", "api_key"): "https://api.moonshot.ai/anthropic",
    ("minimax", "plan_key"): "https://api.minimax.io/anthropic",
    ("minimax", "api_key"): "https://api.minimax.io/anthropic",
    ("opencode", "plan_key"): "https://opencode.ai/inference/go/anthropic",
    ("opencode", "api_key"): "https://opencode.ai/inference/anthropic",
}


def endpoint_root(provider: str, auth_mode: str | None = None, override: str | None = None) -> str:
    mode = auth_mode or "api_key"
    default = BASE_URLS[(provider, mode)]
    return (override or default).strip().rstrip("/")


def effort_levels(provider: str, model: str) -> tuple[str, ...] | None:
    if provider == "kimi":
        return ("low", "high", "max")
    if provider == "minimax":
        return ("low", "medium", "high", "xhigh", "max") if model == "MiniMax-M3.1-Flash" else ()
    # OpenCode translates many models; only live catalog declarations are truth.
    return None
