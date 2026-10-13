"""Adapter factory — the single construction site for the provider backend.

``make_adapter()`` returns a :class:`HarnessAdapter` (the fluid harness) for
every spawn path. There is no feature flag and no fallback: fluid is the only
backend. If the fluid import fails the process fails loudly here: a degraded
session is far harder to diagnose than a startup error.

Wiring: spawn paths construct their adapter via ``make_adapter()`` rather than
naming a concrete class, so a future backend change stays a one-file edit.
"""

from __future__ import annotations

import logging

from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.protocol import ProviderAdapter

logger = logging.getLogger(__name__)


def make_adapter() -> ProviderAdapter:
    """Return the provider adapter: always the fluid :class:`HarnessAdapter`."""
    return HarnessAdapter()
