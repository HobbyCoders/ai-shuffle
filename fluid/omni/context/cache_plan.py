"""Cache planning — the harness's first-class cost lever.

M0 scope: decide how many trailing cache breakpoints the provider serializer
may place (system tail + last stable history position), and enforce the
append-only-prefix rule by construction: the prompt is always
``[system, tools, history-in-node-order]`` and nothing upstream of the newest
node is ever rewritten mid-session.

Later milestones extend this into breakpoint scheduling across tool defs and
pinned notes, plus cache-safe trimming (`trim.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.providers.base import ProviderCaps


@dataclass(frozen=True)
class CachePlan:
    breakpoints: int  # how many cache_control markers the serializer may place


def plan_cache(caps: ProviderCaps, *, has_tools: bool = False) -> CachePlan:
    if not caps.explicit_cache_breakpoints:
        return CachePlan(breakpoints=0)  # automatic caching: stability discipline only
    # Breakpoints, from the most stable prefix position outward:
    #   1. system tail      2. tool-defs tail (M2, only when tools are present)
    #   3. newest history tail
    # Tool defs sit in the stable prefix and never mutate mid-session, so the
    # third breakpoint after them lets history churn without re-billing tools.
    wanted = 3 if has_tools else 2
    return CachePlan(breakpoints=min(wanted, caps.max_cache_breakpoints))
