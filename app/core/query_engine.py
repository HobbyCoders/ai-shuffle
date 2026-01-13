"""
Query engine for executing Claude queries with profiles

Based on patterns from Anvil's SessionManager:
- Keep SDK clients connected for the lifetime of a session
- Don't disconnect after every query (causes async context issues)
- Create new client with 'resume' option when resuming after app restart
"""

import logging
import platform
import re
import subprocess
import sys
import uuid
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime

from claude_agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient, AgentDefinition
from claude_agent_sdk import (
    AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    ResultMessage, SystemMessage
)
from claude_agent_sdk.types import StreamEvent, PermissionResultAllow, PermissionResultDeny

from app.db import database
from app.core.config import settings
from app.core.profiles import get_profile
from app.core.sync_engine import sync_engine
from app.core.permission_handler import permission_handler
from app.core.platform import detect_deployment_mode, DeploymentMode
from app.core.user_question_handler import user_question_handler
from app.core import encryption

logger = logging.getLogger(__name__)

# Maximum size for tool output before truncation (in characters)
# Keep well under 1MB to avoid WebSocket JSON buffer overflow
MAX_TOOL_OUTPUT_SIZE = 500_000  # ~500KB to leave room for JSON overhead
MAX_DISPLAY_OUTPUT_SIZE = 2000  # What we show in the UI

# Default SDK buffer size for reading CLI output (50MB)
# The SDK's default is 1MB which is too small for images/PDFs (base64 encoded)
# This must match the max_buffer_size in ClaudeAgentOptions
DEFAULT_SDK_BUFFER_SIZE = 50 * 1024 * 1024  # 50MB


def truncate_large_payload(content: Any, max_size: int = MAX_TOOL_OUTPUT_SIZE) -> str:
    """
    Intelligently truncate large tool output payloads.

    Handles:
    - Base64 encoded images/files (detected by pattern)
    - Very long text outputs
    - List/dict content that serializes to large strings

    Args:
        content: The raw content from a ToolResultBlock
        max_size: Maximum allowed size in characters

    Returns:
        Truncated string with informative message if content was too large
    """
    if content is None:
        return ""

    # Convert to string for size checking
    content_str = str(content)
    content_len = len(content_str)

    # If it's small enough, return as-is (truncated for display)
    if content_len <= max_size:
        return content_str[:MAX_DISPLAY_OUTPUT_SIZE]

    # Check if it looks like base64 image data
    # Common patterns: data:image/..., very long alphanumeric strings, iVBOR (PNG), /9j/ (JPEG)
    is_likely_base64 = (
        'data:image/' in content_str[:100] or
        content_str[:10].startswith('iVBOR') or  # PNG base64 signature
        content_str[:10].startswith('/9j/') or   # JPEG base64 signature
        content_str[:10].startswith('R0lGOD') or # GIF base64 signature
        # Check for very high ratio of alphanumeric chars (typical of base64)
        (content_len > 10000 and sum(c.isalnum() or c in '+/=' for c in content_str[:1000]) > 950)
    )

    if is_likely_base64:
        # Calculate approximate original size
        original_size_kb = int(content_len * 0.75 / 1024)  # base64 is ~33% larger
        return f"[Image/binary content - {original_size_kb}KB - content processed by Claude but truncated for display]"

    # For other large content, truncate with indicator
    size_kb = content_len // 1024
    truncated = content_str[:MAX_DISPLAY_OUTPUT_SIZE]
    return f"{truncated}\n\n[... truncated - total size: {size_kb}KB]"


def write_agents_to_filesystem(agents_dict: Dict[str, AgentDefinition], cwd: str) -> Optional[Path]:
    """
    Write subagent definitions to .claude/agents/ directory in the working directory.

    WINDOWS-ONLY WORKAROUND: The Claude Code CLI has a known bug where --agents flag
    doesn't work properly on Windows. Instead, we write agent files to .claude/agents/
    which the CLI discovers on startup.

    On Docker/Linux, agents are passed directly via the SDK's --agents flag (which works
    correctly there) and this function should NOT be called.

    Args:
        agents_dict: Dict mapping agent IDs to AgentDefinition objects
        cwd: Working directory where .claude/agents/ will be created

    Returns:
        Path to the agents directory if created, None if no agents
    """
    if not agents_dict:
        return None

    # Create .claude/agents directory in the working directory
    agents_dir = Path(cwd) / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    written_files = []

    for agent_id, agent_def in agents_dict.items():
        # Build YAML frontmatter
        frontmatter_lines = [
            "---",
            f"name: {agent_id}",
            f"description: {agent_def.description}",
        ]

        # Add tools if specified
        if agent_def.tools:
            tools_str = ", ".join(agent_def.tools)
            frontmatter_lines.append(f"tools: {tools_str}")

        # Add model if specified
        if agent_def.model:
            frontmatter_lines.append(f"model: {agent_def.model}")

        frontmatter_lines.append("---")
        frontmatter_lines.append("")  # Empty line after frontmatter

        # Build full file content
        content = "\n".join(frontmatter_lines) + agent_def.prompt

        # Write to file
        agent_file = agents_dir / f"{agent_id}.md"
        agent_file.write_text(content, encoding="utf-8")
        written_files.append(agent_file)
        logger.info(f"Wrote subagent file: {agent_file}")

    logger.info(f"Wrote {len(written_files)} subagent files to {agents_dir}")
    return agents_dir


def cleanup_agents_directory(agents_dir: Path, agent_ids: list) -> None:
    """
    Clean up subagent files that were written for this session.

    Only removes files that match the agent IDs we wrote, to avoid
    removing user-created agents.

    Args:
        agents_dir: Path to .claude/agents/ directory
        agent_ids: List of agent IDs to remove
    """
    if not agents_dir or not agents_dir.exists():
        return

    for agent_id in agent_ids:
        agent_file = agents_dir / f"{agent_id}.md"
        if agent_file.exists():
            try:
                agent_file.unlink()
                logger.debug(f"Removed subagent file: {agent_file}")
            except Exception as e:
                logger.warning(f"Failed to remove subagent file {agent_file}: {e}")


def _build_plugins_list(
    enabled_plugins: Optional[list],
    setting_sources: Optional[list] = None
) -> Optional[list]:
    """
    Build the plugins list for ClaudeAgentOptions from profile's enabled_plugins.

    According to the Claude Agent SDK documentation, plugins are specified as:
    [{"type": "local", "path": "./my-plugin"}, ...]

    IMPORTANT: When setting_sources includes "user" or "project", Claude Code CLI
    automatically loads plugins from ~/.claude/settings.json (enabledPlugins).
    To avoid duplication, we only return explicit plugins here when:
    1. setting_sources doesn't include "user" or "project", OR
    2. enabled_plugins contains paths (not plugin IDs) that wouldn't be auto-loaded

    Supports two formats for enabled_plugins entries:
    1. Plugin IDs: "plugin-name@marketplace" - resolved via installed_plugins.json
    2. Direct paths: "/path/to/plugin" or "relative/path" - used directly

    Args:
        enabled_plugins: List of plugin IDs or paths from profile config
        setting_sources: List of setting sources (e.g., ["user", "project"])

    Returns:
        List of plugin dicts for SDK, or None if no plugins enabled/needed
    """
    if not enabled_plugins:
        return None

    # Check if Claude Code will auto-load plugins from settings
    # When "user" or "project" is in setting_sources, Claude reads settings.json
    # which contains enabledPlugins - those plugins are loaded automatically
    auto_loads_from_settings = setting_sources and (
        "user" in setting_sources or "project" in setting_sources
    )

    if auto_loads_from_settings:
        # Filter to only include direct path entries (not plugin IDs)
        # Plugin IDs (name@marketplace) are already in settings.json and will be auto-loaded
        # Direct paths won't be auto-loaded, so we need to pass them explicitly
        direct_path_plugins = [
            p for p in enabled_plugins
            if p and (p.startswith("/") or p.startswith(".") or "@" not in p)
        ]

        if not direct_path_plugins:
            # All plugins are IDs that will be auto-loaded from settings
            logger.info("Skipping explicit plugins list - Claude Code will auto-load from settings.json")
            return None

        # Only process direct paths
        enabled_plugins = direct_path_plugins
        logger.info(f"Setting sources includes user/project - only passing {len(direct_path_plugins)} direct path plugin(s)")

    # Try to load installed plugins registry to resolve plugin IDs
    # Import here to avoid circular imports
    from app.core.plugin_service import get_plugin_service
    service = get_plugin_service()
    installed_plugins = {}
    installed_plugins_file = service.INSTALLED_FILE
    try:
        if installed_plugins_file.exists():
            import json
            with open(installed_plugins_file, 'r') as f:
                data = json.load(f)
                installed_plugins = data.get("plugins", {})
    except Exception as e:
        logger.warning(f"Failed to load installed_plugins.json: {e}")

    plugins = []
    seen_paths = set()  # Avoid duplicates

    for plugin_entry in enabled_plugins:
        if not plugin_entry:
            continue

        resolved_path = None

        # Check if this is a plugin ID format (name@marketplace)
        if "@" in plugin_entry and not plugin_entry.startswith("/") and not plugin_entry.startswith("."):
            # Look up in installed_plugins registry
            if plugin_entry in installed_plugins:
                installations = installed_plugins[plugin_entry]
                if installations:
                    resolved_path = Path(installations[0].get("installPath", ""))
                    if not resolved_path.exists():
                        logger.warning(f"Plugin install path not found: {resolved_path}")
                        resolved_path = None
            else:
                logger.warning(f"Plugin not found in installed_plugins.json: {plugin_entry}")
        else:
            # Direct path format
            path = Path(plugin_entry)
            if path.is_absolute():
                resolved_path = path
            else:
                resolved_path = settings.workspace_dir / plugin_entry

        if resolved_path and str(resolved_path) not in seen_paths:
            # Verify the plugin directory exists (manifest path checked for existence)
            _plugin_manifest = resolved_path / ".claude-plugin" / "plugin.json"
            if resolved_path.exists():
                plugins.append({
                    "type": "local",
                    "path": str(resolved_path)
                })
                seen_paths.add(str(resolved_path))
                logger.info(f"Adding plugin: {resolved_path}")
            else:
                logger.warning(f"Plugin directory not found: {resolved_path}")

    if not plugins:
        return None

    logger.info(f"Built plugins list with {len(plugins)} plugin(s)")
    return plugins


@dataclass
class SessionState:
    """Track state for an active SDK session"""
    client: ClaudeSDKClient
    sdk_session_id: Optional[str] = None
    is_connected: bool = False
    is_streaming: bool = False
    interrupt_requested: bool = False  # Flag to signal interrupt request
    last_activity: datetime = field(default_factory=datetime.now)
    background_task: Optional[asyncio.Task] = None  # Track background streaming task
    written_agent_ids: list = field(default_factory=list)  # Track agents written to filesystem
    agents_dir: Optional[Path] = None  # Path to .claude/agents/ directory


