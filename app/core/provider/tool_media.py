"""Keep tool image blocks across Fluid's live and persisted transcript adapters."""
from typing import Any

MAX_IMAGE_BASE64_CHARS = 16 * 1024 * 1024


def tool_result_content(output: Any, images=(), ui=None):
    """Add supplied image bytes to display content without reading paths or URLs.

    Native results carry ToolImage objects (dicts after durable event replay);
    delegated results keep their SDK/MCP content list in ``ui``. Text-only
    results retain their original shape for existing clients.
    """
    candidates = list(images or ())
    raw = ui.get('content') if isinstance(ui, dict) else ui
    if isinstance(raw, list):
        candidates.extend(block for block in raw if isinstance(block, dict) and block.get('type') == 'image')
    blocks = list(output) if isinstance(output, list) else ([{'type': 'text', 'text': str(output)}] if output else [])
    seen = set()
    for block in blocks:
        if isinstance(block, dict) and block.get('type') == 'image':
            source = block.get('source') or block
            if not isinstance(source, dict):
                continue
            seen.add((source.get('media_type') or source.get('mimeType') or 'image/png', source.get('data')))
    added = False
    for image in candidates:
        if isinstance(image, dict):
            source = image.get('source') or image
            if not isinstance(source, dict):
                continue
            data = source.get('data')
            mime = source.get('media_type') or source.get('mimeType') or 'image/png'
        else:
            data = getattr(image, 'data', None)
            mime = getattr(image, 'media_type', None) or 'image/png'
        if not isinstance(data, str) or not data or not isinstance(mime, str) or not mime.startswith('image/'):
            continue
        if len(data) > MAX_IMAGE_BASE64_CHARS:
            continue
        if (mime, data) in seen:
            continue
        seen.add((mime, data))
        blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': data}})
        added = True
    return blocks if added else output
