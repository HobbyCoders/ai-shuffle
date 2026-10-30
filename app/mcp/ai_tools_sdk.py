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
from typing import Any, Dict, Optional

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

# Defaults the Node tools fall back to when Settings names no image model
# (tools/image-generation/generateImage.ts, editImage.ts).
_DEFAULT_IMAGE_MODEL = "fal-ai/flux-pro/v1.1-ultra"
_DEFAULT_IMAGE_EDIT_MODEL = "fal-ai/flux-pro/kontext"


def _configured_image_models(tool_env: Optional[Dict[str, str]]) -> tuple:
    """``(generation model, edit model)`` the Node tools will use with no model passed.

    Mirrors the Node resolution order: generation reads IMAGE_MODEL; editing and
    reference generation read IMAGE_EDIT_MODEL, then IMAGE_MODEL.
    """
    env = tool_env or {}
    image_model = env.get("IMAGE_MODEL") or ""
    edit_model = env.get("IMAGE_EDIT_MODEL") or image_model or _DEFAULT_IMAGE_EDIT_MODEL
    return image_model or _DEFAULT_IMAGE_MODEL, edit_model


def _model_override_description(configured: str) -> str:
    # Name the configured model only. Listing example models here made agents
    # pick one (Nano Banana) and tell the owner they were using it (#1796).
    return (f"Optional fal.ai endpoint ID. Omit it to use the configured model ({configured}); "
            "pass one only when the user names a fal.ai model.")


def _create_image_tools(
    worker: NodeWorker,
    tool_env: Optional[Dict[str, str]] = None,
    tool_cwd: Optional[str] = None,
) -> list:
    """Create image generation/editing tool functions.

    The descriptions name the provider these tools run on and the models
    configured in Settings, never a fixed list of other models. A tool whose
    configured model is the ChatGPT subscription runs in this process on that
    sign-in instead of on fal.ai, whatever the chat's own provider (#1796).
    """
    image_model, edit_model = _configured_image_models(tool_env)
    tools = _subscription_image_tools(image_model, edit_model, tool_env, tool_cwd)
    replaced = {t.name for t in tools}

    @tool("generateImage", f"Generate an image from a text prompt with fal.ai, using the configured model {image_model}", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to generate"},
            "model": {"type": "string", "description": _model_override_description(image_model)}
        },
        "required": ["prompt"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def generate_image(args: dict) -> dict:
        result = await worker.call("generateImage", _model_arg(args))
        return _tool_result(result)

    if "generateImage" not in replaced:
        tools.append(generate_image)

    @tool("editImage", f"Edit an existing image (add, remove, or modify elements) with fal.ai, using the configured edit model {edit_model}", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the edit to make"},
            "image_path": {"type": "string", "description": "Path to the image to edit"},
            "model": {"type": "string", "description": _model_override_description(edit_model)}
        },
        "required": ["prompt", "image_path"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def edit_image(args: dict) -> dict:
        result = await worker.call("editImage", _model_arg(args))
        return _tool_result(result)

    if "editImage" not in replaced:
        tools.append(edit_image)

    @tool("generateWithReference", f"Generate an image using reference images for character/style consistency with fal.ai, using the configured edit model {edit_model}", {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to generate"},
            "reference_images": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Paths to reference images (up to 14)"
            },
            "model": {"type": "string", "description": _model_override_description(edit_model)}
        },
        "required": ["prompt", "reference_images"]
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def generate_with_reference(args: dict) -> dict:
        result = await worker.call("generateWithReference", args)
        return _tool_result(result)

    if "generateWithReference" not in replaced:
        tools.append(generate_with_reference)

    return tools


def _subscription_image_tools(
    image_model: str,
    edit_model: str,
    tool_env: Optional[Dict[str, str]],
    tool_cwd: Optional[str],
) -> list:
    """The image tools whose configured model is the ChatGPT subscription.

    Same names and arguments as the fal.ai tools, so every lane (delegated
    Claude, native fluid, voice) sees one image tool set. The handlers resolve
    the stored sign-in in this process; nothing reaches the Node worker.
    """
    from app.core.provider import subscription_images as sub

    tools = []

    def _model_property(configured: str) -> dict:
        return {"type": "string", "description": sub.model_argument_description(configured)}

    if sub.is_subscription_model(image_model):
        @tool("generateImage", sub.describe("Generate an image from a text prompt", image_model), {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text description of the image to generate"},
                "model": _model_property(image_model),
            },
            "required": ["prompt"]
        }, annotations=_LARGE_RESULT_ANNOTATIONS)
        async def generate_image(args: dict) -> dict:
            args = _model_arg(args)
            refused = sub.refusal(args.get("model"), image_model)
            if refused:
                return sub._failure(refused)
            return await sub.run(args.get("prompt"), image_model, cwd=tool_cwd, tool_env=tool_env)

        tools.append(generate_image)

    if sub.is_subscription_model(edit_model):
        @tool("editImage", sub.describe("Edit an existing image (add, remove, or modify elements)", edit_model), {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text description of the edit to make"},
                "image_path": {"type": "string", "description": "Path to the image to edit (PNG, JPEG, WebP or GIF)"},
                "model": _model_property(edit_model),
            },
            "required": ["prompt", "image_path"]
        }, annotations=_LARGE_RESULT_ANNOTATIONS)
        async def edit_image(args: dict) -> dict:
            args = _model_arg(args)
            refused = sub.refusal(args.get("model"), edit_model)
            if refused:
                return sub._failure(refused)
            return await sub.run(args.get("prompt"), edit_model, image_paths=[args.get("image_path")],
                                 cwd=tool_cwd, tool_env=tool_env)

        tools.append(edit_image)

        @tool("generateWithReference", sub.describe(
            "Generate an image using reference images for character/style consistency", edit_model), {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text description of the image to generate"},
                "reference_images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Paths to reference images (up to {sub.MAX_INPUT_IMAGES})"
                },
                "model": _model_property(edit_model),
            },
            "required": ["prompt", "reference_images"]
        }, annotations=_LARGE_RESULT_ANNOTATIONS)
        async def generate_with_reference(args: dict) -> dict:
            args = _model_arg(args)
            refused = sub.refusal(args.get("model"), edit_model)
            if refused:
                return sub._failure(refused)
            refs = args.get("reference_images")
            if not isinstance(refs, list) or not refs:
                return sub._failure("At least one reference image path is required.")
            return await sub.run(args.get("prompt"), edit_model, image_paths=refs,
                                 cwd=tool_cwd, tool_env=tool_env)

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
    """Resolve the fal.ai API key from the encrypted settings store or env.

    None while fal.ai is off (``app.core.fal_credential``): the switch also
    blocks the ``FAL_KEY`` / ``FAL_API_KEY`` environment fallback.
    """
    from app.core import fal_credential

    try:
        if not fal_credential.is_enabled():
            return None
        key = fal_credential.api_key()
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


