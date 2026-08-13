"""Adapter factory — the single construction site for the provider backend.

``make_adapter()`` returns a :class:`HarnessAdapter` (the omni harness) for
every spawn path. There is no feature flag and no fallback: omni is the only
backend. If the omni import fails the process must fail loudly here rather
than silently degrading to the legacy Claude Agent SDK adapter — a degraded
session is far harder to diagnose than a startup error.

:class:`ClaudeAgentAdapter` remains importable and constructible for tests and
for code that still references it directly; it is simply never selected here.

Wiring: spawn paths construct their adapter via ``make_adapter()`` rather than
naming a concrete class, so a future backend change stays a one-file edit.
"""

from __future__ import annotations

import logging

from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.protocol import ProviderAdapter

logger = logging.getLogger(__name__)


def make_adapter() -> ProviderAdapter:
    """Return the provider adapter: always the omni :class:`HarnessAdapter`."""
    return HarnessAdapter()
