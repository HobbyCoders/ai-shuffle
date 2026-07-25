"""In-process SDK MCP server exposing the ``ProviderAgent`` tool (P3-A).

One tool, ``ProviderAgent(instance, prompt, model?, label?)``, which dispatches
a brief to a SEPARATE session bound to a different model provider and returns
that worker's report. Ruling P14: in-process, not a spawned STDIO server — the
tool wraps *ourselves* (session pool, task registry, event log), all of which
are owned in this process; an STDIO server would have to call back over HTTP
with auth + CSRF for something already local.

The heavy lifting lives in :mod:`app.core.provider_agent` (framework-free and
independently testable). This module is the thin SDK adapter: argument shape,
tool-result envelope, and the flag/nesting gates.

Registered ONLY when ``settings.provider_agent_enabled`` is true, and never for
a session that is itself a ProviderAgent worker (one nesting level only).

Like every other in-process server in this repo the config MUST be wrapped by
``app.mcp.sdk_compat.patch_sdk_server``: mcp >= 1.12 mis-iterates the SDK's
``CallToolResult`` pydantic model and the tool silently breaks without it.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SERVER_NAME = "provider-agent"
TOOL_NAME = "ProviderAgent"


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Format a successful tool result for the SDK MCP envelope."""
    return {
        "content": [
            {"type": "text", "text": json.dumps({"success": True, **payload}, default=str)}
        ]
    }


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    """Format a FAILED tool result for the SDK MCP envelope.

    House style (matches ``app/mcp/builtin_tools.py``): failure is encoded in
    the JSON body rather than the SDK's ``is_error`` flag, because
    ``patch_sdk_server`` forwards only ``content``.
    """
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"success": False, "error": message, **extra}, default=str),
            }
        ]
    }


_TOOL_DESCRIPTION = (
    "Dispatch a self-contained task to a worker session running on a DIFFERENT "
    "model provider (e.g. a ChatGPT/Codex or OpenAI provider) and return its "
    "report. The worker is a separate session: it inherits this session's "
    "project, worktree and working directory but NOT its transcript, so the "
    "'prompt' must be a complete, standalone brief — exactly like the Task "
    "tool. Use it to get a second provider's take, or to offload work that "
    "should not consume this session's provider budget. The report returned "
    "here is BOUNDED; the full transcript lives in the returned worker session. "
    "Workers may not dispatch workers."
)

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "instance": {
            "type": "string",
            "description": (
                "Target provider: its id, or its display name when unambiguous."
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "The complete standalone brief for the worker. It sees no "
                "history from this session."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Optional model id from the provider's curated list. Defaults "
                "to the provider's default model."
            ),
        },
        "label": {
            "type": "string",
            "description": "Optional short label for the worker (used as its session title).",
        },
    },
    "required": ["instance", "prompt"],
}


def get_provider_agent_tool_names() -> List[str]:
    """MCP tool names to add to ``allowed_tools`` when the feature is on."""
    return [f"mcp__{SERVER_NAME}__{TOOL_NAME}"]


def build_provider_agent_tool(
    session_id: Optional[str] = None,
    profile_id: Optional[str] = None,
) -> Optional[Any]:
    """The ``ProviderAgent`` ``SdkMcpTool``, with session context baked in.

    Split out from :func:`create_provider_agent_server` so the handler can be
    exercised directly in tests without standing up an MCP server.
    Returns ``None`` when the SDK is unavailable.
    """
    try:
        from claude_agent_sdk import tool
    except ImportError:
        logger.debug("claude_agent_sdk not available — ProviderAgent disabled")
        return None

    @tool(TOOL_NAME, _TOOL_DESCRIPTION, _INPUT_SCHEMA)
    async def provider_agent(args: dict) -> dict:
        from app.core.config import settings
        from app.core.provider_agent import (
            MAX_REPORT_CHARS,
            default_model_for_provider,
            dispatch_worker,
            is_provider_agent_worker,
            resolve_provider_instance,
            validate_provider_model,
        )

        # Re-check the flag at CALL time, not just at registration: a running
        # session's server outlives a config change.
        if not getattr(settings, "provider_agent_enabled", False):
            return _err("ProviderAgent is disabled on this install.")
        if not session_id or not profile_id:
            return _err("ProviderAgent requires an active session context.")
        # Persisted turn source ORed with the in-process set: a worker running
        # on another runner is not in this process's set, so the set alone
        # would let cross-runner recursion through (P3-A4).
        if is_provider_agent_worker(session_id):
            return _err(
                "A ProviderAgent worker may not dispatch another worker "
                "(one nesting level only)."
            )

        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return _err("prompt is required.")
        label = (args.get("label") or "").strip() or None

        resolved = resolve_provider_instance(args.get("instance") or "")
        if not resolved.ok:
            return _err(resolved.error or "Could not resolve the provider instance.",
                        candidates=list(resolved.candidates))
        row = resolved.provider or {}
        pid = str(row.get("id") or "")
        plabel = row.get("name") or pid

        model_id = (args.get("model") or "").strip() or default_model_for_provider(row)
        if not model_id:
            return _err(
                f"Provider '{plabel}' has no curated models; add one in Settings "
                f"before dispatching a worker to it.",
                provider=pid,
            )

        validation = validate_provider_model(pid, model_id, provider_row=row)
        if not validation.ok:
            return _err(
                validation.reason or "The provider/model pair is not usable.",
                provider=pid,
                model=model_id,
                code=validation.code,
                curated_models=list(validation.curated_ids),
            )

        outcome = await dispatch_worker(
            parent_session_id=session_id,
            profile_id=profile_id,
            provider_id=pid,
            model_id=model_id,
            prompt=prompt,
            label=label,
        )
        if not outcome.ok:
            return _err(
                outcome.error or "The worker failed.",
                worker_session_id=outcome.session_id,
                provider=pid,
                model=model_id,
            )

        report = outcome.report
        if outcome.truncated:
            report = (
                f"[Report truncated to the last {MAX_REPORT_CHARS} characters. "
                f"The full transcript is in worker session {outcome.session_id}.]\n\n"
                f"{report}"
            )
        return _ok({
            "worker_session_id": outcome.session_id,
            "provider": pid,
            "model": model_id,
            "label": label,
            "truncated": outcome.truncated,
            "report": report,
        })

    return provider_agent


def create_provider_agent_server(
    session_id: Optional[str] = None,
    profile_id: Optional[str] = None,
) -> Optional[Any]:
    """Build the ``mcp__provider-agent__ProviderAgent`` server for a session.

    Args:
        session_id: The PARENT session — workers are bound to it and their
            registry entries pin it in the pool.
        profile_id: Profile the worker session inherits.

    Returns:
        SDK MCP server config, or ``None`` when the SDK is unavailable.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server
    except ImportError:
        logger.debug("claude_agent_sdk not available — ProviderAgent disabled")
        return None

    from app.mcp.sdk_compat import patch_sdk_server

    provider_agent = build_provider_agent_tool(session_id, profile_id)
    if provider_agent is None:
        return None

    all_tools = [provider_agent]
    logger.info(
        "Registering ProviderAgent MCP tool (session=%s, profile=%s)",
        session_id, profile_id,
    )
    return patch_sdk_server(
        create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=all_tools),
        all_tools,
    )
