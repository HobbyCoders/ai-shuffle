"""
AI Tools SDK MCP Server - Native tool registration for AI content generation.

Registers AI tools as proper SDK MCP tools using the @tool decorator and
create_sdk_mcp_server(). Each tool delegates to a persistent Node.js worker
process that handles execution via stdin/stdout JSON-RPC.

The worker is started lazily on first tool call and stays alive for the session,
eliminating the overhead of temp file creation and cold Node.js startup per call.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from claude_agent_sdk import tool, create_sdk_mcp_server

from app.mcp.node_worker import NodeWorker

logger = logging.getLogger(__name__)


# ============================================================================
# Tool Result Helper
# ============================================================================

def _tool_result(result: dict) -> dict:
    """Wrap a result dict in the MCP tool response format."""
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


# ============================================================================
# Tool Definitions
# ============================================================================
# Each tool is defined with the @tool decorator. The worker instance is
# captured via closure at server creation time.

def _create_image_tools(worker: NodeWorker) -> list:
    """Create image generation/editing tool functions."""
    tools = []

    @tool("generateImage", "Generate an image from a text prompt using AI", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to generate"},
            "provider": {"type": "string", "description": "Provider to use (e.g., google-gemini, google-imagen, openai-gpt-image)"}
        },
        "required": ["prompt"]
    })
    async def generate_image(args: dict) -> dict:
        result = await worker.call("generateImage", args)
        return _tool_result(result)

    tools.append(generate_image)

    @tool("editImage", "Edit an existing image using AI - add, remove, or modify elements", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the edit to make"},
            "image_path": {"type": "string", "description": "Path to the image to edit"},
            "provider": {"type": "string", "description": "Provider to use (gemini or openai only)"}
        },
        "required": ["prompt", "image_path"]
    })
    async def edit_image(args: dict) -> dict:
        result = await worker.call("editImage", args)
        return _tool_result(result)

    tools.append(edit_image)

    @tool("generateWithReference", "Generate an image using reference images for character/style consistency", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to generate"},
            "reference_images": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Paths to reference images (up to 14)"
            }
        },
        "required": ["prompt", "reference_images"]
    })
    async def generate_with_reference(args: dict) -> dict:
        result = await worker.call("generateWithReference", args)
        return _tool_result(result)

    tools.append(generate_with_reference)

    return tools


def _create_video_tools(worker: NodeWorker) -> list:
    """Create video generation/analysis tool functions."""
    tools = []

    @tool("generateVideo", "Generate a video from a text prompt using AI", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the video to generate"},
            "duration": {"type": "integer", "description": "Duration in seconds (4, 6, or 8)"},
            "aspect_ratio": {"type": "string", "description": "Aspect ratio (e.g., 16:9, 9:16)"},
            "provider": {"type": "string", "description": "Provider to use (e.g., google-veo, openai-sora)"},
            "model": {"type": "string", "description": "Model to use (e.g., veo-3-generate-preview for audio)"}
        },
        "required": ["prompt"]
    })
    async def generate_video(args: dict) -> dict:
        result = await worker.call("generateVideo", args)
        return _tool_result(result)

    tools.append(generate_video)

    @tool("imageToVideo", "Animate a still image into a video", {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Path to the image to animate"},
            "prompt": {"type": "string", "description": "Text description of the desired animation"},
            "provider": {"type": "string", "description": "Provider to use (e.g., google-veo, openai-sora)"}
        },
        "required": ["image_path", "prompt"]
    })
    async def image_to_video(args: dict) -> dict:
        result = await worker.call("imageToVideo", args)
        return _tool_result(result)

    tools.append(image_to_video)

    @tool("extendVideo", "Extend an existing Veo video by ~7 seconds", {
        "type": "object",
        "properties": {
            "video_uri": {"type": "string", "description": "Source video URI from a previous result's source_video_uri field"},
            "prompt": {"type": "string", "description": "Text description for the extension"}
        },
        "required": ["video_uri", "prompt"]
    })
    async def extend_video(args: dict) -> dict:
        result = await worker.call("extendVideo", args)
        return _tool_result(result)

    tools.append(extend_video)

    @tool("bridgeFrames", "Generate a smooth video transition between two images (Veo only)", {
        "type": "object",
        "properties": {
            "start_image": {"type": "string", "description": "Path to the starting frame image"},
            "end_image": {"type": "string", "description": "Path to the ending frame image"},
            "prompt": {"type": "string", "description": "Text description of the desired transition"}
        },
        "required": ["start_image", "end_image", "prompt"]
    })
    async def bridge_frames(args: dict) -> dict:
        result = await worker.call("bridgeFrames", args)
        return _tool_result(result)

    tools.append(bridge_frames)

    @tool("analyzeVideo", "Analyze and understand video content - scenes, transcript, and answer questions", {
        "type": "object",
        "properties": {
            "video_path": {"type": "string", "description": "Path to the video file to analyze"},
            "prompt": {"type": "string", "description": "Question or analysis prompt about the video"}
        },
        "required": ["video_path", "prompt"]
    })
    async def analyze_video(args: dict) -> dict:
        result = await worker.call("analyzeVideo", args)
        return _tool_result(result)

    tools.append(analyze_video)

    return tools


def _create_3d_tools(worker: NodeWorker) -> list:
    """Create 3D model generation tool functions."""
    tools = []

    @tool("textTo3D", "Generate a 3D model from a text description (async, 2-5 min)", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the 3D model to generate"},
            "art_style": {"type": "string", "description": "Art style (e.g., realistic, sculpture)"},
            "model": {"type": "string", "description": "Model to use (latest, meshy-5, or fal-ai model IDs like fal-ai/hunyuan-3d/v2/turbo)"},
            "provider": {"type": "string", "description": "Provider to use: 'meshy' (default) or 'fal-ai'"},
            "topology": {"type": "string", "description": "Mesh topology type"},
            "target_polycount": {"type": "integer", "description": "Target polygon count"},
            "wait_for_completion": {"type": "boolean", "description": "Wait for completion (true) or return task ID immediately"}
        },
        "required": ["prompt"]
    })
    async def text_to_3d(args: dict) -> dict:
        result = await worker.call("textTo3D", args)
        return _tool_result(result)

    tools.append(text_to_3d)

    @tool("imageTo3D", "Generate a 3D model from a reference image (async, 2-5 min)", {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Path to the reference image"},
            "prompt": {"type": "string", "description": "Optional text description to guide generation"},
            "model": {"type": "string", "description": "Model to use (latest, meshy-5, or fal-ai model IDs like fal-ai/hunyuan-3d/v2/turbo)"},
            "provider": {"type": "string", "description": "Provider to use: 'meshy' (default) or 'fal-ai'"},
            "topology": {"type": "string", "description": "Mesh topology type"},
            "enable_pbr": {"type": "boolean", "description": "Enable PBR materials"},
            "wait_for_completion": {"type": "boolean", "description": "Wait for completion (true) or return task ID immediately"}
        },
        "required": ["image_path"]
    })
    async def image_to_3d(args: dict) -> dict:
        result = await worker.call("imageTo3D", args)
        return _tool_result(result)

    tools.append(image_to_3d)

    @tool("retexture3D", "Apply new textures to an existing 3D model", {
        "type": "object",
        "properties": {
            "model_path_or_task_id": {"type": "string", "description": "Path to the 3D model file or a Meshy task ID"},
            "style_prompt": {"type": "string", "description": "Text description of the desired texture style"},
            "style_image": {"type": "string", "description": "Optional path to a style reference image"},
            "enable_pbr": {"type": "boolean", "description": "Enable PBR materials"}
        },
        "required": ["model_path_or_task_id", "style_prompt"]
    })
    async def retexture_3d(args: dict) -> dict:
        result = await worker.call("retexture3D", args)
        return _tool_result(result)

    tools.append(retexture_3d)

    @tool("rig3D", "Add an animation skeleton to a 3D model for animation", {
        "type": "object",
        "properties": {
            "model_path_or_task_id": {"type": "string", "description": "Path to the 3D model file or a Meshy task ID"},
            "height_meters": {"type": "number", "description": "Height of the model in meters for scale reference"}
        },
        "required": ["model_path_or_task_id"]
    })
    async def rig_3d(args: dict) -> dict:
        result = await worker.call("rig3D", args)
        return _tool_result(result)

    tools.append(rig_3d)

    @tool("animate3D", "Animate a rigged 3D model with preset animations", {
        "type": "object",
        "properties": {
            "rig_task_id": {"type": "string", "description": "Task ID from a rig3D result"},
            "action_id": {"type": "string", "description": "Animation action (walk_forward, run, jump, idle, wave, dance_hip_hop, etc)"},
            "fps": {"type": "integer", "description": "Frames per second for the animation"}
        },
        "required": ["rig_task_id", "action_id"]
    })
    async def animate_3d(args: dict) -> dict:
        result = await worker.call("animate3D", args)
        return _tool_result(result)

    tools.append(animate_3d)

    @tool("getTask3D", "Check the status of an async 3D generation task", {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task ID to check status for"}
        },
        "required": ["task_id"]
    })
    async def get_task_3d(args: dict) -> dict:
        result = await worker.call("getTask3D", args)
        return _tool_result(result)

    tools.append(get_task_3d)

    return tools


# ============================================================================
# Tool-to-Config Mapping
# ============================================================================

# Maps each @tool name to the ai_tools_config key(s) that enable it
# and whether it requires specific provider availability
TOOL_CONFIG_MAP = {
    # Image tools
    "generateImage": {"config_key": "image_generation"},
    "editImage": {"config_key": "image_editing"},
    "generateWithReference": {"config_key": "image_reference", "requires_gemini": True},
    # Video tools
    "generateVideo": {"config_keys": ["video_generation", "video_with_audio"]},
    "imageToVideo": {"config_key": "image_to_video"},
    "extendVideo": {"config_key": "video_extend", "requires_gemini": True},
    "bridgeFrames": {"config_key": "video_bridge", "requires_gemini": True},
    "analyzeVideo": {"config_key": "video_understanding", "requires_gemini": True},
    # 3D tools
    "textTo3D": {"config_key": "text_to_3d"},
    "imageTo3D": {"config_key": "image_to_3d"},
    "retexture3D": {"config_key": "retexture_3d"},
    "rig3D": {"config_key": "rig_3d"},
    "animate3D": {"config_key": "animate_3d"},
    "getTask3D": {"config_keys_any_3d": True},  # Always available when any 3D tool is enabled
}


def _is_tool_enabled(tool_name: str, ai_tools_config: dict) -> bool:
    """Check if a specific tool should be enabled based on config."""
    mapping = TOOL_CONFIG_MAP.get(tool_name)
    if not mapping:
        return False

    # Special case: getTask3D is enabled when any 3D tool is enabled
    if mapping.get("config_keys_any_3d"):
        return any(
            ai_tools_config.get(key, False)
            for key in ["text_to_3d", "image_to_3d", "retexture_3d", "rig_3d", "animate_3d"]
        )

    # Check multiple config keys (OR logic - any enabled = tool enabled)
    if "config_keys" in mapping:
        return any(ai_tools_config.get(key, False) for key in mapping["config_keys"])

    # Check single config key
    config_key = mapping.get("config_key")
    if config_key:
        return ai_tools_config.get(config_key, False)

    return False


def get_enabled_tool_names(ai_tools_config: dict) -> List[str]:
    """
    Get list of MCP tool names that should be enabled based on config.

    Returns tool names in the mcp__<server>__<tool> format for allowed_tools.
    """
    enabled = []
    for tool_name in TOOL_CONFIG_MAP:
        if _is_tool_enabled(tool_name, ai_tools_config):
            enabled.append(f"mcp__ai-tools__{tool_name}")
    return enabled


# ============================================================================
# Server Creation
# ============================================================================

def create_ai_tools_server(
    ai_tools_config: dict,
    tools_dir: str,
    tool_env: Optional[Dict[str, str]] = None,
    tool_cwd: Optional[str] = None,
) -> Optional[Any]:
    """
    Create an SDK MCP server with AI content generation tools.

    Uses a persistent Node.js worker process instead of spawning a new
    process per tool call. The worker is started lazily on first tool use.

    Only includes tools that are enabled in the ai_tools_config.
    Returns None if no tools are enabled or tools_dir is not configured.

    Args:
        ai_tools_config: Dict with boolean toggles for each tool category
            (e.g., {"image_generation": True, "video_generation": False})
        tools_dir: Absolute path to the AI tools directory containing dist/
        tool_env: Environment variables (API keys, provider settings) to inject
            into the Node.js worker process
        tool_cwd: Working directory for the Node.js worker. Should be the
            workspace dir so generated files (images, videos) are saved where
            the API can serve them.

    Returns:
        McpSdkServerConfig from create_sdk_mcp_server(), or None
    """
    if not ai_tools_config or not tools_dir:
        return None

    # Check if any relevant tools are enabled
    any_image = ai_tools_config.get("image_generation", False) or \
                ai_tools_config.get("image_editing", False) or \
                ai_tools_config.get("image_reference", False)
    any_video = ai_tools_config.get("video_generation", False) or \
                ai_tools_config.get("image_to_video", False) or \
                ai_tools_config.get("video_extend", False) or \
                ai_tools_config.get("video_bridge", False) or \
                ai_tools_config.get("video_with_audio", False) or \
                ai_tools_config.get("video_understanding", False)
    any_3d = ai_tools_config.get("text_to_3d", False) or \
             ai_tools_config.get("image_to_3d", False) or \
             ai_tools_config.get("retexture_3d", False) or \
             ai_tools_config.get("rig_3d", False) or \
             ai_tools_config.get("animate_3d", False)

    if not any_image and not any_video and not any_3d:
        return None

    # Normalize tools_dir path (remove trailing separators)
    tools_dir = tools_dir.rstrip("/\\")

    # Create persistent Node.js worker (started lazily on first tool call)
    worker_script = f"{tools_dir}/dist/worker.js"
    worker = NodeWorker(
        worker_script=worker_script,
        env=tool_env,
        cwd=tool_cwd,
    )

    # Create all tool functions with the worker baked in via closure
    all_image_tools = _create_image_tools(worker)
    all_video_tools = _create_video_tools(worker)
    all_3d_tools = _create_3d_tools(worker)

    # Filter to only enabled tools
    enabled_tools = []

    for t in all_image_tools:
        if _is_tool_enabled(t.name, ai_tools_config):
            enabled_tools.append(t)

    for t in all_video_tools:
        if _is_tool_enabled(t.name, ai_tools_config):
            enabled_tools.append(t)

    for t in all_3d_tools:
        if _is_tool_enabled(t.name, ai_tools_config):
            enabled_tools.append(t)

    if not enabled_tools:
        return None

    logger.info(f"Creating AI tools MCP server with {len(enabled_tools)} tools: {[t.name for t in enabled_tools]}")

    return create_sdk_mcp_server(
        name="ai-tools",
        version="1.0.0",
        tools=enabled_tools,
    )
