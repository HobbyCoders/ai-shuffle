"""One answer to "may a new agent run start on this worker?".

The provider the run uses must have its credential. Every provider counts the
same: Claude, OpenAI, xAI Grok, OpenRouter and custom gateways. Cloud plans do
not restrict local runs.

Worker readiness (startup, ``GET /auth/status``) means at least one agent
provider is usable (:func:`ready_providers`). A worker
with no Claude credential but a connected OpenAI or Grok account is ready.

Turn start is decided per provider (:func:`turn_refusal`). A turn on the
Claude Code CLI leg needs the active Claude auth mode's credential
(``app.core.claude_auth_mode``): in ``subscription`` mode the stock CLI reports
a sign-in (``claude auth status``, via ``claude_cli_status``; the CLI's
credential files are never read); in ``api_key`` mode an Anthropic API key is
stored on this worker; in ``cloud`` mode the owner's chosen cloud provider or
gateway has its required values stored (``claude_cloud_auth``); in ``off`` mode Claude is turned off and every Claude
run is refused with :data:`OFF_MESSAGE`. There is no fallback: a Claude turn
with no Claude credential does not borrow another one, it is refused with a
Claude setup message. The refusal applies whatever the turn's source: chat,
connector, automation, wake-up, background worker or a turn handed over from
another device.

Turns that run on another provider (an OpenAI, xAI, OpenRouter or custom
gateway row) do not need a Claude credential; for those only the license part
applies. Their own missing key is reported by the provider path itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

SETUP_CODE = "agent_setup_required"

# Claude-leg messages: they refuse a turn that runs on Claude and say so.
NO_CLI_MESSAGE = (
    "Claude cannot run on this worker: the Claude Code CLI is not installed. "
    "Install it in Settings > Providers > Claude. You can also pick a model from another provider."
)
NOT_SIGNED_IN_MESSAGE = (
    "Claude cannot run on this worker: Claude is set to subscription mode and the "
    "Claude Code CLI is not signed in. Sign in with your Claude account in "
    "Settings > Providers > Claude (it runs `claude auth login`), or add an "
    "Anthropic API key and switch Claude to API key mode. You can also pick a "
    "model from another provider."
)
NO_KEY_MESSAGE = (
    "Claude cannot run on this worker: Claude is set to API key mode and no "
    "Anthropic API key is stored here. Add your key in Settings > Providers > "
    "Claude, or switch Claude to subscription mode and sign in to the Claude Code "
    "CLI. You can also pick a model from another provider."
)
UNKNOWN_CLI_MESSAGE = (
    "Claude cannot run on this worker: AI Shuffle could not read the Claude Code "
    "CLI's sign-in status ({detail}). Check Settings > Providers > Claude, or pick "
    "a model from another provider."
)
CHECK_FAILED_MESSAGE = (
    "Claude cannot run on this worker: AI Shuffle could not check the Claude "
    "credential. See the server log, or pick a model from another provider."
)
CLOUD_NOT_SET_MESSAGE = (
    "Claude cannot run on this worker: Claude is set to Cloud provider or gateway "
    "mode and no cloud provider or gateway is set up here. Set one up in Settings > "
    "Providers > Claude, or switch Claude to another mode. You can also pick a model "
    "from another provider."
)
CLOUD_MISSING_MESSAGE = (
    "Claude cannot run on this worker: Claude is set to Cloud provider or gateway "
    "mode with {method}, and {missing} is missing. Add it in Settings > Providers > "
    "Claude, or switch Claude to another mode. You can also pick a model from "
    "another provider."
)
CLOUD_LOCKED_MESSAGE = (
    "Claude cannot run on this worker: Claude is set to Cloud provider or gateway "
    "mode and its saved credentials cannot be read because credential storage is "
    "locked. Unlock it, or pick a model from another provider."
)
OFF_MESSAGE = (
    "Claude is off on this worker. Turn it on in Settings > Providers > Claude, "
    "or pick a model from another provider."
)
# Worker-level message: no provider of any kind can run an agent.
NO_PROVIDER_MESSAGE = (
    "Agents cannot start: no AI provider is set up on this worker. "
    "Add one in Settings > Providers."
)

CLAUDE_PROVIDER_ID = "claude"


class AgentSetupRequired(ValueError):
    """A run was refused because this worker is not set up to start agents."""


# What startup found, for logs and the status endpoint: "ready" or
# "setup_required". None before startup ran.
startup_state: Optional[str] = None
startup_reason: Optional[str] = None


def claude_credential_ready(cli=None) -> Tuple[bool, Optional[str]]:
    """Whether the active Claude Code mode has its credential, and why not.

    ``cli`` is a ``ClaudeCliStatus`` (or its dict); when omitted the cached
    probe is used, running ``claude auth status`` on a miss (blocking).
    """
    from app.core import claude_auth_mode

    mode = claude_auth_mode.current_mode()
    if mode == claude_auth_mode.MODE_OFF:
        return False, OFF_MESSAGE
    if mode == claude_auth_mode.MODE_API_KEY:
        if claude_auth_mode.has_stored_api_key():
            return True, None
        return False, NO_KEY_MESSAGE
    if mode == claude_auth_mode.MODE_CLOUD:
        # A presence check of the owner's stored values; no network call.
        # The subscription sign-in and the Anthropic key never stand in.
        from app.core import claude_cloud_auth

        cloud = claude_cloud_auth.status()
        if cloud["ready"]:
            return True, None
        if not cloud["configured"]:
            return False, CLOUD_NOT_SET_MESSAGE
        if cloud.get("storage_locked"):
            return False, CLOUD_LOCKED_MESSAGE
        return False, CLOUD_MISSING_MESSAGE.format(
            method=cloud["method_label"], missing=" and ".join(cloud["missing"]))

    if cli is None:
        from app.core import claude_cli_status

        cli = claude_cli_status.get_status()
    if not isinstance(cli, dict):
        cli = cli.to_dict()
    if cli.get("signed_in"):
        return True, None
    if not cli.get("installed"):
        return False, NO_CLI_MESSAGE
    if cli.get("signed_in") is None and cli.get("error"):
        return False, UNKNOWN_CLI_MESSAGE.format(detail=cli["error"])
    return False, NOT_SIGNED_IN_MESSAGE


def agent_credentials_ready(cli=None) -> Tuple[bool, Optional[str]]:
    """``(ready, reason)`` for a new run on the Claude leg of this worker.

    The Claude credential only. Worker readiness across every
    provider is :func:`status_payload` / :func:`record_startup`.

    Blocking (may run the CLI probe on a cache miss); async callers use
    :func:`agent_credentials_ready_async`.
    """
    try:
        return claude_credential_ready(cli)
    except Exception:  # noqa: BLE001 — a broken check must not look like "ready"
        logger.exception("Credential check failed")
        return False, CHECK_FAILED_MESSAGE


async def agent_credentials_ready_async(cli=None) -> Tuple[bool, Optional[str]]:
    return await asyncio.to_thread(agent_credentials_ready, cli)


def turn_refusal(leg: Optional[str]) -> Optional[str]:
    """The setup message that refuses a turn, or None to let it start.

    ``leg`` is the harness leg the turn will run on (``"delegated"`` is the
    Claude Code CLI; anything else is a provider row with its own credential).
    """
    if leg in (None, "delegated"):
        ready, reason = agent_credentials_ready()
        return None if ready else reason
    return None


async def turn_refusal_async(leg: Optional[str]) -> Optional[str]:
    return await asyncio.to_thread(turn_refusal, leg)


def leg_for_options(options) -> str:
    """The leg a built options object runs on; unknown reads as the CLI."""
    try:
        from app.core.provider.harness_adapter import provider_kind_for_sdk_options

        return provider_kind_for_sdk_options(options) or "delegated"
    except Exception:  # noqa: BLE001
        logger.debug("Could not decide the leg for the credential gate", exc_info=True)
        return "delegated"


# ─── Subscriptions are the owner's own ──────────────────────────────────────
#
# Claude (the Claude Code CLI signed in to a plan), ChatGPT/Codex and SuperGrok
# subscriptions are for the subscriber's personal use: Anthropic, OpenAI and
# xAI all forbid serving other people's requests from them, and so do our own
# terms. A run started for anyone other than the owner (an API user, which
# includes every connector message: a connector turn runs as the connector's
# API user) must use an API-key provider instead. API keys (Anthropic, OpenAI,
# xAI, OpenRouter, custom gateways, local models) stay allowed. The owner,
# their paired devices and their own workers are unrestricted.

SUBSCRIPTION_OWNER_ONLY_CODE = "subscription_owner_only"
SUBSCRIPTION_OWNER_ONLY_MESSAGE = (
    "This model uses the owner's personal subscription and can't run for other "
    "people. Pick a model on an API-key provider."
)


def provider_row_is_subscription(provider_type: Optional[str], auth_mode: Optional[str]) -> bool:
    """Whether a ``model_providers`` row signs in with a subscription.

    ChatGPT/Codex (``chatgpt``) and SuperGrok (``grok``) rows, by type or auth
    mode, and any row whose auth mode is a subscription login
    (``fluid_credentials._SUBSCRIPTION_AUTH_MODES``, e.g. an Anthropic row on a
    Claude plan, which runs on the CLI).
    """
    from app.core.provider.fluid_credentials import _SUBSCRIPTION_AUTH_MODES

    ptype = (provider_type or "").strip().lower()
    mode = (auth_mode or "").strip().lower()
    return ptype in ("chatgpt", "grok") or mode == "codex" or mode in _SUBSCRIPTION_AUTH_MODES


def claude_cli_uses_subscription() -> bool:
    """Whether a Claude run on the CLI leg uses the owner's Claude plan.

    True in subscription mode. API key and cloud modes use a key; off refuses
    every Claude run anyway. Unreadable reads as a subscription: this answer
    only ever narrows what other people may run.
    """
    try:
        from app.core import claude_auth_mode

        return claude_auth_mode.current_mode() == claude_auth_mode.MODE_SUBSCRIPTION
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the Claude auth mode; treating it as a subscription", exc_info=True)
        return True


def options_use_subscription(options) -> bool:
    """Whether a built options object runs on a subscription sign-in.

    The bound provider row decides when there is one (its shape is stamped on
    the options as ``aih_provider_type`` / ``aih_provider_auth_mode``; looked
    up when missing). A run on the Claude Code CLI leg uses a subscription
    when Claude is in subscription mode.
    """
    provider_id = getattr(options, "aih_provider_id", None)
    if provider_id:
        ptype = getattr(options, "aih_provider_type", None)
        mode = getattr(options, "aih_provider_auth_mode", None)
        if not ptype and not mode:
            from app.core.provider.harness_adapter import _lookup_provider_row_shape

            ptype, mode, _dialect = _lookup_provider_row_shape(str(provider_id))
        if provider_row_is_subscription(ptype, mode):
            return True
    if leg_for_options(options) == "delegated":
        return claude_cli_uses_subscription()
    return False


def selection_uses_subscription(model: Optional[str], provider_id: Optional[str] = None) -> bool:
    """Whether a saved model choice (an API user's, or an agent's) would run
    on a subscription sign-in. Blocking (may read one provider row).

    ``model`` is a picker value: a ``prov:<row>:<model>`` composite or a bare
    id, with ``provider_id`` for an agent's separate provider field. A native
    Claude id with no provider runs on the CLI. Anything else (no model, a
    bare non-Claude id) is not a known subscription choice.
    """
    from app.core.query_options import decode_provider_model, is_native_claude_model

    decoded = decode_provider_model(model)
    if decoded:
        provider_id, model = decoded
    if provider_id:
        from app.db import database

        row = database.get_model_provider(provider_id, include_token=False)
        if not row:
            return False
        if provider_row_is_subscription(row.get("provider_type"), row.get("auth_mode")):
            return True
        # An Anthropic row's plain Claude pick keeps its own (API-key) leg.
        return False
    if is_native_claude_model(model):
        return claude_cli_uses_subscription()
    return False


def requester_refusal(options, api_user_id: Optional[str]) -> Optional[str]:
    """Refuse a run for someone other than the owner on a subscription.

    ``api_user_id`` is who the run is for (``None`` is the owner). A connector
    message runs as its connector's API user; the caller passes ``None`` only
    when :func:`connector_sender_is_owner` says the sender is the owner's own
    account on that platform. Blocking.
    """
    if not api_user_id:
        return None
    try:
        uses_subscription = options_use_subscription(options)
    except Exception:  # noqa: BLE001 — fail closed for other people
        logger.warning("Could not tell whether a run uses a subscription; refusing it", exc_info=True)
        uses_subscription = True
    if uses_subscription:
        logger.warning(
            "Refused a run for API user %s: its model uses the owner's subscription", api_user_id,
        )
        return SUBSCRIPTION_OWNER_ONLY_MESSAGE
    return None


def connector_sender_is_owner(sender: Optional[dict], api_user_id: Optional[str]) -> bool:
    """True when a connector turn's ``sender`` (``{"connector_id",
    "platform_user_id"}`` from its payload) is one of the owner's own accounts
    on a connector bound to ``api_user_id``, the API user the turn runs as.
    Only the platform's immutable user id is matched
    (``connector_service.sender_is_owner``). Any doubt is ``False``. Blocking.
    """
    if not isinstance(sender, dict) or not api_user_id:
        return False
    connector_id = sender.get("connector_id")
    try:
        from app.core import connector_service
        from app.db import database

        row = database.get_connector(connector_id) if connector_id else None
        if not row or row.get("api_user_id") != api_user_id:
            return False
        return connector_service.sender_is_owner(connector_id, sender.get("platform_user_id"))
    except Exception:  # noqa: BLE001 — fail closed
        logger.warning("Could not check a connector sender; treating it as not the owner", exc_info=True)
        return False


def refusal_for_options(options, api_user_id: Optional[str] = None) -> Optional[str]:
    """Why a run with these built options must not start, or None.

    The single run-start check every engine path calls once its options are
    built: first who the run is for (:func:`requester_refusal`), then
    :func:`turn_refusal` for the leg. May read the DB.
    """
    refusal = requester_refusal(options, api_user_id)
    if refusal:
        return refusal
    return turn_refusal(leg_for_options(options))


async def refusal_for_options_async(options, api_user_id: Optional[str] = None) -> Optional[str]:
    return await asyncio.to_thread(refusal_for_options, options, api_user_id)


def refusal_event(reason: str) -> dict:
    """The stream's error frame for a refused turn."""
    code = SUBSCRIPTION_OWNER_ONLY_CODE if reason == SUBSCRIPTION_OWNER_ONLY_MESSAGE else SETUP_CODE
    return {"type": "error", "message": reason, "code": code}


