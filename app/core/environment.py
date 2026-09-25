"""
Environment setup, path resolution, and workspace configuration for Claude queries.

Contains functions for building environment variables, resolving credentials,
generating environment details blocks, and configuring security instructions.
"""

import json
import logging
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from app.db import database
from app.db.model_providers import ModelProviderRow
from app.core.config import ENV_FILE_PATH, ENV_FILE_REF_VAR, settings
from app.core.platform import DeploymentMode
from app.core import encryption
from app.core.model_registry import resolve_provider_context_window

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

### Interactive HTML previews
When creating a website mockup, visualization, dashboard, or interactive demo for the user to view, create a self-contained .html file and include a Markdown link to it in your reply:

[Preview the mockup](/api/files/by-path?path=URL_ENCODED_FULL_PATH)

Replace URL_ENCODED_FULL_PATH with the actual file's full path, URL-encoded (including spaces, #, &, and parentheses). Write the file successfully before linking it. Use the workspace or a registered project directory so the file API can serve it. Link each HTML deliverable separately, outside code fences. Do not use a screenshot or a raw HTML code block as a substitute for the interactive deliverable.

The chat shows linked .html and .htm files as interactive preview cards after the message finishes. Users can expand the preview, switch between desktop and mobile widths, inspect the source, refresh after edits, and download the file. Reuse the same path when revising a mockup and include its link again.

