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

from loom.providers.base import ProviderCaps


@dataclass(frozen=True)
class CachePlan:
    breakpoints: int  # how many cache_control markers the serializer may place


def plan_cache(caps: ProviderCaps) -> CachePlan:
    if not caps.explicit_cache_breakpoints:
        return CachePlan(breakpoints=0)  # automatic caching: stability discipline only
    # Reserve two of the provider's breakpoints for now: system tail + newest
    # history tail. Tool-def and notes breakpoints arrive with M2.
    return CachePlan(breakpoints=min(2, caps.max_cache_breakpoints))
