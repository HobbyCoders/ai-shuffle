"""
AI Generation API for profiles and sub-agents

Uses Claude to generate configurations based on user descriptions.
Features comprehensive prompts, domain awareness, and interactive clarification.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional, Dict

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

# App-local mirror classes, NOT the SDK ones. Generation runs through the
# provider adapter seam (see `generate_with_claude`), and the adapter emits
# `app.core.provider.events` instances — isinstance checks against the
# `claude_agent_sdk` classes would silently match nothing and collect no text.
from app.core.provider.events import AssistantMessage, ResultMessage, TextBlock
from app.core.provider.factory import make_adapter

from app.api.auth import require_auth
from app.api.common import api_error_handler
from app.core.model_registry import DEFAULT_MODEL
from app.core.models import (
    AgentExport,
    AgentExportData,
    ClarificationQuestionModel,
    GenerationResponseModel,
)
from app.core.generation import (
    build_profile_generation_prompt,
    build_subagent_generation_prompt,
    should_ask_clarification,
    get_clarification_questions,
)
from app.core.generation.clarification import format_clarification_context
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# [gen-debug] Diagnostic instrumentation for one-shot generation latency.
# Monkey-patches SDK's SubprocessCLITransport._build_command to log the final
# argv list sent to claude.exe. Generation always goes through the SDK
# subprocess path. Cheap one-time patch; no hot-path impact. Safe to remove
# once the investigation closes; idempotent via `_ai_shuffle_gen_debug_patched`
# marker if left in place.
# ---------------------------------------------------------------------------
try:
    import os as _gen_debug_os
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport as _SubprocessCLITransport,
    )

    if not getattr(_SubprocessCLITransport, "_ai_shuffle_gen_debug_patched", False):
        _orig_build_command = _SubprocessCLITransport._build_command

        def _patched_build_command(self):
            cmd = _orig_build_command(self)
            try:
                # Redact large JSON payloads but keep flag names and short values.
                redacted = []
                skip_next = False
                for tok in cmd:
                    if skip_next:
                        if isinstance(tok, str) and len(tok) > 200:
                            redacted.append(f"<{len(tok)} chars>")
                        else:
                            redacted.append(tok)
                        skip_next = False
                        continue
                    redacted.append(tok)
                    if tok in (
                        "--system-prompt",
                        "--append-system-prompt",
                        "--settings",
                        "--mcp-config",
                        "--json-schema",
                    ):
                        skip_next = True
                logger.info(f"[gen-debug] CLI argv ({len(cmd)} tokens): {redacted}")
                # Dump relevant env leaks that could silently change behavior
                leak_keys = [
                    "ANTHROPIC_MODEL",
                    "ANTHROPIC_SMALL_FAST_MODEL",
                    "ANTHROPIC_BASE_URL",
                    "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX",
                    "DISABLE_PROMPT_CACHING",
                    "MAX_THINKING_TOKENS",
                    "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK",
                ]
                env_snapshot = {
                    k: _gen_debug_os.environ.get(k, "<unset>")
                    for k in leak_keys
                }
                logger.info(f"[gen-debug] env snapshot: {env_snapshot}")
                logger.info(
                    f"[gen-debug] options: model={self._options.model!r} "
                    f"effort={self._options.effort!r} "
                    f"thinking={self._options.thinking!r} "
                    f"max_thinking_tokens={self._options.max_thinking_tokens!r} "
                    f"setting_sources={self._options.setting_sources!r} "
                    f"settings={(self._options.settings or '')[:200]!r} "
                    f"cwd={self._options.cwd!r} "
                    f"max_turns={self._options.max_turns!r}"
                )
            except Exception as _e:
                logger.warning(f"[gen-debug] instrumentation failure: {_e}")
            return cmd

        _SubprocessCLITransport._build_command = _patched_build_command
        _SubprocessCLITransport._ai_shuffle_gen_debug_patched = True
        logger.info("[gen-debug] SubprocessCLITransport._build_command patched")
except Exception as _patch_err:
    logger.warning(f"[gen-debug] failed to install SDK patch: {_patch_err}")


router = APIRouter(prefix="/api/v1/generate", tags=["Generation"])


# ============================================================================
# Request/Response Models
# ============================================================================

class GenerateProfileRequest(BaseModel):
    """Request to generate a profile configuration"""
    description: str = Field(..., min_length=10, max_length=5000, description="Description of the desired profile")
    reference_material: Optional[str] = Field(None, max_length=15000, description="Optional reference material like existing prompts or examples to incorporate")
    model: Optional[str] = Field(None, description="Model to use for generation (see /api/v1/models)")
    skip_clarification: bool = Field(False, description="Skip clarification questions")


class GenerateSubagentRequest(BaseModel):
    """Request to generate a subagent configuration"""
    description: str = Field(..., min_length=10, max_length=5000, description="Description of the desired subagent")
    reference_material: Optional[str] = Field(None, max_length=15000, description="Optional reference material like existing prompts or examples to incorporate")
    model: Optional[str] = Field(None, description="Model to use for generation (see /api/v1/models)")
    skip_clarification: bool = Field(False, description="Skip clarification questions")


class ContinueGenerationRequest(BaseModel):
    """Request to continue generation after clarification"""
    generation_type: str = Field(..., description="'profile' or 'subagent'")
    description: str = Field(..., min_length=10, max_length=5000, description="Original description")
    reference_material: Optional[str] = Field(None, max_length=15000, description="Optional reference material")
    answers: Dict[str, str] = Field(..., description="Map of question_id -> user's answer")
    model: Optional[str] = Field(None, description="Model to use for generation")


class SubagentExportData(BaseModel):
    """Subagent configuration data for export"""
    id: str
    name: str
    description: str
    prompt: str
    tools: Optional[list[str]] = None
    model: Optional[str] = None
    skills: Optional[list[str]] = None
    memory: Optional[str] = None
    mcp_servers: Optional[list] = None


class SubagentExport(BaseModel):
    """Full subagent export format"""
    version: str = "1.0"
    type: str = "ai-shuffle-subagent"
    exported_at: str
    subagent: SubagentExportData


# ============================================================================
# Helper Functions
# ============================================================================

def extract_json_from_response(text: str) -> dict:
    """
    Extract JSON from Claude's response, handling markdown code blocks.

    Args:
        text: Raw text response from Claude

    Returns:
        Parsed JSON dictionary

    Raises:
        ValueError: If no valid JSON found
    """
    text = text.strip()

    # Remove markdown code blocks if present
    if text.startswith("```"):
        lines = text.split("\n")
        start_idx = 1
        end_idx = len(lines)
        for i in range(1, len(lines)):
            if lines[i].strip() == "```":
                end_idx = i
                break
        text = "\n".join(lines[start_idx:end_idx]).strip()

    # Try to find JSON object in the text
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")

    # Find the matching closing brace, properly handling strings
    depth = 0
    end = start
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        char = text[i]

        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            escape_next = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if not in_string:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

    json_str = text[start:end]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in response: {e}")


def _result_message_error(message: ResultMessage) -> Optional[str]:
    """Return an error detail string when a ResultMessage reports a failure.

    The harness emits `is_error=True` with `errors=[...]` / `result=<text>` for
    an ErrorEvent (see `HarnessAdapter._translate`); a normal turn ends with
    `subtype="success", is_error=False`. Returns None for a clean result.
    """
    if not getattr(message, "is_error", False):
        return None
    errors = getattr(message, "errors", None)
    if errors:
        return "; ".join(str(e) for e in errors)
    result = getattr(message, "result", None)
    if result:
        return str(result)
    api_error_status = getattr(message, "api_error_status", None)
    if api_error_status is not None:
        return f"provider returned HTTP {api_error_status}"
    return str(getattr(message, "subtype", None) or "unknown error")


async def generate_with_claude(
    prompt: str,
    model: Optional[str] = None,
    timeout_seconds: float = 120.0,
) -> str:
    """
    Generate a response using AI, honoring the Default AI profile.

    The Default AI profile is the user-facing control for all internal AI
    generation (commit messages, PR descriptions, branch names, profile /
    subagent generation, etc.). This function pulls the profile's model,
    provider binding and system prompt, then runs a throwaway turn through the
    provider adapter seam (`app.core.provider.factory.make_adapter`).

    ROUTING — identical to chat. The adapter's `_decide_provider_kind` reads the
    provider binding stamped onto the options by `build_generation_options`
    (`aih_provider_id` / `aih_provider_type` / `aih_provider_auth_mode` /
    `aih_provider_dialect`): a Claude-subscription binding takes the delegated
    `claude.exe` leg, anything else (an OpenAI/OpenRouter/custom row, an
    API-key Anthropic row) takes the fluid native leg. This function used to
    call the SDK's `query()` directly, which always spawned `claude.exe` even
    when the profile pointed at e.g. `gpt-5.6-terra` — a guaranteed hang.

    Tools / MCP / agents / hooks are always forced off for one-shot generation,
    and nothing is persisted to the session store.

    Auth follows the bound provider: a native leg resolves the provider row's
    decrypted credentials; the delegated leg authenticates like every other
    Claude Code session (stored `ANTHROPIC_API_KEY`, else the connected OAuth
    account). There is NO direct-HTTP path here.

    Args:
        prompt: The prompt to send.
        model: Optional model override. If None, uses the model from the
               Default AI profile.
        timeout_seconds: Wall-clock budget for the send+receive phase. On expiry
               the adapter is torn down and an HTTP 504 is raised. Latency-
               sensitive callers (branch names) pass a shorter budget so their
               non-AI fallback fires quickly.

    Returns:
        Text response from the model.
    """
    from app.core.profiles import get_default_ai_profile
    from app.core.generation_options import build_generation_options
    from app.core.environment import ProviderConfigError

    profile = get_default_ai_profile()
    profile_config = (profile or {}).get("config") if profile else None
    effective_model = model or (profile_config or {}).get("model") or DEFAULT_MODEL

    # Single-shot turn: max_turns=1 since we only send one prompt.
    try:
        options = build_generation_options(
            profile_config,
            model_override=model,
            max_turns=1,
        )
    except ProviderConfigError as e:
        # Misconfigured/deleted/disabled model provider — a 400 carrying the
        # provider message, not a generic "Internal server error:" 500 (this
        # call is outside the api_error_handler block below).
        raise HTTPException(status_code=400, detail=str(e)) from e

    t0 = time.perf_counter()
    response_text: list[str] = []

    # ResultMessage accounting, captured for the usage_log write below.
    # ``result_usage is None`` means no ResultMessage was ever seen (timeout,
    # crash) — nothing to bill.
    result_usage: Optional[Dict] = None
    result_cost: Optional[float] = None
    result_duration_ms: Optional[int] = None

    # [gen-debug] Per-message timing. Capture time-to-first-event and
    # time-to-first-text so we can split "leg startup + init handshake" from
    # "model reasoning + token streaming."
    t_first_event: Optional[float] = None
    t_first_text: Optional[float] = None
    msg_count = 0
    msg_types: list[str] = []

    # The decided routing INPUTS. If a generation call is slow or lands on the
    # wrong backend, this line says why before any subprocess/HTTP work starts.
    logger.info(
        "[generate_with_claude] routing model=%r provider_id=%r "
        "provider_type=%r auth_mode=%r dialect=%r timeout=%.0fs "
        "prompt_bytes=%d",
        effective_model,
        getattr(options, "aih_provider_id", None),
        getattr(options, "aih_provider_type", None),
        getattr(options, "aih_provider_auth_mode", None),
        getattr(options, "aih_provider_dialect", None),
        timeout_seconds,
        len(prompt),
    )

    adapter = make_adapter()

    async def _run_turn() -> None:
        nonlocal t_first_event, t_first_text, msg_count
        nonlocal result_usage, result_cost, result_duration_ms
        await adapter.send_query(prompt)
        async for message in adapter.receive_events():
            now = time.perf_counter()
            if t_first_event is None:
                t_first_event = now - t0
                logger.info(
                    f"[gen-debug] first event received after "
                    f"{t_first_event * 1000:.0f}ms type={type(message).__name__}"
                )
            msg_count += 1
            msg_types.append(type(message).__name__)
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        if t_first_text is None:
                            t_first_text = now - t0
                            logger.info(
                                f"[gen-debug] first text block after "
                                f"{t_first_text * 1000:.0f}ms "
                                f"len={len(block.text)}"
                            )
                        response_text.append(block.text)
            elif isinstance(message, ResultMessage):
                # Turn-terminal. A failed turn must not read back as an empty
                # generation — surface the harness's error text so
                # api_error_handler renders something actionable.
                detail = _result_message_error(message)
                if detail:
                    raise RuntimeError(f"AI generation failed: {detail}")
                result_usage = getattr(message, "usage", None) or {}
                result_cost = getattr(message, "total_cost_usd", None)
                result_duration_ms = getattr(message, "duration_ms", None)
                break

    async with api_error_handler("run AI generation"):
        try:
            try:
                await adapter.connect_with_sdk_options(options)
            except ProviderConfigError as e:
                # Missing/undecryptable provider credentials (ProviderCredentialError
                # subclasses ProviderConfigError) or any other provider-config
                # failure raised while building the native client. Actionable 400,
                # not a generic 500.
                raise HTTPException(status_code=400, detail=str(e)) from e

            try:
                await asyncio.wait_for(_run_turn(), timeout=timeout_seconds)
            except asyncio.TimeoutError as e:
                logger.warning(
                    "[generate_with_claude] timed out after %.0fs "
                    "model=%r provider_id=%r",
                    timeout_seconds,
                    effective_model,
                    getattr(options, "aih_provider_id", None),
                )
                raise HTTPException(
                    status_code=504, detail="AI generation timed out"
                ) from e
        finally:
            # One-shot lifecycle: this call owns the adapter outright (never
            # pooled), so tear it down here — including on the timeout and
            # crash paths.
            try:
                await adapter.disconnect()
            except Exception as disconnect_err:  # noqa: BLE001 - teardown is best-effort
                logger.warning(
                    f"[generate_with_claude] adapter disconnect failed: "
                    f"{disconnect_err}"
                )

    t_total = time.perf_counter() - t0

    # Generation calls (commit messages, PR text, branch names, profile /
    # subagent generation) have no session, so session_id is NULL here:
    # usage_log.session_id is nullable and the analytics totals LEFT JOIN
    # sessions, so this spend lands in the totals without inventing a session
    # row. log_usage's in-memory per-session cache append is gated on a truthy
    # session_id, so None skips it — intended, there is nothing to cache.
    if result_usage is not None:
        try:
            await run_sync(
                database.log_usage,
                session_id=None,
                profile_id=(profile or {}).get("id"),
                model=effective_model,
                tokens_in=result_usage.get("input_tokens", 0),
                tokens_out=result_usage.get("output_tokens", 0),
                cost_usd=result_cost or 0,
                duration_ms=result_duration_ms or int(t_total * 1000),
            )
        except Exception as usage_err:  # noqa: BLE001 - accounting is best-effort
            logger.warning(
                f"[generate_with_claude] failed to log generation usage: {usage_err}"
            )

    logger.info(
        f"[generate_with_claude] adapter path model={effective_model} "
        f"took {t_total * 1000:.0f}ms"
    )
    logger.info(
        f"[gen-debug] breakdown total={t_total * 1000:.0f}ms "
        f"first_event={((t_first_event or 0) * 1000):.0f}ms "
        f"first_text={((t_first_text or 0) * 1000):.0f}ms "
        f"msg_count={msg_count} types={msg_types}"
    )
    return "".join(response_text)


def _build_clarification_response(
    description: str,
    generation_type: str,
) -> GenerationResponseModel:
    """Build a clarification response with questions."""
    questions = get_clarification_questions(description)

    return GenerationResponseModel(
        status="needs_clarification",
        result=None,
        questions=[
            ClarificationQuestionModel(
                id=q.id,
                question=q.question,
                options=q.options,
                context=q.context,
            )
            for q in questions
        ],
        generation_type=generation_type,
    )


async def _generate_profile_internal(
    description: str,
    model: Optional[str] = None,
    clarification_context: Optional[str] = None,
    reference_material: Optional[str] = None,
) -> AgentExport:
    """
    Internal profile generation logic.

    Args:
        description: User's description
        model: Model to use
        clarification_context: Additional context from clarification
        reference_material: Optional reference material (existing prompts, examples)

    Returns:
        AgentExport with generated configuration
    """
    # Build the comprehensive prompt
    prompt = build_profile_generation_prompt(description, clarification_context, reference_material)

    # Generate with Claude
    logger.debug(f"Generating profile from description: {description[:100]}...")
    response_text = await generate_with_claude(prompt, model)

    # Parse the response
    try:
        generated = extract_json_from_response(response_text)
    except ValueError as e:
        logger.error(f"Failed to parse generated profile: {e}")
        logger.error(f"Raw response: {response_text[:1000]}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to parse AI response"
        )

    # Validate required fields
    if not generated.get("name"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a profile name"
        )

    # Handle ai_tools configuration
    ai_tools_config = None
    if generated.get("ai_tools"):
        from app.core.ai_tools import AIToolsConfig
        try:
            ai_tools_config = AIToolsConfig(**generated["ai_tools"])
        except Exception as e:
            logger.warning(f"Failed to parse ai_tools config: {e}")

    # Build the export format — fluid-honored fields only (ruling A12). If a
    # model still emits the retired preset shape, fold its append text into
    # the custom prompt content; other SDK-era fields are dropped.
    prompt_content = (
        generated.get("system_prompt_content")
        or generated.get("system_prompt_append")
    )
    agent_data = AgentExportData(
        name=generated.get("name", "Generated Profile"),
        description=generated.get("description"),
        model=generated.get("model", DEFAULT_MODEL),
        permission_mode=generated.get("permission_mode", "default"),
        system_prompt_content=prompt_content,
        allowed_tools=generated.get("allowed_tools"),
        disallowed_tools=generated.get("disallowed_tools"),
        enabled_agents=generated.get("enabled_agents"),
        ai_tools=ai_tools_config,
        memory=generated.get("memory"),
        include_partial_messages=generated.get("include_partial_messages", True),
        app_config_tools=generated.get("app_config_tools", False),
        env=generated.get("env"),
    )

    export_data = AgentExport(
        version="1.0",
        type="ai-shuffle-agent",
        exported_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        agent=agent_data
    )

    logger.debug(f"Successfully generated profile: {agent_data.name}")
    return export_data


async def _generate_subagent_internal(
    description: str,
    model: Optional[str] = None,
    clarification_context: Optional[str] = None,
    reference_material: Optional[str] = None,
) -> SubagentExport:
    """
    Internal subagent generation logic.

    Args:
        description: User's description
        model: Model to use
        clarification_context: Additional context from clarification
        reference_material: Optional reference material (existing prompts, examples)

    Returns:
        SubagentExport with generated configuration
    """
    # Build the comprehensive prompt
    prompt = build_subagent_generation_prompt(description, clarification_context, reference_material)

    # Generate with Claude
    logger.debug(f"Generating subagent from description: {description[:100]}...")
    response_text = await generate_with_claude(prompt, model)

    # Parse the response
    try:
        generated = extract_json_from_response(response_text)
    except ValueError as e:
        logger.error(f"Failed to parse generated subagent: {e}")
        logger.error(f"Raw response: {response_text[:1000]}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to parse AI response"
        )

    # Validate required fields
    if not generated.get("id"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a subagent ID"
        )
    if not generated.get("name"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a subagent name"
        )
    if not generated.get("description"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a subagent description"
        )
    if not generated.get("prompt"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a subagent prompt"
        )

    # Sanitize the ID (lowercase, hyphens only)
    subagent_id = re.sub(r'[^a-z0-9-]', '-', generated["id"].lower())
    subagent_id = re.sub(r'-+', '-', subagent_id).strip('-')

    # Build the export format
    subagent_data = SubagentExportData(
        id=subagent_id,
        name=generated.get("name", "Generated Subagent"),
        description=generated.get("description", ""),
        prompt=generated.get("prompt", ""),
        tools=generated.get("tools"),
        model=generated.get("model"),
    )

    export_data = SubagentExport(
        version="1.0",
        type="ai-shuffle-subagent",
        exported_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        subagent=subagent_data
    )

    logger.debug(f"Successfully generated subagent: {subagent_data.name} ({subagent_data.id})")
    return export_data


# ============================================================================
# API Endpoints
# ============================================================================

@router.post("/profile", response_model=GenerationResponseModel)
async def generate_profile(
    request: GenerateProfileRequest,
    token: str = Depends(require_auth)
):
    """
    Generate a profile configuration using AI.

    Takes a natural language description and generates a complete profile
    configuration. May return clarification questions if the description
    is ambiguous.

    Returns:
        GenerationResponseModel with either:
        - status="complete" and result containing AgentExport
        - status="needs_clarification" and questions to ask
    """
    if not request.description.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Description cannot be empty"
        )

    # Check if clarification is needed (skip if reference material provided - user knows what they want)
    if not request.skip_clarification and not request.reference_material and should_ask_clarification(request.description):
        return _build_clarification_response(request.description, "profile")

    # Generate the profile
    export_data = await _generate_profile_internal(
        request.description,
        request.model,
        reference_material=request.reference_material,
    )

    return GenerationResponseModel(
        status="complete",
        result=export_data.model_dump(),
        questions=None,
        generation_type="profile",
    )


@router.post("/subagent", response_model=GenerationResponseModel)
async def generate_subagent(
    request: GenerateSubagentRequest,
    token: str = Depends(require_auth)
):
    """
    Generate a subagent configuration using AI.

    Takes a natural language description and generates a complete subagent
    configuration. May return clarification questions if the description
    is ambiguous.

    Returns:
        GenerationResponseModel with either:
        - status="complete" and result containing SubagentExport
        - status="needs_clarification" and questions to ask
    """
    if not request.description.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Description cannot be empty"
        )

    # Check if clarification is needed (skip if reference material provided - user knows what they want)
    if not request.skip_clarification and not request.reference_material and should_ask_clarification(request.description):
        return _build_clarification_response(request.description, "subagent")

    # Generate the subagent
    export_data = await _generate_subagent_internal(
        request.description,
        request.model,
        reference_material=request.reference_material,
    )

    return GenerationResponseModel(
        status="complete",
        result=export_data.model_dump(),
        questions=None,
        generation_type="subagent",
    )


@router.post("/continue", response_model=GenerationResponseModel)
async def continue_generation(
    request: ContinueGenerationRequest,
    token: str = Depends(require_auth)
):
    """
    Continue generation after user provides clarification answers.

    Takes the original description plus user's answers to clarification
    questions and generates the final configuration.

    Returns:
        GenerationResponseModel with status="complete" and result
    """
    if request.generation_type not in ("profile", "subagent"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="generation_type must be 'profile' or 'subagent'"
        )

    # Format the clarification context
    clarification_context = format_clarification_context(
        request.answers,
        request.description,
    )

    if request.generation_type == "profile":
        export_data = await _generate_profile_internal(
            request.description,
            request.model,
            clarification_context,
            reference_material=request.reference_material,
        )
        return GenerationResponseModel(
            status="complete",
            result=export_data.model_dump(),
            questions=None,
            generation_type="profile",
        )
    else:
        export_data = await _generate_subagent_internal(
            request.description,
            request.model,
            clarification_context,
            reference_material=request.reference_material,
        )
        return GenerationResponseModel(
            status="complete",
            result=export_data.model_dump(),
            questions=None,
            generation_type="subagent",
        )


# ============================================================================
# Legacy Endpoints (for backward compatibility)
# ============================================================================

@router.post("/profile/direct", response_model=AgentExport)
async def generate_profile_direct(
    request: GenerateProfileRequest,
    token: str = Depends(require_auth)
):
    """
    Generate a profile configuration directly (no clarification).

    Legacy endpoint that always returns AgentExport directly.
    Use /profile for the new clarification-aware flow.
    """
    return await _generate_profile_internal(
        request.description,
        request.model,
        reference_material=request.reference_material,
    )


@router.post("/subagent/direct", response_model=SubagentExport)
async def generate_subagent_direct(
    request: GenerateSubagentRequest,
    token: str = Depends(require_auth)
):
    """
    Generate a subagent configuration directly (no clarification).

    Legacy endpoint that always returns SubagentExport directly.
    Use /subagent for the new clarification-aware flow.
    """
    return await _generate_subagent_internal(
        request.description,
        request.model,
        reference_material=request.reference_material,
    )