Previews run in an isolated sandbox. Embed CSS and JavaScript in the HTML; use inline SVG or data URLs for assets. External scripts, fonts, stylesheets, network requests, sibling files, frames, form submissions, popups, and app/session storage are unavailable. Use client-side sample data and system fonts. Keep the file under 2 MiB. For a site that needs a development server or backend, explain that limitation and provide its normal run instructions rather than claiming the embedded preview runs the full application.
"""


def get_security_instructions() -> str:
    """Get AI Shuffle's editable chat instructions (custom or default)."""
    custom_instructions = database.get_system_setting("security_instructions")
    return custom_instructions or DEFAULT_SECURITY_INSTRUCTIONS


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
    base_extra_args: Dict[str, Optional[str]],
    hooks: Optional[Dict[str, list]],
    deployment_mode: Optional["DeploymentMode"] = None,
) -> Dict[str, Optional[str]]:
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
    subprocess failure — and we NEVER silently fall back to native
    Anthropic (which would leak the prompt to / bill the wrong upstream).
    """


def inject_claude_code_auth(
    env: Dict[str, str], provider_id: Optional[str] = None
) -> Dict[str, str]:
    """Wire the Claude Code credential of the ACTIVE auth mode into a spawn env.

    The user chooses the mode (``app.core.claude_auth_mode``); the app never
    switches it on its own, whatever credentials happen to exist:

    - ``api_key``: the Anthropic API key stored in Settings goes in as
      ``ANTHROPIC_API_KEY``. Other Anthropic credential variables from the host
      or the profile env are kept away from the CLI, so it sees that key and
      nothing else credential-like.
    - ``subscription``: nothing is injected, and any ``ANTHROPIC_API_KEY`` /
      ``ANTHROPIC_AUTH_TOKEN`` from the host or the profile env is kept away,
      so the CLI runs on the sign-in the user completed with ``claude auth
      login``. The CLI's own credential files are never read or touched here.

    Inherited host variables are removed through fluid's
    ``FLUID_CHILD_ENV_UNSET`` marker, which the delegated loop honors and never
    passes on.

    When ``provider_id`` is truthy the session targets a non-Anthropic model
    provider: ``apply_provider_env`` owns auth for those spawns (provider token
    plus an explicitly empty ``ANTHROPIC_API_KEY``), so nothing happens here.
    Returns the same dict for convenience.
    """
    if provider_id:
        return env
    from app.core import claude_auth_mode
    from fluid.tools.child_env import UNSET_ENV

    mode = claude_auth_mode.current_mode()
    if mode == claude_auth_mode.MODE_API_KEY:
        unset = claude_auth_mode.API_KEY_UNSET
        api_key = _get_decrypted_api_key(claude_auth_mode.API_KEY_SETTING)
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        else:
            # No stored key: the credential gate refuses the turn; never let a
            # key from somewhere else stand in for the user's.
            unset = unset + ("ANTHROPIC_API_KEY",)
            env.pop("ANTHROPIC_API_KEY", None)
    else:
        unset = claude_auth_mode.SUBSCRIPTION_UNSET
    for name in unset:
        env.pop(name, None)
    existing = [n for n in (env.get(UNSET_ENV) or "").split(",") if n.strip()]
    env[UNSET_ENV] = ",".join(dict.fromkeys(existing + list(unset)))
    return env


# Native legs with no explicit user choice set NO context-window env and
# inherit the CLI's own per-model defaults. An explicit per-session window from
# the composer uses the helper below and is deliberately authoritative over
# profile/provider defaults.


def apply_context_window_override(env: Dict[str, str], context_window: int) -> None:
    """Pin a user-selected input window onto a spawn environment in place."""
    window = int(context_window)
    if window <= 0:
        raise ValueError("context_window must be a positive integer")
    env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "0" if window >= 1_000_000 else "1"
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(window)
    env["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"] = str(window * 2)


def is_provider_servable(provider: Optional[ModelProviderRow]) -> bool:
    """Whether the app can serve a provider row as a first-class model source.

    True iff ``provider_type`` is one of ``openai`` / ``chatgpt`` / ``xai`` /
    ``grok`` / ``anthropic`` / ``openrouter`` (or ``auth_mode`` in
    ``chatgpt`` / ``grok``). It is the admission rule for cross-provider MODEL
    selection (``provider_agent.validate_provider_model``).

    A ``provider_type`` is admitted when it alone names a fluid client the native
    leg can drive (``provider.harness_adapter._provider_row_kind``):
    ``openai``/``chatgpt``/``xai``/``grok`` → OpenAI Responses, ``openrouter`` →
    ``OpenRouterClient``, ``anthropic`` → ``AnthropicClient`` (or the delegated
    CLI leg for a subscription ``auth_mode``).

    ``custom`` stays OUT on purpose: its leg depends on the row's ``api_dialect``
    plus a user-supplied ``base_url`` whose wire format the app cannot vouch for,
    so a worker bound to one can still 401 at connect. The ``auth_mode`` clause is
    the deliberate exception: such a row IS a subscription row wearing a
    different type, and its leg is known.
    """
    if not provider:
        return False
    ptype = provider.get("provider_type")
    auth_mode = provider.get("auth_mode")
    return (
        ptype in ("openai", "chatgpt", "xai", "grok", "anthropic", "openrouter")
        or auth_mode in ("chatgpt", "grok")
    )


def _small_fast_model(provider: Optional[ModelProviderRow]) -> Optional[str]:
    """The STABLE provider-scoped small/fast model id, or None.

    Resolution chain (never the per-turn ``model``): the provider's
    ``small_fast_model`` → ``default_model`` → first curated model id.
    """
    if not provider:
        return None
    return (
        provider.get("small_fast_model")
        or provider.get("default_model")
        or _first_curated_model_id(provider)
    )


def apply_provider_env(
    env: Dict[str, str],
    provider_id: str,
    model: Optional[str],
    session_id: Optional[str] = None,
) -> Dict[str, str]:
    """Overlay a non-Anthropic model-provider's env vars onto ``env`` (in place)
    so the Claude Code CLI talks to that provider's endpoint instead of
    api.anthropic.com. Returns the same dict for convenience.

    The row's ``extra_env_json`` is applied FIRST, then these forced values, so
    extra_env can never clobber the ``ANTHROPIC_API_KEY=""`` guard:

    - ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN`` — the provider endpoint
      (CLI appends ``/v1/messages``) and the decrypted key (``Authorization: Bearer``)
    - ``ANTHROPIC_API_KEY=""`` — MUST be explicitly empty to block an Anthropic fallback
    - ``CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`` / ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC``
      / ``CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK`` — mitigations for non-Anthropic upstreams
    - ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` when the row sets one
    - ``AIH_GATEWAY_TRANSLATED_MAIN=1`` on a translated row, which
      ``hook_service.attach_hooks_to_options`` reads to register the Agent-arg-strip hook
    - ``ANTHROPIC_SMALL_FAST_MODEL`` — a STABLE provider-scoped id (``_small_fast_model``)

    The context-window vars are ``setdefault`` (a profile/user/extra_env value
    wins) and pinned to the window resolved FOR ``model`` by
    ``model_registry.resolve_provider_context_window``; see the inline comments.
    With no known window, none is set and the CLI defaults apply.

    Translated rows (``provider_type`` openai/chatgpt/xai/grok or ``auth_mode``
    chatgpt/grok) do not speak the Anthropic wire protocol: the fluid native leg
    serves them and resolves their credentials through ``fluid_credentials``, not
    the CLI env, so ``ANTHROPIC_AUTH_TOKEN`` stays blank.

    Fail-closed: a missing/disabled provider, or a token that cannot be decrypted,
    raises :class:`ProviderConfigError`; we NEVER silently fall back to native
    Anthropic. On the direct path a row with NO stored token is allowed only with
    a ``base_url`` (an unauthenticated self-hosted endpoint). ``session_id`` does
    not affect the overlay.
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
    # protocol. Its session is served by the fluid harness's NATIVE
    # openai_responses leg, which resolves the row's real credentials itself
    # (decrypted API key, or the Codex OAuth bundle) — so nothing usable, and no
    # secret, goes into the CLI env here. openrouter/custom/anthropic take the
    # direct-decrypted-token path below (they are Anthropic-compatible endpoints).
    provider_type = provider.get("provider_type")
    auth_mode = provider.get("auth_mode")
    is_translated = (
        provider_type in ("openai", "chatgpt", "xai", "grok")
        or auth_mode in ("chatgpt", "grok")
    )

    if is_translated:
        # NOTE: deliberately NOT subject to the "neither token nor base_url"
        # guard below — a ChatGPT-subscription row legitimately carries neither
        # (its credentials live in the Codex token bundle), and raising here
        # would make every Codex session unspawnable.
        base_url = provider.get("base_url") or ""
        token = ""
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
    # property on every call. This marker wires the downstream PreToolUse hook
    # (via attach_hooks_to_options) that strips the grammar-coerced Agent-tool
    # `model`/`isolation` args so stamped-frontmatter subagent pins survive.
    if is_translated:
        env["AIH_GATEWAY_TRANSLATED_MAIN"] = "1"

    max_output = provider.get("max_output_tokens")
    if max_output:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output)

    # Context-window safety pins so a provider-routed session NEVER lets the CLI
    # self-derive an auto-compact window off an unrecognized model id (e.g.
    # ``gpt-5.x-codex``) and autocompact immediately. The pins apply to ALL
    # provider modes because every one of them faces the same risk:
    #   - translated (codex/openai): the upstream model id is foreign, so the CLI
    #     cannot recognize it — pinning is exactly the fix.
    #   - anthropic_passthrough (anthropic main + foreign legs): the id is a real
    #     ``claude-*`` id the CLI knows, so pinning is harmless.
    #   - direct openrouter/custom: same non-Anthropic-upstream risk.
    # The window is resolved FOR THE SELECTED MODEL (its own ``models_json``
    # entry → the preset roster → the row's column) via the shared
    # ``resolve_provider_context_window`` — the same resolution the registry
    # payload shows the UI. The raw row column is one number shared by every
    # model on the row, so it would pin a 1M model down to the row's value.
    # ``setdefault`` throughout so an explicit profile/user/extra_env value still
    # wins (preset-wins parity with the native path).
    #
    # CAVEAT: the CLI applies the ``env`` block of the user's own
    # ``~/.claude/settings.json`` (via ``--setting-sources=user,...``) over its
    # process env at startup, so a ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` there
    # silently clobbers these pins INSIDE the child — the spawn overlay can be
    # correct and the session still dies with "Prompt is too long". If a
    # provider session compacts/errors near 200k despite a larger
    # context_window, check that file first.
    context_window = resolve_provider_context_window(provider, model)
    if context_window:
        # A window is known for THIS model — that value is AUTHORITATIVE for
        # every window var, including the 1M gate.
        cw = int(context_window)
        env.setdefault(
            "CLAUDE_CODE_DISABLE_1M_CONTEXT", "0" if cw >= 1_000_000 else "1"
        )

        # Pin the auto-compact trigger to the row's EXACT window — no ≥100k
        # floor (inflating a small real window would delay compaction past the
        # point where requests already overflow upstream) and no hardcoded
        # fallback (forcing one universal compaction threshold is wrong across
        # providers with different windows).
        env.setdefault("CLAUDE_CODE_AUTO_COMPACT_WINDOW", str(cw))

        # The CLI's own max-context-window resolver hardcodes 200000 for any
        # model id that doesn't start with "claude-" (which every
        # provider-routed/translated id is) UNLESS CLAUDE_CODE_MAX_CONTEXT_TOKENS
        # is set — and it clamps CLAUDE_CODE_AUTO_COMPACT_WINDOW to whatever it
        # believes that max is. So without this key, a >200k provider
        # context_window is silently truncated back to 200k and near-window
        # requests die with "Prompt is too long". Like the auto-compact pin
        # above, this is the TRUE window — never clamped or defaulted.
        env.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", str(cw))
        # Even with the true window pinned above, the CLI's pre-flight
        # "Prompt is too long" guard uses a conservative local token
        # estimator (~3 chars/token vs o200k's ~4 for prose), so a turn
        # that REALLY fits the provider window can overshoot the local
        # estimate by ~33% and be hard-blocked before any request is sent
        # (e.g. the CLI's ``claude-api`` skill injects ~800k chars ≈ 200k
        # real tokens, estimated locally as ~280k). The same
        # ceiling also blocks the reactive-compaction request itself, so
        # autocompact can't rescue the turn either. Lift the local ceiling
        # to 2x the window: the local estimate is protecting against an
        # overflow the UPSTREAM already rejects cleanly, so it adds
        # nothing here — it only misfires.
        # Autocompaction thresholds are unaffected (they key off
        # CLAUDE_CODE_AUTO_COMPACT_WINDOW above).
        env.setdefault("CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE", str(cw * 2))
    # No window known for this model (no per-model value, no preset, NULL row
    # column) → set NOTHING and leave every window var to the CLI's own
    # defaults. Forcing ``DISABLE_1M=1`` here would clamp a claude-* id on an
    # anthropic row (natively 1M) back to 200k.

    # Point the small/fast model at a model this provider serves. Fall back to
    # a STABLE provider-scoped value (never the per-turn `model`): a within-provider
    # composer model switch must not change this overlay key, or
    # requires_reconnect() forces a respawn that kills background subagents.
    small_fast = _small_fast_model(provider)
    if small_fast:
        env["ANTHROPIC_SMALL_FAST_MODEL"] = small_fast

    # CLAUDE_CODE_SUBAGENT_MODEL is intentionally not set: subagents take their
    # model from per-agent frontmatter, and an unstamped CLI Task subagent
    # inherits the main model.

    return env


