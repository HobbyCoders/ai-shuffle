"""Shared helpers for builtin tools."""

from __future__ import annotations

from pathlib import Path

from omni.tools.registry import ToolCtx

_TRUNCATE_LIMIT = 30_000  # ~30KB; keep head + tail, drop the middle


def resolve(file_path: str, ctx: ToolCtx) -> Path:
    """Resolve a path against the session cwd (absolute paths pass through)."""
    p = Path(file_path)
    if not p.is_absolute():
        p = Path(ctx.cwd) / p
    return p.resolve()


def truncate(text: str, limit: int = _TRUNCATE_LIMIT) -> str:
    """Head+tail truncation for large tool output (prevents context blowup)."""
    if len(text) <= limit:
        return text
    half = limit // 2
    head, tail = text[:half], text[-half:]
    dropped = len(text) - 2 * half
    return f"{head}\n... [{dropped} bytes truncated] ...\n{tail}"
