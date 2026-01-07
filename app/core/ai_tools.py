"""
AI Tools Registry - Single source of truth for all AI tool definitions.

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
#   - category: Grouping (image, video, 3d, etc.)
#   - providers: List of providers that can handle this tool

AI_TOOLS: Dict[str, Dict[str, Any]] = {
    # Image tools
    "image_generation": {
        "name": "Image Generation",
        "description": "Generate AI images from text prompts (supports 1K-4K, multiple images)",
        "category": "image",
        "providers": ["google-gemini", "google-imagen", "openai-gpt-image"]
    },
    "image_editing": {
        "name": "Image Editing",
        "description": "Edit existing images - add, remove, or modify elements",
        "category": "image",
        "providers": ["google-gemini", "openai-gpt-image"]
    },
    "image_reference": {
        "name": "Reference-Based Generation",
        "description": "Generate images with character/style consistency using reference images",
        "category": "image",
        "providers": ["google-gemini"]
    },

    # Video tools
    "video_generation": {
        "name": "Video Generation",
        "description": "Generate AI videos from text prompts (4-12 sec)",
        "category": "video",
        "providers": ["google-veo", "openai-sora"]
    },
    "video_with_audio": {
        "name": "Video with Audio",
        "description": "Generate video with native audio (dialogue, effects, music) using Veo 3",
        "category": "video",
        "providers": ["google-veo"]
    },
    "image_to_video": {
        "name": "Image to Video",
        "description": "Animate a still image into a video",
        "category": "video",
        "providers": ["google-veo", "openai-sora"]
    },
    "video_extend": {
        "name": "Video Extension",
        "description": "Extend existing Veo videos by ~7 seconds",
        "category": "video",
        "providers": ["google-veo"]
    },
    "video_bridge": {
        "name": "Frame Bridging",
        "description": "Generate smooth transitions between two images",
        "category": "video",
        "providers": ["google-veo"]
    },
    "video_understanding": {
        "name": "Video Understanding",
        "description": "Analyze videos up to 2 hours and answer questions about content",
        "category": "video",
        "providers": ["google-gemini-video"]
    },

    # 3D Model tools (Meshy)
    "text_to_3d": {
        "name": "Text to 3D",
        "description": "Generate 3D models from text descriptions",
        "category": "3d",
        "providers": ["meshy"]
    },
    "image_to_3d": {
        "name": "Image to 3D",
        "description": "Generate 3D models from reference images",
        "category": "3d",
        "providers": ["meshy"]
    },
    "retexture_3d": {
        "name": "3D Retexturing",
        "description": "Apply new textures to existing 3D models",
        "category": "3d",
        "providers": ["meshy"]
    },
    "rig_3d": {
        "name": "3D Rigging",
        "description": "Add animation skeleton to 3D models",
        "category": "3d",
        "providers": ["meshy"]
    },
    "animate_3d": {
        "name": "3D Animation",
        "description": "Animate rigged 3D models with preset animations",
        "category": "3d",
        "providers": ["meshy"]
    },
    # AI Collaboration tools (multi-model workflows)
    "ai_collaborate": {
        "name": "AI Collaboration",
        "description": "Collaborate with other AI models for specialized tasks (review, plan, summarize)",
        "category": "collaboration",
        "providers": ["openai", "google", "deepseek"]
    },
    "ai_memory": {
        "name": "AI Memory",
        "description": "Long-term memory using Gemini's 1M context - store and recall information across sessions",
        "category": "collaboration",
        "providers": ["google"]
    },
    "ai_review": {
        "name": "AI Review",
        "description": "Get second opinions on code, plans, or content from another AI model",
        "category": "collaboration",
        "providers": ["openai", "google", "deepseek"]
    },
    "ai_reason": {
        "name": "AI Reasoning",
        "description": "Complex logical reasoning using DeepSeek-R1 or OpenAI o1 models",
        "category": "collaboration",
        "providers": ["deepseek", "openai"]
    },
}

# ============================================================================
# Provider to API Key Mapping
# ============================================================================

PROVIDER_KEY_MAP: Dict[str, str] = {
    "google-gemini": "image_api_key",
    "google-imagen": "image_api_key",
    "openai-gpt-image": "openai_api_key",
    "google-veo": "image_api_key",
    "openai-sora": "openai_api_key",
    "google-gemini-video": "image_api_key",
    "meshy": "meshy_api_key",
    # Collaboration providers
    "openai": "openai_api_key",
    "google": "image_api_key",
    "deepseek": "deepseek_api_key",
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