_RUN_FAL_MODEL_DESCRIPTION = "Run ANY fal.ai model by endpoint ID. Supports 600+ models including Imagen 4, Seedance, Wan 2.2, Lyria (music), Stable Diffusion, and more. Use this when the specific tools (generateImage, generateVideo, etc.) don't support the model you need — including all music/audio generation. If you don't already know the EXACT endpoint_id, call searchModels first to resolve it; do not guess (a single wrong character returns 'Not Found')."


# Mirror of ``inferOutputType`` in tools/providers/fal-generic.ts, which the
# Node worker uses when a runFalModel call names no output_type. Anything none
# of these lists match is run as an image there. Kept in step by
# tests/core/test_subscription_image_tools.py.
_FAL_VIDEO_HINTS = ("video", "wan", "kling", "veo", "sora", "lipsync", "animate", "seedance")
_FAL_3D_HINTS = ("3d", "mesh", "hunyuan-3d")
_FAL_AUDIO_HINTS = ("speech", "tts", "audio", "whisper", "kokoro", "chatterbox", "lyria", "music",
                    "song", "ace-step", "diffrhythm", "stable-audio", "cassetteai", "mmaudio",
                    "elevenlabs", "minimax-music", "sound")

# Not in the Node worker: names that mark an image model, so an explicit
# non-image output_type cannot pass one off while the catalog is not loaded.
_FAL_IMAGE_HINTS = ("image", "imagen", "flux", "nano-banana", "seedream", "recraft", "ideogram",
                    "stable-diffusion", "sdxl", "kontext", "hidream", "dall-e", "midjourney")


