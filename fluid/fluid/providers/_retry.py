"""Shared retry policy for the native provider clients.

Retries happen inside each client's ``stream``, and only when it is safe:
a non-200 on a retriable status, or a transport failure BEFORE any chunk has
been yielded. Once streaming has started, a failure is surfaced instead of
retried — the engine has already seen (and emitted) part of the message.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait per a ``Retry-After`` header, or None when unparseable.

    The header carries either a delay in seconds or an HTTP-date; both forms
    are in the wild. A date in the past means "now".
    """
    if not value:
        return None
    text = value.strip()
    try:
        return max(float(text), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


@dataclass(frozen=True, kw_only=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1.0  # seconds; doubles per attempt
    max_delay: float = 30.0
    # Retries after a transport failure (connect reset, read timeout) before
    # any byte of the response arrived. None means the same budget as
    # ``max_retries``. A metered provider that may have started a generation
    # sets this to 0: a status code says the request was refused, a dropped
    # socket does not.
    max_transport_retries: int | None = None
    # Fraction of the computed delay added at random, so clients that were
    # refused together do not come back together.
    jitter: float = 0.25

    @property
    def transport_retries(self) -> int:
        if self.max_transport_retries is None:
            return self.max_retries
        return self.max_transport_retries

    def delay(self, attempt: int, retry_after: str | None = None) -> float:
        """Backoff for the given 0-based attempt, honoring a Retry-After
        header when the server sent a parseable one."""
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        wait = parse_retry_after(retry_after)
        if wait is not None:
            delay = max(delay, min(wait, self.max_delay))
        if self.jitter > 0 and delay > 0:
            delay += random.uniform(0.0, delay * self.jitter)
        return delay