def provider_id_for_row(row: Mapping[str, Any]) -> str:
    """The Settings > Providers id a ``model_providers`` row belongs to.

    Mirrors the frontend's ``providerOfRow``: subscription sign-in first
    (``auth_mode``), then ``provider_type``; anything else is its own gateway.
    """
    auth_mode = row.get("auth_mode")
    ptype = row.get("provider_type")
    if auth_mode == "chatgpt" or ptype in ("chatgpt", "openai"):
        return "openai"
    if auth_mode == "grok" or ptype in ("grok", "xai"):
        return "grok"
    if ptype == "openrouter":
        return "openrouter"
    return f"gw:{row.get('id')}"


def usable_provider_rows() -> List[dict]:
    """Enabled provider rows that can run a model now; ``[]`` on any failure.

    Agent providers only: the media provider (fal.ai) is not a model row.
    """
    try:
        from app.db import database

        return list(database.list_usable_model_providers() or [])
    except Exception:  # noqa: BLE001 — a DB hiccup reads as "no provider row"
        logger.warning("Could not read the provider rows for the credential gate", exc_info=True)
        return []


def _claude_ok(cli=None) -> Tuple[bool, Optional[str]]:
    try:
        return claude_credential_ready(cli)
    except Exception:  # noqa: BLE001 — a broken check must not look like "ready"
        logger.warning("Claude credential check failed", exc_info=True)
        return False, CHECK_FAILED_MESSAGE