def _first_curated_model_id(provider: ModelProviderRow) -> Optional[str]:
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

    # ALWAYS-ON: point the subprocess at THIS process's config file by absolute
    # path. A session whose cwd is a git worktree has no `.env` of its own (it
    # is gitignored, so `git worktree add` never brings it across), and the
    # workaround agents reached for was copying the real one in — which put a
    # plaintext database password on disk in every worktree, one copy per
    # session, none of them cleaned up. A reference costs nothing, leaves no
    # copy, and keeps the secret out of the process environment: the child only
    # learns where the file is, and only if it can already read it.
    if ENV_FILE_PATH:
        env.setdefault(ENV_FILE_REF_VAR, ENV_FILE_PATH)

    # ALWAYS-ON: apply the Claude Code auth mode the user chose (API key
    # injected in api_key mode, every Anthropic key kept away in subscription
    # mode) for EVERY session, regardless of whether AI tools are enabled. Must
    # happen BEFORE the ai_tools early-returns below. For provider sessions
    # (provider_id set) nothing is touched; apply_provider_env owns auth.
    inject_claude_code_auth(env, provider_id)

    # Browser automation is the ``browser`` STDIO MCP server
    # (app.core.builtin_mcp_servers); mcp_config threads its env
    # (``_BROWSER_ENV_KEYS``), so no browser env vars are set here.

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


