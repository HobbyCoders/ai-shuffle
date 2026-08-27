"""Shared retry policy for the native provider clients.

Retries happen inside each client's ``stream``, and only when it is safe:
a non-200 on a retriable status, or a transport failure BEFORE any chunk has
been yielded. Once streaming has started, a failure is surfaced instead of
retried — the engine has already seen (and emitted) part of the message.
"""

from __future__ import annotations

from dataclasses import dataclass

# HTTP statuses worth an automatic retry (rate limit / transient upstream).
# 520-524 are Cloudflare's edge codes: an origin that misbehaved, refused the
# edge, or timed out. Providers fronted by Cloudflare (Z.AI among them) return
# them for blips that clear on the next attempt.
RETRIABLE_STATUSES = frozenset((408, 429, 500, 502, 503, 520, 521, 522, 523, 524, 529))


def coerce_status(value: object) -> int | None:
    """Best-effort HTTP status out of a provider-supplied code.

    Providers spell the code as an int, a numeric string, or a symbolic name
    ("rate_limit_exceeded"); only the first two carry a status.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def is_retriable_status(value: object) -> bool:
    """True when a provider-supplied status/code is worth another attempt.

    Anything unrecognized — a missing code, a symbolic one, an auth or
    validation failure — is treated as fatal, so a bad request never loops.
    """
    status = coerce_status(value)
    return status is not None and status in RETRIABLE_STATUSES


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