# Track active sessions - key is our session_id, value is SessionState
_active_sessions: Dict[str, SessionState] = {}


def get_session_state(session_id: str) -> Optional[SessionState]:
    """Get the active session state for a session ID"""
    return _active_sessions.get(session_id)


# Default instructions for displaying media and files in chat
# Can be customized via admin settings API
DEFAULT_SECURITY_INSTRUCTIONS = """
Chat and its Capabilities:
## Displaying Media and Files in Chat

The chat UI can render images, videos, and file download cards.

### Media Formats
- **Images:** `![Description](/api/generated-images/filename.png)`
- **Videos:** `[Description](/api/generated-videos/filename.mp4)`
- **Files:** `📎[filename.ext](/api/files/by-path?path=/full/path/to/file.ext)`

---

### Sharing Files for Download

To make any workspace file downloadable, use this markdown format:
```
📎[filename.ext](/api/files/by-path?path=/full/path/to/file.ext)
```

**Example:** If you created `/workspace/my-app/output/report.pdf`:
```
📎[report.pdf](/api/files/by-path?path=/workspace/my-app/output/report.pdf)
```

Provide download links when you create standalone files the user might want (documents, exports, scripts, etc.).

---

## Chat History References

When the user references a chat history file (`.jsonl` file from `~/.claude/projects/`):

1. **Purpose**: This is a previous conversation provided as context - NOT a file to edit
2. **Efficient reading**: Scan for relevant context rather than reading every message in full. The JSONL format contains one message per line with `type`, `message`, and `timestamp` fields
3. **Focus on**: Key decisions made, code discussed, problems solved, user preferences expressed
4. **Don't**: Read the entire file message-by-message unless specifically asked to summarize the whole conversation
5. **Summarize**: Tell the user what relevant context you found from the referenced conversation
6. **Format**: Messages alternate between `"type":"user"` and `"type":"assistant"`. Tool uses are embedded in assistant messages, tool results follow in subsequent user messages.

This helps the model understand past context without overwhelming the current conversation.
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


def _get_available_providers() -> Dict[str, Any]:
    """
    Get all available AI providers based on configured API keys.
    Returns a dict with image_providers, video_providers, and model3d_providers that have valid keys.
    """
    gemini_key = _get_decrypted_api_key("image_api_key")
    openai_key = _get_decrypted_api_key("openai_api_key")
    meshy_key = _get_decrypted_api_key("meshy_api_key")

    # Image providers with their capabilities
    image_providers = {}
    if gemini_key:
        image_providers["google-gemini"] = {
            "name": "Nano Banana",
            "models": ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"],
            "capabilities": ["text-to-image", "image-edit", "image-reference"],
            "best_for": "Fast iteration, editing, reference images"
        }
        image_providers["google-imagen"] = {
            "name": "Imagen 4",
            "models": ["imagen-4.0-fast-generate-001", "imagen-4.0-generate-001", "imagen-4.0-ultra-generate-001"],
            "capabilities": ["text-to-image"],
            "best_for": "Highest quality, photo-realism"
        }
    if openai_key:
        image_providers["openai-gpt-image"] = {
            "name": "GPT Image",
            "models": ["gpt-image-1"],
            "capabilities": ["text-to-image", "image-edit"],
            "best_for": "Accurate text in images, inpainting"
        }

    # Video providers with their capabilities
    video_providers = {}
    if gemini_key:
        video_providers["google-veo"] = {
            "name": "Veo",
            "models": ["veo-3.1-fast-generate-preview", "veo-3.1-generate-preview", "veo-3-fast-generate-preview", "veo-3-generate-preview"],
            "capabilities": ["text-to-video", "image-to-video", "video-extend", "frame-bridge", "video-with-audio"],
            "best_for": "Video extension, frame bridging, native audio (Veo 3)",
            "durations": "4, 6, or 8 seconds"
        }
    if openai_key:
        video_providers["openai-sora"] = {
            "name": "Sora",
            "models": ["sora-2", "sora-2-pro"],
            "capabilities": ["text-to-video", "image-to-video"],
            "best_for": "Fast generation, good quality",
            "durations": "4, 8, or 12 seconds"
        }

    # 3D Model providers with their capabilities
    model3d_providers = {}
    if meshy_key:
        model3d_providers["meshy"] = {
            "name": "Meshy AI",
            "models": ["latest", "meshy-5", "meshy-4"],  # latest = Meshy 6 Preview
            "capabilities": ["text-to-3d", "image-to-3d", "retexture", "rigging", "animation"],
            "best_for": "3D model generation, texturing, rigging, animation",
            "credits": "5-20 per generation"
        }

    return {
        "image_providers": image_providers,
        "video_providers": video_providers,
        "model3d_providers": model3d_providers,
        "has_gemini": bool(gemini_key),
        "has_openai": bool(openai_key),
        "has_meshy": bool(meshy_key)
    }


def _build_ai_tools_section(
    ai_tools_config: Optional[Dict[str, Any]] = None,
    prefix: str = "\n\n",
    api_keys: Optional[Dict[str, str]] = None
) -> str:
    """
    Build the <ai-tools> section for system prompts with multi-provider support.

    This compact version provides tool paths and key parameters without verbose examples.
    Claude knows how to write the code - we just need to tell it what's available.

    Args:
        ai_tools_config: Dict with individual tool toggles (e.g., {"image_generation": True})
        prefix: String to prepend before the section (default: "\n\n")
        api_keys: Optional dict with API keys to include in technical notes

    Returns:
        The formatted <ai-tools> section, or empty string if no tools enabled
    """
    if not ai_tools_config:
        return ""

    providers = _get_available_providers()
    tools_dir = settings.effective_tools_dir

    # Convert Windows paths to file:// URLs for ESM imports
    tools_dir_str = str(tools_dir)
    if platform.system() == "Windows" and len(tools_dir_str) >= 2 and tools_dir_str[1] == ':':
        tools_dir_str = "file:///" + tools_dir_str.replace("\\", "/")

    # Check if any AI tools are enabled
    any_image_tools = ai_tools_config.get("image_generation", False) or ai_tools_config.get("image_editing", False) or ai_tools_config.get("image_reference", False)
    any_video_tools = ai_tools_config.get("video_generation", False) or ai_tools_config.get("image_to_video", False) or ai_tools_config.get("video_extend", False) or ai_tools_config.get("video_bridge", False) or ai_tools_config.get("video_with_audio", False) or ai_tools_config.get("video_understanding", False)
    any_3d_tools = ai_tools_config.get("text_to_3d", False) or ai_tools_config.get("image_to_3d", False) or ai_tools_config.get("retexture_3d", False) or ai_tools_config.get("rig_3d", False) or ai_tools_config.get("animate_3d", False)

    if not any_image_tools and not any_video_tools and not any_3d_tools:
        return ""

    if not providers["image_providers"] and not providers["video_providers"] and not providers["model3d_providers"]:
        return ""

    tools_section = f"{prefix}<ai-tools>\n"
    tools_section += """## AI Content Generation Tools

**Usage:** Write `.mjs` scripts, run with `node script.mjs`, delete after. All tools return JSON with `file_path` and URL fields.

### Tool Selection (MANDATORY)
| Intent | Tool | NOT |
|--------|------|-----|
| Analyze/describe video | `analyzeVideo` | ❌ WebFetch/Read |
| Generate image | `generateImage` | ❌ WebSearch |
| Edit existing image | `editImage` | ❌ generateImage |
| Generate video from text | `generateVideo` | ❌ WebSearch |
| Animate image to video | `imageToVideo` | ❌ generateVideo |
| Extend Veo video | `extendVideo` | ❌ generateVideo |
| Create 3D from text | `textTo3D` | ❌ image gen |
| Convert image to 3D | `imageTo3D` | ❌ textTo3D |

