"""
Streaming utilities for query execution.

Contains functions for processing and truncating SDK event payloads
before sending them to the frontend via WebSocket.
"""

import logging
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# Use centralized constants from config (can be overridden via env vars)
MAX_TOOL_OUTPUT_SIZE = settings.max_tool_output_chars
MAX_DISPLAY_OUTPUT_SIZE = settings.max_display_output_chars
DEFAULT_SDK_BUFFER_SIZE = settings.sdk_buffer_size


def extract_mcp_image(content: Any) -> Optional[str]:
    """
    Extract base64 image data from MCP-style content block lists.

    MCP tools can return content as a list of blocks:
    [{"type": "text", "text": "..."}, {"type": "image", "data": "base64...", "mimeType": "image/jpeg"}]

    Returns the first image's base64 data, or None if no image found.
    """
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "image":
            return block.get("data")
    return None


def truncate_large_payload(content: Any, max_size: int = MAX_TOOL_OUTPUT_SIZE) -> str:
    """
    Intelligently truncate large tool output payloads.

    Handles:
    - Base64 encoded images/files (detected by pattern)
    - Very long text outputs
    - List/dict content that serializes to large strings
    - MCP content block lists (extract text, skip images)

    Args:
        content: The raw content from a ToolResultBlock
        max_size: Maximum allowed size in characters

    Returns:
        Truncated string with informative message if content was too large
    """
    if content is None:
        return ""

    # Handle content block lists: [{"type": "text", "text": "..."}, {"type": "image", ...}]
    # Supports both dict-based (MCP) and Pydantic model-based (SDK) content blocks.
    # Extract text content so downstream consumers get usable strings instead of Python repr
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif hasattr(block, "type") and hasattr(block, "text") and getattr(block, "type") == "text":
                # Pydantic TextBlock objects from the Anthropic SDK
                text_parts.append(getattr(block, "text", ""))
        if text_parts:
            content = "\n".join(text_parts)
        # else: fall through to str() for non-standard lists

    # Convert to string for size checking
    content_str = str(content) if not isinstance(content, str) else content
    content_len = len(content_str)

    # If it's small enough, return as-is (truncated for display)
    if content_len <= max_size:
        return content_str[:MAX_DISPLAY_OUTPUT_SIZE]

    # Check if it looks like base64 image data using quick prefix checks
    # Common patterns: data:image/..., iVBOR (PNG), /9j/ (JPEG), R0lGOD (GIF)
    prefix = content_str[:100]
    is_likely_base64 = (
        'data:image/' in prefix or
        prefix.startswith('iVBOR') or   # PNG base64 signature
        prefix.startswith('/9j/') or    # JPEG base64 signature
        prefix.startswith('R0lGOD') or  # GIF base64 signature
        # For very large content, sample a small window for base64 character ratio
        (content_len > 10000 and sum(c.isalnum() or c in '+/=' for c in prefix) > 95)
    )

    if is_likely_base64:
        # Calculate approximate original size
        original_size_kb = int(content_len * 0.75 / 1024)  # base64 is ~33% larger
        return f"[Image/binary content - {original_size_kb}KB - content processed by Claude but truncated for display]"

    # For other large content, truncate with indicator
    size_kb = content_len // 1024
    truncated = content_str[:MAX_DISPLAY_OUTPUT_SIZE]
    return f"{truncated}\n\n[... truncated - total size: {size_kb}KB]"
