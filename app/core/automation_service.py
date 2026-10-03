"""
Automation service — business logic for CRUD, budget checking, and prompt rendering.
"""

import logging
import re
import secrets
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core import encryption
from app.core.config import settings
from app.core.models import VALID_AUTOMATION_TRIGGER_TYPES
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt template rendering
# ---------------------------------------------------------------------------

def render_prompt(template: str, trigger_data: Optional[Dict[str, Any]] = None) -> str:
    """Render a prompt template by substituting ``{{variable}}`` placeholders.

    Supports nested access via dot notation: ``{{event.session_id}}``.
    Missing variables are left as-is (no error).
    """
    if not trigger_data:
        return template

    def _resolve(match: re.Match) -> str:
        key = match.group(1).strip()
        parts = key.split(".")
        value: Any = trigger_data
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return match.group(0)  # leave unresolved
            if value is None:
                return match.group(0)
        return str(value)

    return re.sub(r"\{\{(.+?)\}\}", _resolve, template)


def render_action_params(params: Dict[str, Any], trigger_data: Dict, response: str = "") -> Dict[str, Any]:
    """Render template variables in action step parameters.

    Supports ``{{trigger.data.xxx}}`` and ``{{response}}`` templates.
    Non-string values are passed through unchanged.
    """
    rendered = {}
    for key, value in params.items():
        if isinstance(value, str):
            rendered[key] = render_prompt(value, {**trigger_data, "response": response})
        else:
            rendered[key] = value
    return rendered


# ---------------------------------------------------------------------------
# Budget checking
# ---------------------------------------------------------------------------

def check_budget(automation: Dict[str, Any]) -> Tuple[bool, str]:
    """Check whether an automation is within its budget limits.

    Returns (ok, reason).  If *ok* is False, *reason* explains which limit
    was hit.
    """
    automation_id = automation["id"]

    # Lifetime cap
    max_total = automation.get("max_cost_total")
    if max_total is not None and automation.get("total_cost_used", 0) >= max_total:
        return False, f"Lifetime budget exceeded (${automation['total_cost_used']:.4f} / ${max_total:.4f})"

    # Daily limits
    daily = database.get_automation_daily_stats(automation_id)

    max_runs = automation.get("max_runs_per_day")
    if max_runs is not None and daily["run_count"] >= max_runs:
        return False, f"Daily run limit reached ({daily['run_count']} / {max_runs})"

    max_day_cost = automation.get("max_cost_per_day")
    if max_day_cost is not None and daily["total_cost"] >= max_day_cost:
        return False, f"Daily cost limit reached (${daily['total_cost']:.4f} / ${max_day_cost:.4f})"

    return True, "OK"


# ---------------------------------------------------------------------------
# Scheduled-runner election
#
# Automations live in the shared database while their project's ``path`` is
# machine-specific, so scheduled-runner ELECTION must map the stored project row
# onto whatever clone THIS install has. The cross-install election helpers
# (``_local_candidate`` / ``_link_group_siblings``, plus
# ``resolve_runnable_local_project`` for the worktree-recreate rebind) live in
# ``app/core/project_links.py``. NOTE: these serve distributed runner election
# / worktree recreation, NOT cwd/adopt resolution (which the origin resolver
# now owns) — that is why they survived PR-B's ladder deletion
# (``resolve_local_project`` did not).
# ---------------------------------------------------------------------------

