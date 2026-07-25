"""Local stub upstream for zero-spend provider E2E.

TEST / DEV TOOLING ONLY. Nothing in this package is imported by, registered
with, or reachable from ``app/``. It exists so provider work (translator
gateway, circuit breaker, failover, subagent legs) can be exercised end-to-end
against a fake vendor on loopback instead of a real, metered account.

See ``tools/stub_upstream/README.md`` for the operator recipe.
"""

from __future__ import annotations

__all__ = ["scenarios", "server", "proxy"]