#: Appended after the placement line when the session executes on a shared
#: runner (or inside a container). One session spent nine minutes and a user
#: interruption starting the app inside the runner and pointing the browser
#: tool at its loopback address; nothing in the prompt said where it was.
PLACEMENT_RUNNER_NOTE = (
    "The app instance the user is looking at is a separate, already-running "
    "process — not one you can start here, and localhost here is not the "
    "browser tool's localhost. Do not treat an app you launch here as live "
    "verification: verify through the browser tool against the user's "
    "instance, a real-provider probe, or an end-to-end test that drives the "
    "producing path."
)


def _placement_lines(placement_where: Optional[str]) -> list:
    """The ``Runtime placement:`` line (plus the shared-runner note), or none.

    ``placement_where`` is ``sessions.placement["where"]`` — ``runner:<id>``
    for a pinned session, ``anywhere``/a capability token for an elected one,
    empty/None for the local lane. The note rides along for a ``runner:`` pin
    and for any session that finds itself inside a container, which are the
    two shapes where "just run the app and look at it" quietly verifies the
    wrong process.

    Session-stable by construction: a re-placed session is rebuilt by the
    runner that adopts it (new working directory, new prompt), so the line
    regenerates with the rest of the block rather than drifting mid-prefix.
    """
    where = (placement_where or "").strip()
    shared = where.startswith("runner:") or _in_container()
    if not where and not shared:
        return []
    label = where or "local"
    if shared:
        return [
            f"Runtime placement: {label} (shared runner). {PLACEMENT_RUNNER_NOTE}"
        ]
    return [f"Runtime placement: {label}"]