def should_run_scheduled_here(automation: Dict[str, Any]) -> Tuple[bool, str]:
    """Decide whether THIS install executes a scheduled (cron/interval) fire.

    Every install schedules every enabled automation, so this gate elects
    exactly one runner per fire from shared database state (all online
    installs compute the same winner): the install owning the automation's
    project row when online, else the first online install (lowest id) that
    has a clone of the project. Projectless automations go to the first
    online install. Fails open — running twice beats never running.

    Returns (should_run, reason) where *reason* is for logging.
    """
    me = settings.effective_instance_id
    # Worker network: exactly one owner device fires a definition.
    try:
        from app.network import ownership
        decided = ownership.decide_automation(automation)
    except Exception:  # noqa: BLE001 — fail closed rather than duplicate external work
        logger.exception("Network ownership unavailable; skipping automation fire")
        return False, "network ownership unavailable"
    if decided is not None:
        return decided
    try:
        installs = database.get_installs_map()
    except Exception as e:  # noqa: BLE001 — registry trouble must not kill the fire
        logger.warning("Runner election: install registry unavailable (%s) — running locally", e)
        return True, "install registry unavailable"

    online = {
        iid for iid, row in installs.items()
        if row.get("online") and row.get("approved", True)
    }
    # This code is running, so this install is alive no matter what its
    # heartbeat row says — counting ourselves closes the boundary race where
    # a stale row makes every install defer and nobody fires.
    if installs.get(me, {}).get("approved", True):
        online.add(me)

    project_id = automation.get("project_id")
    if not project_id:
        winner = sorted(online)[0]
        if winner == me:
            return True, "elected runner (projectless)"
        return False, f"install {winner} is the elected runner"

    # V2 placement election: map the automation's project onto its origin-keyed
    # project_v2 and elect from the runners that actually hold an ACTIVE checkout
    # (``capable_runners`` = online ∩ active-checkout holders). resolve_project_v2
    # lazily seeds the base ``checkouts`` row for legacy projects.
    from pathlib import Path

    from app.core import placement
    from app.core.project_resolver import resolve_project_v2
    from app.core.project_utils import get_project_working_dir

    pv2 = resolve_project_v2({"project_id": project_id})
    if not pv2:
        # Legacy project row missing / unresolvable: elect one install to run and
        # fail visibly — N installs producing N identical failed runs per fire
        # would bury the signal.
        winner = sorted(online)[0]
        if winner == me:
            return True, "project unresolvable; elected to surface the error"
        return False, f"project unresolvable; install {winner} will surface the error"

    project = database.get_project(project_id)
    candidates = placement.capable_runners(pv2["id"], {})

    if not candidates:
        # No runner holds an active checkout for this project. Fail open: run
        # wherever the stored project path exists locally (pre-placement behavior).
        working_dir = get_project_working_dir(project) if project else None
        if working_dir and Path(working_dir).is_dir():
            return True, "no capable runner; resolvable locally"
        # Loud + surfaced on the automation, not a failed run: every online
        # install hits this every fire and run-record spam buries the signal.
        logger.warning(
            "Automation %s: no capable runner holds project %s — scheduled fire skipped "
            "everywhere until an install with the project comes online",
            automation.get("id"), project_id,
        )
        if project:
            _record_no_runner(automation, project)
        return False, "no capable runner holds this project"

    # ``candidates`` is already sorted (deterministic across installs). Prefer the
    # legacy owner when it holds a checkout, else the lowest-id capable runner.
    # (Frozen-table READ of ``projects`` for owner preference is deliberate.)
    owner = project.get("instance_id") if project else None
    winner = owner if owner in candidates else candidates[0]
    if winner == me:
        return True, "elected runner"
    return False, f"install {winner} is the elected runner"


def _record_no_runner(automation: Dict[str, Any], project: Dict[str, Any]) -> None:
    """Surface the nobody-can-run-this state on the automation itself."""
    message = (
        f"No online install has project '{project.get('name') or project.get('id')}' — "
        f"scheduled runs are skipped until one comes online."
    )
    if automation.get("last_error") == message:
        return
    try:
        database.update_automation(automation["id"], last_error=message)
    except Exception as e:  # noqa: BLE001 — visibility must not break the gate
        logger.debug("Could not record no-runner state for automation %s: %s", automation.get("id"), e)


# ---------------------------------------------------------------------------
# Webhook secret encryption helpers
# ---------------------------------------------------------------------------

