"""Fixed-size, monotone evidence-loss tracking for learned workflows.

A journal tail can begin at a later user request, not the session's first one.
Remember every session whose lines were discarded in a 64-Kibit Bloom filter:
false positives conservatively refuse learning; no recorded loss is forgotten.
Only hashes survive, never session content, IDs or private paths. Saturation
simply disables workflow learning, never journaling or ordinary synthesis.
"""
from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import os
from pathlib import Path

from fluid.codemode.skills import atomic_write_text, journal_lock, JOURNAL_APPEND_LOCK_WAIT

_BITS = 65536
_ALL = (1 << _BITS) - 1
_FILE = ".workflow-loss.json"
_MAX_BYTES = _BITS // 4 + 128


def _mask(project: str, session: str) -> int:
    digest = hashlib.sha256(json.dumps([project, session]).encode()).digest()
    return sum(1 << position for position in {
        int.from_bytes(digest[i:i + 2], "big") for i in range(0, 8, 2)
    })


def load_loss(root: Path) -> int:
    """Read a bounded loss snapshot; missing/corrupt metadata fails closed."""
    path = root / _FILE
    try:
        if path.stat().st_size > _MAX_BYTES:
            return _ALL
        document = json.loads(path.read_text(encoding="utf-8"))
        encoded = document["bits"]
        if (document.get("version") != 1 or not isinstance(encoded, str)
                or len(encoded) != _BITS // 4):
            return _ALL
        value = int(encoded, 16)
        return value if 0 <= value <= _ALL else _ALL
    except (OSError, ValueError, KeyError, TypeError):
        return _ALL


def session_lost(bits: int, project: str, session: str) -> bool:
    """Return true if this session may have lost any journal evidence."""
    mask = _mask(project, session)
    return bits & mask == mask


def _with_lines(bits: int, lines: Iterable[str]) -> int:
    for line in lines:
        try:
            row = json.loads(line)
            project, session = row.get("project"), row.get("session")
            if isinstance(project, str) and isinstance(session, str):
                bits |= _mask(project, session)
        except (ValueError, AttributeError):
            # A malformed discarded row may have contained a correction.
            return _ALL
    return bits


def _save(root: Path, bits: int) -> None:
    atomic_write_text(root / _FILE, json.dumps({"version": 1, "bits": f"{bits:0{_BITS // 4}x}"}))
    # Persist the rename itself before allowing a journal trim. File fsync
    # alone can leave the previous directory entry after a power loss.
    # Windows cannot open/fsync directories this way; there we retain atomic
    # replacement but cannot promise POSIX directory-fsync crash durability.
    if os.name == "nt":
        return
    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)  # failures propagate: the journal must stay intact
    finally:
        os.close(descriptor)


def initialize_loss(root: Path, journal: Path) -> None:
    """Quarantine unverifiable legacy tails once, serialized with journal writers."""
    if (root / _FILE).exists():
        return
    with journal_lock(journal, wait=JOURNAL_APPEND_LOCK_WAIT) as held:
        if not held or (root / _FILE).exists():
            return
        bits = 0
        if journal.exists():
            with journal.open(encoding="utf-8") as stream:
                bits = _with_lines(bits, stream)
        _save(root, bits)


def record_loss(root: Path, lines: Iterable[str]) -> None:
    """Persist loss BEFORE discarding lines, under the caller's journal lock.

    POSIX file and directory sync failures prevent the trim. A crash after
    successful sync but before trimming creates only safe false positives.
    Windows skips unsupported directory fsync (atomic replacement only).
    Never reset/evict bits automatically.
    """
    bits = load_loss(root)
    updated = _with_lines(bits, lines)
    # Persist even a saturated snapshot: missing/corrupt metadata must never
    # become an apparently clean empty store after the affected tail is gone.
    _save(root, updated)
