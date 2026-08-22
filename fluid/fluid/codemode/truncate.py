"""Result-size policy for code-mode.

A tool result whose serialized text exceeds ``DEFAULT_LIMIT`` bytes should not
be poured into the model's context in full. The kernel keeps the full payload
alive in its persistent namespace behind a ``ResultRef`` handle (see
``vfs.ResultRef`` — a ``str`` subclass with a truncated ``repr``), so the model
slices/parses it in a later ``run_python`` with no further host round-trip and
no token blowup.

This module is the tiny, dependency-free policy shared by host and kernel: it
decides *when* a result is large and wraps it, taking the ``ResultRef`` class by
injection to avoid an import cycle (``vfs`` is a top-level module inside the
kernel but ``fluid.codemode.vfs`` on the host).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

DEFAULT_LIMIT = 8192  # bytes; results larger than this ride behind a ResultRef


def exceeds(text: str, limit: int = DEFAULT_LIMIT) -> bool:
    """True when ``text``'s UTF-8 size is over ``limit``."""
    return len(text.encode("utf-8", "replace")) > limit


def to_result_ref(
    text: str, ref_cls: Callable[[str], Any], *, limit: int = DEFAULT_LIMIT
) -> Any:
    """Return ``ref_cls(text)`` when ``text`` is large, else ``text`` unchanged.

    ``ref_cls`` is ``vfs.ResultRef``, injected so this module needs no import of
    ``vfs`` (whose import path differs between host and kernel).
    """
    return ref_cls(text) if exceeds(text, limit) else text