def ready_providers(cli=None, *, claude_ok: Optional[bool] = None) -> List[str]:
    """Ids of the agent providers with a usable credential on this worker.

    ``"claude"`` when the active Claude mode has its credential, then one id per
    usable provider row (``"openai"``, ``"grok"``, ``"openrouter"``,
    ``"gw:<row id>"``), each listed once. Never raises.
    """
    if claude_ok is None:
        claude_ok = _claude_ok(cli)[0]
    ids: List[str] = [CLAUDE_PROVIDER_ID] if claude_ok else []
    for row in usable_provider_rows():
        try:
            pid = provider_id_for_row(row)
        except Exception:  # noqa: BLE001
            continue
        if pid not in ids:
            ids.append(pid)
    return ids


def _default_candidate_rows(servable_only: bool) -> List[dict]:
    """The usable rows a default model may come from.

    Every row :func:`ready_providers` counts, custom gateways included: the
    main chat leg runs any enabled row (``validate_provider_model(...,
    require_servable=False)``). ``servable_only`` keeps only
    :func:`~app.core.environment.is_provider_servable` rows, for a caller
    that validates its pick with that rule (the voice delegation).
    """
    rows = [r for r in usable_provider_rows() if r.get("id")]
    if servable_only:
        from app.core.environment import is_provider_servable

        rows = [r for r in rows if is_provider_servable(r)]
    return rows


