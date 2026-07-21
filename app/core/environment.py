"""
Environment setup, path resolution, and workspace configuration for Claude queries.

Contains functions for building environment variables, resolving credentials,
generating environment details blocks, and configuring security instructions.
"""

import json
import logging
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any, NamedTuple, Sequence, List, Tuple, TYPE_CHECKING

from app.db import database
from app.core.config import settings
from app.core.platform import DeploymentMode
from app.core import encryption
from app.core.model_registry import has_1m_context

if TYPE_CHECKING:
    # Type-only import (no runtime coupling): the leg-map assembler and the
    # apply_provider_env signature annotate with LegEntry, but the routing module
    # is imported lazily at call time to match the existing lazy pattern.
    from app.core.translator_gateway.routing import LegEntry

logger = logging.getLogger(__name__)


def _inject_model_params(env: Dict[str, str], env_var: str, setting_key: str) -> None:
    """Inject stored schema-driven params for a model as a JSON env var.

    The Node tool worker reads these (e.g. IMAGE_MODEL_PARAMS) and merges them
    into the fal payload, so uncurated models honor their configured params.
    """
    try:
        from app.api.settings_media import get_model_params
        params = get_model_params(setting_key)
        if params:
            env[env_var] = json.dumps(params)
    except Exception as e:  # noqa: BLE001 - param injection is best-effort
        logger.debug(f"Could not inject {env_var}: {e}")


# Default instructions for displaying media and files in chat
# Can be customized via admin settings API
DEFAULT_SECURITY_INSTRUCTIONS = """
## App-Identity
You are Claude Code running inside AI Shuffle — a self-hosted web chat application (SvelteKit + FastAPI + Claude Agent SDK). When the user mentions "this app", "the app", or discusses bugs/features without specifying another project, they mean the current project that is in your workspace. Never forget this context.

## Showing Files & Media in Chat
The chat renders Markdown and turns media links into inline players. Make everything you produce visible and clickable — never leave the user with a raw filesystem path they cannot open.

### Generated media — image, video, audio
The AI media tools (generateImage, editImage, generateWithReference, generateVideo, imageToVideo, extendVideo, generateAudio, audioToAudio) return a ready-to-use URL in their result. The moment a generation or edit succeeds, embed that media inline in your reply.

WHEN: Embed immediately on success — do not wait to be asked, and do not just describe the result in prose. If the result has an `images` array (multiple images), embed EVERY image, each on its own line.

HOW: Copy the URL verbatim from the result field. Never invent, guess, or rewrite a path, and never hand-write or paste raw base64 — always use the URL the tool returned.

- Image → `![short description](IMAGE_URL)` — from the `image_url` field
- Video → `[short description](VIDEO_URL)` — from the `video_url` field
- Audio → `[short description](AUDIO_URL)` — from the `audio_url` field (music, sound effects, or speech)

The app renders these as an inline player with a download button (and click-to-expand for image/video). A short caption is welcome, but the embed itself is mandatory.

Example — generateImage returns `{"success": true, "image_url": "/api/generated-images/by-path?path=/work/generated-images/sunset.png"}`. Reply with:
A warm sunset over the hills:
![Sunset over rolling hills](/api/generated-images/by-path?path=/work/generated-images/sunset.png)

### Other files — documents, code, archives, 3D models
For any other file the user may want to download or view — including 3D models (.glb from textTo3D / imageTo3D), which have no inline viewer — provide a download link built from the file's full path:

📎[filename.ext](/api/files/by-path?path=/full/path/to/file.ext)

## Chat-History
When the user references a .jsonl chat history file from ~/.claude/projects/: this is a previous conversation for context, NOT a file to edit. Scan for key decisions, code discussed, and user preferences. Summarize what you found. Do not read every line unless explicitly asked to summarize the full conversation.

## Worktrees & Subagents
When your primary working directory is a git worktree — a path under `.claude/worktrees/` — that worktree is the project root for the whole session. All reading, editing, and committing must happen inside it, never in the main checkout at the repository root.

- Edit files only at their worktree path. The same file also exists at the main checkout (the repo root); editing it there puts your changes on the wrong branch, outside the worktree's commits, so `git status` in the worktree shows nothing to commit while the edits sit uncommitted on the main checkout.
- Subagents (Explore, Plan, general-purpose, etc.) and search tools frequently report absolute paths rooted at the main checkout rather than the worktree. Before you read or edit a reported path, rebase it onto your worktree directory.
- When you launch a subagent, state the worktree path explicitly and instruct it to search, read, and report paths within that worktree only.
- When unsure which checkout a path belongs to, prefer paths relative to your current working directory, or run `git rev-parse --show-toplevel` to confirm the root before editing.
"""


def get_security_instructions() -> str:
    """
    Get the active security instructions to prepend to system prompts.

    Returns custom instructions if set by admin, otherwise returns the default.
    """
    custom_instructions = database.get_system_setting("security_instructions")
    if custom_instructions:
        return custom_instructions
    return DEFAULT_SECURITY_INSTRUCTIONS


