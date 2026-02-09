"""
Note file management utilities.

Notes are stored as physical markdown files on disk so the Claude Agent SDK
can read them natively via file references. The database remains the source
of truth; files are a materialized view kept in sync via CRUD operations.
"""

import re
import unicodedata
import logging
from pathlib import Path
from typing import Optional, Dict, List

from app.core.config import settings

logger = logging.getLogger(__name__)


def get_notes_dir() -> Path:
    """Return the notes directory path, creating it if it doesn't exist."""
    notes_dir = settings.effective_data_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    return notes_dir


def slugify(title: str) -> str:
    """Convert a note title to a filesystem-safe slug.

    - Normalizes unicode to ASCII
    - Lowercases
    - Removes non-word characters (except hyphens)
    - Collapses whitespace and hyphens into single hyphens
    - Strips leading/trailing hyphens
    - Truncates to 80 characters
    """
    value = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode('ascii')
    value = value.lower()
    value = re.sub(r'[^\w\s-]', '', value)
    value = re.sub(r'[-\s]+', '-', value).strip('-')
    return value[:80] if value else 'untitled'


def generate_filename(title: str, note_id: str, existing_filenames: Optional[set] = None) -> str:
    """Generate a unique filename for a note.

    Returns '{slug}.md', or '{slug}-{short_id}.md' if the base name collides
    with an existing filename.
    """
    slug = slugify(title)
    filename = f"{slug}.md"

    if existing_filenames and filename in existing_filenames:
        short_id = note_id.replace("note-", "")[:6]
        filename = f"{slug}-{short_id}.md"

    return filename


def build_file_content(title: str, content: str) -> str:
    """Build the markdown file content for a note."""
    return f"# {title}\n\n{content}"


def write_note_file(filename: str, title: str, content: str) -> Path:
    """Write a note to disk as a markdown file. Returns the absolute file path."""
    notes_dir = get_notes_dir()
    file_path = notes_dir / filename
    file_content = build_file_content(title, content)
    file_path.write_text(file_content, encoding='utf-8')
    return file_path


def delete_note_file(filename: str) -> bool:
    """Delete a note file from disk. Returns True if the file was deleted."""
    notes_dir = get_notes_dir()
    file_path = notes_dir / filename
    if file_path.exists():
        file_path.unlink()
        return True
    return False


def rename_note_file(old_filename: str, new_filename: str, title: str, content: str) -> Path:
    """Rename a note file by writing the new one first, then deleting the old.

    Write-then-delete ordering prevents data loss if write fails.
    """
    new_path = write_note_file(new_filename, title, content)
    if old_filename != new_filename:
        delete_note_file(old_filename)
    return new_path


def get_file_path_for_storage(filename: str) -> str:
    """Return the POSIX-style relative path for storing in the database.

    Stored as 'notes/{filename}' for cross-platform portability.
    """
    return f"notes/{filename}"


def get_absolute_file_path(relative_path: str) -> Path:
    """Convert a stored relative file path to an absolute path."""
    return settings.effective_data_dir / Path(relative_path)


def materialize_all_notes(notes: List[Dict]) -> Dict[str, str]:
    """Write all provided notes to disk as markdown files.

    Used at startup to materialize notes that don't yet have physical files.
    Returns a mapping of {note_id: relative_file_path}.
    """
    results = {}
    existing_filenames: set = set()

    # Also check what files already exist on disk
    notes_dir = get_notes_dir()
    for f in notes_dir.iterdir():
        if f.is_file() and f.suffix == '.md':
            existing_filenames.add(f.name)

    for note in notes:
        try:
            filename = generate_filename(note["title"], note["id"], existing_filenames)
            existing_filenames.add(filename)
            write_note_file(filename, note["title"], note.get("content") or "")
            results[note["id"]] = get_file_path_for_storage(filename)
        except Exception as e:
            logger.warning(f"Failed to materialize note '{note.get('id', '?')}': {e}")

    return results