def _infer_fal_output_type(endpoint_id: str) -> Optional[str]:
    """The Node worker's output type for ``endpoint_id``; ``None`` where it
    falls back to ``image`` only because nothing matched."""
    lowered = endpoint_id.lower()
    for kind, hints in (("video", _FAL_VIDEO_HINTS), ("3d", _FAL_3D_HINTS), ("audio", _FAL_AUDIO_HINTS)):
        if any(hint in lowered for hint in hints):
            return kind
    return None


def _catalog_output_type(endpoint_id: str) -> Optional[str]:
    """The fal.ai catalog's output type for the endpoint, if the catalog knows it."""
    try:
        from app.services.fal_catalog import catalog_service

        return next((m.output_type for m in catalog_service._cache if m.endpoint_id == endpoint_id), None)
    except Exception:  # noqa: BLE001 - no catalog is the same as an unknown endpoint
        return None


def _fal_image_refusal(args: dict) -> Optional[str]:
    """Why a runFalModel call is refused while images are set to the ChatGPT
    subscription (#1796), or ``None`` when it may run.

    Refused: output_type ``image``; an endpoint the catalog lists as an image
    model (or, absent from the catalog, whose name marks one and none of the
    worker's video, 3D or audio hints), whatever output_type says; and an
    endpoint neither the catalog nor
    the Node worker's detection can place when no output_type is given, since
    the worker would run it as an image. An unplaceable endpoint with an
    explicit video, 3d or audio output_type runs (a known limit: an image model
    neither the catalog nor those names identify can be passed off that way).
    """
    from app.core.provider import subscription_images as sub

    endpoint_id = args.get("endpoint_id")
    endpoint_id = endpoint_id.strip() if isinstance(endpoint_id, str) else ""
    requested = args.get("output_type")
    known = _catalog_output_type(endpoint_id) if endpoint_id else None
    if known is None:
        known = _infer_fal_output_type(endpoint_id) if endpoint_id else None
    if known is None and any(hint in endpoint_id.lower() for hint in _FAL_IMAGE_HINTS):
        known = "image"
    if known is None and requested in (None, ""):
        return (f"'{endpoint_id}' is not a known video, 3D or audio model, and fal.ai would run it as an "
                f"image. Image generation here is set to {sub.PROVIDER_NAME}, so nothing was run. Use "
                "generateImage or editImage for images; for another kind of model, call again with "
                "its output_type (video, 3d or audio).")
    if requested != "image" and known != "image":
        return None
    return ("Images are not generated with fal.ai here: image generation is set to "
            f"{sub.PROVIDER_NAME}. Use generateImage or editImage; nothing was run.")