def _in_container() -> bool:
    """Whether this process runs inside a container. Never raises."""
    try:
        from app.core.platform import is_docker_mode

        return bool(is_docker_mode())
    except Exception:  # noqa: BLE001 — detection must never break the prompt
        return False


def generate_environment_details(
    working_dir: str,
    execution_mode: str = "local",
    worktree_info: Optional[Dict[str, str]] = None,
    include_volatile: bool = True,
    placement_where: Optional[str] = None,
) -> str:
    """
    Generate environment details block for custom system prompts.
    Similar to Claude Code's dynamic environment injection.

    Args:
        working_dir: The working directory path
        execution_mode: Either "local" or "worktree"
        worktree_info: Dict with branch and base_branch info when in worktree mode
        include_volatile: Whether to render the facts that change WITHIN a
            session (today's date, the checked-out branch). False renders the
            session-stable half only — see :func:`generate_turn_environment_note`
            for where the other half goes and why.
        placement_where: ``sessions.placement["where"]`` for this session, when
            the caller has the row. Renders WHERE the session executes — see
            :func:`_placement_lines`.

    Every line here lands in the provider's cached prompt prefix, so a line
    that changes mid-session re-bills the whole prefix (system + tools +
    history) on the next turn. What stays:

    * working directory — the model's whole frame of reference for every path
      it reads or writes, and it only ever changes together with the session's
      checkout (which invalidates the prefix on its own merits);
    * is-git / platform / OS / shell — properties of the machine and checkout,
      fixed for the life of the process;
    * execution mode — local vs worktree, which moves in lockstep with the
      working directory above;
    * runtime placement — where this session executes. It changes only when
      the session is re-placed onto another runner, and that already moves the
      working directory and the whole prompt with it.

    What leaves: today's date and the branch pair. Both change under a session
    that is otherwise untouched (UTC midnight; a checkout switch), and neither
    is worth a full prefix miss.
    """
    is_git = _is_git_repo(working_dir)
    os_version = _get_os_version()

    lines = [
        f"Working directory: {working_dir}",
        f"Is directory a git repo: {'Yes' if is_git else 'No'}",
        f"Platform: {sys.platform}",
        f"OS Version: {os_version}",
        "Shell: " + (
            "cmd.exe (use Windows cmd syntax)" if os.name == "nt"
            else "/bin/sh-compatible (use POSIX shell syntax)"
        ),
    ]
    if include_volatile:
        lines.append(f"Today's date: {_today()}")
    lines.append(f"Execution mode: {execution_mode}")
    lines.extend(_placement_lines(placement_where))
    if include_volatile:
        lines.extend(_worktree_lines(execution_mode, worktree_info))

    body = "\n".join(lines)
    return (
        "Here is useful information about the environment you are running in:\n"
        f"<env>\n{body}\n</env>"
    )


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _worktree_lines(
    execution_mode: str, worktree_info: Optional[Dict[str, str]]
) -> list:
    """``Current branch`` / ``Base branch``, or nothing outside worktree mode."""
    if execution_mode != "worktree" or not worktree_info:
        return []
    return [
        f"Current branch: {worktree_info.get('branch', 'unknown')}",
        f"Base branch: {worktree_info.get('base_branch', 'main')}",
    ]