def encrypt_webhook_secret(trigger_config: Dict[str, Any]) -> Dict[str, Any]:
    """Encrypt the webhook secret in trigger_config if present."""
    config = dict(trigger_config)
    secret = config.get("secret")
    if secret and isinstance(secret, str) and not encryption.is_encrypted(secret):
        try:
            config["secret"] = encryption.encrypt_value(secret)
        except Exception:
            raise ValueError("Cannot store webhook secret: credential storage requires local recovery.") from None
    return config


def decrypt_webhook_secret(trigger_config: Dict[str, Any]) -> Dict[str, Any]:
    """Decrypt the webhook secret in trigger_config if present."""
    config = dict(trigger_config)
    secret = config.get("secret")
    if secret and isinstance(secret, str) and encryption.is_encrypted(secret):
        if encryption.is_encryption_ready():
            try:
                config["secret"] = encryption.decrypt_value(secret)
            except Exception as e:
                logger.error(f"Failed to decrypt webhook secret: {e}")
    return config


def reveal_webhook_secrets(automation: Any) -> Any:
    """Return a copy of ``automation`` with its webhook secrets decrypted.

    For owner-only API responses: the edit form shows the secret so the
    owner can sign requests, and ciphertext is useless for that. Covers the
    legacy ``trigger_config`` and every webhook entry in ``triggers``. A
    secret that cannot be decrypted is left as stored, so a save round-trips
    it unchanged instead of rotating it.
    """
    if not isinstance(automation, dict):
        return automation
    revealed = dict(automation)
    if revealed.get("trigger_type") == "webhook" and isinstance(revealed.get("trigger_config"), dict):
        revealed["trigger_config"] = decrypt_webhook_secret(revealed["trigger_config"])
    triggers = revealed.get("triggers")
    if isinstance(triggers, list):
        revealed["triggers"] = [
            {**trig, "config": decrypt_webhook_secret(trig["config"])}
            if isinstance(trig, dict) and trig.get("type") == "webhook" and isinstance(trig.get("config"), dict)
            else trig
            for trig in triggers
        ]
    return revealed


# ---------------------------------------------------------------------------
# Model choice
# ---------------------------------------------------------------------------
#
# Agents do not pin a model; a session gets one when it is created. An
# automation mints its run sessions itself, so it stores the choice the owner
# would otherwise make in the composer: ``model`` holds the picker's value (a
# native Claude alias / ``claude-*`` id, or a ``prov:<providerId>:<modelId>``
# composite) and ``provider_id`` the provider that value binds (NULL = native
# Claude). The run passes ``model`` to ``create_session`` exactly as the
# composer's new-session path does, so every turn of the run resolves from the
# session row.
#
# An automation with no stored choice keeps running the way it always has: the
# session is created unbound and resolution falls back to the agent's legacy
# ``config.model`` / ``config.provider``, then to ``DEFAULT_MODEL``.
# :func:`resolve_automation_model` reports which of those a run would use.

#: ``model_source`` values. ``automation`` / ``step`` are explicit choices;
#: ``agent`` and ``default`` are the legacy fallbacks.
MODEL_SOURCE_STEP = "step"
MODEL_SOURCE_AUTOMATION = "automation"
MODEL_SOURCE_AGENT = "agent"
MODEL_SOURCE_DEFAULT = "default"


