"""Manage the worker-local blocks that prompt assembly actually reads.

Legacy Markdown files match a substring of the model ID. The automatic learner
uses exact-scope records in a separate subdirectory so they cannot consume
Fluid's Markdown slots or match another model by substring.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fluid._filelock import file_lock
from fluid.cli.evolve import guidance_validator
from fluid.codemode.evolution import Proposal
from fluid.context.guidance import GUIDANCE_DIR_MAX_FILES, GUIDANCE_FILE_MAX_CHARS

from app.core.guidance_surface import guidance_directory

_STEM = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)


class GuidanceConflict(ValueError):
    """The target changed or is controlled by an explicit owner action."""


class GuidanceValidationError(ValueError):
    """The proposed content or target is not acceptable."""


@dataclass(frozen=True)
class SavedGuidance:
    id: str
    stem: str
    content: str
    revision: str
    scope: str
    model: str
    provider: str | None
    source: str
    inactive_reason: str | None = None


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _id(path: Path, scoped: bool) -> str:
    return ("s-" if scoped else "m-") + _digest(os.fsencode(path.name))


def _manual_marker(root: Path, stem: str, scoped: bool) -> Path:
    # Case/whitespace aliases in legacy filenames match the same model IDs.
    # Editing/deleting one must also stop automation recreating the alias.
    key = ("s-" if scoped else "m-") + _digest(os.fsencode(stem.strip().lower()))
    return root / ".manual" / key


def _read_regular(path: Path) -> bytes | None:
    try:
        if stat.S_ISREG(path.lstat().st_mode):
            return path.read_bytes()
    except FileNotFoundError:
        pass
    return None


def _marker_bytes(root: Path, stem: str, scoped: bool) -> bytes:
    return _read_regular(_manual_marker(root, stem, scoped)) or b""


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()  # Windows does not allow replace on an open temp file.
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def _write_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / ".manage.lock", wait=2) as held:
        if not held:
            raise OSError("Could not lock the guidance store")
        yield


def _entry(root: Path, path: Path, scoped: bool, *, beyond_cap: bool = False) -> SavedGuidance | None:
    raw = _read_regular(path)
    if raw is None:
        return None
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    provider = None
    model = path.stem.strip().lower()
    inactive_reason = None
    if scoped:
        try:
            record = json.loads(content)
            content, model, provider = (record[k] for k in ("content", "model", "provider"))
            if not all(isinstance(v, str) for v in (content, model, provider)):
                raise ValueError("invalid scoped guidance fields")
        except (TypeError, KeyError, ValueError) as exc:
            raise OSError("Could not read a scoped guidance record") from exc
    elif beyond_cap:
        inactive_reason = f"Outside the first {GUIDANCE_DIR_MAX_FILES} guidance files used by Fluid."
    elif not model or not content.strip():
        inactive_reason = "This file has no model match or content."
    marker = _marker_bytes(root, path.stem, scoped)
    return SavedGuidance(
        id=_id(path, scoped), stem=path.stem, content=content,
        revision=_digest(raw + b"\0" + marker),
        scope="exact" if scoped else "model_substring", model=model,
        provider=provider, source="owner" if marker else ("automatic" if scoped else "file"),
        inactive_reason=inactive_reason,
    )


def _paths(root: Path) -> Iterator[tuple[Path, bool, bool]]:
    for index, path in enumerate(sorted(root.glob("*.md"))):
        yield path, False, index >= GUIDANCE_DIR_MAX_FILES
    for path in sorted((root / "scoped").glob("*.json")):
        yield path, True, False


def list_saved_guidance() -> list[SavedGuidance]:
    root = guidance_directory()
    return [
        block for path, scoped, beyond_cap in _paths(root)
        if (block := _entry(root, path, scoped, beyond_cap=beyond_cap)) is not None
    ]


def _find(root: Path, block_id: str, revision: str) -> tuple[SavedGuidance, Path, bool, bool]:
    for path, scoped, beyond_cap in _paths(root):
        if _id(path, scoped) == block_id:
            block = _entry(root, path, scoped, beyond_cap=beyond_cap)
            if block and block.revision == revision:
                return block, path, scoped, beyond_cap
            break
    raise GuidanceConflict("This block changed or was deleted. Reload saved guidance before retrying.")


def _validated_content(stem: str, content: str) -> str:
    # Use Fluid's content checks, but don't revalidate the legacy filename on
    # edit. Owners must be able to manage existing blocks with older filenames.
    content = content.strip()
    errors = guidance_validator(Proposal(kind="guidance", target=stem, content=content))
    if len(content) > GUIDANCE_FILE_MAX_CHARS or "\0" in content:
        errors.append(f"Use at most {GUIDANCE_FILE_MAX_CHARS} characters and no null characters")
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        errors.append("Guidance must be valid Unicode")
    if errors:
        raise GuidanceValidationError("; ".join(errors))
    return content


def _save(path: Path, content: str, *, model: str | None = None, provider: str | None = None) -> None:
    payload = (content + "\n") if model is None else json.dumps({
        "content": content, "model": model, "provider": provider,
    })
    _atomic_write(path, payload.encode("utf-8"))


def _mark_manual(root: Path, stem: str, scoped: bool) -> None:
    # Write authority before changing the prompt input. If the second disk
    # operation fails, the block is still protected from automatic changes.
    _atomic_write(_manual_marker(root, stem, scoped), uuid.uuid4().bytes)


def edit_saved_guidance(block_id: str, revision: str, content: str) -> SavedGuidance:
    content = _validated_content("manual", content)
    root = guidance_directory()
    with _write_lock(root):
        block, path, scoped, beyond_cap = _find(root, block_id, revision)
        _mark_manual(root, block.stem, scoped)
        _save(path, content, model=block.model if scoped else None, provider=block.provider)
        saved = _entry(root, path, scoped, beyond_cap=beyond_cap)
        assert saved is not None
        return saved


def delete_saved_guidance(block_id: str, revision: str) -> None:
    root = guidance_directory()
    with _write_lock(root):
        block, path, scoped, _ = _find(root, block_id, revision)
        _mark_manual(root, block.stem, scoped)
        path.unlink()


def _target(root: Path, stem: str, scoped: bool) -> Path:
    if not _STEM.fullmatch(stem):
        raise GuidanceValidationError("Invalid guidance target")
    return root / "scoped" / f"{stem}.json" if scoped else root / f"{stem}.md"


def read_saved_guidance(stem: str, *, scoped: bool = False) -> SavedGuidance | None:
    root = guidance_directory()
    path = _target(root, stem, scoped)
    return _entry(root, path, scoped)


def guidance_revision(stem: str, *, scoped: bool = False) -> str | None:
    block = read_saved_guidance(stem, scoped=scoped)
    return block.revision if block else None


def apply_automatic_guidance(
    stem: str, content: str, *, expected_revision: str | None,
    model: str | None = None, provider: str | None = None,
) -> SavedGuidance:
    """Compare and save under the owner-write lock; a manual marker always wins."""
    scoped = model is not None or provider is not None
    if scoped and (not model or not provider):
        raise GuidanceValidationError("Exact guidance needs a model and provider")
    root = guidance_directory()
    path = _target(root, stem, scoped)
    content = _validated_content(stem, content)
    with _write_lock(root):
        if _marker_bytes(root, stem, scoped):
            raise GuidanceConflict("This guidance was edited or deleted by the owner")
        block = _entry(root, path, scoped)
        if (block.revision if block else None) != expected_revision:
            raise GuidanceConflict("Saved guidance changed since the proposal was prepared")
        if block is None and os.path.lexists(path):
            raise GuidanceConflict("A non-guidance file already occupies this target")
        if block is not None and scoped and (block.model, block.provider) != (model, provider):
            raise GuidanceConflict("The proposal does not match the saved model and provider")
        _save(path, content, model=model, provider=provider)
        saved = _entry(root, path, scoped)
        assert saved is not None
        return saved