def default_provider_model(*, servable_only: bool = False) -> Optional[Tuple[str, str]]:
    """``(provider row id, model id)`` for a run that names no model and has
    no usable most used model, or None.

    The same pick as the composer's ``resolveDefaultModel``
    (frontend/src/lib/constants/models.svelte.ts), so the run, ``/api/models``
    and the UI agree: providers in :func:`ready_providers` order (newest row
    first), a subscription row before an API-key row of the same provider,
    and on each row its own default model when it is curated, else its first
    curated model. A usable row that lists no model is skipped.
    """
    try:
        from app.core.model_registry import _is_subscription_row
        from app.core.provider_agent import curated_model_ids, default_model_for_provider
    except Exception:  # noqa: BLE001
        return None
    rows = _default_candidate_rows(servable_only)
    brands: List[str] = []
    for row in rows:
        brand = provider_id_for_row(row)
        if brand not in brands:
            brands.append(brand)
    for brand in brands:
        entries = [r for r in rows if provider_id_for_row(r) == brand]
        entries.sort(key=lambda r: not _is_subscription_row(r))
        for row in entries:
            ids = curated_model_ids(row)
            model = default_model_for_provider(row) or (ids[0] if ids else None)
            if model:
                return row["id"], model
    return None


def _claude_ready_for_default(probe: bool) -> bool:
    """Whether Claude has its credential, for choosing a default model.

    ``probe=False`` reads only the last CLI probe and treats "not probed yet"
    as ready, so a request path never spawns the CLI and an unknown state
    keeps the registry default (the old behavior).
    """
    if probe:
        return bool(claude_credential_ready()[0])
    from app.core import claude_auth_mode, claude_cli_status

    if claude_auth_mode.current_mode() == claude_auth_mode.MODE_SUBSCRIPTION:
        cached = claude_cli_status.peek()
        if cached is None:
            return True
        return bool(claude_credential_ready(cached)[0])
    return bool(claude_credential_ready({})[0])


