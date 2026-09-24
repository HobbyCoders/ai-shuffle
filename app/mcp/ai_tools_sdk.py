"""
AI Tools SDK MCP Server - Native tool registration for AI content generation.

Registers AI tools as proper SDK MCP tools using the @tool decorator and
create_tool_server(). Each tool delegates to a persistent Node.js worker
process that handles execution via stdin/stdout JSON-RPC.

The worker is started lazily on first tool call and stays alive for the session,
eliminating the overhead of temp file creation and cold Node.js startup per call.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from app.mcp.inproc import tool, create_tool_server
from mcp.types import ToolAnnotations

from app.mcp.config_helpers import _json_default
from app.mcp.node_worker import NodeWorker
from app.mcp.sdk_compat import patch_sdk_server

logger = logging.getLogger(__name__)


# ============================================================================
# Tool Result Helper
# ============================================================================

def _tool_result(result: dict) -> dict:
    """Wrap a result dict in the MCP tool response format.

    Uses ``_json_default`` so datetime/date values in results ISO-normalize
    instead of crashing the tool with 'Object of type datetime is not JSON
    serializable' (same failure class as the CronList crash).
    """
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, default=_json_default)}]}


def _model_arg(args: dict) -> dict:
    """Pass the fal model ID as ``model``, the field the Node tools read.

    These schemas used to advertise the model ID as ``provider``, which the Node
    tools ignore (fal is the only provider), so a requested model silently fell
    back to the default one. Calls made against the old schema still arrive with
    ``provider``; carry that value over rather than dropping it.
    """
    args = dict(args)
    legacy = args.pop("provider", None)
    if legacy and legacy != "fal-ai" and not args.get("model"):
        args["model"] = legacy
    return args


# Annotation for tools that may return large results (base64 previews, lengthy metadata).
# Prevents the SDK from silently truncating results beyond the default 50K char limit.
_LARGE_RESULT_ANNOTATIONS = ToolAnnotations(maxResultSizeChars=200_000)


# ============================================================================
# Tool Definitions
# ============================================================================
# Each tool is defined with the @tool decorator. The worker instance is
# captured via closure at server creation time.

def _create_image_tools(worker: NodeWorker) -> list:
    """Create image generation/editing tool functions."""
    tools = []

    @tool("generateImage", "Generate an image from a text prompt using AI (fal.ai models: Flux Pro Ultra, Flux Dev, Flux Schnell, Nano Banana 2, Recraft V4)", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to generate"},
            "model": {"type": "string", "description": "fal.ai model ID (e.g., fal-ai/flux-pro/v1.1-ultra, fal-ai/flux/dev, fal-ai/flux/schnell, fal-ai/nano-banana-2, fal-ai/recraft-v4)"}
        },
        "required": ["prompt"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def generate_image(args: dict) -> dict:
        result = await worker.call("generateImage", _model_arg(args))
        return _tool_result(result)

    tools.append(generate_image)

    @tool("editImage", "Edit an existing image using AI - add, remove, or modify elements (fal.ai models: Nano Banana 2 Edit, Flux Kontext, Nano Banana Pro Edit)", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the edit to make"},
            "image_path": {"type": "string", "description": "Path to the image to edit"},
            "model": {"type": "string", "description": "fal.ai model ID (e.g., fal-ai/nano-banana-2, fal-ai/flux-pro/kontext, fal-ai/nano-banana-pro)"}
        },
        "required": ["prompt", "image_path"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def edit_image(args: dict) -> dict:
        result = await worker.call("editImage", _model_arg(args))
        return _tool_result(result)

    tools.append(edit_image)

    @tool("generateWithReference", "Generate an image using reference images for character/style consistency (fal.ai: uses the configured image-edit model — Flux Kontext or Nano Banana)", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to generate"},
            "reference_images": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Paths to reference images (up to 14)"
            },
            "model": {"type": "string", "description": "Optional fal.ai model ID override (e.g. fal-ai/nano-banana-pro, fal-ai/flux-pro/kontext). Defaults to the user's configured image-edit model."}
        },
        "required": ["prompt", "reference_images"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def generate_with_reference(args: dict) -> dict:
        result = await worker.call("generateWithReference", args)
        return _tool_result(result)

    tools.append(generate_with_reference)

    return tools


def _create_video_tools(worker: NodeWorker) -> list:
    """Create video generation/analysis tool functions."""
    tools = []

    @tool("generateVideo", "Generate a video from a text prompt using AI (fal.ai models: Kling O3, Veo 3.1, Sora 2)", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the video to generate"},
            "duration": {"type": "integer", "description": "Duration in seconds (4, 6, or 8)"},
            "aspect_ratio": {"type": "string", "description": "Aspect ratio (e.g., 16:9, 9:16)"},
            "model": {"type": "string", "description": "fal.ai model ID (e.g., fal-ai/kling-video/v2.1/master, fal-ai/veo3, fal-ai/sora-v2)"}
        },
        "required": ["prompt"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def generate_video(args: dict) -> dict:
        result = await worker.call("generateVideo", _model_arg(args))
        return _tool_result(result)

    tools.append(generate_video)

    @tool("imageToVideo", "Animate a still image into a video (fal.ai models: Kling O3, Veo 3.1, Sora 2)", {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Path to the image to animate"},
            "prompt": {"type": "string", "description": "Text description of the desired animation"},
            "model": {"type": "string", "description": "fal.ai model ID (e.g., fal-ai/kling-video/v2.1/master, fal-ai/veo3, fal-ai/sora-v2)"}
        },
        "required": ["image_path", "prompt"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def image_to_video(args: dict) -> dict:
        result = await worker.call("imageToVideo", _model_arg(args))
        return _tool_result(result)

    tools.append(image_to_video)

    @tool("extendVideo", "Extend an existing video's duration by generating AI continuation frames (fal.ai: Kling extend endpoints)", {
        "type": "object",
        "properties": {
            "video_path": {"type": "string", "description": "Path to the video file to extend"},
            "prompt": {"type": "string", "description": "Optional text description of what should happen next in the video"},
            "model": {"type": "string", "description": "fal.ai model ID for extension (e.g., fal-ai/kling-video/o3/standard/extend)"}
        },
        "required": ["video_path"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def extend_video(args: dict) -> dict:
        result = await worker.call("extendVideo", args)
        return _tool_result(result)

    tools.append(extend_video)

    return tools


def _create_3d_tools(worker: NodeWorker) -> list:
    """Create 3D model generation tool functions."""
    tools = []

    @tool("textTo3D", "Generate a 3D model from a text description (fal.ai models: Meshy v6, Hunyuan-3D)", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the 3D model to generate"},
            "art_style": {"type": "string", "description": "Art style (e.g., realistic, sculpture)"},
            "model": {"type": "string", "description": "fal.ai model ID (e.g., fal-ai/meshy/text-to-3d, fal-ai/hunyuan-3d/v2/turbo)"},
            "topology": {"type": "string", "description": "Mesh topology type"},
            "target_polycount": {"type": "integer", "description": "Target polygon count"},
            "wait_for_completion": {"type": "boolean", "description": "Wait for completion (true) or return task ID immediately"}
        },
        "required": ["prompt"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def text_to_3d(args: dict) -> dict:
        result = await worker.call("textTo3D", args)
        return _tool_result(result)

    tools.append(text_to_3d)

    @tool("imageTo3D", "Generate a 3D model from a reference image (fal.ai models: Meshy v6, Hunyuan-3D)", {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Path to the reference image"},
            "prompt": {"type": "string", "description": "Optional text description to guide generation"},
            "model": {"type": "string", "description": "fal.ai model ID (e.g., fal-ai/meshy/image-to-3d, fal-ai/hunyuan-3d/v2/turbo)"},
            "topology": {"type": "string", "description": "Mesh topology type"},
            "enable_pbr": {"type": "boolean", "description": "Enable PBR materials"},
            "wait_for_completion": {"type": "boolean", "description": "Wait for completion (true) or return task ID immediately"}
        },
        "required": ["image_path"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def image_to_3d(args: dict) -> dict:
        result = await worker.call("imageTo3D", args)
        return _tool_result(result)

    tools.append(image_to_3d)

    return tools


def _create_audio_gen_tools(worker: NodeWorker) -> list:
    """Create audio (music/sound) generation and transform tool functions."""
    tools = []

    @tool("generateAudio", "Generate music or sound effects from a text prompt using AI (fal.ai models: Lyria, Stable Audio, ACE-Step). Uses the configured audio-generation model.", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the music or sound to generate"},
            "duration": {"type": "number", "description": "Duration in seconds (model-dependent)"},
            "model": {"type": "string", "description": "Optional fal.ai model ID override (e.g. fal-ai/lyria2, fal-ai/stable-audio). Defaults to the configured audio-generation model."}
        },
        "required": ["prompt"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def generate_audio(args: dict) -> dict:
        result = await worker.call("generateAudio", args)
        return _tool_result(result)

    tools.append(generate_audio)

    @tool("audioToAudio", "Transform an existing audio file into new audio — style transfer, remix, stem separation, or restoration (fal.ai audio-to-audio models). Uses the configured audio-to-audio model.", {
        "type": "object",
        "properties": {
            "audio_path": {"type": "string", "description": "Path to the source audio file to transform"},
            "prompt": {"type": "string", "description": "Optional text description guiding the transformation"},
            "model": {"type": "string", "description": "Optional fal.ai model ID override. Defaults to the configured audio-to-audio model."}
        },
        "required": ["audio_path"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def audio_to_audio(args: dict) -> dict:
        result = await worker.call("audioToAudio", args)
        return _tool_result(result)

    tools.append(audio_to_audio)

    return tools


def _get_fal_api_key() -> Optional[str]:
    """Resolve the fal.ai API key from the encrypted settings store or env."""
    try:
        from app.api.settings import get_decrypted_api_key
        key = get_decrypted_api_key("fal_api_key")
        if key:
            return key
    except Exception:  # noqa: BLE001 - settings store optional in some contexts
        pass
    import os
    return os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY")


async def _search_catalog(query: str, output_type: Optional[str] = None, limit: int = 15) -> dict:
    """Search the in-process fal.ai catalog, refreshing it if empty or stale."""
    from app.services.fal_catalog import catalog_service

    if catalog_service.model_count == 0 or catalog_service.is_stale:
        try:
            await catalog_service.refresh(_get_fal_api_key())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"searchModels: catalog refresh failed: {e}")

    matches = catalog_service.search(q=query, output_type=output_type)
    total = len(matches)
    matches = matches[: max(1, min(limit, 50))]

    return {
        "success": True,
        "query": query,
        "total_matches": total,
        "returned": len(matches),
        "catalog_size": catalog_service.model_count,
        "models": [
            {
                "endpoint_id": m.endpoint_id,
                "display_name": m.display_name,
                "output_type": m.output_type,
                "category": m.category,
                "description": (m.description or "")[:240],
            }
            for m in matches
        ],
        "hint": (
            "Pick the endpoint_id you want and call runFalModel with it, passing the "
            "matching output_type. If total_matches is 0, try a broader/shorter query."
        ),
    }


def _create_catalog_tools() -> list:
    """Create the model discovery tool. Runs in-process — no Node worker needed."""
    tools = []

    @tool("searchModels", "Look up the exact fal.ai endpoint_id for a model before calling runFalModel. Searches the live 600+ model catalog by name/keyword. ALWAYS use this first when the user names a specific model (e.g. 'lyria 3 pro', 'seedance', 'flux 2') or when you are unsure of the exact endpoint string — do not guess endpoint IDs. Returns endpoint_id, output_type, category, and description for each match.", {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Model name, family, or keyword to search (e.g. 'lyria', 'music', 'seedance', 'upscale'). Matches display name, description, endpoint id, and tags. Shorter is broader."},
            "output_type": {"type": "string", "enum": ["image", "video", "3d", "audio"], "description": "Optional filter to only models that produce this output type."},
            "limit": {"type": "integer", "description": "Max results to return (default 15, max 50)."}
        },
        "required": ["query"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def search_models(args: dict) -> dict:
        result = await _search_catalog(
            query=args.get("query", ""),
            output_type=args.get("output_type"),
            limit=args.get("limit") or 15,
        )
        return _tool_result(result)

    tools.append(search_models)
    return tools


def _create_generic_tools(worker: NodeWorker) -> list:
    """Create the generic fal.ai model executor tool."""
    tools = []

    @tool("runFalModel", "Run ANY fal.ai model by endpoint ID. Supports 600+ models including Imagen 4, Seedance, Wan 2.2, Lyria (music), Stable Diffusion, and more. Use this when the specific tools (generateImage, generateVideo, etc.) don't support the model you need — including all music/audio generation. If you don't already know the EXACT endpoint_id, call searchModels first to resolve it; do not guess (a single wrong character returns 'Not Found').", {
        "type": "object",
        "properties": {
            "endpoint_id": {
                "type": "string",
                "description": "fal.ai endpoint ID (e.g., fal-ai/imagen4/preview, fal-ai/seedance-2.0, fal-ai/wan-2.2/text-to-video, fal-ai/stable-diffusion-v35-large)"
            },
            "input": {
                "type": "object",
                "description": "Input parameters for the model. Most models accept 'prompt' (string). Check the model's API docs for specific parameters.",
                "properties": {
                    "prompt": {"type": "string", "description": "Text prompt (required for most models)"}
                }
            },
            "output_type": {
                "type": "string",
                "enum": ["image", "video", "3d", "audio"],
                "description": "Expected output type for proper file handling. Auto-detected from endpoint_id if not specified."
            }
        },
        "required": ["endpoint_id", "input"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def run_fal_model(args: dict) -> dict:
        result = await worker.call("runFalModel", args)
        return _tool_result(result)

    tools.append(run_fal_model)

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
    "generateWithReference": {"config_key": "image_reference"},
    # Video tools
    "generateVideo": {"config_keys": ["video_generation", "video_with_audio"]},
    "imageToVideo": {"config_key": "image_to_video"},
    "extendVideo": {"config_key": "video_extend"},
    # 3D tools
    "textTo3D": {"config_key": "text_to_3d"},
    "imageTo3D": {"config_key": "image_to_3d"},
    # Audio generation tools
    "generateAudio": {"config_key": "audio_generation"},
    "audioToAudio": {"config_key": "audio_to_audio"},
    # Generic tool — enabled when ANY other tool is enabled
    "runFalModel": {"config_keys": [
        "image_generation", "image_editing", "image_reference",
        "video_generation", "video_with_audio", "image_to_video", "video_extend",
        "text_to_3d", "image_to_3d",
        "audio_generation", "audio_to_audio",
    ]},
    # Model discovery — enabled whenever the generic executor is (same condition)
    "searchModels": {"config_keys": [
        "image_generation", "image_editing", "image_reference",
        "video_generation", "video_with_audio", "image_to_video", "video_extend",
        "text_to_3d", "image_to_3d",
        "audio_generation", "audio_to_audio",
    ]},
}


def _is_tool_enabled(tool_name: str, ai_tools_config: dict) -> bool:
    """Check if a specific tool should be enabled based on config."""
    mapping = TOOL_CONFIG_MAP.get(tool_name)
    if not mapping:
        return False

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
        McpSdkServerConfig from create_tool_server(), or None
    """
    if not ai_tools_config or not tools_dir:
        return None

    # Check if any relevant tools are enabled
    any_image = ai_tools_config.get("image_generation", False) or \
                ai_tools_config.get("image_editing", False) or \
                ai_tools_config.get("image_reference", False)
    any_video = ai_tools_config.get("video_generation", False) or \
                ai_tools_config.get("image_to_video", False) or \
                ai_tools_config.get("video_with_audio", False)
    any_3d = ai_tools_config.get("text_to_3d", False) or \
             ai_tools_config.get("image_to_3d", False)
    any_audio_gen = ai_tools_config.get("audio_generation", False) or \
                    ai_tools_config.get("audio_to_audio", False)

    # Generic tool is enabled when any other tool is enabled
    any_generic = any_image or any_video or any_3d or any_audio_gen

    if not any_generic:
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
    all_audio_gen_tools = _create_audio_gen_tools(worker)
    all_generic_tools = _create_generic_tools(worker)
    all_catalog_tools = _create_catalog_tools()

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

    for t in all_audio_gen_tools:
        if _is_tool_enabled(t.name, ai_tools_config):
            enabled_tools.append(t)

    for t in all_generic_tools:
        if _is_tool_enabled(t.name, ai_tools_config):
            enabled_tools.append(t)

    for t in all_catalog_tools:
        if _is_tool_enabled(t.name, ai_tools_config):
            enabled_tools.append(t)

    if not enabled_tools:
        return None

    logger.info(f"Creating AI tools MCP server with {len(enabled_tools)} tools: {[t.name for t in enabled_tools]}")

    return patch_sdk_server(create_tool_server(
        name="ai-tools",
        version="1.0.0",
        tools=enabled_tools,
    ), enabled_tools)
