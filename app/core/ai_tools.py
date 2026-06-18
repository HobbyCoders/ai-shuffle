"""
AI Tools Registry - Single source of truth for all AI tool definitions.

All AI tools are powered by fal.ai through a single API key.
Add new AI tools here and they will automatically be available in:
- Settings API (available tools endpoint)
- Profile configuration (AIToolsConfig model)
- Provider availability checks
"""

from typing import Dict, Any
from pydantic import create_model

# ============================================================================
# AI Tools Definitions
# ============================================================================
# Each tool specifies:
#   - name: Display name
#   - description: What the tool does
#   - category: Grouping (image, video, 3d, audio)
#   - providers: Always ["fal-ai"] — single provider architecture

AI_TOOLS: Dict[str, Dict[str, Any]] = {
    # Image tools
    "image_generation": {
        "name": "Image Generation",
        "description": "Generate AI images from text prompts (supports 1K-4K, multiple images)",
        "category": "image",
        "providers": ["fal-ai"]
    },
    "image_editing": {
        "name": "Image Editing",
        "description": "Edit existing images - add, remove, or modify elements",
        "category": "image",
        "providers": ["fal-ai"]
    },
    "image_reference": {
        "name": "Reference-Based Generation",
        "description": "Generate images with character/style consistency using reference images",
        "category": "image",
        "providers": ["fal-ai"]
    },

    # Video tools
    "video_generation": {
        "name": "Video Generation",
        "description": "Generate AI videos from text prompts (4-12 sec)",
        "category": "video",
        "providers": ["fal-ai"]
    },
    "image_to_video": {
        "name": "Image to Video",
        "description": "Animate a still image into a video",
        "category": "video",
        "providers": ["fal-ai"]
    },
    "video_extend": {
        "name": "Extend Video",
        "description": "Extend an existing video's duration with AI-generated continuation",
        "category": "video",
        "providers": ["fal-ai"]
    },

    # 3D Model tools
    "text_to_3d": {
        "name": "Text to 3D",
        "description": "Generate 3D models from text descriptions",
        "category": "3d",
        "providers": ["fal-ai"]
    },
    "image_to_3d": {
        "name": "Image to 3D",
        "description": "Generate 3D models from reference images",
        "category": "3d",
        "providers": ["fal-ai"]
    },

    # Audio tools
    "tts": {
        "name": "Text to Speech",
        "description": "Convert text to natural-sounding speech",
        "category": "audio",
        "providers": ["fal-ai"]
    },
    "stt": {
        "name": "Speech to Text",
        "description": "Transcribe audio to text with speaker identification",
        "category": "audio",
        "providers": ["fal-ai"]
    },
    "audio_generation": {
        "name": "Audio Generation",
        "description": "Generate music and sound from text prompts (Lyria, Stable Audio, ACE-Step)",
        "category": "audio",
        "providers": ["fal-ai"]
    },
    "audio_to_audio": {
        "name": "Audio to Audio",
        "description": "Transform audio into audio — style transfer, restoration, stems, remix",
        "category": "audio",
        "providers": ["fal-ai"]
    },
}

# ============================================================================
# Provider to API Key Mapping
# ============================================================================

PROVIDER_KEY_MAP: Dict[str, str] = {
    "fal-ai": "fal_api_key",
}

# ============================================================================
# Dynamic Model Generation
# ============================================================================

def _create_ai_tools_config_class():
    """
    Dynamically create the AIToolsConfig Pydantic model from AI_TOOLS.

    This ensures the model always matches the tool definitions - no manual sync needed.
    """
    # Build field definitions: each tool ID becomes a bool field defaulting to False
    fields = {
        tool_id: (bool, False)
        for tool_id in AI_TOOLS.keys()
    }

    # Create the model dynamically
    model = create_model(
        'AIToolsConfig',
        **fields
    )

    # Add docstring
    model.__doc__ = "Configuration for individual AI tools - each tool can be enabled/disabled. Auto-generated from AI_TOOLS registry."

    return model


# Export the dynamically created model
AIToolsConfig = _create_ai_tools_config_class()


# ============================================================================
# Helper Functions
# ============================================================================

def get_tool_ids() -> list[str]:
    """Get list of all tool IDs"""
    return list(AI_TOOLS.keys())


def get_tools_by_category(category: str) -> Dict[str, Dict[str, Any]]:
    """Get all tools in a specific category"""
    return {
        tool_id: tool_info
        for tool_id, tool_info in AI_TOOLS.items()
        if tool_info["category"] == category
    }


def get_categories() -> list[str]:
    """Get list of all unique categories"""
    return list(set(tool["category"] for tool in AI_TOOLS.values()))