"""

    # =========================================================================
    # IMAGE TOOLS
    # =========================================================================
    if any_image_tools and providers["image_providers"]:
        tools_section += "### 🖼️ Image Tools\n"
        tools_section += "**Providers:** "
        provider_list = []
        for pid, pinfo in providers["image_providers"].items():
            edit_mark = " (edit)" if "image-edit" in pinfo["capabilities"] else ""
            provider_list.append(f"`{pid}`{edit_mark}")
        tools_section += ", ".join(provider_list) + "\n\n"

        if ai_tools_config.get("image_generation", False):
            tools_section += f"- **generateImage**: `import {{ generateImage }} from '{tools_dir_str}/dist/image-generation/generateImage.js'`\n"
            tools_section += "  - Params: `prompt`, `provider` → Returns: `image_url`, `file_path`\n"

        if ai_tools_config.get("image_editing", False):
            tools_section += f"- **editImage**: `import {{ editImage }} from '{tools_dir_str}/dist/image-generation/editImage.js'`\n"
            tools_section += "  - Params: `prompt`, `image_path`, `provider` (gemini/openai only) → Returns: `image_url`\n"

        if ai_tools_config.get("image_reference", False) and providers["has_gemini"]:
            tools_section += f"- **generateWithReference**: `import {{ generateWithReference }} from '{tools_dir_str}/dist/image-generation/generateWithReference.js'`\n"
            tools_section += "  - Params: `prompt`, `reference_images[]` (up to 14) → Returns: `image_url`\n"

        tools_section += "\n"

    # =========================================================================
    # VIDEO TOOLS
    # =========================================================================
    if any_video_tools and providers["video_providers"]:
        tools_section += "### 🎬 Video Tools\n"
        tools_section += "**Providers:** "
        provider_list = []
        for pid, pinfo in providers["video_providers"].items():
            caps = []
            if "video-extend" in pinfo["capabilities"]:
                caps.append("extend")
            if "video-with-audio" in pinfo["capabilities"]:
                caps.append("audio")
            cap_str = f" ({','.join(caps)})" if caps else ""
            provider_list.append(f"`{pid}`{cap_str}")
        tools_section += ", ".join(provider_list) + "\n\n"

        if ai_tools_config.get("video_generation", False) or ai_tools_config.get("video_with_audio", False):
            tools_section += f"- **generateVideo**: `import {{ generateVideo }} from '{tools_dir_str}/dist/video-generation/generateVideo.js'`\n"
            tools_section += "  - Params: `prompt`, `duration` (4/6/8), `aspect_ratio`, `provider` → Returns: `video_url`, `source_video_uri`\n"
            tools_section += "  - For audio: use `veo-3-generate-preview` model. Takes 1-6 min.\n"

        if ai_tools_config.get("image_to_video", False):
            tools_section += f"- **imageToVideo**: `import {{ imageToVideo }} from '{tools_dir_str}/dist/video-generation/imageToVideo.js'`\n"
            tools_section += "  - Params: `image_path`, `prompt`, `provider` → Returns: `video_url`\n"

        if ai_tools_config.get("video_extend", False) and providers["has_gemini"]:
            tools_section += f"- **extendVideo** (Veo only): `import {{ extendVideo }} from '{tools_dir_str}/dist/video-generation/extendVideo.js'`\n"
            tools_section += "  - Params: `video_uri` (from previous result.source_video_uri), `prompt` → Returns: `video_url`\n"

        if ai_tools_config.get("video_bridge", False) and providers["has_gemini"]:
            tools_section += f"- **bridgeFrames** (Veo only): `import {{ bridgeFrames }} from '{tools_dir_str}/dist/video-generation/bridgeFrames.js'`\n"
            tools_section += "  - Params: `start_image`, `end_image`, `prompt` → Returns: `video_url`\n"

        if ai_tools_config.get("video_understanding", False) and providers["has_gemini"]:
            tools_section += f"- **analyzeVideo**: `import {{ analyzeVideo }} from '{tools_dir_str}/dist/video-analysis/analyzeVideo.js'`\n"
            tools_section += "  - Params: `video_path`, `prompt` → Returns: `response`, `scenes`, `transcript`\n"
            tools_section += "  - **ONLY tool for video understanding** - not WebFetch/Read\n"

        tools_section += "\n"

    # =========================================================================
    # 3D MODEL TOOLS
    # =========================================================================
    if any_3d_tools and providers["model3d_providers"]:
        tools_section += "### 🧊 3D Model Tools (Meshy)\n"
        tools_section += "**Note:** Async (2-5 min). Use `wait_for_completion: true` or poll with `getTask3D`.\n\n"

        if ai_tools_config.get("text_to_3d", False):
            tools_section += f"- **textTo3D**: `import {{ textTo3D }} from '{tools_dir_str}/dist/model3d-generation/textTo3D.js'`\n"
            tools_section += "  - Params: `prompt`, `art_style` (realistic/sculpture), `model` (latest/meshy-5), `topology`, `target_polycount`, `wait_for_completion`\n"

        if ai_tools_config.get("image_to_3d", False):
            tools_section += f"- **imageTo3D**: `import {{ imageTo3D }} from '{tools_dir_str}/dist/model3d-generation/imageTo3D.js'`\n"
            tools_section += "  - Params: `image_path`, `prompt` (optional), `model`, `topology`, `enable_pbr`, `wait_for_completion`\n"

        if ai_tools_config.get("retexture_3d", False):
            tools_section += f"- **retexture3D**: `import {{ retexture3D }} from '{tools_dir_str}/dist/model3d-generation/retexture3D.js'`\n"
            tools_section += "  - Params: `model_path_or_task_id`, `style_prompt`, `style_image` (optional), `enable_pbr`\n"

        if ai_tools_config.get("rig_3d", False):
            tools_section += f"- **rig3D**: `import {{ rig3D }} from '{tools_dir_str}/dist/model3d-generation/rig3D.js'`\n"
            tools_section += "  - Params: `model_path_or_task_id`, `height_meters` → Use result.task_id with animate3D\n"

        if ai_tools_config.get("animate_3d", False):
            tools_section += f"- **animate3D**: `import {{ animate3D }} from '{tools_dir_str}/dist/model3d-generation/animate3D.js'`\n"
            tools_section += "  - Params: `rig_task_id` (from rig3D), `action_id` (walk_forward, run, jump, idle, wave, dance_hip_hop, etc), `fps`\n"

        tools_section += f"- **getTask3D**: `import {{ getTask3D }} from '{tools_dir_str}/dist/model3d-generation/getTask3D.js'`\n"
        tools_section += "  - Params: `task_id` → Returns: `status`, `model_urls` (glb/fbx/obj/usdz), `thumbnail_url`\n"

        tools_section += "\n"

    tools_section += "</ai-tools>"

    return tools_section


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
        "openai_api_key": "openai_api_key",
        "gemini_api_key": "image_api_key",
        "meshy_api_key": "meshy_api_key",
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
                    logger.info(f"Found hooked tool: '{tool_name}'")

    return tools


def _build_extra_args(
    base_extra_args: Dict[str, str],
    hooks: Optional[Dict[str, list]]
) -> Dict[str, str]:
    """
    Build extra_args dict, adding --allowed-tools for any hooked tools.

    When we have hooks that handle specific tools (like AskUserQuestion),
    we must pass them via --allowed-tools CLI flag so the model can call them.
    The SDK's allowed_tools option only filters existing tools, it doesn't add new ones.

    Args:
        base_extra_args: Base extra args from profile config
        hooks: Dict of hook configurations (e.g., {"PreToolUse": [HookMatcher(...)]})

    Returns:
        Dict of extra args to pass to CLI
    """
    extra_args = dict(base_extra_args) if base_extra_args else {}

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
                logger.info(f"Adding '{tool}' to --allowed-tools (required for hook handler)")

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

    if not ai_tools_config:
        return env

    # Check if any AI tools are enabled
    any_image_tools = ai_tools_config.get("image_generation", False) or ai_tools_config.get("image_editing", False) or ai_tools_config.get("image_reference", False)
    any_video_tools = ai_tools_config.get("video_generation", False) or ai_tools_config.get("image_to_video", False) or ai_tools_config.get("video_extend", False) or ai_tools_config.get("video_bridge", False) or ai_tools_config.get("video_with_audio", False) or ai_tools_config.get("video_understanding", False)
    any_3d_tools = ai_tools_config.get("text_to_3d", False) or ai_tools_config.get("image_to_3d", False) or ai_tools_config.get("retexture_3d", False) or ai_tools_config.get("rig_3d", False) or ai_tools_config.get("animate_3d", False)

    if not any_image_tools and not any_video_tools and not any_3d_tools:
        return env

    # Resolve API keys based on user/policy
    # If api_user_id is provided, use policy-based resolution (user key → admin fallback)
    # Otherwise, use admin keys directly (local/admin sessions)
    if api_user_id:
        gemini_api_key = _resolve_api_credential("gemini_api_key", api_user_id)
        openai_api_key = _resolve_api_credential("openai_api_key", api_user_id)
        meshy_api_key = _resolve_api_credential("meshy_api_key", api_user_id)
        logger.debug(f"Resolved credentials for API user {api_user_id}")
    else:
        # Admin/local session - use admin keys directly
        gemini_api_key = _get_decrypted_api_key("image_api_key")
        openai_api_key = _get_decrypted_api_key("openai_api_key")
        meshy_api_key = _get_decrypted_api_key("meshy_api_key")

    # Inject ALL available API keys so Claude can dynamically choose providers
    # This is the key change - instead of only injecting the default provider's key,
    # we inject all keys so any provider can be used per-request

    if gemini_api_key:
        env["GEMINI_API_KEY"] = gemini_api_key
        env["IMAGE_API_KEY"] = gemini_api_key  # For backwards compatibility
        env["VIDEO_API_KEY"] = gemini_api_key  # Veo uses same key
        logger.debug("Injected GEMINI_API_KEY for Google AI tools (Nano Banana, Imagen, Veo)")

    if openai_api_key:
        env["OPENAI_API_KEY"] = openai_api_key
        logger.debug("Injected OPENAI_API_KEY for OpenAI tools (GPT Image, Sora)")

    if meshy_api_key:
        env["MESHY_API_KEY"] = meshy_api_key
        env["MODEL3D_API_KEY"] = meshy_api_key  # Alternative name
        logger.debug("Injected MESHY_API_KEY for 3D model tools (Meshy)")

    # Set default provider/model from settings (Claude can override these per-request)
    if any_image_tools:
        image_provider = database.get_system_setting("image_provider")
        image_model = database.get_system_setting("image_model")

        if image_provider:
            env["IMAGE_PROVIDER"] = image_provider
            logger.debug(f"Set default IMAGE_PROVIDER={image_provider}")

        if image_model:
            env["IMAGE_MODEL"] = image_model
            env["GEMINI_MODEL"] = image_model  # Legacy compatibility
            logger.debug(f"Set default IMAGE_MODEL={image_model}")

    if any_video_tools:
        video_provider = database.get_system_setting("video_provider")
        video_model = database.get_system_setting("video_model")

        if video_provider:
            env["VIDEO_PROVIDER"] = video_provider
            logger.debug(f"Set default VIDEO_PROVIDER={video_provider}")

        if video_model:
            env["VIDEO_MODEL"] = video_model
            env["VEO_MODEL"] = video_model  # Legacy compatibility
            logger.debug(f"Set default VIDEO_MODEL={video_model}")

    if any_3d_tools:
        model3d_provider = database.get_system_setting("model3d_provider")
        model3d_model = database.get_system_setting("model3d_model")

        if model3d_provider:
            env["MODEL3D_PROVIDER"] = model3d_provider
            logger.debug(f"Set default MODEL3D_PROVIDER={model3d_provider}")

        if model3d_model:
            env["MODEL3D_MODEL"] = model3d_model
            logger.debug(f"Set default MODEL3D_MODEL={model3d_model}")

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
        ai_tools_config: Dict with individual tool toggles (e.g., {"image_generation": True})
        execution_mode: Either "local" or "worktree"
        worktree_info: Dict with branch and base_branch info when in worktree mode
    """
    is_git = _is_git_repo(working_dir)
    os_version = _get_os_version()
    today = datetime.now().strftime("%Y-%m-%d")

    # Get AI tools section using helper function
    tools_section = _build_ai_tools_section(ai_tools_config)

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
</env>{tools_section}"""


def build_options_from_profile(
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    resume_session_id: Optional[str] = None,
    can_use_tool: Optional[callable] = None,
    hooks: Optional[Dict[str, list]] = None,
    api_user_id: Optional[str] = None,
    session_id: Optional[str] = None
) -> tuple[ClaudeAgentOptions, Optional[Dict[str, AgentDefinition]]]:
    """
    Convert a profile to ClaudeAgentOptions with all available options.

    Returns:
        Tuple of (ClaudeAgentOptions, agents_dict)
        - ClaudeAgentOptions: SDK options with agents included for Docker/Linux, excluded for Windows
        - agents_dict: Dict of agent_id -> AgentDefinition, or None if no agents

    Args:
        profile: Profile configuration dict
        project: Optional project configuration
        overrides: Optional override settings
        resume_session_id: Optional SDK session ID to resume (from Claude SDK)
        can_use_tool: Optional permission callback
        hooks: Optional hooks configuration
        api_user_id: Optional API user ID for user-specific credential resolution
        session_id: Optional our session ID (for worktree lookup)

    Note: On Windows local mode, --agents CLI flag has a bug where agents are not discovered.
    Callers should use write_agents_to_filesystem() on Windows only. On Docker/Linux, agents
    are passed directly via the SDK's --agents flag (which works correctly).
    """
    config = profile["config"]
    overrides = overrides or {}

    # Determine working directory and execution mode (needed for env details injection)
    # Priority order:
    # 1. Override cwd (explicit worktree path from agent engine)
    # 2. Session worktree (for resumed sessions)
    # 3. Project path
    # 4. Profile config cwd
    # 5. Default workspace
    execution_mode = "local"
    worktree_info = None

    if overrides.get("cwd"):
        # Override takes highest priority - used by agent engine for worktrees
        working_dir = overrides.get("cwd")
        # Check if override includes worktree info
        if overrides.get("worktree_info"):
            execution_mode = "worktree"
            worktree_info = overrides.get("worktree_info")
    elif session_id:
        # Check if this session has an associated worktree
        worktree = database.get_worktree_by_session(session_id)
        if worktree and worktree.get("status") == "active":
            working_dir = str(settings.workspace_dir / worktree["worktree_path"])
            execution_mode = "worktree"
            worktree_info = {
                "branch": worktree.get("branch_name", "unknown"),
                "base_branch": worktree.get("base_branch", "main")
            }
        elif project:
            working_dir = str(settings.workspace_dir / project["path"])
        else:
            working_dir = str(settings.workspace_dir)
    elif project:
        working_dir = str(settings.workspace_dir / project["path"])
    elif config.get("cwd"):
        working_dir = config.get("cwd")
    else:
        working_dir = str(settings.workspace_dir)

    # Build system prompt
    system_prompt = config.get("system_prompt")
    override_append = overrides.get("system_prompt_append", "")

    # Get AI tools config from profile config (not system_prompt)
    ai_tools_config = config.get("ai_tools")

    # Get the active security instructions (custom or default)
    security_instructions = get_security_instructions()

    if system_prompt is None:
        # No preset - just security instructions as plain text
        final_system_prompt = security_instructions

        # Add AI tools section if any are enabled
        final_system_prompt += _build_ai_tools_section(ai_tools_config)

        if override_append:
            final_system_prompt += "\n\n" + override_append
    elif isinstance(system_prompt, dict):
        prompt_type = system_prompt.get("type", "preset")

        if prompt_type == "custom":
            # Custom system prompt - use content directly with security instructions
            # Content can be empty/blank
            custom_content = system_prompt.get("content", "") or ""
            inject_env = system_prompt.get("inject_env_details", False)

            # Start building the prompt
            prompt_parts = [security_instructions]

            # Add environment details if enabled (includes AI tools based on ai_tools_config)
            if inject_env:
                prompt_parts.append(generate_environment_details(
                    working_dir,
                    ai_tools_config,
                    execution_mode=execution_mode,
                    worktree_info=worktree_info
                ))
            else:
                # If env details not enabled, still add AI tools section if any are enabled
                tools_section = _build_ai_tools_section(ai_tools_config, prefix="")
                if tools_section:
                    prompt_parts.append(tools_section)

            # Add custom content if provided
            if custom_content:
                prompt_parts.append(custom_content)

            # Add override append if provided
            if override_append:
                prompt_parts.append(override_append)

            final_system_prompt = "\n\n".join(prompt_parts)
        else:
            # Preset system prompt - pass to SDK with append
            existing_append = system_prompt.get("append", "")
            full_append = security_instructions
            if existing_append:
                full_append += "\n\n" + existing_append
            if override_append:
                full_append += "\n\n" + override_append

            # Add AI tools section if any are enabled
            full_append += _build_ai_tools_section(ai_tools_config)

            final_system_prompt = {
                "type": "preset",
                "preset": system_prompt.get("preset", "claude_code"),
                "append": full_append
            }
    else:
        # String system prompt - use it directly with security instructions
        final_system_prompt = security_instructions + "\n\n" + str(system_prompt)

        # Add AI tools section if any are enabled
        final_system_prompt += _build_ai_tools_section(ai_tools_config)

        if override_append:
            final_system_prompt += "\n\n" + override_append

    # Build agents dict from profile's enabled_agents
    # Profile stores a list of subagent IDs that reference global subagents in the database
    # The SDK expects AgentDefinition dataclass instances, not raw dicts
    enabled_agent_ids = config.get("enabled_agents", [])
    agents_dict = None
    logger.info(f"Building agents from enabled_agent_ids: {enabled_agent_ids}")
    if enabled_agent_ids:
        agents_dict = {}
        for agent_id in enabled_agent_ids:
            # Look up subagent from global database
            subagent = database.get_subagent(agent_id)
            if subagent:
                # Create AgentDefinition dataclass instance
                agent_def = AgentDefinition(
                    description=subagent.get("description", ""),
                    prompt=subagent.get("prompt", ""),
                    tools=subagent.get("tools"),
                    model=subagent.get("model")
                )
                agents_dict[agent_id] = agent_def
                logger.info(f"Loaded subagent '{agent_id}': description='{agent_def.description[:50]}...', tools={agent_def.tools}, model={agent_def.model}")
            else:
                logger.warning(f"Subagent not found in database: {agent_id}")
        logger.info(f"Final agents_dict keys: {list(agents_dict.keys()) if agents_dict else 'None'}")

    # Inject built-in agent instructions into system prompt (only for enabled built-in agents)
    if enabled_agent_ids:
        from app.core.builtin_subagents import get_system_prompt_instructions
        builtin_instructions = get_system_prompt_instructions(enabled_agent_ids)
        if builtin_instructions:
            if isinstance(final_system_prompt, dict):
                # Append to the append field
                current_append = final_system_prompt.get("append", "")
                final_system_prompt["append"] = current_append + builtin_instructions
            else:
                # Append to string system prompt
                final_system_prompt += builtin_instructions

    # Determine permission mode
    permission_mode = overrides.get("permission_mode") or config.get("permission_mode", "default")

    # Create stderr callback to log CLI output
    # Note: We only log stderr, don't forward to frontend. The SDK sends proper
    # typed messages for important events (compact_boundary, api errors via
    # synthetic AssistantMessage with isApiErrorMessage, etc.)
    def stderr_callback(line: str):
        logger.info(f"[Claude CLI stderr] {line}")

    # Process model selection - handle special 1M context models
    # Models ending in "-1m" use the base model with 1M context beta header
    raw_model = overrides.get("model") or config.get("model")
    model_to_use = raw_model
    betas_to_use = []

    # Map special model names to base model + beta headers
    MODEL_1M_MAPPING = {
        "sonnet-1m": ("sonnet", ["context-1m-2025-08-07"]),
        "claude-sonnet-4-5-1m": ("claude-sonnet-4-5-20250514", ["context-1m-2025-08-07"]),
        "claude-sonnet-4-1m": ("claude-sonnet-4-20250514", ["context-1m-2025-08-07"]),
    }

    if raw_model in MODEL_1M_MAPPING:
        model_to_use, betas_to_use = MODEL_1M_MAPPING[raw_model]
        logger.info(f"1M context model selected: {raw_model} -> model={model_to_use}, betas={betas_to_use}")

    # Build tool configuration (SDK 0.1.12+ supports new 'tools' parameter)
    # Priority: tools > tools_preset > allowed_tools (legacy)
    tools_config = config.get("tools")  # Explicit list of tools
    tools_preset = config.get("tools_preset")  # "claude_code" preset
    allowed_tools = config.get("allowed_tools") or []
    disallowed_tools = config.get("disallowed_tools") or []

    # Determine tools parameter for SDK
    # New SDK way: use 'tools' param with list or preset dict
    # Legacy way: use 'allowed_tools' param
    sdk_tools = None
    if tools_config:
        # Explicit tool list provided
        sdk_tools = tools_config
    elif tools_preset == "claude_code":
        # Use preset (TypedDict format for SDK)
        sdk_tools = {"type": "preset", "preset": "claude_code"}

    # Build options with all ClaudeAgentOptions fields
    # Note: We don't set cli_path - let the SDK use its bundled CLI or find system CLI automatically
    # The SDK handles finding Claude properly on all platforms
    options = ClaudeAgentOptions(
        # Core settings
        model=model_to_use,
        permission_mode=permission_mode,
        max_turns=overrides.get("max_turns") or config.get("max_turns"),

        # Tool configuration (SDK 0.1.12+)
        # 'tools' is the new way - explicit list or preset
        # 'allowed_tools' is legacy - used if tools not set
        # 'disallowed_tools' always applies as a blocklist
        tools=sdk_tools,
        allowed_tools=allowed_tools if not sdk_tools else [],  # Only use if tools not set
        disallowed_tools=disallowed_tools,

        # System prompt
        system_prompt=final_system_prompt,

        # Streaming behavior - enable partial messages for real-time streaming
        include_partial_messages=config.get("include_partial_messages", True),

        # Session behavior
        continue_conversation=config.get("continue_conversation", False),
        fork_session=config.get("fork_session", False),

        # Settings loading
        setting_sources=config.get("setting_sources"),

        # Environment and arguments - inject AI tool credentials if enabled
        # Pass api_user_id for policy-based credential resolution
        env=_build_env_with_ai_tools(config.get("env"), ai_tools_config, api_user_id),
        extra_args=_build_extra_args(config.get("extra_args") or {}, hooks),

        # Buffer settings - Default to 50MB to handle large file reads (images, PDFs)
        # The SDK's default is 1MB which causes "JSON message exceeded maximum buffer size" errors
        # when reading images or PDFs (their base64 content easily exceeds 1MB)
        max_buffer_size=config.get("max_buffer_size") or DEFAULT_SDK_BUFFER_SIZE,

        # User identification
        user=config.get("user"),

        # Subagents - On Docker/Linux, pass via --agents (works correctly)
        # On Windows, agents=None because --agents CLI flag has a bug - must write to filesystem instead
        agents=agents_dict if detect_deployment_mode() != DeploymentMode.LOCAL else None,

        # Permission callback - set if callback provided (handles AskUserQuestion in all modes)
        can_use_tool=can_use_tool if can_use_tool else None,

        # Hooks - for interactive tool handling like AskUserQuestion
        hooks=hooks,

        # Stderr callback for debugging
        stderr=stderr_callback,

        # Plugins - load from enabled_plugins in profile config
        # Each plugin path is resolved relative to workspace_dir if relative
        # Pass setting_sources to avoid duplicating plugins that Claude Code auto-loads
        plugins=_build_plugins_list(
            config.get("enabled_plugins"),
            config.get("setting_sources")
        ),

        # Beta features - e.g., 1M context window
        betas=betas_to_use,

        # File checkpointing for rewind (SDK 0.1.15+)
        enable_file_checkpointing=config.get("enable_file_checkpointing", False),
    )

    deployment_mode = detect_deployment_mode()
    logger.info(f"Built options with model={model_to_use}, betas={betas_to_use}, permission_mode={permission_mode}, can_use_tool={options.can_use_tool is not None}, hooks={hooks is not None}, deployment_mode={deployment_mode}")
    if deployment_mode == DeploymentMode.LOCAL:
        logger.info(f"Windows local mode: Agents to write to filesystem: {list(agents_dict.keys()) if agents_dict else 'None'}")
    else:
        logger.info(f"Docker mode: Agents passed via SDK: {list(agents_dict.keys()) if agents_dict else 'None'}")

    # Apply working directory (already computed above for env details)
    options.cwd = working_dir

    # Additional directories
    add_dirs = config.get("add_dirs")
    if add_dirs:
        options.add_dirs = add_dirs

    # Resume existing session
    if resume_session_id:
        options.resume = resume_session_id

    return options, agents_dict


async def execute_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Execute a non-streaming query"""

    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Get or create session
    if session_id:
        session = database.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        resume_id = session.get("sdk_session_id")
    else:
        session_id = str(uuid.uuid4())
        # Generate title from first message (truncate to 50 chars)
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        resume_id = None

    # Store user message with profile_id for per-query tracking
    database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Build options with user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    # agents_dir created for local mode - used implicitly by SDK reading from filesystem
    _agents_dir = None
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        _agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)

    # Execute query and collect response
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    metadata = {}
    sdk_session_id = None

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, SystemMessage):
                # session_id comes in warmup message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": getattr(block, 'id', None),
                            "input": block.input
                        })
                    elif isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content)
                        tool_messages.append({
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "tool_id": getattr(block, 'tool_use_id', None),
                            "output": output
                        })
                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        tool_messages.append({
                            "type": "tool_result",
                            "name": "unknown",
                            "tool_id": block.tool_use_id,
                            "output": output
                        })

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

    except Exception as e:
        logger.error(f"Query execution error: {e}")
        raise

    # Update session with SDK session ID for resume
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            tokens_in_increment=metadata.get("tokens_in", 0),
            tokens_out_increment=metadata.get("tokens_out", 0),
            turn_increment=metadata.get("num_turns", 0)
        )

    # Store tool messages (tool_use and tool_result)
    for tool_msg in tool_messages:
        if tool_msg["type"] == "tool_use":
            database.add_session_message(
                session_id=session_id,
                role="tool_use",
                content=f"Using tool: {tool_msg['name']}",
                tool_name=tool_msg["name"],
                tool_input=tool_msg.get("input"),
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )
        elif tool_msg["type"] == "tool_result":
            database.add_session_message(
                session_id=session_id,
                role="tool_result",
                content=tool_msg.get("output", ""),
                tool_name=tool_msg["name"],
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )

    # Store assistant response
    full_response = "\n".join(response_text)
    database.add_session_message(
        session_id=session_id,
        role="assistant",
        content=full_response,
        metadata=metadata,
        profile_id=profile_id
    )

    # Log usage
    database.log_usage(
        session_id=session_id,
        profile_id=profile_id,
        model=metadata.get("model"),
        tokens_in=metadata.get("tokens_in", 0),
        tokens_out=metadata.get("tokens_out", 0),
        cost_usd=metadata.get("total_cost_usd", 0),
        duration_ms=metadata.get("duration_ms", 0)
    )

    return {
        "response": full_response,
        "session_id": session_id,
        "metadata": metadata
    }


