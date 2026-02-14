"""
AI Tools SDK MCP Server - Native tool registration for AI content generation.

Instead of injecting import paths into the system prompt and having Claude write
.mjs scripts, this module registers AI tools as proper SDK MCP tools using the
@tool decorator and create_sdk_mcp_server().

Each tool calls the underlying Node.js implementation via a helper that creates
a temp .mjs script, runs it with `node`, and parses the JSON output.
"""

import asyncio
import json
import logging
import os
import platform
import tempfile
from typing import Any, Dict, List, Optional

from claude_agent_sdk import tool, create_sdk_mcp_server

logger = logging.getLogger(__name__)


# ============================================================================
# Node.js Tool Runner
# ============================================================================

async def run_node_tool(module_path: str, function_name: str, args: dict, extra_env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> dict:
    """
    Run a Node.js AI tool function and return the JSON result.

    Creates a temporary .mjs script that imports and calls the specified function,
    runs it with Node.js, and parses the JSON output from stdout.

    Args:
        module_path: Absolute path to the .js module file
        function_name: Name of the exported function to call
        args: Arguments dict to pass to the function
        extra_env: Additional environment variables (e.g., API keys) to inject
        cwd: Working directory for the Node.js subprocess. Node tools use
            process.cwd() to determine output paths (e.g., generated-images/).
            Should be set to the workspace dir so files are saved where the
            API can serve them.

    Returns:
        Parsed JSON result dict from the tool
    """
    # Convert Windows paths to file:// URLs for ESM imports
    if platform.system() == "Windows" and len(module_path) >= 2 and module_path[1] == ':':
        module_path = "file:///" + module_path.replace("\\", "/")

    # Create the temp .mjs script
    script_content = f"""
import {{ {function_name} }} from '{module_path}';
try {{
    const result = await {function_name}({json.dumps(args)});
    console.log(JSON.stringify(result));
}} catch (error) {{
    console.log(JSON.stringify({{ error: error.message }}));
    process.exit(1);
}}
"""

    # Write to temp file
    temp_dir = os.environ.get('TEMP', os.environ.get('TMP', '/tmp'))
    fd, script_path = tempfile.mkstemp(suffix='.mjs', dir=temp_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(script_content)

        # Run with node, merging extra env vars (API keys) into process env
        node_env = os.environ.copy()
        if extra_env:
            node_env.update(extra_env)

        process = await asyncio.create_subprocess_exec(
            'node', script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=node_env,
            cwd=cwd
        )

        # 5 min timeout for async tools like 3D generation
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=300
        )

        if process.returncode != 0:
            error_msg = stderr.decode().strip() or "Tool execution failed"
            logger.error(f"Node tool {function_name} failed (exit {process.returncode}): {error_msg}")
            return {"error": error_msg}

        output = stdout.decode().strip()
        if not output:
            return {"error": "Tool returned no output"}

        return json.loads(output)

    except asyncio.TimeoutError:
        logger.error(f"Node tool {function_name} timed out after 300s")
        return {"error": f"Tool {function_name} timed out after 5 minutes"}
    except json.JSONDecodeError as e:
        logger.error(f"Node tool {function_name} returned invalid JSON: {e}")
        return {"error": f"Tool returned invalid JSON output"}
    except Exception as e:
        logger.error(f"Node tool {function_name} error: {e}")
        return {"error": str(e)}
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


# ============================================================================
# Tool Result Helper
# ============================================================================

def _tool_result(result: dict) -> dict:
    """Wrap a result dict in the MCP tool response format."""
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


# ============================================================================
# Tool Definitions
# ============================================================================
# Each tool is defined with the @tool decorator. The tools_dir is resolved
# at server creation time via closure in create_ai_tools_server().

def _create_image_tools(tools_dir: str, tool_env: Optional[Dict[str, str]] = None, tool_cwd: Optional[str] = None) -> list:
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/image-generation/generateImage.js",
            function_name="generateImage",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/image-generation/editImage.js",
            function_name="editImage",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/image-generation/generateWithReference.js",
            function_name="generateWithReference",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
        return _tool_result(result)

    tools.append(generate_with_reference)

    return tools


def _create_video_tools(tools_dir: str, tool_env: Optional[Dict[str, str]] = None, tool_cwd: Optional[str] = None) -> list:
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/video-generation/generateVideo.js",
            function_name="generateVideo",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/video-generation/imageToVideo.js",
            function_name="imageToVideo",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/video-generation/extendVideo.js",
            function_name="extendVideo",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/video-generation/bridgeFrames.js",
            function_name="bridgeFrames",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/video-analysis/analyzeVideo.js",
            function_name="analyzeVideo",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
        return _tool_result(result)

    tools.append(analyze_video)

    return tools


def _create_3d_tools(tools_dir: str, tool_env: Optional[Dict[str, str]] = None, tool_cwd: Optional[str] = None) -> list:
    """Create 3D model generation tool functions."""
    tools = []

    @tool("textTo3D", "Generate a 3D model from a text description (async, 2-5 min)", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the 3D model to generate"},
            "art_style": {"type": "string", "description": "Art style (e.g., realistic, sculpture)"},
            "model": {"type": "string", "description": "Model to use (latest, meshy-5)"},
            "topology": {"type": "string", "description": "Mesh topology type"},
            "target_polycount": {"type": "integer", "description": "Target polygon count"},
            "wait_for_completion": {"type": "boolean", "description": "Wait for completion (true) or return task ID immediately"}
        },
        "required": ["prompt"]
    })
    async def text_to_3d(args: dict) -> dict:
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/model3d-generation/textTo3D.js",
            function_name="textTo3D",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
        return _tool_result(result)

    tools.append(text_to_3d)

    @tool("imageTo3D", "Generate a 3D model from a reference image (async, 2-5 min)", {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Path to the reference image"},
            "prompt": {"type": "string", "description": "Optional text description to guide generation"},
            "model": {"type": "string", "description": "Model to use (latest, meshy-5)"},
            "topology": {"type": "string", "description": "Mesh topology type"},
            "enable_pbr": {"type": "boolean", "description": "Enable PBR materials"},
            "wait_for_completion": {"type": "boolean", "description": "Wait for completion (true) or return task ID immediately"}
        },
        "required": ["image_path"]
    })
    async def image_to_3d(args: dict) -> dict:
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/model3d-generation/imageTo3D.js",
            function_name="imageTo3D",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/model3d-generation/retexture3D.js",
            function_name="retexture3D",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/model3d-generation/rig3D.js",
            function_name="rig3D",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/model3d-generation/animate3D.js",
            function_name="animate3D",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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
        result = await run_node_tool(
            module_path=f"{tools_dir}/dist/model3d-generation/getTask3D.js",
            function_name="getTask3D",
            args=args,
            extra_env=tool_env,
            cwd=tool_cwd
        )
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

    Only includes tools that are enabled in the ai_tools_config.
    Returns None if no tools are enabled or tools_dir is not configured.

    Args:
        ai_tools_config: Dict with boolean toggles for each tool category
            (e.g., {"image_generation": True, "video_generation": False})
        tools_dir: Absolute path to the AI tools directory containing dist/
        tool_env: Environment variables (API keys, provider settings) to inject
            into Node.js subprocesses when executing tools
        tool_cwd: Working directory for Node.js subprocesses. Should be the
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

    # Create all tool functions with the tools_dir, env, and cwd baked in
    all_image_tools = _create_image_tools(tools_dir, tool_env, tool_cwd)
    all_video_tools = _create_video_tools(tools_dir, tool_env, tool_cwd)
    all_3d_tools = _create_3d_tools(tools_dir, tool_env, tool_cwd)

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
