"""
Environment setup, path resolution, and workspace configuration for Claude queries.

Contains functions for building environment variables, resolving credentials,
generating environment details blocks, and configuring security instructions.
"""

import logging
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from app.db import database
from app.core.config import settings
from app.core.platform import DeploymentMode
from app.core import encryption
from app.core.model_registry import has_1m_context

logger = logging.getLogger(__name__)


# Default instructions for displaying media and files in chat
# Can be customized via admin settings API
DEFAULT_SECURITY_INSTRUCTIONS = """
## App-Identity
You are Claude Code running inside AI Shuffle — a self-hosted web chat application (SvelteKit + FastAPI + Claude Agent SDK). When the user mentions "this app", "the app", or discusses bugs/features without specifying another project, they mean the current project that is in your workspace. Never forget this context.

## File-Sharing
MANDATORY: When you create, generate, or modify ANY file the user may want to download or view, you MUST provide a download link. Do NOT just mention the file path in prose — the user cannot click a raw path.

Format: 📎[filename.ext](/api/files/by-path?path=/full/path/to/file.ext)

## Generated Media in Chat

When an AI tool (generateImage, editImage, generateWithReference, generateVideo, etc.) returns a successful result, display the media inline using the `image_url` or `video_url` from the tool result:

- **Images:** `![Description](IMAGE_URL_FROM_RESULT)`
- **Videos:** `[Description](VIDEO_URL_FROM_RESULT)`

Example - after generateImage returns `{"success": true, "image_url": "/api/generated-images/by-path?path=..."}`:

## Chat-History
When the user references a .jsonl chat history file from ~/.claude/projects/: this is a previous conversation for context, NOT a file to edit. Scan for key decisions, code discussed, and user preferences. Summarize what you found. Do not read every line unless explicitly asked to summarize the full conversation.
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


def _build_env_with_ai_tools(
    base_env: Optional[Dict[str, str]],
    ai_tools_config: Optional[Dict[str, Any]],
    api_user_id: Optional[str] = None
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
    any_audio_tools = ai_tools_config.get("tts", False) or ai_tools_config.get("stt", False)

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

    return env


def apply_1m_context_env(env: Dict[str, str], model: Optional[str]) -> Dict[str, str]:
    """Pin the 1M context window to the profile's toggle (mutates and returns env).

    1M-capable models (e.g. ``claude-opus-4-8``) default to the 1M context
    window on the CLI side, so disabling 1M for a profile requires explicitly
    sending ``CLAUDE_CODE_DISABLE_1M_CONTEXT=1``. The ``[1m]`` suffix on the
    model ID is the source of truth for the toggle:

        ``[1m]`` present (enabled)  -> ``CLAUDE_CODE_DISABLE_1M_CONTEXT=0``
        ``[1m]`` absent  (disabled) -> ``CLAUDE_CODE_DISABLE_1M_CONTEXT=1``

    An explicit value already present in ``env`` (e.g. a profile's custom env
    var) wins as an escape hatch and is left untouched.
    """
    if "CLAUDE_CODE_DISABLE_1M_CONTEXT" not in env:
        env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "0" if has_1m_context(model) else "1"
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
