"""Adapter factory — feature-flagged selection of the provider backend.

Default behavior is UNCHANGED: ``make_adapter()`` returns a
:class:`ClaudeAgentAdapter` (the Claude Agent SDK path) exactly as before.

Only when the ``AIH_USE_OMNI_HARNESS`` env flag is truthy (``1``/``true``/``yes``/``on``)
does it return a :class:`HarnessAdapter` wrapping the omni harness. The
HarnessAdapter is imported lazily so that a deployment without ``omni``
installed (the common case) is never affected by an import error.

Wiring: replace a bare ``ClaudeAgentAdapter()`` construction site with
``make_adapter()``. With the flag off this is byte-for-byte equivalent, so the
change is minimal and reversible.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from app.core.provider.claude_adapter import ClaudeAgentAdapter
from app.core.provider.protocol import ProviderAdapter

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def use_omni_harness(flag: Optional[str] = None) -> bool:
    """Whether the omni-harness backend is enabled via ``AIH_USE_OMNI_HARNESS``."""
    value = flag if flag is not None else os.environ.get("AIH_USE_OMNI_HARNESS", "")
    return value.strip().lower() in _TRUTHY


def make_adapter() -> ProviderAdapter:
    """Return the configured provider adapter.

    Default (flag off): ClaudeAgentAdapter — the existing, unchanged path.
    Flag on: HarnessAdapter (omni). Falls back to ClaudeAgentAdapter if omni
    is not importable, so enabling the flag can never hard-crash startup.
    """
    if not use_omni_harness():
        return ClaudeAgentAdapter()

    try:
        from app.core.provider.harness_adapter import HarnessAdapter
    except Exception as exc:  # omni missing / import error → safe fallback
        logger.error(
            "[factory] AIH_USE_OMNI_HARNESS set but HarnessAdapter unavailable "
            "(%r); falling back to ClaudeAgentAdapter",
            exc,
        )
        return ClaudeAgentAdapter()

    logger.info("[factory] AIH_USE_OMNI_HARNESS enabled — using HarnessAdapter (omni)")
    return HarnessAdapter()