async def stream_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    device_id: Optional[str] = None  # Source device for sync
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Execute a streaming query using ClaudeSDKClient.

    Following Anvil's SessionManager pattern:
    - Keep clients connected for session lifetime
    - Don't disconnect after each query (causes async context issues)
    - Reuse existing client if available, create new one with 'resume' if needed
    """

    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            yield {"type": "error", "message": f"Project not found: {project_id}"}
            return

    # Get or create session in database
    _is_new_session = False  # Tracks new vs resumed for potential analytics
    resume_id = None

    if session_id:
        session = database.get_session(session_id)
        if not session:
            yield {"type": "error", "message": f"Session not found: {session_id}"}
            return
        resume_id = session.get("sdk_session_id")
        logger.info(f"Resuming session {session_id} with SDK session {resume_id}")
    else:
        session_id = str(uuid.uuid4())
        # Generate title from first message (truncate to 50 chars)
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        _is_new_session = True  # Flag available for future analytics/logging
        logger.info(f"Created new session {session_id} with title: {title}")

    # Store user message and broadcast to other devices
    user_msg = database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Broadcast user message to other devices (include profile_id for display)
    await sync_engine.broadcast_message_added(
        session_id=session_id,
        message={**user_msg, "profile_id": profile_id},
        source_device_id=device_id
    )

    # Log to sync log for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="message_added",
        entity_type="message",
        entity_id=str(user_msg["id"]),
        data=user_msg
    )

    # Build options with user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    agents_dir = None
    written_agent_ids = []
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)
        written_agent_ids = list(agents_dict.keys())

    # Yield init event
    yield {"type": "init", "session_id": session_id}

    # For now, always create a new client for each query.
    # This is simpler and avoids issues with reusing clients that may be in an
    # inconsistent state. The key fix from Anvil is to NOT disconnect after each
    # query (which we do in the finally block now).
    #
    # Clean up any existing state for this session
    state = _active_sessions.get(session_id)
    if state:
        logger.info(f"Cleaning up existing state for session {session_id}")
        try:
            await state.client.disconnect()
        except Exception:
            pass
        # Clean up agent files from previous session
        if state.agents_dir and state.written_agent_ids:
            cleanup_agents_directory(state.agents_dir, state.written_agent_ids)
        del _active_sessions[session_id]

    # Always create new client
    logger.info(f"Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")
    logger.info(f"Options cwd: {options.cwd}")
    logger.info(f"Agents written to filesystem: {written_agent_ids if written_agent_ids else None}")
    client = ClaudeSDKClient(options=options)
    logger.info("ClaudeSDKClient created, attempting connect...")

    # Connect without timeout - Anvil doesn't use timeout for connect()
    try:
        await client.connect()
        logger.info(f"Connected to Claude SDK for session {session_id}")
    except Exception as e:
        import traceback
        logger.error(f"Failed to connect to Claude SDK for session {session_id}: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
        yield {"type": "error", "message": f"Connection failed: {e}"}
        return

    state = SessionState(
        client=client,
        sdk_session_id=resume_id,
        is_connected=True,
        written_agent_ids=written_agent_ids,
        agents_dir=agents_dir
    )
    _active_sessions[session_id] = state

    # Mark as streaming
    state.is_streaming = True
    state.last_activity = datetime.now()

    # Generate a message ID for streaming sync
    assistant_msg_id = f"streaming-{session_id}-{datetime.now().timestamp()}"

    # Broadcast stream start to other devices
    await sync_engine.broadcast_stream_start(
        session_id=session_id,
        message_id=assistant_msg_id,
        source_device_id=device_id
    )

    # Log stream start for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="stream_start",
        entity_type="message",
        entity_id=assistant_msg_id,
        data={"message_id": assistant_msg_id}
    )

    # Execute query
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    metadata = {}
    sdk_session_id = resume_id  # Start with existing SDK session ID if resuming
    interrupted = False

    try:
        await state.client.query(prompt)

        async for message in state.client.receive_response():
            if isinstance(message, SystemMessage):
                # session_id comes in init message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.info(f"Captured SDK session ID: {sdk_session_id}")

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                        yield {"type": "text", "content": block.text}

                        # Broadcast text chunk to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="text",
                            chunk_data={"content": block.text},
                            source_device_id=device_id
                        )

                    elif isinstance(block, ToolUseBlock):
                        yield {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input
                        }

                        # Collect tool use for storage
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": getattr(block, 'id', None),
                            "input": block.input
                        })

                        # Broadcast tool use to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_use",
                            chunk_data={"name": block.name, "input": block.input},
                            source_device_id=device_id
                        )

                    elif isinstance(block, ToolResultBlock):
                        # Truncate large outputs
                        output = truncate_large_payload(block.content)
                        yield {
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "output": output
                        }

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "tool_id": getattr(block, 'tool_use_id', None),
                            "output": output
                        })

                        # Broadcast tool result to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={"name": getattr(block, 'name', 'unknown'), "output": output},
                            source_device_id=device_id
                        )

                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        logger.debug(f"UserMessage ToolResultBlock - tool_use_id: {block.tool_use_id}, content length: {len(str(block.content) if block.content else '')}")

                        yield {
                            "type": "tool_result",
                            "name": "unknown",
                            "tool_use_id": block.tool_use_id,
                            "output": output
                        }

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": "unknown",
                            "tool_id": block.tool_use_id,
                            "output": output
                        })

                        # Broadcast tool result to other devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={"name": "unknown", "output": output, "tool_use_id": block.tool_use_id},
                            source_device_id=device_id
                        )

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

    except asyncio.CancelledError:
        interrupted = True
        logger.info(f"Query interrupted for session {session_id}")
        yield {"type": "interrupted", "message": "Query was interrupted"}

    except Exception as e:
        logger.error(f"Stream query error for session {session_id}: {e}")
        # Mark client as disconnected on error
        state.is_connected = False
        yield {"type": "error", "message": str(e)}

    finally:
        # Mark as not streaming
        state.is_streaming = False
        state.last_activity = datetime.now()

        # Broadcast stream end to other devices
        await sync_engine.broadcast_stream_end(
            session_id=session_id,
            message_id=assistant_msg_id,
            metadata=metadata,
            interrupted=interrupted,
            source_device_id=device_id
        )

    # Update session in database
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            tokens_in_increment=metadata.get("tokens_in", 0),
            tokens_out_increment=metadata.get("tokens_out", 0),
            turn_increment=metadata.get("num_turns", 0)
        )
        logger.info(f"Updated session {session_id}, sdk_session_id={sdk_session_id}")

    # Store tool messages (tool_use and tool_result)
    for tool_msg in tool_messages:
        if tool_msg["type"] == "tool_use":
            database.add_session_message(
                session_id=session_id,
                role="tool_use",
                content=f"Using tool: {tool_msg['name']}",
                tool_name=tool_msg["name"],
                tool_input=tool_msg.get("input"),
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )
        elif tool_msg["type"] == "tool_result":
            database.add_session_message(
                session_id=session_id,
                role="tool_result",
                content=tool_msg.get("output", ""),
                tool_name=tool_msg["name"],
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )

    # Store assistant response
    full_response = "\n".join(response_text)
    if full_response or interrupted or tool_messages:
        assistant_msg = database.add_session_message(
            session_id=session_id,
            role="assistant",
            content=full_response + ("\n[Interrupted]" if interrupted else ""),
            metadata=metadata,
            profile_id=profile_id
        )

        # Log stream end for polling fallback
        database.add_sync_log(
            session_id=session_id,
            event_type="stream_end",
            entity_type="message",
            entity_id=str(assistant_msg["id"]),
            data={
                "message": assistant_msg,
                "metadata": metadata,
                "interrupted": interrupted
            }
        )

    # Log usage
    if metadata:
        database.log_usage(
            session_id=session_id,
            profile_id=profile_id,
            model=metadata.get("model"),
            tokens_in=metadata.get("tokens_in", 0),
            tokens_out=metadata.get("tokens_out", 0),
            cost_usd=metadata.get("total_cost_usd", 0),
            duration_ms=metadata.get("duration_ms", 0)
        )

    # Yield done event (unless already yielded error/interrupted)
    if not interrupted:
        yield {
            "type": "done",
            "session_id": session_id,
            "metadata": metadata
        }


async def _run_background_query(
    session_id: str,
    prompt: str,
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]],
    overrides: Optional[Dict[str, Any]],
    resume_id: Optional[str],
    device_id: Optional[str],
    api_user_id: Optional[str],
    message_id: Optional[str] = None
):
    """
    Run a streaming query in the background, independent of HTTP connection.

    This allows work to continue even when:
    - User locks their phone
    - Browser tab is backgrounded
    - HTTP connection is interrupted

    All events are broadcast via sync_engine for WebSocket delivery.
    """
    # Extract profile_id for per-query tracking
    profile_id = profile.get("id")

    # Store user message and broadcast to other devices
    user_msg = database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt,
        profile_id=profile_id
    )

    # Broadcast user message to other devices (include profile_id for display)
    await sync_engine.broadcast_message_added(
        session_id=session_id,
        message={**user_msg, "profile_id": profile_id},
        source_device_id=device_id
    )

    # Log to sync log for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="message_added",
        entity_type="message",
        entity_id=str(user_msg["id"]),
        data=user_msg
    )

    # Build options with user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    agents_dir = None
    written_agent_ids = []
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)
        written_agent_ids = list(agents_dict.keys())

    # Clean up any existing state for this session
    state = _active_sessions.get(session_id)
    if state:
        logger.info(f"Cleaning up existing state for session {session_id}")
        try:
            await state.client.disconnect()
        except Exception:
            pass
        # Clean up agent files from previous session
        if state.agents_dir and state.written_agent_ids:
            cleanup_agents_directory(state.agents_dir, state.written_agent_ids)
        del _active_sessions[session_id]

    # Always create new client
    logger.info(f"[Background] Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")
    logger.info(f"[Background] Agents written to filesystem: {written_agent_ids if written_agent_ids else None}")
    client = ClaudeSDKClient(options=options)

    # Connect
    try:
        await client.connect()
        logger.info(f"[Background] Connected to Claude SDK for session {session_id}")
    except Exception as e:
        logger.error(f"[Background] Failed to connect to Claude SDK for session {session_id}: {e}")
        # Broadcast error
        await sync_engine.broadcast_stream_end(
            session_id=session_id,
            message_id=f"error-{session_id}",
            metadata={"error": str(e)},
            interrupted=True,
            source_device_id=device_id
        )
        return

    state = SessionState(
        client=client,
        sdk_session_id=resume_id,
        is_connected=True,
        written_agent_ids=written_agent_ids,
        agents_dir=agents_dir
    )
    _active_sessions[session_id] = state

    # Mark as streaming
    state.is_streaming = True
    state.last_activity = datetime.now()

    # Use provided message_id or generate a new one
    # If message_id is provided, stream_start was already broadcast by start_background_query
    assistant_msg_id = message_id or f"streaming-{session_id}-{datetime.now().timestamp()}"

    # Only broadcast stream_start if we generated a new message_id
    # (i.e., if message_id wasn't provided by start_background_query)
    if not message_id:
        await sync_engine.broadcast_stream_start(
            session_id=session_id,
            message_id=assistant_msg_id,
            source_device_id=None  # Don't exclude any device - all should see it
        )

    # Log stream start for polling fallback
    database.add_sync_log(
        session_id=session_id,
        event_type="stream_start",
        entity_type="message",
        entity_id=assistant_msg_id,
        data={"message_id": assistant_msg_id}
    )

    # Execute query
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    metadata = {}
    sdk_session_id = resume_id
    interrupted = False

    try:
        await state.client.query(prompt)

        async for message in state.client.receive_response():
            if isinstance(message, SystemMessage):
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.info(f"[Background] Captured SDK session ID: {sdk_session_id}")

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                        # Broadcast text chunk to all devices
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="text",
                            chunk_data={"content": block.text},
                            source_device_id=None
                        )

                    elif isinstance(block, ToolUseBlock):
                        # Collect tool use for storage
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": getattr(block, 'id', None),
                            "input": block.input
                        })

                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_use",
                            chunk_data={
                                "name": block.name,
                                "id": getattr(block, 'id', None),
                                "input": block.input
                            },
                            source_device_id=None
                        )

                    elif isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content)

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "tool_id": getattr(block, 'tool_use_id', None),
                            "output": output
                        })

                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={
                                "name": getattr(block, 'name', 'unknown'),
                                "tool_use_id": getattr(block, 'tool_use_id', None),
                                "output": output
                            },
                            source_device_id=None
                        )

                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage contains tool results from Claude's tool executions
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        logger.debug(f"[Background] UserMessage ToolResultBlock - tool_use_id: {block.tool_use_id}, content length: {len(str(block.content) if block.content else '')}")

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": "unknown",
                            "tool_id": block.tool_use_id,
                            "output": output
                        })

                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={
                                "name": "unknown",
                                "tool_use_id": block.tool_use_id,
                                "output": output
                            },
                            source_device_id=None
                        )

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

    except asyncio.CancelledError:
        interrupted = True
        logger.info(f"[Background] Query interrupted for session {session_id}")

    except Exception as e:
        logger.error(f"[Background] Stream query error for session {session_id}: {e}")
        state.is_connected = False
        metadata["error"] = str(e)

    finally:
        state.is_streaming = False
        state.last_activity = datetime.now()
        state.background_task = None

        # Broadcast stream end to all devices
        await sync_engine.broadcast_stream_end(
            session_id=session_id,
            message_id=assistant_msg_id,
            metadata=metadata,
            interrupted=interrupted,
            source_device_id=None
        )

    # Update session in database
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            tokens_in_increment=metadata.get("tokens_in", 0),
            tokens_out_increment=metadata.get("tokens_out", 0),
            turn_increment=metadata.get("num_turns", 0)
        )
        logger.info(f"[Background] Updated session {session_id}, sdk_session_id={sdk_session_id}")

    # Store tool messages (tool_use and tool_result)
    for tool_msg in tool_messages:
        if tool_msg["type"] == "tool_use":
            database.add_session_message(
                session_id=session_id,
                role="tool_use",
                content=f"Using tool: {tool_msg['name']}",
                tool_name=tool_msg["name"],
                tool_input=tool_msg.get("input"),
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )
        elif tool_msg["type"] == "tool_result":
            database.add_session_message(
                session_id=session_id,
                role="tool_result",
                content=tool_msg.get("output", ""),
                tool_name=tool_msg["name"],
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )

    # Store assistant response
    full_response = "\n".join(response_text)
    if full_response or interrupted or tool_messages:
        assistant_msg = database.add_session_message(
            session_id=session_id,
            role="assistant",
            content=full_response + ("\n[Interrupted]" if interrupted else ""),
            metadata=metadata,
            profile_id=profile_id
        )

        # Log stream end for polling fallback
        database.add_sync_log(
            session_id=session_id,
            event_type="stream_end",
            entity_type="message",
            entity_id=str(assistant_msg["id"]),
            data={
                "message": assistant_msg,
                "metadata": metadata,
                "interrupted": interrupted
            }
        )

    # Log usage
    if metadata and not metadata.get("error"):
        database.log_usage(
            session_id=session_id,
            profile_id=profile["id"],
            model=metadata.get("model"),
            tokens_in=metadata.get("tokens_in", 0),
            tokens_out=metadata.get("tokens_out", 0),
            cost_usd=metadata.get("total_cost_usd", 0),
            duration_ms=metadata.get("duration_ms", 0)
        )

    logger.info(f"[Background] Query completed for session {session_id}")


async def start_background_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    device_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Start a streaming query in a background task.

    Returns immediately with session info. Work continues in background.
    Use interrupt_session() to stop.
    """
    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Get or create session in database
    resume_id = None

    if session_id:
        session = database.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        resume_id = session.get("sdk_session_id")
        logger.info(f"Resuming session {session_id} with SDK session {resume_id}")
    else:
        session_id = str(uuid.uuid4())
        title = prompt[:50].strip()
        if len(prompt) > 50:
            title += "..."
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id
        )
        logger.info(f"Created new session {session_id} with title: {title}")

    # Generate a message ID for streaming
    assistant_msg_id = f"streaming-{session_id}-{datetime.now().timestamp()}"

    # Broadcast stream_start BEFORE returning so the WebSocket can pick it up
    # This ensures is_streaming=true and buffer exists when client connects
    await sync_engine.broadcast_stream_start(
        session_id=session_id,
        message_id=assistant_msg_id,
        source_device_id=None  # Don't exclude any device - all should see it
    )
    logger.info(f"[Background] Broadcast stream_start for session {session_id}")

    # Start background task
    task = asyncio.create_task(
        _run_background_query(
            session_id=session_id,
            prompt=prompt,
            profile=profile,
            project=project,
            overrides=overrides,
            resume_id=resume_id,
            device_id=device_id,
            api_user_id=api_user_id,
            message_id=assistant_msg_id
        )
    )

    # Store task reference for interrupt support
    # Note: state may not exist yet, it will be created in the background task
    # We'll store the task reference after the state is created
    # For now, we use a temporary holder
    asyncio.get_event_loop().call_soon(
        lambda: _store_background_task(session_id, task)
    )

    return {
        "session_id": session_id,
        "status": "started",
        "message_id": assistant_msg_id
    }


