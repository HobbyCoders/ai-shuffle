"""The Chat attachment reader shared by native and delegated harnesses."""
import base64
import io
from itertools import islice
from pathlib import Path

from app.core.confined_files import open_attachment
from app.core.session_modes import chat_uploads_dir
from fluid.tools.builtins.read import IMAGE_MEDIA_TYPES, MAX_IMAGE_BYTES, DEFAULT_LINE_LIMIT, MAX_LINE_CHARS
from fluid.tools.registry import ToolOutcome, ToolImage

DELEGATED_READ = "mcp__attachments__Read"


async def read_attachment(arguments, ctx):
    path = Path(arguments["file_path"])
    if not path.is_absolute():
        path = chat_uploads_dir() / path
    try:
        with open_attachment(chat_uploads_dir(), path) as stream:
            media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
            if media_type:
                data = stream.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    return ToolOutcome(text="Image exceeds the attachment image limit.", is_error=True)
                return ToolOutcome(text=f"[image: {path}, {media_type}, {len(data)} bytes]",
                    images=(ToolImage(media_type=media_type, data=base64.b64encode(data).decode('ascii')),))
            sample = stream.read(8192)
            if b'\x00' in sample:
                return ToolOutcome(text="Read shows text and images only.", is_error=True)
            stream.seek(0)
            offset = max(1, int(arguments.get('offset', 1)))
            limit = min(DEFAULT_LINE_LIMIT, max(1, int(arguments.get('limit') or DEFAULT_LINE_LIMIT)))
            lines = io.TextIOWrapper(stream, encoding='utf-8', errors='replace')
            result = []
            for number, line in enumerate(islice(lines, offset - 1, offset - 1 + limit), offset):
                line = line.rstrip('\r\n')
                if len(line) > MAX_LINE_CHARS:
                    line = line[:MAX_LINE_CHARS] + '… [line truncated]'
                result.append(f'{number}\t{line}')
            if next(lines, None) is not None:
                result.append(f'... [output limited to {limit} lines — continue with offset={offset + limit}]')
            ctx.read_files.add(str(path))
            return ToolOutcome(text='\n'.join(result))
    except (OSError, ValueError, RuntimeError):
        return ToolOutcome(text="Attachment unavailable: only regular, singly linked files inside the uploads directory can be read.",
                           is_error=True)
