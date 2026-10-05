"""Shared retry policy for the native provider clients.

Retries happen inside each client's ``stream``, and only when it is safe:
a non-200 on a retriable status, or a transport failure BEFORE any chunk has
been yielded. Once streaming has started, a failure is surfaced instead of
retried — the engine has already seen (and emitted) part of the message.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# HTTP statuses worth an automatic retry (rate limit / transient upstream).
# 504 is a gateway timeout (and Anthropic's ``timeout_error``), the same blip
# as Cloudflare's 524. 520-524 are Cloudflare's edge codes: an origin that
# misbehaved, refused the edge, or timed out. Providers fronted by Cloudflare
# (Z.AI among them) return them for blips that clear on the next attempt.
RETRIABLE_STATUSES = frozenset((408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529))

# A refusal about money, not load: the account is out of credit or over a hard
# spend limit. OpenAI sends ``insufficient_quota`` with HTTP 429, the status of
# a rate limit, but no wait clears it, so it is never retried and is reported
# as 402 (Payment Required). A host then does not tell the user to wait for a
# usage window to reset.
BILLING_STATUS = 402
BILLING_ERROR_CODES = frozenset((
    "insufficient_quota",          # OpenAI, with HTTP 429
    "billing_hard_limit_reached",  # OpenAI
    "billing_not_active",          # OpenAI
    "insufficient_credits",        # symbolic spelling; OpenRouter sends ``code: 402``
    "billing_error",               # Anthropic error type
))

# A usage limit, not a rate limit: a subscription's session, 5-hour, weekly or
# plan allowance is spent ("You've hit your session limit · resets 11:10pm",
# Codex's ``usage_limit_reached``). It arrives as HTTP 429 like a per-minute
# rate limit, but no backoff clears it, so it is never retried: the turn stops
# and the user sees the limit. A per-minute 429 ("Rate limit reached ... try
# again in 1.8s", a Retry-After of seconds) is still a blip and retried.
#
# Told apart by, in order: an error code naming a usage limit; a wait the
# provider asks for (Retry-After header or ``resets_in_seconds``) longer than
# USAGE_LIMIT_MIN_WAIT_SECONDS; the provider's own words.
USAGE_LIMIT_ERROR_CODES = frozenset((
    "usage_limit_reached",  # ChatGPT/Codex backend
    "usage_not_included",   # ChatGPT/Codex: the plan has no allowance at all
))
#: A 429 that asks for a longer wait than this is a spent allowance, not a
#: blip. Matches the ceiling task re-dispatch already treats as "not a retry".
USAGE_LIMIT_MIN_WAIT_SECONDS = 600.0
_USAGE_LIMIT_TEXT = re.compile(
    r"usage[ _-]limit"
    r"|session limit"
    r"|weekly limit"
    r"|(?:5|five)[ -]hour limit"
    # "You've hit your limit", "... your session limit", never "your rate
    # limit" or "your request limit for this minute" (a per-minute blip).
    r"|hit your (?!(?:rate|request|requests|token|tokens)[ -])(?:[a-z0-9-]+ )?limit"
    r"|limit reached\s*\|\s*\d{9,13}",
    re.IGNORECASE,
)


def _error_objects(body: object) -> list[dict]:
    if isinstance(body, (str, bytes)):
        text = body.decode(errors="replace") if isinstance(body, bytes) else body
        if "{" not in text:
            return []
        try:
            body = json.loads(text.strip())
        except ValueError:
            return []
    if not isinstance(body, dict):
        return []
    out = [body]
    if isinstance(body.get("error"), dict):
        out.insert(0, body["error"])
    return out


def is_usage_limit_text(text: object) -> bool:
    """True when an error's own words say a usage allowance is spent."""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return isinstance(text, str) and bool(_USAGE_LIMIT_TEXT.search(text))


def is_usage_limit(
    status: object, body: object = None, retry_after: str | None = None,
) -> bool:
    """True when a provider refusal is a usage limit (never retried).

    ``status`` is the HTTP status or in-stream code (None when the error gave
    none: an in-stream ``usage_limit_reached`` still counts). ``body`` is the
    response body, its text, or an error object; ``retry_after`` the header.
    A status other than 429 is never a usage limit.
    """
    code = coerce_status(status)
    if code is not None and code != 429:
        return False
    objects = _error_objects(body)
    for obj in objects:
        for key in ("code", "type"):
            value = obj.get(key)
            if isinstance(value, str) and value in USAGE_LIMIT_ERROR_CODES:
                return True
    if isinstance(status, str) and status in USAGE_LIMIT_ERROR_CODES:
        return True
    if code is None:
        return False
    wait = parse_retry_after(retry_after)
    if wait is not None and wait > USAGE_LIMIT_MIN_WAIT_SECONDS:
        return True
    for obj in objects:
        resets_in = obj.get("resets_in_seconds")
        if (
            isinstance(resets_in, (int, float)) and not isinstance(resets_in, bool)
            and resets_in > USAGE_LIMIT_MIN_WAIT_SECONDS
        ):
            return True
        if is_usage_limit_text(obj.get("message")) or is_usage_limit_text(obj.get("detail")):
            return True
    if isinstance(body, (str, bytes)) and not objects:
        return is_usage_limit_text(body)
    return False


# Symbolic in-stream error codes whose HTTP twin is unambiguous (OpenRouter
# and OpenAI-compatible servers spell some codes as names, not numbers).
SYMBOLIC_CODE_STATUS = {
    "rate_limit_exceeded": 429,
    "server_error": 500,
    "server_is_overloaded": 503,
    **dict.fromkeys(BILLING_ERROR_CODES, BILLING_STATUS),
}


def billing_error_code(body: object) -> str | None:
    """The billing code in a provider error body or error object, else None.

    ``body`` is a response body (text or parsed JSON) or an error object; the
    code may sit in ``error.code``, ``error.type`` or at the top level.
    """
    if isinstance(body, (str, bytes)):
        text = body.decode(errors="replace") if isinstance(body, bytes) else body
        if "{" not in text:
            return None
        try:
            body = json.loads(text.strip())
        except ValueError:
            return None
    if not isinstance(body, dict):
        return None
    candidates = [body]
    if isinstance(body.get("error"), dict):
        candidates.insert(0, body["error"])
    for obj in candidates:
        for key in ("code", "type"):
            value = obj.get(key)
            if isinstance(value, str) and value in BILLING_ERROR_CODES:
                return value
    return None


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