def _store_background_task(session_id: str, task: asyncio.Task):
    """Store background task reference after state is created"""
    state = _active_sessions.get(session_id)
    if state:
        state.background_task = task


async def interrupt_session(session_id: str) -> bool:
    """Interrupt an active streaming session.

    This calls the SDK's interrupt() method which signals the Claude API
    to stop processing. This is more reliable than just cancelling the
    asyncio task, which only takes effect at the next await point.
    """
    state = _active_sessions.get(session_id)
    if not state:
        logger.warning(f"No active session found for {session_id}")
        return False

    if not state.is_connected:
        logger.warning(f"Session {session_id} is not connected")
        return False

    # Set interrupt flag first - this is checked in the streaming loop as a failsafe
    state.interrupt_requested = True

    # Don't check is_streaming too strictly - there might be race conditions
    # and it's better to try to interrupt anyway
    logger.info(f"Attempting to interrupt session {session_id} (streaming={state.is_streaming})")

    try:
        await state.client.interrupt()
        # Mark as not streaming immediately after interrupt
        state.is_streaming = False
        logger.info(f"Successfully interrupted session {session_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to interrupt session {session_id}: {e}", exc_info=True)
        # Even if interrupt failed, mark as not streaming to prevent getting stuck
        state.is_streaming = False
        return False