def normalize_model_choice(
    model: Optional[str],
    provider_id: Optional[str] = None,
    *,
    stored: Iterable[Optional[str]] = (),
) -> Tuple[Optional[str], Optional[str]]:
    """Canonicalize a requested ``(model, provider_id)`` pick.

    Returns ``(selection, provider_id)`` where ``selection`` is the picker
    value stored on the automation and passed to ``create_session``. Both
    empty means "no choice" and returns ``(None, None)``. Raises
    ``ValueError`` with a plain-language reason when the pick is malformed or
    not available right now.

    ``stored`` lists selections already saved on the automation. Re-sending one
    of them unchanged skips the availability check: the edit form sends every
    field on save, and an unrelated edit must not be refused because the saved
    model went away (the run reports that instead).
    """
    from app.core.query_options import decode_provider_model

    clean_model = (model or "").strip()
    clean_provider = (provider_id or "").strip()
    if not clean_model:
        if clean_provider:
            raise ValueError("Pick a model for that provider.")
        return None, None

    decoded = decode_provider_model(clean_model)
    if decoded is not None:
        pid, mid = decoded
        if clean_provider and clean_provider != pid:
            raise ValueError(
                f"The model '{mid}' belongs to provider '{pid}', not '{clean_provider}'."
            )
        selection, bound = clean_model, pid
    elif clean_provider:
        selection, bound = f"prov:{clean_provider}:{clean_model}", clean_provider
    else:
        selection, bound = clean_model, None

    if selection in {s for s in stored if s}:
        return selection, bound
    problem = model_choice_problem(selection)
    if problem:
        raise ValueError(problem)
    return selection, bound


class ModelLookups:
    """Agent and provider reads for model resolution.

    One instance answers repeated lookups from memory. ``prefetch()`` loads
    both tables in two queries, so resolving a whole list of automations costs
    the same no matter how many there are.
    """

    def __init__(self) -> None:
        self._profiles: Dict[str, Any] = {}
        self._providers: Dict[str, Any] = {}
        self._complete = False

    def prefetch(self) -> "ModelLookups":
        self._profiles = {p["id"]: p for p in database.get_all_profiles()}
        self._providers = {
            p["id"]: p for p in database.get_all_model_providers(include_token=False)
        }
        self._complete = True
        return self

    def profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        if self._complete or profile_id in self._profiles:
            return self._profiles.get(profile_id)
        row = database.get_profile(profile_id)
        self._profiles[profile_id] = row
        return row

    def provider(self, provider_id: str) -> Optional[Dict[str, Any]]:
        if self._complete or provider_id in self._providers:
            return self._providers.get(provider_id)
        row = database.get_model_provider(provider_id, include_token=False)
        self._providers[provider_id] = row
        return row


def model_choice_problem(
    selection: str, lookups: Optional[ModelLookups] = None
) -> Optional[str]:
    """Why a stored model choice cannot run right now, or ``None`` if it can.

    A provider pick must name an enabled provider that still lists the model
    (the same checks the composer's model override gets at turn time). A bare
    pick must be a native Claude model, since that is the only leg a bare id
    reaches.
    """
    from app.core.query_options import decode_provider_model, is_native_claude_model

    decoded = decode_provider_model(selection)
    if decoded is not None:
        from app.core.provider_agent import validate_provider_model

        pid, mid = decoded
        if lookups is not None:
            validation = validate_provider_model(
                pid, mid, provider_row=lookups.provider(pid), require_servable=False
            )
        else:
            validation = validate_provider_model(pid, mid, require_servable=False)
        if not validation.ok:
            return validation.reason or f"Model '{mid}' is not available on provider '{pid}'."
        return None
    if not is_native_claude_model(selection):
        return (
            f"'{selection}' is not a Claude model. Pick a provider model "
            "instead (choose it from that provider's list)."
        )
    return None


def split_model_selection(selection: str) -> Tuple[str, Optional[str]]:
    """``(model_id, provider_id)`` for a picker value."""
    from app.core.query_options import decode_provider_model

    decoded = decode_provider_model(selection)
    if decoded is not None:
        return decoded[1], decoded[0]
    return selection, None


