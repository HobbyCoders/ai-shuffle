"""Resolve legacy note references used in stored prompts."""
import re

from app.db import database


def resolve_note_references(prompt: str) -> str:
    """Resolve legacy @Notes/note-id references in a prompt.

    New notes use physical file paths that the SDK reads natively.
    This handles legacy virtual references from older messages by
    redirecting to the physical file or inlining the content.
    """
    def replace_note(match):
        note_id = match.group(1)
        note = database.get_note(note_id)
        if note:
            # If the note has a physical file, redirect to it
            if note.get("file_path"):
                from app.core.note_files import get_absolute_file_path
                abs_path = str(get_absolute_file_path(note["file_path"]))
                return f"@{abs_path}"
            # Otherwise inline the content (pre-migration fallback)
            title = note.get("title", "Untitled")
            content = note.get("content", "")
            return f"[Note: {title}]\n{content}"
        return match.group(0)  # Keep original if not found

    return re.sub(r'@Notes/(note-[a-f0-9]+)', replace_note, prompt)