def get_active_sessions() -> list:
    """Get list of active session IDs (connected clients)"""
    return [
        session_id
        for session_id, state in _active_sessions.items()
        if state.is_connected
    ]


def get_streaming_sessions() -> list:
    """Get list of currently streaming session IDs"""
    return [
        session_id
        for session_id, state in _active_sessions.items()
        if state.is_streaming
    ]


async def stream_to_websocket(
    prompt: str,
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    broadcast_func: Optional[callable] = None,
    api_user_id: Optional[str] = None
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Stream Claude response directly to WebSocket.

    This is the simplified streaming function for the WebSocket-first architecture.
    No sync engine, no background tasks - just direct streaming.

    Yields events:
    - {"type": "chunk", "content": "..."}
    - {"type": "tool_use", "name": "...", "input": {...}}
    - {"type": "tool_result", "name": "...", "output": "..."}
    - {"type": "permission_request", ...} - When tool permission is needed
    - {"type": "done", "session_id": "...", "metadata": {...}}
    - {"type": "error", "message": "..."}

    Args:
        prompt: The user's message
        session_id: The chat session ID
        profile_id: The profile to use
        project_id: Optional project ID
        overrides: Optional overrides for model, permission_mode, etc.
        broadcast_func: Async function to broadcast messages to the WebSocket
        api_user_id: Optional API user ID for user-specific credential resolution
    """
    # Get profile
    profile = get_profile(profile_id)
    if not profile:
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            yield {"type": "error", "message": f"Project not found: {project_id}"}
            return

    # Get session for resume ID
    session = database.get_session(session_id)
    resume_id = session.get("sdk_session_id") if session else None

    # Determine permission mode
    config = profile.get("config", {})
    permission_mode = overrides.get("permission_mode") if overrides else None
    if not permission_mode:
        permission_mode = config.get("permission_mode", "default")

    logger.info(f"[WS] Permission mode for session {session_id}: {permission_mode} (profile config: {config.get('permission_mode')}, override: {overrides.get('permission_mode') if overrides else None})")
    logger.info(f"[WS] broadcast_func provided: {broadcast_func is not None}")

    # Get tool restrictions for use in callback
    allowed_tools = config.get("allowed_tools") or []
    disallowed_tools = config.get("disallowed_tools") or []

    # Create canUseTool callback if we have broadcast_func
    # This handles both permission requests (in 'default' mode) and AskUserQuestion (in all modes)
    can_use_tool_callback = None
    if broadcast_func:
        logger.info(f"[WS] Creating canUseTool callback for session {session_id}")

        async def can_use_tool_callback(tool_name: str, tool_input: dict, context):
            """
            Permission callback that routes tool requests through the permission handler
            and handles AskUserQuestion tool specially.

            Args:
                tool_name: Name of the tool being requested
                tool_input: Tool input parameters
                context: ToolPermissionContext with signal and suggestions
            """
            # Check profile-level tool restrictions FIRST (applies to all tools including AskUserQuestion)
            # These are set by the user in the profile settings (Allow Only / Disallow modes)
            if disallowed_tools and tool_name in disallowed_tools:
                logger.info(f"[WS] Tool {tool_name} blocked by profile disallowed_tools")
                return PermissionResultDeny(message=f"Tool '{tool_name}' is disabled in this profile")
            if allowed_tools and tool_name not in allowed_tools:
                logger.info(f"[WS] Tool {tool_name} blocked by profile allowed_tools (not in whitelist)")
                return PermissionResultDeny(message=f"Tool '{tool_name}' is not enabled in this profile")

            # Handle AskUserQuestion specially - this is how the SDK expects it
            if tool_name == "AskUserQuestion":
                questions = tool_input.get("questions", [])
                if not questions:
                    # No questions, allow the tool to proceed
                    return PermissionResultAllow(updated_input=tool_input)

                request_id = f"question-{session_id}-{uuid.uuid4().hex[:8]}"
                logger.info(f"[WS] AskUserQuestion canUseTool triggered: {request_id} with {len(questions)} questions")

                # Get answers from user via WebSocket
                answers = await user_question_handler.request_answers(
                    request_id=request_id,
                    tool_use_id=request_id,  # Use request_id as tool_use_id placeholder
                    session_id=session_id,
                    questions=questions,
                    broadcast_func=broadcast_func
                )

                logger.info(f"[WS] Got answers for {request_id}: {list(answers.keys())}")

                # Return updated input with answers - SDK expects PermissionResultAllow
                return PermissionResultAllow(updated_input={**tool_input, "answers": answers})

            # For other tools in default mode, route through permission handler
            if permission_mode == "default":
                request_id = f"perm-{session_id}-{uuid.uuid4().hex[:8]}"
                logger.info(f"[WS] Permission request for {tool_name}: {request_id}")

                # Request permission through the handler
                # The handler will broadcast to WebSocket and wait for response
                result = await permission_handler.request_permission(
                    request_id=request_id,
                    session_id=session_id,
                    profile_id=profile_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    broadcast_func=broadcast_func
                )

                return result
            else:
                # In non-default modes (bypassPermissions, acceptEdits), allow other tools
                return PermissionResultAllow()

    # No longer using hooks for AskUserQuestion - it's handled in canUseTool
    hooks_config = None

    # Build options with the callback, hooks, and user-scoped credential resolution
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        can_use_tool=can_use_tool_callback,
        hooks=hooks_config,
        api_user_id=api_user_id,
        session_id=session_id
    )

    # On Windows local mode, write agents to filesystem (workaround for --agents CLI bug)
    # On Docker/Linux, agents are already passed via SDK's --agents flag
    agents_dir = None
    written_agent_ids = []
    if agents_dict and options.cwd and detect_deployment_mode() == DeploymentMode.LOCAL:
        agents_dir = write_agents_to_filesystem(agents_dict, options.cwd)
        written_agent_ids = list(agents_dict.keys())

    # Clean up any existing state for this session
    state = _active_sessions.get(session_id)
    if state:
        logger.info(f"[WS] Cleaning up existing state for session {session_id}")
        try:
            await state.client.disconnect()
        except Exception:
            pass
        # Clean up agent files from previous session
        if state.agents_dir and state.written_agent_ids:
            cleanup_agents_directory(state.agents_dir, state.written_agent_ids)
        del _active_sessions[session_id]

    # Create new client
    logger.info(f"[WS] Creating ClaudeSDKClient for session {session_id} (resume={resume_id is not None}, include_partial={options.include_partial_messages})")
    logger.info(f"[WS] Options cwd: {options.cwd}")
    logger.info(f"[WS] Agents written to filesystem: {written_agent_ids if written_agent_ids else None}")
    client = ClaudeSDKClient(options=options)
    logger.info("[WS] ClaudeSDKClient created, attempting connect...")

    # Connect
    try:
        await client.connect()
        logger.info(f"[WS] Connected to Claude SDK for session {session_id}")
    except Exception as e:
        import traceback
        logger.error(f"[WS] Failed to connect for session {session_id}: {e}")
        logger.error(f"[WS] Exception type: {type(e).__name__}")
        logger.error(f"[WS] Full traceback:\n{traceback.format_exc()}")
        yield {"type": "error", "message": f"Connection failed: {e}"}
        return

    state = SessionState(
        client=client,
        sdk_session_id=resume_id,
        is_connected=True,
        written_agent_ids=written_agent_ids,
        agents_dir=agents_dir
    )
    _active_sessions[session_id] = state

    # Mark as streaming
    state.is_streaming = True
    state.last_activity = datetime.now()

    # Initialize sdk_session_id - use resume_id if resuming, otherwise None until we get it from SDK
    sdk_session_id = resume_id

    # Execute query
    # Note: SDK built-in commands like /context and /compact are passed directly to the SDK
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    task_tool_uses = {}  # Track Task (subagent) tool uses by tool_id
    metadata = {}
    interrupted = False
    include_partial = options.include_partial_messages  # Track if streaming events are enabled

    try:
        await state.client.query(prompt)

        async for message in state.client.receive_response():
            # Check for interrupt request as a failsafe
            if state.interrupt_requested:
                logger.info(f"[WS] Interrupt flag detected for session {session_id}, breaking out of loop")
                interrupted = True
                break

            if isinstance(message, SystemMessage):
                logger.info(f"[WS] SystemMessage received: subtype={message.subtype}, data_keys={list(message.data.keys()) if message.data else []}, data_preview={str(message.data)[:500] if message.data else 'None'}")
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    state.sdk_session_id = sdk_session_id
                    logger.info(f"[WS] Captured SDK session ID: {sdk_session_id}")
                else:
                    # Forward other system messages (like /context output) to frontend
                    logger.info(f"[WS] Forwarding SystemMessage to frontend: subtype={message.subtype}")
                    yield {
                        "type": "system",
                        "subtype": message.subtype,
                        "data": message.data
                    }

            elif isinstance(message, StreamEvent):
                # StreamEvent is sent when include_partial_messages=True
                # Contains real-time streaming events for character-by-character display
                event = message.event
                event_type = event.get("type") if event else None

                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    delta_type = delta.get("type")

                    if delta_type == "text_delta" and delta.get("text"):
                        # Real-time text streaming chunk
                        text = delta["text"]
                        logger.debug(f"[WS] StreamEvent text_delta: {len(text)} chars")
                        yield {
                            "type": "stream_delta",
                            "delta_type": "text",
                            "content": text,
                            "index": event.get("index", 0)
                        }
                    elif delta_type == "thinking_delta" and delta.get("thinking"):
                        # Thinking block streaming
                        yield {
                            "type": "stream_delta",
                            "delta_type": "thinking",
                            "content": delta["thinking"],
                            "index": event.get("index", 0)
                        }
                    elif delta_type == "input_json_delta" and delta.get("partial_json"):
                        # Tool input streaming
                        yield {
                            "type": "stream_delta",
                            "delta_type": "tool_input",
                            "content": delta["partial_json"],
                            "index": event.get("index", 0)
                        }

                elif event_type == "message_start":
                    # Start of a new streaming message
                    logger.debug("[WS] StreamEvent message_start")
                    yield {
                        "type": "stream_start",
                        "message": event.get("message", {})
                    }

                elif event_type == "content_block_start":
                    # Start of a content block (text, thinking, tool_use)
                    content_block = event.get("content_block", {})
                    block_type = content_block.get("type")
                    logger.debug(f"[WS] StreamEvent content_block_start: {block_type}")
                    yield {
                        "type": "stream_block_start",
                        "block_type": block_type,
                        "index": event.get("index", 0),
                        "content_block": content_block
                    }

                elif event_type == "content_block_stop":
                    # End of a content block
                    yield {
                        "type": "stream_block_stop",
                        "index": event.get("index", 0)
                    }

                elif event_type == "message_delta":
                    # Final message metadata (stop_reason, usage)
                    yield {
                        "type": "stream_message_delta",
                        "delta": event.get("delta", {}),
                        "usage": event.get("usage", {})
                    }

            elif isinstance(message, AssistantMessage):
                # Check interrupt before processing each message block
                if state.interrupt_requested:
                    logger.info(f"[WS] Interrupt flag detected during message processing for session {session_id}")
                    interrupted = True
                    break

                # Check for synthetic API error messages (model is "<synthetic>")
                # These are generated by the SDK when an API error occurs
                if message.model == "<synthetic>" or message.error:
                    # Extract error text from content blocks
                    error_text = ""
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            error_text = block.text
                            break
                    if error_text:
                        logger.warning(f"[WS] API error message: {error_text[:100]}...")
                        yield {
                            "type": "system",
                            "subtype": "api_error",
                            "data": {"content": error_text}
                        }
                    continue

                # Check if this message is from a subagent using parent_tool_use_id
                parent_tool_id = message.parent_tool_use_id

                for block in message.content:
                    if isinstance(block, TextBlock):
                        # Check if this text is from a subagent
                        if parent_tool_id and parent_tool_id in task_tool_uses:
                            # Text from subagent - send as subagent_chunk
                            logger.debug(f"[WS] Subagent chunk len={len(block.text)} for agent={parent_tool_id}")
                            yield {
                                "type": "subagent_chunk",
                                "agent_id": parent_tool_id,
                                "content": block.text
                            }
                        else:
                            # When include_partial_messages=True, text was already streamed via stream_delta
                            # Only send chunk when partial messages is disabled
                            response_text.append(block.text)
                            if not include_partial:
                                logger.debug(f"[WS] Text chunk len={len(block.text)} for session={session_id}")
                                yield {"type": "chunk", "content": block.text}

                    elif isinstance(block, ToolUseBlock):
                        tool_id = getattr(block, 'id', None)
                        tool_input = block.input or {}

                        # Collect tool use for storage
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": tool_id,
                            "input": tool_input
                        })

                        # Check if this is a Task tool (subagent) invocation
                        if block.name == "Task":
                            # Track this Task tool use
                            task_tool_uses[tool_id] = {
                                "agent_type": tool_input.get("subagent_type", "unknown"),
                                "description": tool_input.get("description", ""),
                                "prompt": tool_input.get("prompt", "")
                            }

                            # Yield subagent_start event instead of regular tool_use
                            yield {
                                "type": "subagent_start",
                                "tool_id": tool_id,
                                "agent_type": tool_input.get("subagent_type", "unknown"),
                                "description": tool_input.get("description", ""),
                                "prompt": tool_input.get("prompt", ""),
                                "agent_id": tool_id  # Use tool_id as agent_id for tracking
                            }
                        # Check if this tool use is from a subagent
                        elif parent_tool_id and parent_tool_id in task_tool_uses:
                            # Tool use from subagent - send as subagent_tool_use
                            yield {
                                "type": "subagent_tool_use",
                                "agent_id": parent_tool_id,
                                "name": block.name,
                                "id": tool_id,
                                "input": tool_input
                            }
                        else:
                            yield {
                                "type": "tool_use",
                                "name": block.name,
                                "id": tool_id,
                                "input": tool_input
                            }

                    elif isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content)
                        tool_use_id = getattr(block, 'tool_use_id', None)
                        tool_name = getattr(block, 'name', 'unknown')

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output
                        })

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            yield {
                                "type": "subagent_done",
                                "tool_id": tool_use_id,
                                "agent_id": tool_use_id,  # Use tool_id as agent_id
                                "agent_type": task_info.get("agent_type", "unknown"),
                                "result": output,
                                "is_error": getattr(block, 'is_error', False)
                            }
                            # Clean up tracking
                            del task_tool_uses[tool_use_id]
                        # Check if this tool result is from a subagent
                        elif parent_tool_id and parent_tool_id in task_tool_uses:
                            # Tool result from subagent - send as subagent_tool_result
                            yield {
                                "type": "subagent_tool_result",
                                "agent_id": parent_tool_id,
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": getattr(block, 'is_error', False)
                            }
                        else:
                            yield {
                                "type": "tool_result",
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": getattr(block, 'is_error', False)
                            }

                metadata["model"] = message.model

            elif isinstance(message, UserMessage):
                # UserMessage can contain:
                # 1. Tool results from Claude's tool executions (ToolResultBlock)
                # 2. Local command output like /context, /compact (TextBlock with <local-command-stdout>)
                # Check if this message is from a subagent using parent_tool_use_id
                parent_tool_id = message.parent_tool_use_id
                logger.info(f"[WS] UserMessage received: content_type={type(message.content).__name__}, parent_tool_id={parent_tool_id}")

                # Handle string content (rare but possible)
                if isinstance(message.content, str):
                    content = message.content
                    logger.info(f"[WS] UserMessage string content preview: {content[:200] if content else 'empty'}")
                    # Check for local command output
                    if "<local-command-stdout>" in content:
                        match = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', content, re.DOTALL)
                        if match:
                            yield {
                                "type": "system",
                                "subtype": "local_command",
                                "data": {"content": match.group(1).strip()}
                            }
                    continue

                for block in message.content:
                    # Handle TextBlock - may contain local command output
                    if isinstance(block, TextBlock):
                        text = block.text
                        # Check for local command output (e.g., /context, /compact)
                        if "<local-command-stdout>" in text:
                            match = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
                            if match:
                                yield {
                                    "type": "system",
                                    "subtype": "local_command",
                                    "data": {"content": match.group(1).strip()}
                                }
                        # Don't yield regular text from UserMessage - it's usually meta/system info

                    elif isinstance(block, ToolResultBlock):
                        output = truncate_large_payload(block.content) if block.content else ""
                        tool_use_id = block.tool_use_id
                        logger.debug(f"[WS] UserMessage ToolResultBlock - tool_use_id: {tool_use_id}, parent_tool_id: {parent_tool_id}, content length: {len(str(block.content) if block.content else '')}, is_error: {block.is_error}")

                        # Collect tool result for storage
                        tool_messages.append({
                            "type": "tool_result",
                            "name": "unknown",  # UserMessage ToolResultBlock doesn't have name
                            "tool_id": tool_use_id,
                            "output": output
                        })

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            yield {
                                "type": "subagent_done",
                                "tool_id": tool_use_id,
                                "agent_id": tool_use_id,  # Use tool_id as agent_id
                                "agent_type": task_info.get("agent_type", "unknown"),
                                "result": output,
                                "is_error": block.is_error
                            }
                            # Clean up tracking
                            del task_tool_uses[tool_use_id]
                        # Check if this tool result is from a subagent
                        elif parent_tool_id and parent_tool_id in task_tool_uses:
                            # Tool result from subagent - send as subagent_tool_result
                            yield {
                                "type": "subagent_tool_result",
                                "agent_id": parent_tool_id,
                                "name": "unknown",
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": block.is_error
                            }
                        else:
                            yield {
                                "type": "tool_result",
                                "name": "unknown",
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": block.is_error
                            }

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

    except asyncio.CancelledError:
        interrupted = True
        logger.info(f"[WS] Query cancelled for session {session_id}")
        # Don't re-raise - we handle it gracefully by yielding interrupted message

    except Exception as e:
        logger.error(f"[WS] Query error for session {session_id}: {e}")
        state.is_connected = False
        yield {"type": "error", "message": str(e)}
        return

    finally:
        # Mark as not streaming and reset interrupt flag
        state.is_streaming = False
        state.interrupt_requested = False
        state.last_activity = datetime.now()

        # Disconnect the SDK client to terminate the Claude Code subprocess
        # This MUST be done in the same task that created/connected the client
        # (anyio cancel scopes are task-bound)
        if state.client:
            try:
                await state.client.disconnect()
                logger.info(f"[WS] Disconnected SDK client for session {session_id}")
            except Exception as e:
                logger.warning(f"[WS] Error disconnecting SDK client for session {session_id}: {e}")
            state.is_connected = False

        # Remove from active sessions and cleanup agent files
        if session_id in _active_sessions:
            if state.agents_dir and state.written_agent_ids:
                cleanup_agents_directory(state.agents_dir, state.written_agent_ids)
                logger.info(f"[WS] Cleaned up agent files for session {session_id}")
            del _active_sessions[session_id]
            logger.info(f"[WS] Removed session {session_id} from active sessions")

    # Update session in database
    # Only set title for new sessions (when resume_id was None, meaning first message)
    # This preserves the first user message as the session title
    title_to_set = None
    if not resume_id:
        title_to_set = prompt[:50].strip()
        if len(prompt) > 50:
            title_to_set += "..."

    database.update_session(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        title=title_to_set,
        cost_increment=metadata.get("total_cost_usd", 0),
        tokens_in_increment=metadata.get("tokens_in", 0),
        tokens_out_increment=metadata.get("tokens_out", 0),
        turn_increment=metadata.get("num_turns", 0)
    )
    logger.info(f"[WS] Updated session {session_id}, sdk_session_id={sdk_session_id}, title={title_to_set}")

    # Store tool messages (tool_use and tool_result)
    for tool_msg in tool_messages:
        if tool_msg["type"] == "tool_use":
            database.add_session_message(
                session_id=session_id,
                role="tool_use",
                content=f"Using tool: {tool_msg['name']}",
                tool_name=tool_msg["name"],
                tool_input=tool_msg.get("input"),
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )
        elif tool_msg["type"] == "tool_result":
            database.add_session_message(
                session_id=session_id,
                role="tool_result",
                content=tool_msg.get("output", ""),
                tool_name=tool_msg["name"],
                metadata={"tool_id": tool_msg.get("tool_id")},
                profile_id=profile_id
            )

    # Store assistant response
    full_response = "".join(response_text)
    if full_response or tool_messages or interrupted:
        database.add_session_message(
            session_id=session_id,
            role="assistant",
            content=full_response + ("\n\n[Interrupted]" if interrupted else ""),
            metadata=metadata,
            profile_id=profile_id
        )

    # Log usage
    if metadata:
        database.log_usage(
            session_id=session_id,
            profile_id=profile_id,
            model=metadata.get("model"),
            tokens_in=metadata.get("tokens_in", 0),
            tokens_out=metadata.get("tokens_out", 0),
            cost_usd=metadata.get("total_cost_usd", 0),
            duration_ms=metadata.get("duration_ms", 0)
        )

    # Yield done or interrupted event
    if interrupted:
        yield {
            "type": "interrupted",
            "session_id": session_id,
            "message": "Query was interrupted"
        }
    else:
        yield {
            "type": "done",
            "session_id": session_id,
            "metadata": metadata
        }