def resolve_automation_model(
    automation: Dict[str, Any],
    step: Optional[Dict[str, Any]] = None,
    lookups: Optional[ModelLookups] = None,
) -> Dict[str, Any]:
    """The model and provider a run (or a ``send_prompt`` step) would use.

    Returns a dict with:

    - ``selection``: the explicit picker value to stamp on the run's session,
      or ``None`` when there is no explicit choice (fallback);
    - ``model`` / ``provider_id``: the effective model id and provider
      (``provider_id`` ``None`` = native Claude; ``model`` may be ``None`` for
      a provider-bound agent with no saved model, which then runs on the
      provider's own default);
    - ``source``: ``step`` / ``automation`` (explicit) or ``agent`` /
      ``default`` (fallback);
    - ``error``: a plain-language reason when an explicit choice is no longer
      available, else ``None``. A run with an error must fail rather than fall
      through to another model.

    Blocking (profile/provider reads) — call off the event loop.
    """
    lookups = lookups or ModelLookups()
    step = step or {}
    step_model = (step.get("model") or "").strip()
    automation_model = (automation.get("model") or "").strip()
    if step_model:
        selection, source = step_model, MODEL_SOURCE_STEP
    elif automation_model:
        selection, source = automation_model, MODEL_SOURCE_AUTOMATION
    else:
        selection, source = None, None

    if selection:
        model_id, provider_id = split_model_selection(selection)
        problem = model_choice_problem(selection, lookups)
        return {
            "selection": selection,
            "model": model_id,
            "provider_id": provider_id,
            "source": source,
            "error": (
                f"This automation's model is no longer available: {problem} "
                "Pick another model in the automation's settings."
            ) if problem else None,
        }

    # Fallback: mirror what an unbound session resolves to
    # (query_options.build_options_from_profile): the agent's legacy
    # config.provider (stamped by create_session) and config.model, then the
    # app default for a native agent with nothing saved.
    profile_id = step.get("profile_id") or automation.get("profile_id")
    config: Dict[str, Any] = {}
    if profile_id:
        try:
            profile = lookups.profile(profile_id)
            config = (profile or {}).get("config") or {}
        except Exception:  # noqa: BLE001 — reporting only; the run resolves for real
            logger.debug("Could not read agent %s for model reporting", profile_id, exc_info=True)
    provider_id = (config.get("provider") or "").strip() or None
    model_id = (config.get("model") or "").strip() or None
    if model_id:
        return {"selection": None, "model": model_id, "provider_id": provider_id,
                "source": MODEL_SOURCE_AGENT, "error": None}
    if provider_id:
        from app.core.provider_agent import default_model_for_provider

        row = None
        try:
            row = lookups.provider(provider_id)
        except Exception:  # noqa: BLE001
            logger.debug("Could not read provider %s for model reporting", provider_id, exc_info=True)
        return {"selection": None, "model": default_model_for_provider(row),
                "provider_id": provider_id, "source": MODEL_SOURCE_AGENT, "error": None}
    from app.core.model_registry import DEFAULT_MODEL

    return {"selection": None, "model": DEFAULT_MODEL, "provider_id": None,
            "source": MODEL_SOURCE_DEFAULT, "error": None}


def with_model_resolution(automation: Any, lookups: Optional[ModelLookups] = None) -> Any:
    """Attach the effective model fields the API reports for an automation.

    Pass one prefetched :class:`ModelLookups` when presenting a list.
    Blocking — call off the event loop.
    """
    if not isinstance(automation, dict):
        return automation
    resolved = resolve_automation_model(automation, lookups=lookups)
    return {
        **automation,
        "effective_model": resolved["model"],
        "effective_provider_id": resolved["provider_id"],
        "model_source": resolved["source"],
        "model_error": resolved["error"],
    }


