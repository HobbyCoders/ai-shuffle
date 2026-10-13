"""Event shapes AI Shuffle still has to decode, but fluid no longer emits.

``SafetyWarning`` was a fluid event until the engine stopped inferring safety
stops. Sessions persisted before that change still hold the encoded shape, so
the codec has to be able to rebuild it or replaying an old session raises. The
class is AI Shuffle's now — fluid knows nothing about it — and it is registered
with ``fluid.events.register_event_type`` at import time.

Anything that decodes stored events (``fluid_pg_store``) must import this
module first; ``harness_adapter`` translates the decoded event to a
``SystemMessage``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fluid.events import register_event_type
from fluid.events.model import Event


@register_event_type
@dataclass(frozen=True, kw_only=True)
class SafetyWarning(Event):
    """Legacy replay shape for turns recorded before safety guards were removed.

    NativeLoop no longer emits this event. Keeping the codec shape lets older
    persisted sessions remain readable after upgrade.
    """

    code: str
    message: str
    hard: bool = False
    counters: dict[str, Any] = field(default_factory=dict)


__all__ = ["SafetyWarning"]