def _create_generic_tools(worker: NodeWorker, tool_env: Optional[Dict[str, str]] = None) -> list:
    """Create the generic fal.ai model executor tool.

    While image generation is set to the ChatGPT subscription, this tool is
    not a back door to fal.ai image models the owner did not choose (#1796).
    """
    from app.core.provider import subscription_images as sub

    tools = []
    subscription_images = any(sub.is_subscription_model(m) for m in _configured_image_models(tool_env))
    description = _RUN_FAL_MODEL_DESCRIPTION
    if subscription_images:
        description = description.replace("Imagen 4, ", "") + (
            " Not for images: image generation and editing are set to the ChatGPT subscription "
            "here; use generateImage or editImage. For a video, 3D or audio model, pass its output_type.")

    @tool("runFalModel", description, {
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
        refused = _fal_image_refusal(args) if subscription_images else None
        if refused:
            return _tool_result({"success": False, "error": refused})
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
    "searchModels": {"config_key": "generation_catalog"},
}


def _is_tool_enabled(tool_name: str, ai_tools_config: dict) -> bool:
    """Check if a specific tool should be enabled based on config."""
    if tool_name == "searchModels":
        return bool(ai_tools_config.get("generation_catalog")) or any(
            ai_tools_config.get(key, False) for key in TOOL_CONFIG_MAP["runFalModel"]["config_keys"])
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



# Unified entry points replace only generation, catalog and generic execution.
# Transform/edit tools retain their existing names and handlers.
UNIFIED_TOOL_KINDS = {"generateImage": "image", "generateVideo": "video", "generateAudio": "audio", "textTo3D": "3d"}
UNIFIED_TOOL_NAMES = {*UNIFIED_TOOL_KINDS, "searchModels", "runFalModel"}


# How a unified generation call runs (#379):
# - Every call to the four generation tools (image, video, audio, 3D) and
#   runFalModel goes through the resolver's guard: price, confirmation, daily
#   cap. A remembered choice (an account preference, one
#   migrated from a saved Settings default included) runs unasked; one the
#   guard cannot price or run shows the choice card (interactive) or is
#   skipped and reported (unattended). When the account's choices cannot be
#   read, nothing runs unasked either: unattended skips, interactive asks.
# - Main's fal handlers are never a lane for the four generation tools. A
#   saved fal default is migrated into a remembered choice before the run,
#   and an unreadable or unmigrated state must not fall back to an unguarded
#   fal job with no known price, cap or confirmation.
# - Subscription images also use the resolver: Included pricing still needs
#   admission, a deliberate choice and the daily-cap check.
# - runFalModel uses its fal lane only to pin the guarded request to this
#   worker, never to call the raw handler. Unsupported inputs are refused
#   (interactive) or skipped and reported (unattended), not run unpriced.


def _settings_lanes(legacy_tools: dict, tool_env: Optional[Dict[str, str]], fal_usable: bool) -> dict:
    """Pin guarded runFalModel to this worker; never admit raw generation."""
    return {name: ("fal", legacy.handler) for name, legacy in legacy_tools.items()
            if fal_usable and name not in UNIFIED_TOOL_KINDS and name != "searchModels"}


def _compact_generation_option(option):
    """Bound discovery schemas; execution and choice cards retain the full one."""
    import json

    view = {k: v for k, v in option.items() if k not in ("schema", "quick_settings", "options")}
    if len(json.dumps(option.get("options", {}), ensure_ascii=True)) <= 1024:
        view["options"] = option.get("options", {})
    else:
        view["options_omitted"] = True
    schema = option.get("schema")
    if not isinstance(schema, dict):
        view["schema"] = None
        return view
    # Native capabilities use a mapping of option names to allowed values,
    # not fal's list of ParamField records. Preserve that selectable schema.
    if isinstance(schema.get("fields"), dict):
        view["schema"] = schema
        return view
    compact = {"primary_text_field": schema.get("primary_text_field"), "fields": [], "compact": True, "truncated": False}
    truncated = False
    for field in schema.get("fields", []):
        if len(compact["fields"]) >= 32:
            truncated = True
            break
        item = {}
        for key in ("name", "type", "required", "is_file", "is_primary_text", "enum", "default", "minimum", "maximum"):
            value = field.get(key)
            if value is None:
                continue
            if len(json.dumps(value, ensure_ascii=True)) > 512:
                truncated = True
                continue
            item[key] = value
        if len(json.dumps({**compact, "fields": [*compact["fields"], item]}, ensure_ascii=True)) > 4096:
            truncated = True
            break
        compact["fields"].append(item)
    compact["truncated"] = truncated
    view["schema"] = compact
    return view


# The Claude CLI saves an MCP tool result longer than 50,000 characters to a
# file and hands on a ``<persisted-output>`` stub instead (Claude Code 2.1.x,
# unless the tool declares a larger ``anthropic/maxResultSizeChars``). The chat
# draws the choice card from that result, so a ``choose`` result stays below.
CHOOSE_RESULT_CHARS = 40_000


def _bounded_choose(output):
    """A ``choose`` result the chat can still draw as a card on every leg.

    The card reads each option's ``schema.fields`` (name, type, enum, range,
    default); saving and execution re-read the full schema from the catalog.
    So the full JSON ``input_schema`` is never sent, and when the result is
    still too long the field list is compacted as for searchModels.
    """
    import json

    if not isinstance(output, dict) or output.get("status") != "choose" or not isinstance(output.get("options"), list):
        return output

    def size(value):
        return len(json.dumps(value, indent=2, default=_json_default))

    options = []
    for option in output["options"]:
        schema = option.get("schema") if isinstance(option, dict) else None
        if isinstance(schema, dict) and "input_schema" in schema:
            option = {**option, "schema": {k: v for k, v in schema.items() if k != "input_schema"}}
        options.append(option)
    output = {**output, "options": options}
    if size(output) > CHOOSE_RESULT_CHARS:
        output["options"] = [{**o, "schema": _compact_generation_option(o)["schema"]}
                             if isinstance(o, dict) and isinstance(o.get("schema"), dict) else o
                             for o in output["options"]]
    while size(output) > CHOOSE_RESULT_CHARS and len(output["options"]) > 1:
        output["options"].pop()
        output["truncated"] = True
    return output


def _create_unified_tools(worker, context=None, *, lanes=None, tool_env=None):
    from app.core.generation_resolver import (
        Resolver, RunContext, current_context, no_active_turn_result, normalize_source, same_backend_identity,
    )
    from app.services.generation_sources import Sources

    context = context or RunContext()
    # Only runFalModel's local fal lane is used, and never on the chat lane
    # (decided per call, from the current turn).
    all_lanes = dict(lanes or {})
    backend = Sources(worker, context)
    resolver = Resolver(backend)

    async def for_this_call():
        # A pooled client keeps this server across turns: run each call under
        # its session's CURRENT turn (unattended, owner, API user, chat lane
        # and fresh choices), the context the permission callback reads too.
        current = await current_context(context)
        if same_backend_identity(current, context):
            return current, resolver
        return current, Resolver(Sources(worker, current))

    tools = []
    properties = {
        "prompt": {"type": "string"},
        "source": {"type": "string", "description": "fal|openai|xai|chatgpt|supergrok or account ID."},
        "model": {"type": "string", "description": "searchModels ID; never guess."},
        "worker": {"type": "string", "description": "Worker ID from a choice."},
        "options": {"type": "object", "description": "Declared options only."},
        "confirmed": {"type": "boolean", "description": "Only after the user approves."},
        "confirmation_token": {"type": "string", "description": "Receipt for this exact request."},
    }

    from app.core.provider import subscription_images as sub
    configured_image = _configured_image_models(tool_env)[0]
    # Main's #1796 rule: while images are set to the ChatGPT plan, no image
    # call is a back door to fal models the owner did not choose.
    image_restricted = any(sub.is_subscription_model(m) for m in _configured_image_models(tool_env))

    def plan_image_refusal(request):
        if normalize_source(request.get("source")) == "fal":
            return sub.fal_image_refusal()
        # The verified native ChatGPT route calls its provider-default image
        # model "default". Accept the card's exact ID, then let the resolver
        # check account, schema, Included pricing and admission.
        if (request.get("model") not in (None, "", "default")
                and normalize_source(request.get("source")) in (None, "chatgpt")):
            return sub.refusal(request["model"], configured_image)
        return None

    def generation_tool(name, kind):
        legacy = {"video": {"duration": {"type": "integer"}, "aspect_ratio": {"type": "string"}},
                  "audio": {"duration": {"type": "number"}},
                  "3d": {"art_style": {"type": "string"}, "topology": {"type": "string"},
                         "target_polycount": {"type": "integer"}, "wait_for_completion": {"type": "boolean"}}}.get(kind, {})
        description = (f"Generate {kind}. No source/model: the user's saved choice, else they pick; never pick for them. "
                       "IDs/schema: searchModels. Unknown prices never run (unattended skips, interactive asks); "
                       "above-threshold spend needs an approved retry.")
        @tool(name, description, {
            "type": "object", "properties": {**properties, **legacy}, "required": ["prompt"], "additionalProperties": False,
        }, annotations=_LARGE_RESULT_ANNOTATIONS)
        async def generate(args):
            from app.core.generation_resolver import result
            request = _model_arg(args)
            restricted = kind == "image" and image_restricted
            if restricted:
                refused = plan_image_refusal(request)
                if refused:
                    return _tool_result(result("refused", reason="image_provider_restricted", error=refused))
            options = request.get("options", {})
            if not isinstance(options, dict):
                return _tool_result(result("refused", error="options must be an object."))
            options = dict(options)
            for key in legacy:
                if key in request:
                    if key in options and options[key] != request[key]:
                        return _tool_result(result("refused", error="Conflicting top-level and declared model options."))
                    options[key] = request.pop(key)
            request["options"] = options
            current, call_resolver = await for_this_call()
            output = await call_resolver.generate(kind, request, current,
                                                  exclude_sources=("fal",) if restricted else ())
            return _tool_result(_bounded_choose(output))
        return generate

    tools.extend(generation_tool(name, kind) for name, kind in UNIFIED_TOOL_KINDS.items())

    @tool("searchModels", "Look up accounts, exact model IDs, workers, prices and compact declared fields. Use full_schema=true for complete schemas. Unknown prices are never free.", {
        "type": "object", "properties": {
            "query": {"type": "string"}, "source": {"type": "string"}, "model": {"type": "string"},
            "output_type": {"type": "string", "enum": ["image", "video", "audio", "3d"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "full_schema": {"type": "boolean", "default": False},
        }, "additionalProperties": False,
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def search_models(args):
        import asyncio
        from app.core.generation_resolver import KINDS, result
        current, call_resolver = await for_this_call()
        if not current.active_turn:
            return _tool_result(no_active_turn_result())
        limit = max(1, min(args.get("limit") or 15, 50))
        query, model = args.get("query") or "", args.get("model")
        kinds = [args["output_type"]] if args.get("output_type") else list(KINDS)
        source = normalize_source(args.get("source"))
        # Quote only the top few fal models per kind: every quote is a schema
        # and pricing fetch. The full fal catalog search follows, unpriced.
        catalogs = await asyncio.gather(*(call_resolver.catalog(kind, source, model, query=query or None,
                                                           limit=3 if len(kinds) == 1 else 1) for kind in kinds),
                                        return_exceptions=True)
        failed = next((c for c in catalogs if isinstance(c, BaseException)), None)
        if isinstance(failed, asyncio.CancelledError):
            raise failed
        # A failed priced lookup never costs main's fal discovery below.
        options = [o for catalog in catalogs if isinstance(catalog, dict) for o in catalog.get("options", [])]
        # fal entries already matched the catalog's full-text search.
        needle = query.casefold()
        options = [o for o in options if o["source"] == "fal" or not needle
                   or needle in (o["source"] + " " + o["account"] + " " + o["model"]).casefold()]
        # A single quote has a real price; do not mask it with result()'s
        # unknown-price envelope default. Multiple choices have no single price.
        output = result("catalog", price=options[0]["price"] if len(options) == 1 else None,
                        options=options[:limit], total_matches=len(options))
        if failed is not None:
            output["priced_lookup_error"] = "Some accounts or workers could not be listed."
        if source in (None, "fal") and (query or not model) and await call_resolver.backend.fal_key():
            # Main's discovery, unchanged: every category, description and tag
            # matches, endpoint_id fields. Price one with source "fal" + model.
            fal = await _search_catalog(query, args.get("output_type"), limit)
            output.update(fal_models=fal["models"], fal_total_matches=fal["total_matches"],
                          hint="Quote a fal model with source \"fal\" and its endpoint_id as model, or run it with runFalModel.")
        if not args.get("full_schema", False):
            import json
            output["options"] = [_compact_generation_option(o) for o in output["options"]]
            output["schema_hint"] = "Use full_schema=true for complete schemas and defaults."
            # Bound the complete default result too, including fal discovery.
            # Preserve original match counts and explicitly signal omissions.
            while len(json.dumps(output, indent=2, default=_json_default)) > 32768:
                key = "fal_models" if output.get("fal_models") else "options"
                if not output.get(key):
                    break
                output[key].pop()
                output["truncated"] = True
        return _tool_result(output)

    tools.append(search_models)

    @tool("runFalModel", "Run a fal model by endpoint ID through price, confirmation and daily-cap checks. Unknown prices never run; unattended calls need a stored choice or skip and report. A declared text prompt is required. Prefer the kind's tool; get IDs/schema via searchModels.", {
        "type": "object", "properties": {
            "endpoint_id": {"type": "string"}, "input": {"type": "object"},
            "output_type": {"type": "string", "enum": ["image", "video", "audio", "3d"]},
            "confirmed": properties["confirmed"], "confirmation_token": properties["confirmation_token"],
        }, "required": ["endpoint_id", "input"], "additionalProperties": False,
    }, annotations=_LARGE_RESULT_ANNOTATIONS)
    async def run_fal(args):
        from app.core.generation_resolver import KINDS, result
        from app.services.fal_schema import schema_service
        # Main's #1796 rule: not a back door to fal images the owner did not choose.
        refused = _fal_image_refusal(args) if image_restricted else None
        if refused:
            return _tool_result({"success": False, "error": refused})
        current, call_resolver = await for_this_call()
        if not current.active_turn:
            return _tool_result(no_active_turn_result())
        endpoint = args.get("endpoint_id")
        kind = args.get("output_type") or (
            (_catalog_output_type(endpoint) or _infer_fal_output_type(endpoint)) if isinstance(endpoint, str) else None)
        schema = await schema_service.get_schema(endpoint, await call_resolver.backend.fal_key()) if isinstance(endpoint, str) else None
        values = dict(args["input"]) if isinstance(args.get("input"), dict) else None
        prompt = values.pop(schema.primary_text_field, None) if schema and values is not None and schema.primary_text_field else None
        if kind not in KINDS or schema is None or values is None or not isinstance(prompt, str) or not prompt.strip():
            message = "A declared input schema with a text prompt and a known output type is required. Nothing was submitted."
            return _tool_result(result("skipped" if current.unattended else "refused",
                                       reason="invalid_request", error=message, message=message))
        request = {"prompt": prompt, "source": "fal", "model": endpoint, "options": values,
                   "confirmed": args.get("confirmed"), "confirmation_token": args.get("confirmation_token")}
        local_lane = bool(all_lanes.get("runFalModel")) and not current.chat_mode
        if local_lane and not current.unattended and not (current.job_choice or current.remembered_for(kind)):
            request["worker"] = backend.local_worker()
        output = await call_resolver.generate(kind, request, current)
        if current.unattended and output.get("status") == "refused":
            output.update(status="skipped", message=output.get("error", "Generation skipped; nothing was submitted."))
        return _tool_result(_bounded_choose(output))

    tools.append(run_fal)
    return tools


# ============================================================================
# Server Creation
# ============================================================================

def create_ai_tools_server(
    ai_tools_config: dict,
    tools_dir: str,
    tool_env: Optional[Dict[str, str]] = None,
    tool_cwd: Optional[str] = None,
    generation_context=None,
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
    any_generic = any_image or any_video or any_3d or any_audio_gen or ai_tools_config.get("generation_catalog", False)

    if not any_generic:
        return None

    # Normalize tools_dir path (remove trailing separators)
    tools_dir = tools_dir.rstrip("/\\")

    # The worker inherits the host environment. While fal.ai is off, blank any
    # fal key it would find there so a tool call cannot spend it.
    from app.core import fal_credential

    try:
        fal_off = not fal_credential.is_enabled()
    except Exception:  # noqa: BLE001 - settings store optional in some contexts
        fal_off = False
    if fal_off:
        tool_env = {**(tool_env or {}), **{name: "" for name in fal_credential.ENV_KEYS}}

    # Create persistent Node.js worker (started lazily on first tool call)
    worker_script = f"{tools_dir}/dist/worker.js"
    worker = NodeWorker(
        worker_script=worker_script,
        env=tool_env,
        cwd=tool_cwd,
    )

    # Create all tool functions with the worker baked in via closure
    all_image_tools = _create_image_tools(worker, tool_env, tool_cwd)
    all_video_tools = _create_video_tools(worker)
    all_3d_tools = _create_3d_tools(worker)
    all_audio_gen_tools = _create_audio_gen_tools(worker)
    all_generic_tools = _create_generic_tools(worker, tool_env)
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

    legacy_tools = {t.name: t for t in enabled_tools if t.name in UNIFIED_TOOL_NAMES}
    enabled_tools = [t for t in enabled_tools if t.name not in UNIFIED_TOOL_NAMES]
    fal_usable = not fal_off and bool(_get_fal_api_key() or any(
        (tool_env or {}).get(name) for name in fal_credential.ENV_KEYS))
    lanes = _settings_lanes(legacy_tools, tool_env, fal_usable)
    enabled_tools.extend(t for t in _create_unified_tools(worker, generation_context, lanes=lanes, tool_env=tool_env)
                         if _is_tool_enabled(t.name, ai_tools_config))
    if not enabled_tools:
        return None

    logger.info(f"Creating AI tools MCP server with {len(enabled_tools)} tools: {[t.name for t in enabled_tools]}")

    return patch_sdk_server(create_tool_server(
        name="ai-tools",
        version="1.0.0",
        tools=enabled_tools,
    ), enabled_tools)