TURN_ENV_HEADER = "Environment facts that change while a session runs"


def generate_turn_environment_note(
    execution_mode: str = "local",
    worktree_info: Optional[Dict[str, str]] = None,
) -> str:
    """The volatile half of the <env> block, rendered fresh for one turn.

    This rides the turn's user message instead of the system prompt: it sits
    after every cache breakpoint (system tail, tool-defs tail, last stable
    history position), so a date rollover or a branch switch costs the tokens
    of these few lines rather than a re-send of the entire prefix. The model
    reads the same facts either way — only their position in the request moved.

    Re-rendered per turn rather than once per session, so the newest copy is
    always current; the header says so, because older turns keep their own
    copies in history and those can name a branch that has since changed.
    """
    lines = [f"Today's date: {_today()}"]
    lines.extend(_worktree_lines(execution_mode, worktree_info))
    body = "\n".join(lines)
    return (
        "<system-reminder>\n"
        f"{TURN_ENV_HEADER} (current as of this message — any earlier copy in "
        "this conversation may be out of date):\n"
        f"{body}\n"
        "</system-reminder>"
    )


# ─── "# Workspace facts" — the first-turn bootstrap card ─────────────────────
#
# Without it, agents rediscover the same environment facts by probing in session
# after session: node_modules missing in a fresh worktree, which interpreter
# has pytest, that the agent browser lives on another host, that the app's own
# UI rejects proxied requests, that frontend/vite.config.ts does not exist.
# This card states the ones that are TRUE for this worker, probed once per
# process, rendered once per session, and never varying between turns (it sits
# in the cached prompt prefix).

WORKSPACE_FACTS_HEADER = "# Workspace facts"
#: Hard ceiling on the card, in characters (~600 tokens).
WORKSPACE_FACTS_MAX_CHARS = 2400


def _package_dirs(cwd: Path) -> list:
    """Directories under ``cwd`` (root and one level of the usual names) that
    carry a package.json, as paths relative to ``cwd``."""
    found = []
    for rel in ("", "frontend", "browser-mcp", "tools", "desktop"):
        directory = cwd / rel if rel else cwd
        if (directory / "package.json").is_file():
            found.append(rel or ".")
    return found


def _node_dependency_facts(cwd: Path) -> list:
    """One line per package dir: whether its node_modules is installed."""
    missing = []
    present = []
    for rel in _package_dirs(cwd):
        directory = cwd / rel if rel != "." else cwd
        (present if (directory / "node_modules" / ".bin").is_dir() else missing).append(rel)
    lines = []
    if missing:
        lines.append(
            "Node: node_modules MISSING in " + ", ".join(missing) + " — run `npm ci` "
            "there before any dev server, build or test (never junction or copy "
            "node_modules from another checkout)."
        )
    if present:
        lines.append("Node: node_modules installed in " + ", ".join(present) + ".")
    return lines


