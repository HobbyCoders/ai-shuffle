"""Shared retry policy for the native provider clients.

Retries happen inside each client's ``stream``, and only when it is safe:
a non-200 on a retriable status, or a transport failure BEFORE any chunk has
been yielded. Once streaming has started, a failure is surfaced instead of
retried — the engine has already seen (and emitted) part of the message.
"""

from __future__ import annotations

from dataclasses import dataclass

# HTTP statuses worth an automatic retry (rate limit / transient upstream).
RETRIABLE_STATUSES = frozenset((408, 429, 500, 502, 503, 529))


@dataclass(frozen=True, kw_only=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1.0  # seconds; doubles per attempt
    max_delay: float = 30.0

    def delay(self, attempt: int, retry_after: str | None = None) -> float:
        """Backoff for the given 0-based attempt, honoring a Retry-After
        header when the server sent a parseable one."""
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), self.max_delay))
            except ValueError:
                pass
        return delay