# The owner's most used model, the first choice for a run that names none.
#: Look-back window, in days.
MOST_USED_WINDOW_DAYS = 30
#: Fewest counted turns before a model can become the default, so one stray
#: message does not flip it.
MOST_USED_MIN_TURNS = 3
#: Seconds the usage ranking is reused. Only the ranking is cached: whether
#: its provider is still usable is checked live on every call, so a provider
#: that loses its credential stops being the default at once.
MOST_USED_TTL_SECONDS = 60.0

_most_used_cache: Optional[Tuple[float, List[Tuple[Optional[str], str, int, Any]]]] = None

_CLAUDE_FAMILIES = ("opus", "sonnet", "haiku", "fable")


def invalidate_most_used_cache() -> None:
    """Drop the cached usage ranking (tests; any caller that must re-read)."""
    global _most_used_cache
    _most_used_cache = None


def _claude_alias(model: Optional[str]) -> Optional[str]:
    """The registry alias a logged Claude model id maps to, or None."""
    from app.core.model_registry import get_model

    match = get_model(model)
    if match is not None:
        return match.family
    lowered = (model or "").lower()
    if lowered.startswith("claude"):
        for family in _CLAUDE_FAMILIES:
            if family in lowered:
                return family
    return None


def _most_used_ranking() -> List[Tuple[Optional[str], str, int, Any]]:
    """``(provider row id or None for Claude, model, turns, last used)``,
    most turns first, then most recent. Cached for
    :data:`MOST_USED_TTL_SECONDS`; ``[]`` on any failure.

    Claude ids are folded onto their registry alias (a ``claude-opus-…`` turn
    counts for ``opus``) so the result is a selectable value.
    """
    global _most_used_cache
    import time
    from datetime import datetime, timedelta, timezone

    now = time.monotonic()
    cached = _most_used_cache
    if cached is not None and cached[0] > now:
        return cached[1]
    ranking: List[Tuple[Optional[str], str, int, Any]] = []
    try:
        from app.db import database

        since = (datetime.now(timezone.utc) - timedelta(days=MOST_USED_WINDOW_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S+00:00"
        )
        merged: dict = {}
        for row in database.get_most_used_models(since) or []:
            provider_id = row.get("provider_id") or None
            model = row.get("model")
            if provider_id:
                key = (provider_id, model)
            elif row.get("provider_type") in (None, "", CLAUDE_PROVIDER_ID):
                alias = _claude_alias(model)
                if not alias:
                    continue
                key = (None, alias)
            else:
                continue
            turns = int(row.get("turn_count") or 0)
            last_used = row.get("last_used")
            if key in merged:
                prev_turns, prev_last = merged[key]
                turns += prev_turns
                if prev_last is not None and (last_used is None or prev_last > last_used):
                    last_used = prev_last
            merged[key] = (turns, last_used)
        ranking = [(k[0], k[1], v[0], v[1]) for k, v in merged.items()]
        ranking.sort(key=lambda r: str(r[3] or ""), reverse=True)
        ranking.sort(key=lambda r: r[2], reverse=True)
    except Exception:  # noqa: BLE001 — usage history is a preference, never a failure
        logger.warning("Could not read the most used models", exc_info=True)
        ranking = []
    _most_used_cache = (now + MOST_USED_TTL_SECONDS, ranking)
    return ranking


def most_used_model(
    *, probe: bool = True, servable_only: bool = False,
) -> Optional[Tuple[Optional[str], str]]:
    """``(provider row id or None, model)`` the owner used most, or None.

    The highest-ranked model with at least :data:`MOST_USED_MIN_TURNS` turns
    in the last :data:`MOST_USED_WINDOW_DAYS` days whose provider can still
    run it: Claude when it has its credential (``probe`` as in
    :func:`default_run_model`), a provider row when it is usable and still
    lists the model. Never raises.
    """
    try:
        ranking = _most_used_ranking()
        if not ranking:
            return None
        from app.core.provider_agent import curated_model_ids

        rows: Optional[dict] = None
        claude_ok: Optional[bool] = None
        for provider_id, model, turns, _last in ranking:
            if turns < MOST_USED_MIN_TURNS:
                break
            if provider_id is None:
                if claude_ok is None:
                    claude_ok = _claude_ready_for_default(probe)
                if claude_ok:
                    return None, model
                continue
            if rows is None:
                rows = {r.get("id"): r for r in _default_candidate_rows(servable_only)}
            row = rows.get(provider_id)
            if row is not None and model in curated_model_ids(row):
                return provider_id, model
    except Exception:  # noqa: BLE001
        logger.warning("Could not pick the most used model", exc_info=True)
    return None


def default_run_model(
    *, probe: bool = True, prefer_most_used: bool = True, servable_only: bool = False,
) -> Tuple[Optional[str], str]:
    """``(provider row id or None, model id)`` for a run that named no model.

    Every provider counts the same. In order:

    1. the owner's most used chat model (:func:`most_used_model`) while its
       provider can still run it;
    2. the newest usable provider row's own default model
       (:func:`default_provider_model`); Claude is the remaining candidate,
       whose default is the registry default;
    3. the registry default, so a worker with nothing set up still gets the
       Claude setup message.

    ``prefer_most_used=False`` is for internal text generation (titles,
    suggestions, commit text), where the chat model the owner uses most may
    be an expensive one: it skips step 1 and keeps the earlier order, the
    registry default while Claude has its credential, else the newest usable
    provider's default model.

    Provider rows are every row :func:`ready_providers` counts, custom
    gateways included; ``servable_only`` narrows them to
    :func:`~app.core.environment.is_provider_servable` rows for a caller
    that cannot run a gateway row.

    Only for runs with no model: an explicit choice is never passed through
    here. Never raises.
    """
    from app.core.model_registry import DEFAULT_MODEL

    try:
        if prefer_most_used:
            picked = most_used_model(probe=probe, servable_only=servable_only)
            if picked:
                return picked[0], picked[1]
        elif _claude_ready_for_default(probe):
            return None, DEFAULT_MODEL
        picked = default_provider_model(servable_only=servable_only)
        if picked:
            return picked[0], picked[1]
    except Exception:  # noqa: BLE001 — a failed check keeps the registry default
        logger.warning("Could not pick a default model", exc_info=True)
    return None, DEFAULT_MODEL


def default_run_selection(
    *, probe: bool = True, prefer_most_used: bool = True, servable_only: bool = False,
) -> str:
    """:func:`default_run_model` as a picker value: a native alias, or a
    ``prov:<providerId>:<modelId>`` composite for a provider row."""
    provider_id, model = default_run_model(
        probe=probe, prefer_most_used=prefer_most_used, servable_only=servable_only,
    )
    # Same shape as query_options.PROVIDER_MODEL_PREFIX / decode_provider_model.
    return f"prov:{provider_id}:{model}" if provider_id else model


def record_startup(cli=None) -> Tuple[bool, Optional[str]]:
    """Startup's check: log whether agents can run on this worker.

    Ready means at least one provider is usable.
    Claude missing or off while another provider is ready is logged as
    information, not as a setup problem.
    """
    global startup_state, startup_reason
    claude_ok, claude_reason = _claude_ok(cli)
    providers = ready_providers(claude_ok=claude_ok)
    ready = bool(providers)
    reason = None if providers else NO_PROVIDER_MESSAGE
    startup_state = "ready" if ready else "setup_required"
    startup_reason = reason
    if ready:
        logger.info("Agent runs: ready (providers: %s)", ", ".join(providers))
    else:
        logger.warning("Agent runs are blocked until a provider is set up: %s", reason)
    if not claude_ok:
        if claude_reason == OFF_MESSAGE:
            logger.info("Claude is off on this worker; Claude runs are refused. Other providers still run.")
        else:
            logger.info("Claude is not set up on this worker; Claude runs are refused: %s", claude_reason)
    return ready, reason


def status_payload(cli=None) -> dict:
    """The gate as ``/auth/status`` and Settings report it.

    ``ready`` means any agent provider is usable; ``claude_credential_ready``
    is the Claude-only answer. ``reason`` explains a missing provider.
    """
    from app.core import claude_auth_mode
    claude_ok, _ = _claude_ok(cli)
    providers = ready_providers(claude_ok=claude_ok)
    any_ok = bool(providers)
    reason = None if any_ok else NO_PROVIDER_MESSAGE
    return {
        "ready": any_ok,
        "reason": reason,
        "mode": claude_auth_mode.current_mode(),
        "claude_credential_ready": claude_ok,
        "license_allows_runs": True,
        "startup_state": startup_state,
        "any_provider_ready": any_ok,
        "ready_providers": providers,
    }