def _interpreter_has_pytest(executable: str) -> bool:
    """Whether ``executable`` can import pytest. Bounded subprocess; False on
    any failure (a missing binary, a timeout, a broken install)."""
    import subprocess

    try:
        result = subprocess.run(
            [executable, "-c", "import pytest"],
            capture_output=True, timeout=10, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_PYTEST_PROBE: dict = {}


def pytest_interpreter() -> Optional[str]:
    """The first interpreter that has pytest: this process's own, then the
    conventional names on PATH. Probed once per process — the answer does not
    change while the worker runs, and the card it feeds must not either."""
    if "value" in _PYTEST_PROBE:
        return _PYTEST_PROBE["value"]
    import importlib.util
    import shutil

    found: Optional[str] = None
    try:
        if importlib.util.find_spec("pytest") is not None:
            found = sys.executable
    except (ImportError, ValueError):
        found = None
    if found is None:
        names = ("python3", "python", "py") if os.name != "nt" else ("python", "py", "python3")
        for name in names:
            resolved = shutil.which(name)
            if not resolved or os.path.normcase(resolved) == os.path.normcase(sys.executable):
                continue
            if _interpreter_has_pytest(resolved):
                found = resolved
                break
    _PYTEST_PROBE["value"] = found
    return found


def _python_facts(cwd: Path, is_shuffle: bool) -> list:
    exe = pytest_interpreter()
    if exe is None:
        return [
            f"Python: no interpreter with pytest was found (this worker runs "
            f"{sys.executable}); install pytest into a venv before running the "
            "backend suite."
        ]
    own = os.path.normcase(exe) == os.path.normcase(sys.executable)
    where = "this worker's own interpreter" if own else "on PATH"
    line = f"Python: pytest is importable in `{exe}` ({where})."
    if is_shuffle:
        line += (
            f" Backend suite: `PYTHONPATH=fluid {exe} -m pytest tests/ -q -p "
            "no:cacheprovider` from the checkout root (PYTHONPATH=fluid is "
            "required; pytest-timeout is not installed, so drop --timeout)."
        )
    return [line]


def _is_ai_shuffle_checkout(cwd: Path) -> bool:
    return (cwd / "app" / "preview.py").is_file() and (cwd / "fluid").is_dir()


def _shuffle_facts(cwd: Path, app_port: Optional[int], preview_url: Optional[str]) -> list:
    """Facts that are only true for an ai-shuffle checkout."""
    lines = []
    port = app_port or 8000
    ui = (
        f"This app: the worker listens on :{port} behind an owner-auth guard "
        "that admits direct LAN peers only and rejects proxied/forwarded "
        "requests."
    )
    if preview_url:
        ui += (
            f" To open its UI from the agent browser, navigate ONCE to "
            f"{preview_url} (a token bound to this session; it sets the cookie "
            "the rest of the visit uses, and the cookie also passes through a "
            "branch's Vite dev proxy). Mutations of auth, credentials, "
            "settings and network config are refused on that cookie."
        )
    lines.append(ui)
    lines.append(
        "Branch UI preview: build the frontend, run `python -m app.preview "
        "stamp`, then start an isolated instance with AI_SHUFFLE_PREVIEW=1 on "
        "another port (see the preview-branch skill). Never start a second "
        "worker against the running instance's data dir."
    )
    vite = "frontend/vite.config.js" if (cwd / "frontend" / "vite.config.js").is_file() else None
    if vite and not (cwd / "frontend" / "vite.config.ts").is_file():
        lines.append(f"Frontend: the Vite config is `{vite}` — there is no vite.config.ts.")
    if (cwd / "tests" / "pg_shim.py").is_file():
        lines.append(
            "Tests: tests/pg_shim.py cannot run `SET`; failures whose message "
            "names SET syntax are pre-existing shim limits, not your change."
        )
    return lines


def workspace_facts_card(
    working_dir: str,
    *,
    browser_host: Optional[str] = None,
    app_port: Optional[int] = None,
    preview_url: Optional[str] = None,
) -> str:
    """The first-turn bootstrap card. Deterministic for a given worker and
    session: every probe here is either filesystem state at render time or a
    per-process cached answer, so re-rendering inside one session produces the
    same bytes. Never raises."""
    cwd = Path(working_dir or ".")
    lines: list = []
    try:
        is_shuffle = _is_ai_shuffle_checkout(cwd)
        lines.extend(_node_dependency_facts(cwd))
        lines.extend(_python_facts(cwd, is_shuffle))
        if browser_host:
            lines.append(
                "Agent browser: may run on a different host from this shell (it "
                "does on the container and shared runners). Bind any server you "
                f"start to 0.0.0.0 and open it at http://{browser_host}:<port> — "
                "that address works whether the browser is local or not; "
                "localhost only works when it is."
            )
        else:
            lines.append(
                "Agent browser: may run on a different host from this shell. Bind "
                "servers to 0.0.0.0 and open them by this machine's LAN address, "
                "which works whether the browser is local or not."
            )
        if is_shuffle:
            lines.extend(_shuffle_facts(cwd, app_port, preview_url))
    except Exception:  # noqa: BLE001 — a card never fails a session
        logger.warning("workspace facts card failed", exc_info=True)
    body = "\n".join(f"- {line}" for line in lines)
    card = (
        f"{WORKSPACE_FACTS_HEADER}\n"
        "Probed once for this worker at session start; these hold for every "
        "turn. Recall before re-probing anything not listed here, and Remember "
        "what you establish.\n"
        f"{body}"
    )
    if len(card) > WORKSPACE_FACTS_MAX_CHARS:
        card = card[:WORKSPACE_FACTS_MAX_CHARS - 1].rstrip() + "…"
    return card