def _is_git_repo(working_dir: str) -> bool:
    """Check if a directory is a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def _get_os_version() -> str:
    """Get the OS version string."""
    try:
        if sys.platform == "linux":
            return f"Linux {platform.release()}"
        elif sys.platform == "darwin":
            return f"macOS {platform.mac_ver()[0]}"
        elif sys.platform == "win32":
            return f"Windows {platform.release()}"
        else:
            return f"{sys.platform} {platform.release()}"
    except Exception:
        return sys.platform


def _get_decrypted_api_key(setting_name: str) -> Optional[str]:
    """
    Get an API key from the database and decrypt it if encrypted.

    Args:
        setting_name: The name of the setting (e.g., "openai_api_key")

    Returns:
        The decrypted API key, or None if not found or decryption fails
    """
    value = database.get_system_setting(setting_name)
    if not value:
        return None

    # Check if the value is encrypted
    if encryption.is_encrypted(value):
        if not encryption.is_encryption_ready():
            logger.warning(f"Cannot decrypt {setting_name}: encryption key not available")
            return None
        try:
            decrypted = encryption.decrypt_value(value)
            return decrypted
        except Exception as e:
            logger.error(f"Failed to decrypt {setting_name}: {e}")
            return None

    # Return plaintext value (for backwards compatibility during migration)
    return value


def _resolve_api_credential(
    credential_type: str,
    api_user_id: Optional[str] = None
) -> Optional[str]:
    """
    Resolve an API credential based on policy (user key vs admin fallback).

    Resolution order based on policy:
    1. If policy is 'user_provided' or 'optional': check user's credential first
    2. If not found and policy is 'admin_provided' or 'optional': use admin's key
    3. If not found: return None

    Args:
        credential_type: The credential type (e.g., "openai_api_key", "gemini_api_key")
        api_user_id: The API user ID (None for admin/local users)

    Returns:
        The resolved API key, or None if not available
    """
    # Map credential types to admin settings
    admin_setting_map = {
        "fal_api_key": "fal_api_key",
        "github_pat": None  # No admin fallback for GitHub
    }

    if credential_type not in admin_setting_map:
        logger.warning(f"Unknown credential type: {credential_type}")
        return None

    # Get per-user policy for this credential
    # Policies are always per-user, no global fallback
    # Default to "user_provided" if no policy exists (shouldn't happen for properly created users)
    policy_obj = None
    if api_user_id:
        policy_obj = database.get_user_credential_policy(api_user_id, credential_type)
    policy = policy_obj.get("policy", "user_provided") if policy_obj else "user_provided"

    resolved_key = None
    source = None

    # Try user's credential if applicable
    if api_user_id and policy in ["user_provided", "optional"]:
        user_cred = database.get_user_credential(api_user_id, credential_type)
        if user_cred:
            encrypted = user_cred.get("encrypted_value")
            if encrypted:
                if encryption.is_encrypted(encrypted) and encryption.is_encryption_ready():
                    try:
                        resolved_key = encryption.decrypt_value(encrypted)
                        source = "user"
                    except Exception as e:
                        logger.error(f"Failed to decrypt user credential {credential_type}: {e}")
                elif not encryption.is_encrypted(encrypted):
                    # Plaintext (shouldn't happen in production, but handle it)
                    resolved_key = encrypted
                    source = "user"

    # Try admin's key if applicable
    if not resolved_key and policy in ["admin_provided", "optional"]:
        admin_setting = admin_setting_map.get(credential_type)
        if admin_setting:
            resolved_key = _get_decrypted_api_key(admin_setting)
            if resolved_key:
                source = "admin"

    if resolved_key:
        logger.debug(f"Resolved {credential_type} from {source} (policy={policy})")
    elif policy == "user_provided":
        logger.debug(f"Credential {credential_type} not found - user must provide (policy={policy})")
    else:
        logger.debug(f"Credential {credential_type} not configured (policy={policy})")

    return resolved_key


def _get_hook_tool_names(hooks: Optional[Dict[str, list]]) -> list:
    """
    Extract tool names from hook matchers.

    When we have hooks that handle specific tools (like AskUserQuestion),
    we need to know which tools to enable so the model can call them.
    The hook then intercepts and handles the tool call.

    Args:
        hooks: Dict of hook configurations (e.g., {"PreToolUse": [HookMatcher(...)]})

    Returns:
        List of tool names that have hooks configured
    """
    tools = []

    if not hooks:
        return tools

    # Check for PreToolUse hooks and extract tool names from matchers
    pre_tool_hooks = hooks.get("PreToolUse", [])
    for hook_matcher in pre_tool_hooks:
        if hasattr(hook_matcher, "matcher") and hook_matcher.matcher:
            # The matcher can be a tool name like "AskUserQuestion"
            # or a pattern like "Bash|Edit" - we add all parts
            tool_names = hook_matcher.matcher.split("|")
            for tool_name in tool_names:
                tool_name = tool_name.strip()
                if tool_name and tool_name not in tools:
                    tools.append(tool_name)
                    logger.debug(f"Found hooked tool: '{tool_name}'")

    return tools


def _build_extra_args(
    base_extra_args: Dict[str, str],
    hooks: Optional[Dict[str, list]],
    deployment_mode: Optional["DeploymentMode"] = None,
) -> Dict[str, str]:
    """
    Build extra_args dict, adding --allowed-tools for any hooked tools.

    When we have hooks that handle specific tools (like AskUserQuestion),
    we must pass them via --allowed-tools CLI flag so the model can call them.
    The SDK's allowed_tools option only filters existing tools, it doesn't add new ones.

    Args:
        base_extra_args: Base extra args from profile config
        hooks: Dict of hook configurations (e.g., {"PreToolUse": [HookMatcher(...)]})
        deployment_mode: Current deployment mode (LOCAL or DOCKER)

    Returns:
        Dict of extra args to pass to CLI
    """
    extra_args = dict(base_extra_args) if base_extra_args else {}

    # CLI debug output to stderr: opt-in only via LOG_LEVEL=DEBUG.
    # Previously this was always-on in Docker mode, but the Claude CLI
    # generates tens of thousands of debug lines per query during tool use.
    # Even with Python's log level filtering, every line still requires:
    # pipe read -> bytes decode -> string format -> callback -> logger check,
    # causing significant CPU overhead in production.
    if deployment_mode and deployment_mode != DeploymentMode.LOCAL:
        if settings.log_level.upper() == "DEBUG" and "debug-to-stderr" not in extra_args:
            extra_args["debug-to-stderr"] = None
            # NOTE (SDK 0.1.65): the transport no longer *detects* this flag to
            # decide whether to pipe stderr — piping is now controlled solely
            # by whether a ``stderr=`` callback is registered on options (we
            # always register one via ``hook_service.stderr_callback``). We
            # still pass the flag because the CLI uses it to raise its own
            # log verbosity; it will be removed from the CLI in a future
            # release, at which point this block should be deleted.
            logger.debug("Added --debug-to-stderr for Docker CLI diagnostics (LOG_LEVEL=DEBUG)")

    # Get tool names from hooks
    hook_tools = _get_hook_tool_names(hooks)

    if hook_tools:
        # Merge with any existing allowed-tools
        existing = extra_args.get("allowed-tools", "")
        existing_tools = [t.strip() for t in existing.split(",") if t.strip()] if existing else []

        # Add hook tools that aren't already in the list
        for tool in hook_tools:
            if tool not in existing_tools:
                existing_tools.append(tool)
                logger.debug(f"Adding '{tool}' to --allowed-tools (required for hook handler)")

        if existing_tools:
            extra_args["allowed-tools"] = ",".join(existing_tools)

    return extra_args


class ProviderConfigError(ValueError):
    """A session references a model provider that cannot be applied — the
    provider row is missing, disabled, or its stored auth token can't be
    decrypted (encryption not ready / corrupt blob).

    Subclasses ``ValueError`` deliberately: the query engines already convert a
    ``ValueError`` raised out of ``build_options_from_profile`` (via their
    ``except (WorktreeUnavailableError, ValueError)`` guards) into a
    user-visible spawn/config error event. Reusing that channel means a
    misconfigured provider surfaces as a clear error instead of an opaque
    subprocess failure — and, per R5, we NEVER silently fall back to native
    Anthropic (which would leak the prompt to / bill the wrong upstream).
    """


def inject_claude_code_auth(
    env: Dict[str, str], provider_id: Optional[str] = None
) -> Dict[str, str]:
    """Inject the Claude Code auth credential into a subprocess env (in place).

    The stored `anthropic_api_key` is an authentication credential for Claude
    Code — NOT a separate generation transport. This is the always-on hook
    that wires it into EVERY Claude Code subprocess (chat sessions, agents,
    one-shot generation), mirroring how `FAL_KEY` is injected but unconditional
    on ai_tools.

    Precedence (matches `claude.exe`'s native behavior, where `ANTHROPIC_API_KEY`
    overrides any stored OAuth credentials):

    - **API key set** → inject `ANTHROPIC_API_KEY` (API-key auth wins). The
      connected Claude account / OAuth credentials are ignored by the CLI for
      that session, so billing flows through the API key.
    - **API key absent** → inject nothing; leave the env untouched so the CLI
      falls back to the existing OAuth account credentials.

    When ``provider_id`` is truthy the session targets a non-Anthropic model
    provider: DO NOT inject the stored Anthropic key. ``apply_provider_env``
    owns auth for those spawns (it sets ANTHROPIC_AUTH_TOKEN for the gateway and
    an explicitly-empty ANTHROPIC_API_KEY). Injecting the Anthropic key here
    would either leak it to the gateway or cause a native-Anthropic fallback.

    We never delete or alter the OAuth login flow — this only ADDS the
    key-injection path. Returns the same dict for convenience.
    """
    if provider_id:
        # Provider session — leave Anthropic auth untouched (see apply_provider_env).
        return env
    api_key = _get_decrypted_api_key("anthropic_api_key")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
        logger.debug(
            "Injected ANTHROPIC_API_KEY for Claude Code auth (API-key login "
            "active; overrides connected account)"
        )
    # No key → inject nothing; OAuth account credentials remain in effect.
    return env


def apply_context_window_env(env: Dict[str, str], model: Optional[str]) -> None:
    """Force the standard 200k context window unless ``model`` explicitly opted
    into the 1M window via the app's ``[1m]`` suffix.

    The bundled Claude CLI runs natively-1M models (e.g. ``fable`` →
    ``claude-fable-5``) AND every ``[1m]`` variant at 1M *by default*; the sole
    lever that forces the standard 200k window back on is the
    ``CLAUDE_CODE_DISABLE_1M_CONTEXT`` env var (truthy → 200k, per the CLI's
    boolean-env parser). We set it explicitly in both directions so a value
    inherited from the server's own environment can never leak the wrong window
    into a spawn — and so a bare ``fable``/custom native-1M model does not
    silently run at 1M when the user never enabled it.
    """
    if has_1m_context(model):
        # 1M explicitly opted in — allow the wide window and DON'T touch
        # auto-compaction: users who want 1M want the CLI's native (auto)
        # compaction tuning, not a surprise 200k override.
        env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "0"
        return

    env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "1"

    # Belt-and-suspenders for the auto-compact threshold. The CLI's auto-compact
    # window is clamped to the model's *maximum* context window; whether that
    # max correctly drops to 200k under DISABLE_1M has varied across bundled CLI
    # versions, so a native-1M model can end up auto-compacting off the 1M
    # window even though ``/context`` reports 200k — the session then blows well
    # past 200k before compaction fires. Pinning ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``
    # to the effective 200k window forces the CLI's ``source:"env"`` branch, which
    # caps the trigger at 200k regardless of how the max window was derived (this
    # is exactly the user's proven manual workaround, now automatic). We never
    # clobber an existing value — a profile/user-supplied
    # ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` (exposed as a profile preset) wins.
    env.setdefault("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "200000")


def is_gateway_servable(provider: Optional[Dict[str, Any]]) -> bool:
    """Whether the translator gateway can serve a provider row.

    True iff ``provider_type`` is one of ``openai`` / ``chatgpt`` / ``anthropic``
    (or ``auth_mode == "chatgpt"``). This MIRRORS
    :func:`app.core.translator_gateway.upstream.provider_mode`'s accepted set —
    kept in sync by hand (a ``provider_type`` outside this set raises there). Used
    by the Chunk F cross-provider subagent leg: both the main and the subagent
    provider must be gateway-servable for the leg to route, and an anthropic-type
    main with a servable leg is pulled onto the gateway (anthropic_passthrough)
    rather than the direct-base_url path.
    """
    if not provider:
        return False
    ptype = provider.get("provider_type")
    auth_mode = provider.get("auth_mode")
    return ptype in ("openai", "chatgpt", "anthropic") or auth_mode == "chatgpt"


def _small_fast_model(provider: Optional[Dict[str, Any]]) -> Optional[str]:
    """The STABLE provider-scoped small/fast model id, or None.

    Resolution chain (never the per-turn ``model``): the provider's
    ``small_fast_model`` → ``default_model`` → first curated model id. Shared by
    the ``ANTHROPIC_SMALL_FAST_MODEL`` overlay and the S3 leg assembler's
    session-default fallback so both agree on one value. Forward-references
    ``_first_curated_model_id`` (defined below) — resolved at call time, matching
    the module's existing helper-ordering convention.
    """
    if not provider:
        return None
    return (
        provider.get("small_fast_model")
        or provider.get("default_model")
        or _first_curated_model_id(provider)
    )


class SubagentLegPlan(NamedTuple):
    """The result of assembling a gateway session's subagent leg-map (S3).

    ``honored_legs`` is the ordered N-leg map handed to ``apply_provider_env`` /
    the routing token — the profile-default leg FIRST, then per-enabled-subagent
    composite legs, deduped on the exact ``(mid, pid, acct)`` tuple. ``dropped``
    records non-servable/disabled candidates for a single aggregate log (ids
    only). ``session_default`` is the frontmatter fallback stamped on any
    unpinned / not-honored subagent (the honored default-leg model if present,
    else the main provider's small/fast id). ``main_serves_claude_ids`` is True
    only for an anthropic-type main (its gateway can route bare Claude ids).
    """

    honored_legs: List["LegEntry"]      # default leg FIRST, then enabled-subagent legs
    dropped: List[Tuple[str, str]]      # (subagent_id | "<profile-default-leg>", provider_id)
    session_default: Optional[str]      # honored default-leg mid if honored, else main small_fast
    main_serves_claude_ids: bool        # main provider_type == "anthropic"


def assemble_subagent_legs(
    *,
    main_provider_row: Optional[Dict[str, Any]],
    default_leg_provider: Optional[str],
    default_leg_model: Optional[str],
    enabled_subagents: Dict[str, Dict[str, Any]],
) -> SubagentLegPlan:
    """Build the gateway leg-map for a provider session from the profile-default
    subagent leg + each enabled subagent's ``prov:<pid>:<mid>`` composite.

    The default leg is emitted FIRST, then each enabled subagent whose stored
    model is a servable+enabled ``prov:`` composite. Same-model-id collisions are
    resolved deterministically here: for each upstream model id at most ONE leg
    survives — the profile-default leg wins for its mid; otherwise the entry with
    the lexicographically-smallest provider id. Exact-duplicate tuples of the
    winner drop silently; any non-exact-dup losers are named in one aggregate
    ``warning`` (ids only). Legs are pre-honored here (servable + enabled, accounts resolved) so
    ``apply_provider_env`` trusts them without re-gating. If the MAIN provider is
    not gateway-servable, the WHOLE map is dropped (A2). Non-servable/disabled
    candidates are collected and logged ONCE (ids only, never tokens).
    """
    from app.core.translator_gateway.routing import LegEntry

    main_servable = is_gateway_servable(main_provider_row)
    small_fast = _small_fast_model(main_provider_row)
    main_serves_claude_ids = (main_provider_row or {}).get("provider_type") == "anthropic"

    honored_legs: List[LegEntry] = []
    dropped: List[Tuple[str, str]] = []
    default_leg_mid: Optional[str] = None

    if not main_servable:
        # A2: a non-servable main cannot host ANY gateway leg — drop the whole map.
        if default_leg_provider:
            dropped.append(("<profile-default-leg>", default_leg_provider))
        for agent_id, subagent in enabled_subagents.items():
            raw = subagent.get("model")
            if raw and raw.startswith("prov:"):
                remainder = raw[len("prov:"):]
                pid, sep, mid = remainder.partition(":")
                if sep and mid:
                    dropped.append((agent_id, pid))
        if dropped:
            logger.error(
                "Dropping %d non-servable/disabled subagent leg(s): %s",
                len(dropped),
                dropped,
            )
        return SubagentLegPlan([], dropped, small_fast, main_serves_claude_ids)

    # Main is servable: build the DEFAULT leg first, then per-subagent composites.
    if default_leg_provider and default_leg_model:
        dl_row = database.get_model_provider(default_leg_provider, include_token=False)
        if dl_row and is_gateway_servable(dl_row) and dl_row.get("enabled"):
            acct = (
                dl_row.get("codex_account_id")
                if (dl_row.get("provider_type") == "chatgpt" or dl_row.get("auth_mode") == "chatgpt")
                else None
            )
            honored_legs.append((default_leg_model, default_leg_provider, acct))
            default_leg_mid = default_leg_model
        else:
            dropped.append(("<profile-default-leg>", default_leg_provider))

    for agent_id, subagent in enabled_subagents.items():
        raw = subagent.get("model")
        if not raw or not raw.startswith("prov:"):
            continue
        remainder = raw[len("prov:"):]
        pid, sep, mid = remainder.partition(":")
        if not sep or not mid:
            continue
        pid_row = database.get_model_provider(pid, include_token=False)
        if pid_row and is_gateway_servable(pid_row) and pid_row.get("enabled"):
            acct = (
                pid_row.get("codex_account_id")
                if (pid_row.get("provider_type") == "chatgpt" or pid_row.get("auth_mode") == "chatgpt")
                else None
            )
            honored_legs.append((mid, pid, acct))
        else:
            dropped.append((agent_id, pid))

    session_default = default_leg_mid if default_leg_mid is not None else small_fast

    # S5: resolve same-model-id collisions deterministically. Group by mid; for
    # each mid with >1 entry exactly ONE survives — the profile-default leg
    # (always index 0 when present) wins for its mid, else the entry with the
    # lexicographically-smallest provider id. Exact-duplicate tuples of the winner
    # drop silently; non-exact-dup losers are named in one aggregate warning.
    default_leg_entry = honored_legs[0] if default_leg_mid is not None else None

    winners: Dict[str, LegEntry] = {}
    for leg in honored_legs:
        mid = leg[0]
        current = winners.get(mid)
        if current is None:
            winners[mid] = leg
        elif current is default_leg_entry:
            # Profile-default leg already holds this mid — it always wins.
            pass
        elif leg[1] < current[1]:
            winners[mid] = leg

    survivors: List[LegEntry] = []
    losers: List[Tuple[str, str]] = []
    for leg in honored_legs:
        winner = winners[leg[0]]
        if leg is winner:
            survivors.append(leg)
        elif leg != winner:
            # A genuine collision loser (an exact-dup of the winner drops silently).
            losers.append((leg[0], leg[1]))
    honored_legs = survivors

    if losers:
        logger.warning(
            "Resolved %d same-model-id leg collision(s); dropped loser(s): %s",
            len(losers),
            losers,
        )

    if dropped:
        logger.error(
            "Dropping %d non-servable/disabled subagent leg(s): %s",
            len(dropped),
            dropped,
        )

    return SubagentLegPlan(honored_legs, dropped, session_default, main_serves_claude_ids)


def apply_provider_env(
    env: Dict[str, str],
    provider_id: str,
    model: Optional[str],
    session_id: Optional[str] = None,
    *,
    subagent_legs: "Sequence[LegEntry]" = (),
) -> Dict[str, str]:
    """Overlay a non-Anthropic model-provider's gateway env vars onto ``env``
    (in place) so the bundled Claude Code CLI talks to that gateway instead of
    api.anthropic.com. Returns the same dict for convenience.

    Looks up the enabled provider row (with its encrypted token) and applies the
    officially-supported LLM-gateway env vars:

    - ``ANTHROPIC_BASE_URL`` — the gateway (CLI appends ``/v1/messages``)
    - ``ANTHROPIC_AUTH_TOKEN`` — the decrypted key (sent as ``Authorization: Bearer``)
    - ``ANTHROPIC_API_KEY=""`` — MUST be explicitly empty to prevent an
      Anthropic fallback
    - mitigation flags for non-Anthropic upstreams
      (``CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS``,
      ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC``,
      ``CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK``)
    - optional ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` / ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``
      (clamped ≥100k)
    - ``AIH_GATEWAY_TRANSLATED_MAIN=1`` — set IFF the MAIN provider is translated
      (openai/chatgpt ``provider_type`` or ``chatgpt`` ``auth_mode``); consumed by
      :func:`attach_hooks_to_options` to register the S8-R1 Agent-arg-strip
      PreToolUse hook
    - R4: ``ANTHROPIC_SMALL_FAST_MODEL`` set to ``small_fast_model or
      default_model or first curated model id`` (a STABLE provider-scoped value,
      NEVER the per-turn ``model``) so background probes / title-gen never send an
      unknown Claude alias to the gateway, and a within-provider composer switch
      doesn't churn the key into a subagent-killing respawn. Left unset if no
      curated models either. ``CLAUDE_CODE_SUBAGENT_MODEL`` is NO LONGER emitted
      (S3): subagent model selection moved to per-agent frontmatter stamping, so
      an unstamped CLI built-in Task subagent inherits the MAIN model.

    The provider's ``extra_env_json`` entries are applied FIRST, then the forced
    values LAST, so extra_env can never clobber the ``ANTHROPIC_API_KEY=""``
    guard (or any other overlay-controlled key).

    Codex/OpenAI-subscription providers (``provider_type`` in
    ``{"openai", "chatgpt"}`` or ``auth_mode == "chatgpt"``) do NOT speak the
    Anthropic wire protocol, so their sessions are pointed at the in-process
    translator gateway (Chunk A) instead: ``ANTHROPIC_BASE_URL`` becomes the
    loopback gateway and ``ANTHROPIC_AUTH_TOKEN`` a per-session opaque routing
    token (G4) — the gateway holds/decrypts the REAL upstream credentials at
    request time. All guard/mitigation/small-fast env is applied identically.
    openrouter/custom keep the direct-decrypted-token path unchanged.

    R5 (binding): a missing/disabled provider, or a token that cannot be
    decrypted (encryption not ready / corrupt), raises :class:`ProviderConfigError`
    — surfaced to the user as a spawn/config error. We NEVER silently fall back
    to native Anthropic. A provider with NO stored token is allowed only when a
    ``base_url`` is set (some self-hosted gateways are unauthenticated); the
    token is then the empty string.

    S3 (ruling A2/G2-r): an OPTIONAL N-leg subagent map, ``subagent_legs`` — an
    ordered sequence of ``LegEntry`` = ``(upstream_model, provider_id,
    account_id)``. The caller (query_options) assembles and PRE-HONORS these legs
    via :func:`assemble_subagent_legs` (servable + enabled, accounts resolved,
    whole-map dropped for a non-servable main), so this function trusts them and
    does not re-gate. When ``subagent_legs`` is non-empty the session is routed
    through the gateway and the routing token carries the whole map (so a request
    for any leg's ``upstream_model`` routes to that leg's provider); every other
    request stays on the main ``provider_id``. ``ANTHROPIC_SMALL_FAST_MODEL``
    keeps its existing main-provider chain (small/fast probes stay on the main).

    This is what makes the flagship mixed case work: an anthropic-type MAIN
    (real Anthropic API key) with one or more foreign (e.g. Codex) subagent legs.
    A bare anthropic main is servable but NOT ``is_translated``, so without a leg
    it keeps the direct-base_url path (byte-identical to today); a non-empty leg
    map pulls it onto the gateway, where the main is served by
    anthropic_passthrough at request time (reading the real key from the provider
    row — never env). Per-agent frontmatter (stamped by the caller via the S2
    resolver) is what points each subagent at its leg's model; this function only
    threads the leg map into the routing token.
    """
    provider = database.get_model_provider(provider_id, include_token=True)
    if not provider:
        raise ProviderConfigError(
            f"Model provider '{provider_id}' is not configured (it may have been "
            f"deleted). Update the profile to pick an available model."
        )
    if not provider.get("enabled"):
        raise ProviderConfigError(
            f"Model provider '{provider.get('name') or provider_id}' is disabled. "
            f"Re-enable it in Settings or pick a different model."
        )

    # A Codex/OpenAI-subscription provider does NOT speak the Anthropic wire
    # protocol, so its session must talk to the in-process translator gateway
    # (Chunk A) rather than the provider's own endpoint. We hand the CLI the
    # LOOPBACK gateway base URL and a per-session opaque routing token (G4) as
    # ``ANTHROPIC_AUTH_TOKEN``; the gateway holds and decrypts the REAL upstream
    # credentials (Codex OAuth / OpenAI key) at request time. The CLI never sees
    # a real upstream secret. openrouter/custom keep the direct-gateway path
    # below UNCHANGED (they are already Anthropic-compatible endpoints).
    provider_type = provider.get("provider_type")
    auth_mode = provider.get("auth_mode")
    is_translated = provider_type in ("openai", "chatgpt") or auth_mode == "chatgpt"

    # Route through the loopback translator gateway when the main provider is
    # itself translated (openai/chatgpt) OR the caller supplied a non-empty
    # subagent leg map that needs the gateway to serve its extra legs. The latter
    # is the flagship anthropic-main + foreign-subagent case: the main leg is
    # served via anthropic_passthrough at request time, which reads the REAL
    # Anthropic API key straight from the provider row — so we deliberately do NOT
    # put that key in env in this mode. A bare anthropic/custom main with no legs
    # keeps the direct path below, byte-identical to pre-Chunk-F behavior. The
    # legs are already pre-honored (servable + enabled) by the caller's
    # assemble_subagent_legs, so we do not re-gate them here.
    route_through_gateway = is_translated or bool(subagent_legs)

    if route_through_gateway:
        from app.core.translator_gateway import routing

        gateway_port = settings.translator_gateway_port
        base_url = f"http://127.0.0.1:{gateway_port}"
        token = routing.mint_session_token(
            session_id,
            provider_id,
            provider.get("codex_account_id"),
            legs=tuple(subagent_legs),
        )
    else:
        base_url = provider.get("base_url")
        raw_token = provider.get("auth_token")

        # Resolve the auth token. A stored token must be decryptable; a provider
        # with NO token is allowed only when a base_url is configured
        # (unauthenticated self-hosted gateway) — never a native-Anthropic
        # fallback.
        if raw_token:
            if encryption.is_encrypted(raw_token):
                if not encryption.is_encryption_ready():
                    raise ProviderConfigError(
                        f"Cannot decrypt the auth token for model provider "
                        f"'{provider.get('name') or provider_id}' — the encryption key "
                        f"is not available (admin must be logged in). Refusing to spawn."
                    )
                try:
                    token = encryption.decrypt_value(raw_token)
                except Exception as e:  # noqa: BLE001 — surfaced as a config error
                    raise ProviderConfigError(
                        f"Failed to decrypt the auth token for model provider "
                        f"'{provider.get('name') or provider_id}': {e}"
                    ) from e
            else:
                # Plaintext (migration compatibility) — pass through.
                token = raw_token
        else:
            if not base_url:
                raise ProviderConfigError(
                    f"Model provider '{provider.get('name') or provider_id}' has neither "
                    f"an auth token nor a base URL configured — nothing to route to."
                )
            token = ""  # unauthenticated self-hosted gateway

    # extra_env FIRST so the forced guard values below can never be clobbered.
    extra_env = provider.get("extra_env_json") or {}
    if isinstance(extra_env, dict):
        for k, v in extra_env.items():
            env[str(k)] = str(v)

    # Forced overlay values LAST (extra_env cannot override these).
    env["ANTHROPIC_BASE_URL"] = base_url or ""
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env["ANTHROPIC_API_KEY"] = ""  # explicit empty → blocks the Anthropic fallback
    env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK"] = "1"

    # A translated (Responses-coerced) main forces the CLI to fill every Agent-tool
    # property on every call. This marker wires the downstream S8-R1 PreToolUse hook
    # (via attach_hooks_to_options) that strips the grammar-coerced Agent-tool
    # `model`/`isolation` args so stamped-frontmatter subagent pins survive.
    if is_translated:
        env["AIH_GATEWAY_TRANSLATED_MAIN"] = "1"

    max_output = provider.get("max_output_tokens")
    if max_output:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output)

    context_window = provider.get("context_window")
    if context_window:
        # Clamp to ≥100k: the CLI's auto-compact window must not undercut a usable
        # working context even if the provider advertises a tiny window. Use
        # setdefault so a profile-supplied preset wins — preset-wins parity with
        # the native path (see the setdefault at the top of this module).
        env.setdefault(
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
            str(max(int(context_window), 100000)),
        )

    # R4: point the small/fast model at something the gateway knows. Fall back to
    # a STABLE provider-scoped value (never the per-turn `model`): a within-provider
    # composer model switch must not change this overlay key, or
    # requires_reconnect() forces a respawn that kills background subagents.
    small_fast = _small_fast_model(provider)
    if small_fast:
        env["ANTHROPIC_SMALL_FAST_MODEL"] = small_fast

    # S3: CLAUDE_CODE_SUBAGENT_MODEL is intentionally NOT emitted here. Subagent
    # model selection moved to per-agent frontmatter stamping (the caller stamps
    # each enabled subagent's `model:` via the S2 resolver, and threads the
    # honored leg map into the routing token above). An unstamped CLI built-in
    # Task subagent therefore inherits the MAIN model rather than a provider-wide
    # small/fast override.

    return env


def _first_curated_model_id(provider: Dict[str, Any]) -> Optional[str]:
    """First curated model id from a provider row, or None.

    Defensive about the row shape: ``models_json`` is normally a parsed list of
    ``{id, label}`` dicts, but may arrive as a raw JSON string (or be absent).
    Reads the first entry's ``id`` without raising.
    """
    models = provider.get("models_json")
    if isinstance(models, str):
        try:
            models = json.loads(models)
        except Exception:
            return None
    if not isinstance(models, list) or not models:
        return None
    first = models[0]
    if isinstance(first, dict):
        model_id = first.get("id")
        return model_id if isinstance(model_id, str) and model_id else None
    return None


def _build_env_with_ai_tools(
    base_env: Optional[Dict[str, str]],
    ai_tools_config: Optional[Dict[str, Any]],
    api_user_id: Optional[str] = None,
    provider_id: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build environment variables dict, injecting AI tool credentials when enabled.

    This allows the Claude CLI to execute scripts that use AI tools
    with the necessary API keys, without exposing them in the prompt.

    NEW DESIGN: Inject ALL available API keys so Claude can dynamically switch
    between providers without needing settings changes. The default provider/model
    is still set, but Claude can override per-request.

    CREDENTIAL RESOLUTION: When api_user_id is provided, credentials are resolved
    based on admin policy (admin_provided, user_provided, optional). This allows
    API users to use their own keys when required by policy.

    Args:
        base_env: Base environment variables from profile config
        ai_tools_config: Dict with individual tool toggles (e.g., {"image_generation": True})
        api_user_id: Optional API user ID for user-specific credential resolution

    Returns:
        Dict of environment variables to pass to Claude CLI
    """
    env = dict(base_env) if base_env else {}

    # Strip CLAUDECODE env var to prevent "nested session" detection when
    # the server is started from within a Claude Code session.  The SDK
    # merges os.environ with this dict, so setting it to empty overrides
    # the inherited value.
    env.pop("CLAUDECODE", None)
    env["CLAUDECODE"] = ""

    # ALWAYS-ON: positively attribute every spawned CLI subprocess to this
    # install. The boot-time stray-process sweep (app.core.startup_reconcile)
    # only terminates leftover CLI processes whose environment carries THIS
    # install's instance id — inherited env is the one marker that survives the
    # parent server dying, works on both Windows and Linux via psutil, and can
    # never match a process we didn't spawn.
    env["AI_SHUFFLE_MANAGED_BY"] = settings.effective_instance_id

    # ALWAYS-ON: wire the stored anthropic_api_key in as the Claude Code auth
    # credential for EVERY session, regardless of whether AI tools are enabled.
    # Must happen BEFORE the ai_tools early-returns below — auth is not gated on
    # ai_tools. (Key set → API-key auth wins; key absent → OAuth account auth.)
    # For provider sessions (provider_id set) the Anthropic key is NOT injected;
    # apply_provider_env owns auth for the gateway.
    inject_claude_code_auth(env, provider_id)

    # NOTE: The browser integration is now "Claude in Chrome" (the bundled CLI's
    # --chrome flag), not the Playwright MCP server. The CLI manages its own
    # native-messaging host and browser connection, so no browser env vars need
    # to be threaded here (the old PLAYWRIGHT_BROWSERS_PATH threading was removed).

    if not ai_tools_config:
        return env

    # Check if any AI tools are enabled
    any_image_tools = ai_tools_config.get("image_generation", False) or ai_tools_config.get("image_editing", False) or ai_tools_config.get("image_reference", False)
    any_video_tools = ai_tools_config.get("video_generation", False) or ai_tools_config.get("image_to_video", False) or ai_tools_config.get("video_extend", False)
    any_3d_tools = ai_tools_config.get("text_to_3d", False) or ai_tools_config.get("image_to_3d", False)
    any_audio_gen_tools = ai_tools_config.get("audio_generation", False) or ai_tools_config.get("audio_to_audio", False)
    any_audio_tools = ai_tools_config.get("tts", False) or ai_tools_config.get("stt", False) or any_audio_gen_tools

    if not any_image_tools and not any_video_tools and not any_3d_tools and not any_audio_tools:
        return env

    # Resolve fal.ai API key (single provider architecture)
    if api_user_id:
        fal_api_key = _resolve_api_credential("fal_api_key", api_user_id)
        logger.debug(f"Resolved fal.ai credential for API user {api_user_id}")
    else:
        fal_api_key = _get_decrypted_api_key("fal_api_key")

    if fal_api_key:
        env["FAL_KEY"] = fal_api_key
        logger.debug("Injected FAL_KEY for fal.ai tools")

    # Set default provider/model from settings
    if any_image_tools:
        image_model = database.get_system_setting("image_model")
        image_edit_model = database.get_system_setting("image_edit_model")
        env["IMAGE_PROVIDER"] = "fal-ai"
        if image_model:
            env["IMAGE_MODEL"] = image_model
            logger.debug(f"Set default IMAGE_MODEL={image_model}")
        if image_edit_model:
            env["IMAGE_EDIT_MODEL"] = image_edit_model
            logger.debug(f"Set default IMAGE_EDIT_MODEL={image_edit_model}")
        image_aspect_ratio = database.get_system_setting("image_aspect_ratio")
        image_resolution = database.get_system_setting("image_resolution")
        if image_aspect_ratio:
            env["IMAGE_ASPECT_RATIO"] = image_aspect_ratio
        if image_resolution:
            env["IMAGE_RESOLUTION"] = image_resolution
        _inject_model_params(env, "IMAGE_MODEL_PARAMS", "image_model")
        _inject_model_params(env, "IMAGE_EDIT_MODEL_PARAMS", "image_edit_model")

    if any_video_tools:
        video_model = database.get_system_setting("video_model")
        i2v_model = database.get_system_setting("i2v_model")
        env["VIDEO_PROVIDER"] = "fal-ai"
        if video_model:
            env["VIDEO_MODEL"] = video_model
            logger.debug(f"Set default VIDEO_MODEL={video_model}")
        if i2v_model:
            env["I2V_MODEL"] = i2v_model
            logger.debug(f"Set default I2V_MODEL={i2v_model}")
        video_aspect_ratio = database.get_system_setting("video_aspect_ratio")
        video_duration = database.get_system_setting("video_duration")
        video_resolution = database.get_system_setting("video_resolution")
        if video_aspect_ratio:
            env["VIDEO_ASPECT_RATIO"] = video_aspect_ratio
        if video_duration:
            env["VIDEO_DURATION"] = video_duration
        if video_resolution:
            env["VIDEO_RESOLUTION"] = video_resolution
        _inject_model_params(env, "VIDEO_MODEL_PARAMS", "video_model")
        _inject_model_params(env, "I2V_MODEL_PARAMS", "i2v_model")

    if any_3d_tools:
        model3d_model = database.get_system_setting("model3d_model")
        image_to_3d_model = database.get_system_setting("image_to_3d_model")
        env["MODEL3D_PROVIDER"] = "fal-ai"
        if model3d_model:
            env["MODEL3D_MODEL"] = model3d_model
            logger.debug(f"Set default MODEL3D_MODEL={model3d_model}")
        if image_to_3d_model:
            env["IMAGE_TO_3D_MODEL"] = image_to_3d_model
            logger.debug(f"Set default IMAGE_TO_3D_MODEL={image_to_3d_model}")
        _inject_model_params(env, "MODEL3D_MODEL_PARAMS", "model3d_model")
        _inject_model_params(env, "IMAGE_TO_3D_MODEL_PARAMS", "image_to_3d_model")

    if any_audio_tools:
        tts_model = database.get_system_setting("tts_model")
        stt_model = database.get_system_setting("stt_model")
        if tts_model:
            env["TTS_MODEL"] = tts_model
        if stt_model:
            env["STT_MODEL"] = stt_model
        stt_language = database.get_system_setting("stt_language")
        if stt_language:
            env["STT_LANGUAGE"] = stt_language

    if any_audio_gen_tools:
        audio_model = database.get_system_setting("audio_model")
        audio_to_audio_model = database.get_system_setting("audio_to_audio_model")
        env["AUDIO_PROVIDER"] = "fal-ai"
        if audio_model:
            env["AUDIO_MODEL"] = audio_model
            logger.debug(f"Set default AUDIO_MODEL={audio_model}")
        if audio_to_audio_model:
            env["AUDIO_TO_AUDIO_MODEL"] = audio_to_audio_model
            logger.debug(f"Set default AUDIO_TO_AUDIO_MODEL={audio_to_audio_model}")
        _inject_model_params(env, "AUDIO_MODEL_PARAMS", "audio_model")
        _inject_model_params(env, "AUDIO_TO_AUDIO_MODEL_PARAMS", "audio_to_audio_model")

    return env


def generate_environment_details(
    working_dir: str,
    ai_tools_config: Optional[Dict[str, Any]] = None,
    execution_mode: str = "local",
    worktree_info: Optional[Dict[str, str]] = None
) -> str:
    """
    Generate environment details block for custom system prompts.
    Similar to Claude Code's dynamic environment injection.

    Args:
        working_dir: The working directory path
        ai_tools_config: Deprecated, ignored. AI tools are registered as native MCP tools
            with their own descriptions — no system prompt injection needed.
        execution_mode: Either "local" or "worktree"
        worktree_info: Dict with branch and base_branch info when in worktree mode
    """
    is_git = _is_git_repo(working_dir)
    os_version = _get_os_version()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build execution mode lines
    execution_lines = f"\nExecution mode: {execution_mode}"
    if execution_mode == "worktree" and worktree_info:
        branch = worktree_info.get("branch", "unknown")
        base_branch = worktree_info.get("base_branch", "main")
        execution_lines += f"\nCurrent branch: {branch}\nBase branch: {base_branch}"

    return f"""Here is useful information about the environment you are running in:
<env>
Working directory: {working_dir}
Is directory a git repo: {"Yes" if is_git else "No"}
Platform: {sys.platform}
OS Version: {os_version}
Today's date: {today}{execution_lines}
</env>"""
