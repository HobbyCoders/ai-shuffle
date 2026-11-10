"""Resolve the @Notes/note-id references a prompt carries on the running worker."""
import re

from app.db import database


def resolve_note_references(prompt: str) -> str:
    """Resolve @Notes/note-id references in a prompt on this worker.

    The chat @ menu inserts these references for a note picked in a chat on
    another worker (#1364): a note's file path is per-worker, so the worker
    that runs the turn redirects the reference to its own copy of the file, or
    inlines the content when it has no file for the note yet.
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