def _normalize_step_models(
    action_steps: Optional[List[Dict[str, Any]]],
    stored_steps: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Canonicalize each ``send_prompt`` step's optional model pick in place.

    ``stored_steps`` are the automation's saved steps; a model already used by
    one of them is accepted unchanged (see :func:`normalize_model_choice`).
    """
    stored = [
        s.get("model") for s in (stored_steps or []) if isinstance(s, dict)
    ]
    for i, step in enumerate(action_steps or []):
        if not isinstance(step, dict) or step.get("type") != "send_prompt":
            continue
        if not (step.get("model") or step.get("provider_id")):
            step.pop("model", None)
            step.pop("provider_id", None)
            continue
        try:
            selection, provider_id = normalize_model_choice(
                step.get("model"), step.get("provider_id"), stored=stored
            )
        except ValueError as e:
            raise ValueError(f"Action step {i + 1}: {e}") from None
        step["model"] = selection
        step["provider_id"] = provider_id


def model_choice_errors_for_run(automation: Dict[str, Any]) -> Optional[str]:
    """The first unavailable explicit model choice a run would hit, or ``None``.

    Checked before the run starts anything, so a removed provider or model
    fails the run with a clear reason instead of half-running it.
    Blocking — call off the event loop.
    """
    if (automation.get("prompt") or "").strip():
        error = resolve_automation_model(automation)["error"]
        if error:
            return error
    for i, step in enumerate(automation.get("action_steps") or []):
        if not isinstance(step, dict) or step.get("type") != "send_prompt":
            continue
        resolved = resolve_automation_model(automation, step)
        if resolved["error"]:
            if resolved["source"] == MODEL_SOURCE_STEP:
                return f"Action step {i + 1}: {resolved['error']}"
            return resolved["error"]
    return None


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

async def create_automation(
    name: str,
    trigger_type: str,
    trigger_config: Dict[str, Any],
    profile_id: str,
    prompt: Optional[str] = None,
    project_id: Optional[str] = None,
    description: Optional[str] = None,
    output_config: Optional[Dict[str, Any]] = None,
    max_turns: Optional[int] = 1,
    timeout_seconds: Optional[int] = 300,
    max_cost_per_run: Optional[float] = None,
    max_cost_per_day: Optional[float] = None,
    max_cost_total: Optional[float] = None,
    max_runs_per_day: Optional[int] = None,
    requires_approval: bool = False,
    action_steps: Optional[List[Dict[str, Any]]] = None,
    triggers: Optional[List[Dict[str, Any]]] = None,
    enabled: bool = False,
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
) -> Dict:
    """Create a new automation.

    ``model`` / ``provider_id`` are the optional model choice for its runs (see
    :func:`normalize_model_choice`); omitted, runs fall back to the agent's
    saved model or the app default.

    ``enabled`` sets the initial ``is_enabled`` state (default ``False`` to
    preserve the create-then-enable UI flow). When ``True`` and the automation
    has a cron/interval trigger, it is scheduled immediately so it fires without
    waiting on the periodic reconcile.

    Returns the automation dict.
    """
    # Normalize triggers array
    if not triggers:
        triggers = [{"type": trigger_type, "config": trigger_config}]

    # Validate each trigger
    for i, trig in enumerate(triggers):
        t_type = trig.get("type")
        if t_type not in VALID_AUTOMATION_TRIGGER_TYPES:
            raise ValueError(f"Invalid trigger type at index {i}: {t_type}")
        t_config = trig.get("config", {})
        if t_type == "interval":
            interval_seconds = t_config.get("seconds", 0)
            if interval_seconds < 60:
                raise ValueError(f"Interval trigger at index {i} must be at least 60 seconds")
        if t_type == "webhook":
            if not t_config.get("secret"):
                t_config["secret"] = secrets.token_urlsafe(32)
            trig["config"] = encrypt_webhook_secret(t_config)

    # Set primary trigger from first in array
    trigger_type = triggers[0]["type"]
    trigger_config = triggers[0]["config"]

    def _create_blocking() -> Tuple[str, Optional[Dict]]:
        """Validate-then-insert, in ONE thread hop and ONE atomic block.

        The two existence checks and the INSERT are contiguous (no awaits
        between them), so they run as a unit off the loop instead of three
        blocking round trips. The ``ValueError``s raised here propagate out of
        the ``await`` to the caller exactly as they did when this ran inline.
        """
        # Validate profile exists
        profile = database.get_profile(profile_id)
        if not profile:
            raise ValueError(f"Profile not found: {profile_id}")

        # Validate project exists if specified
        if project_id:
            project = database.get_project(project_id)
            if not project:
                raise ValueError(f"Project not found: {project_id}")

        selection, bound_provider = normalize_model_choice(model, provider_id)
        _normalize_step_models(action_steps)

        # Generate automation ID
        automation_id = f"auto-{secrets.token_hex(4)}"

        return automation_id, database.create_automation(
            automation_id=automation_id,
            name=name,
            trigger_type=trigger_type,
            trigger_config=trigger_config,
            triggers=triggers,
            prompt=prompt,
            profile_id=profile_id,
            project_id=project_id,
            description=description,
            output_config=output_config,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            max_cost_per_run=max_cost_per_run,
            max_cost_per_day=max_cost_per_day,
            max_cost_total=max_cost_total,
            max_runs_per_day=max_runs_per_day,
            requires_approval=requires_approval,
            action_steps=action_steps,
            enabled=enabled,
            model=selection,
            provider_id=bound_provider,
        )

    automation_id, automation = await run_sync(_create_blocking)

    if not automation:
        raise RuntimeError("Failed to create automation in database")

    # Schedule immediately when created enabled with a cron/interval trigger,
    # so it fires without waiting for the periodic reconcile loop. Event-driven
    # triggers (webhook/connector) need no APScheduler job.
    if automation.get("is_enabled"):
        has_scheduled = any(
            t.get("type") in ("cron", "interval") for t in (automation.get("triggers") or [])
        )
        if has_scheduled:
            try:
                from app.core.automation_manager import automation_manager
                await automation_manager.schedule_automation(automation_id)
            except Exception as e:  # noqa: BLE001 — reconcile loop is the backstop
                logger.warning(f"Automation {automation_id} created enabled but scheduling failed: {e}")

    return automation


async def update_automation(automation_id: str, **kwargs) -> Optional[Dict]:
    """Update an existing automation.

    If trigger config changes and the automation is enabled, it will be rescheduled.
    """
    def _update_blocking() -> Tuple[Optional[Dict], bool]:
        """The whole read-validate-write sequence, in ONE thread hop.

        Up to four contiguous round trips (the existing row, the two optional
        existence checks, the UPDATE) with nothing but pure validation and
        secret encryption between them — so they stay a single unit off the
        loop. Keeping the read and the write in one block also preserves the
        original ordering: the row is re-read, validated and only then written.
        ``ValueError`` still propagates to the caller unchanged. Returns
        ``(updated_row_or_None, trigger_changed)``; a missing row yields
        ``(None, False)``, the same ``None`` the caller returned before.
        """
        existing = database.get_automation(automation_id)
        if not existing:
            return None, False

        # Validate profile if being changed
        if "profile_id" in kwargs and kwargs["profile_id"]:
            profile = database.get_profile(kwargs["profile_id"])
            if not profile:
                raise ValueError(f"Profile not found: {kwargs['profile_id']}")

        # Validate project if being changed
        if "project_id" in kwargs and kwargs["project_id"]:
            project = database.get_project(kwargs["project_id"])
            if not project:
                raise ValueError(f"Project not found: {kwargs['project_id']}")

        # Model choice: either field may be sent alone. A model sent without a
        # provider is a complete pick (a native id or a ``prov:`` composite),
        # so it replaces the stored provider; a provider alone re-binds the
        # stored model id to it. Empty model clears the choice (fallback).
        if "model" in kwargs or "provider_id" in kwargs:
            if "model" in kwargs:
                req_model, req_provider = kwargs.get("model"), kwargs.get("provider_id")
            else:
                req_model = split_model_selection(existing.get("model") or "")[0] if existing.get("model") else None
                req_provider = kwargs.get("provider_id")
            kwargs["model"], kwargs["provider_id"] = normalize_model_choice(
                req_model, req_provider, stored=(existing.get("model"),)
            )
        if kwargs.get("action_steps") is not None:
            _normalize_step_models(kwargs["action_steps"], existing.get("action_steps"))

        # Handle triggers array — validate and encrypt webhook secrets
        trigger_changed = False
        if "triggers" in kwargs and kwargs["triggers"] is not None:
            for i, trig in enumerate(kwargs["triggers"]):
                t_type = trig.get("type")
                if t_type and t_type not in VALID_AUTOMATION_TRIGGER_TYPES:
                    raise ValueError(f"Invalid trigger type at index {i}: {t_type}")
                t_config = trig.get("config", {})
                if t_type == "interval" and t_config.get("seconds", 0) < 60:
                    raise ValueError(f"Interval trigger at index {i} must be at least 60 seconds")
                if t_type == "webhook":
                    if not t_config.get("secret"):
                        t_config["secret"] = secrets.token_urlsafe(32)
                    trig["config"] = encrypt_webhook_secret(t_config)
            trigger_changed = True

        # Encrypt webhook secret if trigger config is being updated (legacy path)
        if "trigger_config" in kwargs and kwargs["trigger_config"] is not None:
            trigger_type = kwargs.get("trigger_type", existing["trigger_type"])
            if trigger_type == "webhook":
                kwargs["trigger_config"] = encrypt_webhook_secret(kwargs["trigger_config"])
            trigger_changed = True

        if "trigger_type" in kwargs:
            trigger_changed = True

        return database.update_automation(automation_id, **kwargs), trigger_changed

    updated, trigger_changed = await run_sync(_update_blocking)
    if not updated:
        return None

    # Reschedule if the automation is enabled and trigger changed
    if trigger_changed and updated.get("is_enabled"):
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.schedule_automation(automation_id)
        except Exception as e:
            logger.error(f"Failed to reschedule automation {automation_id}: {e}")

    return updated


async def delete_automation(automation_id: str) -> bool:
    """Delete an automation, stop it if running."""
    # Two hops, not one: the unschedule between the read and the DELETE is a
    # genuine await that must stay on the loop (APScheduler job removal).
    automation = await run_sync(database.get_automation, automation_id)
    if not automation:
        return False

    # Unschedule if enabled
    if automation.get("is_enabled"):
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.unschedule_automation(automation_id)
        except Exception as e:
            logger.error(f"Error unscheduling automation {automation_id}: {e}")

    return await run_sync(database.delete_automation, automation_id)


async def test_automation(automation_id: str) -> Dict:
    """Dry-run: render prompt and check budget, but do NOT execute.

    Returns ``{"prompt_rendered": str, "budget_ok": bool, "message": str}``.
    """
    def _test_blocking() -> Dict:
        """Row read + budget check in ONE hop. ``check_budget`` is itself a
        blocking read (the daily-stats aggregate), so offloading only the
        ``get_automation`` here would have left half the stall behind."""
        automation = database.get_automation(automation_id)
        if not automation:
            return {"prompt_rendered": "", "budget_ok": False, "message": "Automation not found"}

        # Render prompt with empty trigger data to show what it looks like
        rendered = render_prompt(automation.get("prompt") or "", {"event": {}, "payload": {}})

        # Check budget
        ok, reason = check_budget(automation)

        return {
            "prompt_rendered": rendered,
            "budget_ok": ok,
            "message": reason,
        }

    return await run_sync(_test_blocking)


def get_automation_stats(automation_id: str) -> Dict:
    """Get statistics for an automation."""
    automation = database.get_automation(automation_id)
    if not automation:
        return {}

    daily = database.get_automation_daily_stats(automation_id)

    # Get all-time stats via SQL aggregates (avoids loading all rows into memory)
    agg = database.get_automation_aggregate_stats(automation_id)

    return {
        "automation_id": automation_id,
        "total_runs": agg["total_runs"],
        "successful_runs": agg["successful_runs"],
        "failed_runs": agg["failed_runs"],
        "total_cost_usd": agg["total_cost_usd"],
        "avg_duration_ms": agg["avg_duration_ms"],
        "today_runs": daily["run_count"],
        "today_cost_usd": daily["total_cost"],
    }
