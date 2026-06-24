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
    Extract the first image from a tool-result content block list as a data URI.

    Image tool results (e.g. the Claude-in-Chrome browser tool / computer-use)
    arrive in one of two block shapes, depending on the path:

    1. Anthropic API format (what the Agent SDK actually delivers at runtime and
       what is written to the JSONL transcript)::

           {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": "<base64>"}}

    2. MCP wire format (raw MCP server output, kept as a fallback)::

           {"type": "image", "data": "<base64>", "mimeType": "image/png"}

    Blocks may be plain dicts (parsed JSONL / MCP wire) or Pydantic content
    objects (SDK ImageContent), so both forms are handled for each shape.

    Returns a ready-to-render ``data:<mime>;base64,<data>`` URI for the first
    image block, or None if the content has no image. The mime type defaults to
    ``image/png`` when the block omits it.
    """
    if not isinstance(content, list):
        return None
    for block in content:
        # dict-based blocks (Anthropic API format / MCP wire / parsed JSONL)
        if isinstance(block, dict):
            if block.get("type") != "image":
                continue
            # Anthropic API format: data nested under "source".
            source = block.get("source")
            if isinstance(source, dict) and source.get("data"):
                mime = source.get("media_type") or source.get("mimeType") or "image/png"
                return f"data:{mime};base64,{source['data']}"
            # MCP wire format: flat "data" on the block.
            if block.get("data"):
                mime = block.get("mimeType") or block.get("mime_type") or "image/png"
                return f"data:{mime};base64,{block['data']}"
        # Pydantic content-block objects (e.g. ImageContent from the SDK)
        elif getattr(block, "type", None) == "image":
            # Anthropic API format: data nested under ``.source``.
            source = getattr(block, "source", None)
            if source is not None:
                data = (
                    source.get("data") if isinstance(source, dict)
                    else getattr(source, "data", None)
                )
                if data:
                    mime = (
                        source.get("media_type") if isinstance(source, dict)
                        else getattr(source, "media_type", None)
                    ) or "image/png"
                    return f"data:{mime};base64,{data}"
            # MCP wire format: flat ``.data``.
            data = getattr(block, "data", None)
            if data:
                mime = getattr(block, "mimeType", None) or "image/png"
                return f"data:{mime};base64,{data}"
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
