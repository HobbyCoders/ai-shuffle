"""Capability registry for every provider usage source the app reads.

Each source states, in one place, which endpoint it uses, which credential it
needs, whose usage it describes, whether it is read passively from inference
responses or polled, its minimum polling interval, what happens on failure,
and where the owner can check the figure on the provider's own dashboard.

The demand-driven poller (``app.core.provider_usage``) takes its jobs from
:func:`polled_sources`, and the usage API labels every snapshot from
:func:`source_for_snapshot`. A figure no source here can report is shown as
unavailable with the dashboard link. Readers must never fall back to
dashboard-internal or alpha endpoints that are not listed here.

Support labels are descriptive, not compliance claims. ``UNOFFICIAL_SUBSCRIPTION``
marks endpoints that a subscription client uses but that are not published as
a third-party API; the app reads them only for the account the owner signed in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# What a reading means. The UI must never present one kind as another.
RATE_LIMIT = "rate_limit"  # short-term request/token headroom
SUBSCRIPTION_ALLOWANCE = "subscription_allowance"  # plan usage windows
REPORTED_SPEND = "reported_spend"  # spend the provider reports for an account/org/key
MONETARY_CREDIT = "monetary_credit"  # remaining purchased/prepaid credit
# App-local estimates (token counts x catalog prices) never come from a source
# in this registry; see docs/provider-usage-sources.md.

MEASURE_LABELS = {
    RATE_LIMIT: "Request/token rate limits",
    SUBSCRIPTION_ALLOWANCE: "Subscription allowance",
    REPORTED_SPEND: "Provider-reported spend",
    MONETARY_CREDIT: "Provider-reported credit balance",
}

# How the source is exposed by the provider.
DOCUMENTED = "documented"
RESPONSE_METADATA = "response_metadata"
UNOFFICIAL_SUBSCRIPTION = "unofficial_subscription"

SUPPORT_LABELS = {
    DOCUMENTED: "Documented API",
    RESPONSE_METADATA: "Metadata on inference responses",
    UNOFFICIAL_SUBSCRIPTION: "Unofficial, subscription-specific endpoint",
}

PASSIVE = "passive"
POLLED = "polled"

STANDARD_FAILURE_POLICY = (
    "401/403 are recorded as access denied and not re-probed until the credential "
    "or its revision changes; 405/410/501 likewise as unsupported. 404, 429, 5xx "
    "and timeouts back off exponentially to one hour, honoring Retry-After. The "
    "last successful reading is kept."
)
PASSIVE_FAILURE_POLICY = (
    "No extra request. Missing headers leave the last reading; capture never "
    "affects the inference request."
)

# HTTP statuses that mark a capability as denied/unsupported for a credential.
DENIED_STATUSES = frozenset((401, 403))
UNSUPPORTED_STATUSES = frozenset((405, 410, 501))


@dataclass(frozen=True)
class Endpoint:
    url: str
    #: Minimum seconds between successful observations (application budget,
    #: not a provider-approved rate).
    min_interval: int = 300


@dataclass(frozen=True)
class UsageSource:
    id: str
    #: ``provider_usage_snapshots.provider_type`` the source writes ("claude"
    #: for the per-install Claude subscription snapshot).
    snapshot_type: str
    label: str
    credential: str
    #: One of api_key, project, organization, account, subscription.
    scope: str
    scope_label: str
    measure: str
    collection: str
    support: str
    dashboard_url: str
    endpoints: tuple[Endpoint, ...] = ()
    failure_policy: str = STANDARD_FAILURE_POLICY
    #: Figures users often expect that this source cannot report.
    unavailable: tuple[str, ...] = ()
    #: ``ProviderUsageMonitor`` method that polls this source.
    poller: str | None = None
    #: Polled once per install rather than once per model-provider row.
    global_source: bool = False
    #: Included in the usage popover.
    shown: bool = True
    #: False when a normalizer exists but no inference path captures it yet.
    collected: bool = True
    applies: Callable[[Mapping[str, Any] | None], bool] | None = field(
        default=None, compare=False, repr=False
    )

    @property
    def min_interval(self) -> int:
        return max((e.min_interval for e in self.endpoints), default=0)

    @property
    def subscription_specific(self) -> bool:
        return self.support == UNOFFICIAL_SUBSCRIPTION

    def public(self) -> dict[str, Any]:
        """JSON-safe description for the usage API and docs."""
        return {
            "id": self.id,
            "label": self.label,
            "credential": self.credential,
            "scope": self.scope,
            "scope_label": self.scope_label,
            "measure": self.measure,
            "measure_label": MEASURE_LABELS[self.measure],
            "collection": self.collection,
            "support": self.support,
            "support_label": SUPPORT_LABELS[self.support],
            "endpoints": [e.url for e in self.endpoints],
            "min_interval_seconds": self.min_interval or None,
            "failure_policy": self.failure_policy,
            "dashboard_url": self.dashboard_url,
            "unavailable": list(self.unavailable),
        }


# ---------------------------------------------------------------------------
# Row classification (pure; shared by the poller, capture and the API)
# ---------------------------------------------------------------------------

OPENAI_API_HOST = "api.openai.com"


def is_openai_platform_row(row: Mapping[str, Any] | None) -> bool:
    """Official OpenAI API-key rows (not custom OpenAI-compatible hosts)."""
    r = row or {}
    if r.get("provider_type") != "openai":
        return False
    host = (urlsplit(r.get("base_url") or "").hostname or "").lower()
    return host in ("", OPENAI_API_HOST)


def is_grok_subscription_row(row: Mapping[str, Any] | None) -> bool:
    """SuperGrok OAuth rows. xAI API-key rows are ``provider_type='xai'``."""
    r = row or {}
    return r.get("provider_type") == "grok" or r.get("auth_mode") == "grok"


def is_official_openrouter_row(row: Mapping[str, Any] | None) -> bool:
    try:
        url = urlsplit((row or {}).get("base_url") or "")
        return ((row or {}).get("provider_type") == "openrouter" and url.scheme == "https"
                and url.hostname == "openrouter.ai" and url.port in (None, 443)
                and not url.username and not url.password)
    except ValueError:
        return False


def _is_openrouter_row(row: Mapping[str, Any] | None) -> bool:
    return (row or {}).get("provider_type") == "openrouter"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENAI_ADMIN_BASE = "https://api.openai.com/v1/organization"
FAL_BILLING_URL = "https://api.fal.ai/v1/account/billing?expand=credits"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"

_SOURCES: tuple[UsageSource, ...] = (
    UsageSource(
        id="anthropic_headers",
        snapshot_type="anthropic",
        label="Anthropic rate-limit headers",
        credential="Anthropic API key (the inference key)",
        scope="organization",
        scope_label="Organization request/token limits for this API key's workspace",
        measure=RATE_LIMIT,
        collection=PASSIVE,
        support=RESPONSE_METADATA,
        dashboard_url="https://console.anthropic.com/settings/limits",
        failure_policy=PASSIVE_FAILURE_POLICY,
        unavailable=("spend", "credit balance"),
        shown=False,
        collected=False,
    ),
    UsageSource(
        id="openai_headers",
        snapshot_type="openai",
        label="OpenAI rate-limit headers",
        credential="OpenAI API key (the inference key)",
        scope="project",
        scope_label="Per-minute request/token limits for this key's project",
        measure=RATE_LIMIT,
        collection=PASSIVE,
        support=RESPONSE_METADATA,
        dashboard_url="https://platform.openai.com/settings/organization/limits",
        failure_policy=PASSIVE_FAILURE_POLICY,
        unavailable=("spend", "credit balance"),
        # Stored on the OpenAI row but hidden; the row headlines Admin usage.
        shown=False,
    ),
    UsageSource(
        id="compatible_headers",
        snapshot_type="xai",
        label="OpenAI-compatible rate-limit headers",
        credential="Provider API key (the inference key)",
        scope="api_key",
        scope_label="Per-minute request/token headroom reported by the host",
        measure=RATE_LIMIT,
        collection=PASSIVE,
        support=RESPONSE_METADATA,
        dashboard_url="https://console.x.ai/",
        failure_policy=PASSIVE_FAILURE_POLICY,
        unavailable=("spend", "credit balance"),
        shown=False,
    ),
    UsageSource(
        id="codex_headers",
        snapshot_type="chatgpt",
        label="Codex quota headers",
        credential="ChatGPT sign-in used for Codex inference",
        scope="subscription",
        scope_label="ChatGPT subscription usage windows; not API credit",
        measure=SUBSCRIPTION_ALLOWANCE,
        collection=PASSIVE,
        support=UNOFFICIAL_SUBSCRIPTION,
        dashboard_url="https://chatgpt.com/codex/settings/usage",
        failure_policy=PASSIVE_FAILURE_POLICY,
        unavailable=("OpenAI API spend", "OpenAI API credit balance"),
    ),
    UsageSource(
        id="openai_admin",
        snapshot_type="openai",
        label="OpenAI organization Admin API",
        credential="Separate OpenAI Admin key and organization ID (local only)",
        scope="organization",
        scope_label="Organization spend and completions usage this month (UTC)",
        measure=REPORTED_SPEND,
        collection=POLLED,
        support=DOCUMENTED,
        dashboard_url="https://platform.openai.com/usage",
        endpoints=(
            Endpoint(OPENAI_ADMIN_BASE + "/costs", 900),
            Endpoint(OPENAI_ADMIN_BASE + "/usage/completions", 900),
        ),
        unavailable=("prepaid credit balance", "ChatGPT subscription allowance"),
        poller="_poll_openai_provider",
        applies=is_openai_platform_row,
    ),
    UsageSource(
        id="openrouter_key",
        snapshot_type="openrouter",
        label="OpenRouter key API",
        credential="OpenRouter model key (the inference key)",
        scope="api_key",
        scope_label="This model key's reported spend and limit (USD)",
        measure=REPORTED_SPEND,
        collection=POLLED,
        support=DOCUMENTED,
        dashboard_url="https://openrouter.ai/settings/keys",
        endpoints=(Endpoint("{origin}/api/v1/key", 300),),
        unavailable=("account credit balance (needs a management key)",),
        poller="_poll_provider",
        applies=_is_openrouter_row,
    ),
    UsageSource(
        id="openrouter_credits",
        snapshot_type="openrouter_account",
        label="OpenRouter management API",
        credential="Separate OpenRouter management key (local only)",
        scope="account",
        scope_label="Account credit ledger (USD)",
        measure=MONETARY_CREDIT,
        collection=POLLED,
        support=DOCUMENTED,
        dashboard_url="https://openrouter.ai/settings/credits",
        endpoints=(Endpoint(OPENROUTER_CREDITS_URL, 300),),
        poller="_poll_openrouter_account",
        applies=is_official_openrouter_row,
    ),
    UsageSource(
        id="fal_billing",
        snapshot_type="fal",
        label="fal account billing API",
        credential="Separate fal Admin key (local only)",
        scope="account",
        scope_label="Account credit balance",
        measure=MONETARY_CREDIT,
        collection=POLLED,
        support=DOCUMENTED,
        dashboard_url="https://fal.ai/dashboard/billing",
        endpoints=(Endpoint(FAL_BILLING_URL, 300),),
        poller="_poll_fal",
        global_source=True,
    ),
    UsageSource(
        id="claude_rate_limit_events",
        snapshot_type="claude",
        label="Claude Code rate-limit events",
        credential="None beyond the running Claude Code session",
        scope="subscription",
        scope_label="Single plan window refreshed from the running session",
        measure=SUBSCRIPTION_ALLOWANCE,
        collection=PASSIVE,
        support=RESPONSE_METADATA,
        dashboard_url="https://claude.ai/settings/usage",
        failure_policy=PASSIVE_FAILURE_POLICY,
    ),
    UsageSource(
        id="grok_subscription",
        snapshot_type="grok",
        label="SuperGrok subscription billing endpoint",
        credential="SuperGrok sign-in (Grok CLI OAuth)",
        scope="subscription",
        scope_label="SuperGrok subscription allowance and plan; not xAI API billing",
        measure=SUBSCRIPTION_ALLOWANCE,
        collection=POLLED,
        support=UNOFFICIAL_SUBSCRIPTION,
        dashboard_url="https://grok.com/",
        endpoints=(Endpoint(GROK_BILLING_URL, 300), Endpoint(GROK_SETTINGS_URL, 300)),
        unavailable=("xAI API spend", "xAI API credit balance"),
        poller="_poll_grok_provider",
        applies=is_grok_subscription_row,
    ),
)

SOURCES: dict[str, UsageSource] = {s.id: s for s in _SOURCES}

# Snapshot rows -> the source that labels them in the usage API. OpenAI rows
# carry both passive headers and Admin data; Admin usage is the displayed one.
_SNAPSHOT_SOURCES = {
    "anthropic": "anthropic_headers",
    "openai": "openai_admin",
    "xai": "compatible_headers",
    "openai_compatible": "compatible_headers",
    "chatgpt": "codex_headers",
    "openrouter": "openrouter_key",
    "openrouter_account": "openrouter_credits",
    "fal": "fal_billing",
    "grok": "grok_subscription",
}


def source_for_snapshot(snapshot_type: str | None) -> UsageSource | None:
    source_id = _SNAPSHOT_SOURCES.get(snapshot_type or "")
    return SOURCES.get(source_id) if source_id else None


def polled_sources(row: Mapping[str, Any] | None) -> tuple[UsageSource, ...]:
    """Row-scoped polled sources for one enabled ``model_providers`` row."""
    return tuple(
        s for s in _SOURCES
        if s.poller and not s.global_source and s.applies is not None and s.applies(row)
    )


def global_sources() -> tuple[UsageSource, ...]:
    return tuple(s for s in _SOURCES if s.poller and s.global_source)


def passive_snapshot_type(row: Mapping[str, Any] | None, codex_backend: bool) -> str | None:
    """Snapshot type for OpenAI-style response headers, or None to skip.

    Grok subscription rows are owned by the subscription poller; per-minute
    headers would overwrite its allowance with a different measure.
    """
    if codex_backend:
        return "chatgpt"
    if row is None:
        return "openai"
    if is_grok_subscription_row(row):
        return None
    if is_openai_platform_row(row):
        return "openai"
    if (row.get("provider_type") or "") == "xai":
        return "xai"
    return "openai_compatible"


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def endpoint_for(source_id: str, url: str) -> Endpoint:
    """The registered endpoint ``url`` belongs to, else ``ValueError``.

    ``{origin}`` templates match the configured provider host (OpenRouter's
    key endpoint is read from the same host the model key is used on).
    """
    source = SOURCES.get(source_id)
    for endpoint in source.endpoints if source else ():
        if endpoint.url == url:
            return endpoint
        if "{origin}" in endpoint.url and urlsplit(url).scheme in ("http", "https") \
                and endpoint.url.replace("{origin}", _origin(url)) == url:
            return endpoint
    raise ValueError(f"Endpoint is not registered for usage source {source_id}")


def interval_for(source_id: str, url: str) -> int:
    return endpoint_for(source_id, url).min_interval


def matrix() -> list[dict[str, Any]]:
    return [s.public() for s in _SOURCES]
