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
import re

# What a reading means. The UI must never present one kind as another.
RATE_LIMIT = "rate_limit"  # short-term request/token headroom
SUBSCRIPTION_ALLOWANCE = "subscription_allowance"  # plan usage windows
REPORTED_SPEND = "reported_spend"  # spend the provider reports for an account/org/key
MONETARY_CREDIT = "monetary_credit"  # remaining purchased/prepaid credit

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
UNDOCUMENTED = "undocumented"

SUPPORT_LABELS = {
    DOCUMENTED: "Documented API",
    RESPONSE_METADATA: "Metadata on inference responses",
    UNOFFICIAL_SUBSCRIPTION: "Unofficial, subscription-specific endpoint",
    UNDOCUMENTED: "Undocumented endpoint",
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
    #: Link text for ``dashboard_url`` where it is not a usage dashboard.
    dashboard_label: str = "Dashboard"
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
            "freshness_seconds": self.min_interval * 2 if self.id == "xai_management" else self.min_interval or None,
            "failure_policy": self.failure_policy,
            "dashboard_url": self.dashboard_url,
            "dashboard_label": self.dashboard_label,
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


def is_codex_subscription_row(row: Mapping[str, Any] | None) -> bool:
    """ChatGPT sign-in rows used for Codex. OpenAI API keys are ``openai``."""
    r = row or {}
    return r.get("provider_type") == "chatgpt" or r.get("auth_mode") == "chatgpt"


def is_official_openrouter_row(row: Mapping[str, Any] | None) -> bool:
    try:
        url = urlsplit((row or {}).get("base_url") or "")
        return ((row or {}).get("provider_type") == "openrouter" and url.scheme == "https"
                and url.hostname == "openrouter.ai" and url.port in (None, 443)
                and not url.username and not url.password)
    except ValueError:
        return False


def is_antigravity_row(row: Mapping[str, Any] | None) -> bool:
    """Google Antigravity rows (the ``agy`` agent; app/core/antigravity.py)."""
    return (row or {}).get("provider_type") == "antigravity"


def _is_openrouter_row(row: Mapping[str, Any] | None) -> bool:
    return (row or {}).get("provider_type") == "openrouter"


def _native_row_on(row: Mapping[str, Any] | None, provider: str, plan: bool, host: str) -> bool:
    """A native Messages row of this credential kind on the vendor's own host."""
    from fluid.providers.compatible import endpoint_root

    r = row or {}
    if r.get("provider_type") != provider or (r.get("auth_mode") == "plan_key") != plan:
        return False
    try:
        url = urlsplit(endpoint_root(provider, r.get("auth_mode"), r.get("base_url")))
        return (url.scheme == "https" and url.hostname == host and url.port in (None, 443)
                and not url.username and not url.password)
    except ValueError:
        return False


def is_kimi_api_row(row: Mapping[str, Any] | None) -> bool:
    """Moonshot pay-as-you-go keys (international platform), not Kimi Code."""
    return _native_row_on(row, "kimi", False, "api.moonshot.ai")


def is_minimax_plan_row(row: Mapping[str, Any] | None) -> bool:
    """MiniMax M Plan keys on the international platform."""
    return _native_row_on(row, "minimax", True, "api.minimax.io")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
# fal's documented billing API needs an Admin key; this endpoint answers the
# ordinary API key the media tools already use.
FAL_BALANCE_URL = "https://rest.alpha.fal.ai/billing/user_balance"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
ANTIGRAVITY_USAGE_COMMAND = "agy -p /usage --output-format json"
# https://platform.kimi.ai/docs/api/balance
KIMI_BALANCE_URL = "https://api.moonshot.ai/v1/users/me/balance"
# https://platform.minimax.io/docs/m-plan/faq ("Use the API endpoint")
MINIMAX_PLAN_URL = "https://www.minimax.io/v1/token_plan/remains"

_SOURCES: tuple[UsageSource, ...] = (
    UsageSource(
        id="xai_management", snapshot_type="xai_balance", label="xAI Management API",
        credential="Separate xAI management key encrypted across network workers; inference key resolves team ID",
        scope="organization", scope_label="Team prepaid credit and current billing cycle",
        measure=MONETARY_CREDIT, collection=POLLED, support=DOCUMENTED,
        dashboard_url="https://console.x.ai/",
        endpoints=(Endpoint("https://api.x.ai/v1/api-key", 300),
                   Endpoint("https://management-api.x.ai/v1/billing/teams/{team_id}/prepaid/balance", 300),
                   Endpoint("https://management-api.x.ai/v1/billing/teams/{team_id}/postpaid/invoice/preview", 300)),
        poller="_poll_api_balance", applies=lambda row: (row or {}).get("provider_type") == "xai",
    ),
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
        # Captured for rate-limit handling only, never API billing or usage UI.
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
        # Stored on the ChatGPT row but not the label; the row headlines the
        # polled reading, which is the one that refreshes without a turn.
        shown=False,
    ),
    UsageSource(
        id="codex_usage",
        snapshot_type="chatgpt",
        label="Codex subscription usage endpoint",
        credential="ChatGPT sign-in used for Codex inference",
        scope="subscription",
        scope_label="ChatGPT subscription usage windows; not API credit",
        measure=SUBSCRIPTION_ALLOWANCE,
        collection=POLLED,
        support=UNOFFICIAL_SUBSCRIPTION,
        dashboard_url="https://chatgpt.com/codex/settings/usage",
        endpoints=(Endpoint(CODEX_USAGE_URL, 300),),
        unavailable=("OpenAI API spend", "OpenAI API credit balance"),
        poller="_poll_codex_provider",
        applies=is_codex_subscription_row,
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
        unavailable=("account credit balance (reported separately by the credits API)",),
        poller="_poll_provider",
        applies=_is_openrouter_row,
    ),
    UsageSource(
        id="openrouter_credits",
        snapshot_type="openrouter_account",
        label="OpenRouter credits API",
        credential="Existing OpenRouter model key (or previously configured local management key)",
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
        label="fal account balance",
        credential="fal.ai API key (the media tools key: saved, or FAL_KEY / FAL_API_KEY)",
        scope="account",
        scope_label="Account credit balance",
        measure=MONETARY_CREDIT,
        collection=POLLED,
        support=UNDOCUMENTED,
        dashboard_url="https://fal.ai/dashboard/billing",
        endpoints=(Endpoint(FAL_BALANCE_URL, 300),),
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
    UsageSource(
        id="antigravity_usage",
        snapshot_type="antigravity",
        label="Antigravity CLI /usage command",
        credential="The Antigravity agent's own sign-in or key on this worker (never read by the app)",
        scope="subscription",
        scope_label="Model quota windows Google's agent reports for its sign-in",
        measure=SUBSCRIPTION_ALLOWANCE,
        collection=POLLED,
        support=UNDOCUMENTED,
        # Google publishes no usage page for Antigravity plans; the FAQ
        # explains its quotas, so the link says what it is.
        dashboard_url="https://antigravity.google/docs/faq",
        dashboard_label="Antigravity FAQ",
        # Not HTTP: the binary's own ``agy -p /usage --output-format json``,
        # run only when the usage popover asks (on-demand refresh).
        endpoints=(Endpoint(ANTIGRAVITY_USAGE_COMMAND, 300),),
        failure_policy=(
            "Read only on an on-demand refresh, never on the background poll. A "
            "reply that does not parse, a timeout or a signed-out agent records "
            "'unavailable' and keeps the last reading."
        ),
        unavailable=("spend", "credit balance", "per-turn cost"),
        poller="_poll_antigravity_provider",
        applies=is_antigravity_row,
    ),
    UsageSource(
        id="kimi_balance",
        snapshot_type="kimi_balance",
        label="Kimi balance API",
        credential="Kimi API platform key (the inference key)",
        scope="account",
        scope_label="Account available balance, cash plus vouchers (USD)",
        measure=MONETARY_CREDIT,
        collection=POLLED,
        support=DOCUMENTED,
        dashboard_url="https://platform.kimi.ai/console/account",
        endpoints=(Endpoint(KIMI_BALANCE_URL, 300),),
        unavailable=("Kimi Code membership quota", "per-key spend"),
        poller="_poll_kimi_balance",
        applies=is_kimi_api_row,
    ),
    UsageSource(
        id="minimax_plan",
        snapshot_type="minimax_plan",
        label="MiniMax M Plan remaining-quota API",
        credential="MiniMax M Plan key (the inference key)",
        scope="subscription",
        scope_label="M Plan 5-hour and weekly text-model windows; not pay-as-you-go balance",
        measure=SUBSCRIPTION_ALLOWANCE,
        collection=POLLED,
        support=DOCUMENTED,
        dashboard_url="https://platform.minimax.io/console/usage",
        endpoints=(Endpoint(MINIMAX_PLAN_URL, 300),),
        unavailable=("pay-as-you-go balance", "image and video model windows"),
        poller="_poll_minimax_plan",
        applies=is_minimax_plan_row,
    ),
)

SOURCES: dict[str, UsageSource] = {s.id: s for s in _SOURCES}

# Snapshot rows -> the source that labels them in the usage API. ChatGPT rows
# headline the polled usage endpoint over passive Codex quota headers.
_SNAPSHOT_SOURCES = {
    "anthropic": "anthropic_headers",
    "openai": "openai_headers",
    "xai": "compatible_headers",
    "openai_compatible": "compatible_headers",
    "chatgpt": "codex_usage",
    "openrouter": "openrouter_key",
    "openrouter_account": "openrouter_credits",
    "fal": "fal_billing",
    "grok": "grok_subscription",
    "antigravity": "antigravity_usage",
    "kimi_balance": "kimi_balance",
    "minimax_plan": "minimax_plan",
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


# ---------------------------------------------------------------------------
# Key kinds with no meter: a note and the vendor's own usage page
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageLink:
    """A key kind whose vendor documents no usage or balance API.

    Never polled and never stored: the usage API lists the row with this note
    and the vendor's page (URLs from docs/provider-usage-sources.md, "Kimi,
    MiniMax and OpenCode keys").
    """
    id: str
    label: str
    #: The one line the usage popover shows; true for this kind of key.
    note: str
    dashboard_url: str
    subscription: bool
    applies: Callable[[Mapping[str, Any] | None], bool] = field(compare=False, repr=False)


_LINKS: tuple[UsageLink, ...] = (
    UsageLink(
        id="kimi_code", label="Kimi Code membership", note="No quota meter for this key",
        dashboard_url="https://www.kimi.com/membership/subscription?tab=quota", subscription=True,
        applies=lambda row: _native_row_on(row, "kimi", True, "api.kimi.ai"),
    ),
    UsageLink(
        id="minimax_api", label="MiniMax API (pay as you go)", note="No balance meter for this key",
        dashboard_url="https://platform.minimax.io/user-center/payment/balance", subscription=False,
        applies=lambda row: _native_row_on(row, "minimax", False, "api.minimax.io"),
    ),
    UsageLink(
        id="opencode_go", label="OpenCode Go", note="No quota meter for this key",
        dashboard_url="https://opencode.ai/console", subscription=True,
        applies=lambda row: _native_row_on(row, "opencode", True, "opencode.ai"),
    ),
    # Per-turn cost is recorded from responses; only a balance is missing.
    UsageLink(
        id="opencode_console", label="OpenCode Console (pay as you go)", note="No balance meter for this key",
        dashboard_url="https://opencode.ai/console", subscription=False,
        applies=lambda row: _native_row_on(row, "opencode", False, "opencode.ai"),
    ),
)

USAGE_LINKS: dict[str, UsageLink] = {link.id: link for link in _LINKS}


def usage_link_for(row: Mapping[str, Any] | None) -> UsageLink | None:
    """The link-only entry for a row with no usage source, else None.

    A row with a polled source never gets one, and a key on a custom endpoint
    or another vendor platform matches no kind.
    """
    if polled_sources(row):
        return None
    return next((link for link in _LINKS if link.applies(row)), None)


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
        if "{team_id}" in endpoint.url:
            pattern = re.escape(endpoint.url).replace(r"\{team_id\}", r"[A-Za-z0-9_-]{1,180}")
            if re.fullmatch(pattern, url):
                return endpoint
        if "{origin}" in endpoint.url and urlsplit(url).scheme in ("http", "https") \
                and endpoint.url.replace("{origin}", _origin(url)) == url:
            return endpoint
    raise ValueError(f"Endpoint is not registered for usage source {source_id}")


def interval_for(source_id: str, url: str) -> int:
    return endpoint_for(source_id, url).min_interval


def matrix() -> list[dict[str, Any]]:
    return [s.public() for s in _SOURCES]
