"""
AutomationManager — scheduler integration, event listener, execution lifecycle.

Singleton that manages the APScheduler instance, handles trigger firing,
budget enforcement, execution, and output delivery. Prompted runs execute
as normal streaming chat sessions (see _run_prompt_as_session).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple


from app.core import turn_outcome
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

# APScheduler 3.x numbers day_of_week Monday=0, while crontab (and the UI,
# docs and MCP tool examples) number it Sunday=0 with 7 also meaning Sunday.
_CRON_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _crontab_dow(token: str) -> Optional[int]:
    """A day-of-week token as crontab's number (Sunday=0), or None."""
    if token.isdigit():
        return int(token)
    name = token.lower()[:3]
    return _CRON_DOW_NAMES.index(name) if len(token) >= 3 and name in _CRON_DOW_NAMES else None


def crontab_day_of_week(field: str) -> str:
    """Translate a crontab day-of-week field into APScheduler's syntax.

    Values follow crontab semantics (0 and 7 = Sunday, ``1-5`` = Monday to
    Friday) and are expanded into explicit weekday names, so the result never
    depends on APScheduler's Monday=0 numbering. Name ranges are expanded the
    same way (``sun-thu`` = Sunday to Thursday) and may wrap past Saturday
    (``fri-mon``). Bare ``*`` passes through unchanged.
    """
    field = field.strip()
    if field == "*":
        return field
    out: List[str] = []
    for item in field.split(","):
        base, _, step_s = item.partition("/")
        wraps = False
        if base == "*":
            lo, hi = 0, 6
        elif "-" in base:
            a, _, b = base.partition("-")
            lo, hi = _crontab_dow(a), _crontab_dow(b)
            if lo is None or hi is None:
                raise ValueError(f"invalid day-of-week value: {item!r}")
            wraps = not (a.isdigit() and b.isdigit())
        else:
            lo = _crontab_dow(base)
            if lo is None:
                raise ValueError(f"invalid day-of-week value: {item!r}")
            hi = 7 if step_s else lo  # crontab: "N/S" means N-7/S
        if step_s and not step_s.isdigit():
            raise ValueError(f"invalid day-of-week step: {item!r}")
        step = int(step_s) if step_s else 1
        if wraps and lo > hi:
            hi += 7  # a named range past Saturday: fri-mon
        if not (0 <= lo <= 7 and 0 <= hi <= 14 and lo <= hi and step > 0) or (hi > 7 and not wraps):
            raise ValueError(f"invalid day-of-week value: {item!r}")
        for n in range(lo, hi + 1, step):
            name = _CRON_DOW_NAMES[n % 7]
            if name not in out:
                out.append(name)
    return ",".join(out)

# Automations are shared across installs but APScheduler jobs are per-process,
# so each install periodically re-syncs its jobs from the database to pick up
# automations created, changed, or deleted on other installs.
SCHEDULE_RECONCILE_SECONDS = 60

# How long an approval stays good for the restart re-queue. An approved run a
# shutdown cancelled is started again only when it was approved within this
# window; an older one is stamped cancelled instead of surprising the owner
# with a run they approved days ago (the first start after an upgrade would
# otherwise run every run ever left ``approved``).
APPROVED_RUN_REQUEUE_MAX_AGE_SECONDS = 24 * 3600


def _parse_utc(value: Any) -> Optional[datetime]:
    """A stored timestamp (datetime on PostgreSQL, ISO text on SQLite) as aware UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class _ShutdownBeforeTurn(Exception):
    """Shutdown began after the run moved to ``running`` but before its turn
    was queued: the run is settled ``cancelled`` instead of leaving a queued
    turn nobody watches (it would run unattended on the next boot)."""


class AutomationManager:
    """Central hub for automation scheduling, execution, and output delivery."""

    def __init__(self):
        self._scheduler = None
        self._running: Dict[str, asyncio.Task] = {}  # automation_id → running task
        self._lock = asyncio.Lock()
        self._initialized = False
        self._scheduled_versions: Dict[str, str] = {}  # automation_id → trigger fingerprint
        self._reconcile_task: Optional[asyncio.Task] = None
        # Approved runs queued behind another run of the same automation.
        # They hold no latch entry, so shutdown cancels them from here.
        self._waiting_approved: Set[asyncio.Task] = set()
        self._shutting_down = False
        self._requeue_task: Optional[asyncio.Task] = None

    def _create_tracked_task(self, coro, *, name: str) -> asyncio.Task:
        """Create a background task with error logging on failure."""
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._on_task_done)
        return task

    def _shutdown_begun(self) -> bool:
        """Whether shutdown has begun, app-wide or for this manager.

        The app flags ``startup.is_shutting_down`` first, then stops running
        sessions, and reaches ``shutdown()`` (which sets ``_shutting_down``)
        last. A run started in between would mint a session and a queued turn
        the scheduler no longer starts, so every point where a run can move to
        ``running`` checks both.
        """
        from app.core import startup

        return self._shutting_down or bool(getattr(startup, "is_shutting_down", False))

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("Background task %s failed: %s", task.get_name(), exc, exc_info=exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize scheduler and load enabled automations."""
        if self._initialized:
            return
        self._shutting_down = False
        # Approved runs a shutdown cancelled while they waited kept their
        # ``approved`` row; nothing else starts them again.
        self._requeue_task = self._create_tracked_task(
            self._requeue_approved_runs(), name="automation-requeue-approved"
        )

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            self._scheduler = AsyncIOScheduler()
            self._scheduler.start()
            logger.info("Automation scheduler started")
        except ImportError:
            logger.warning("APScheduler not available — automation scheduling disabled")
            self._initialized = True
            return

        # Load and schedule enabled automations
        try:
            await self._reconcile_schedules()
        except Exception as e:
            logger.error(f"Failed to schedule automations at startup: {e}")
        if self._scheduled_versions:
            logger.info(f"Scheduled {len(self._scheduled_versions)} automation(s)")

        self._reconcile_task = self._create_tracked_task(
            self._reconcile_loop(), name="automation-schedule-reconcile"
        )

        self._initialized = True

    async def shutdown(self) -> None:
        """Cancel all running tasks and shutdown scheduler."""
        if self._reconcile_task:
            self._reconcile_task.cancel()
            self._reconcile_task = None
        if self._requeue_task:
            self._requeue_task.cancel()
            self._requeue_task = None

        # Cancel running tasks, and the approved runs queued behind them: a
        # queued run would otherwise wake to a free latch and start now.
        async with self._lock:
            self._shutting_down = True
            for auto_id, task in self._running.items():
                task.cancel()
                logger.debug(f"Cancelled running automation {auto_id}")
            self._running.clear()
            for task in self._waiting_approved:
                task.cancel()
            self._waiting_approved.clear()

        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("Automation scheduler shut down")

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    @staticmethod
    def _trigger_version(automation: Dict[str, Any]) -> str:
        """Fingerprint of an automation's trigger config, for reconcile diffing.

        Deliberately NOT ``updated_at``: every run completion touches that
        column, and re-adding an unchanged IntervalTrigger job resets its
        next-fire time — reconciling on it would push interval automations
        back one interval after every run.
        """
        return json.dumps(automation.get("triggers") or [], sort_keys=True, default=str)

    async def _reconcile_loop(self) -> None:
        """Periodically re-sync APScheduler jobs with the shared database.

        Without this, an automation created or re-enabled on another install
        never gets a job here — and if the runner election picks this install
        (e.g. it's the only one with the project), the automation never fires.
        """
        while True:
            await asyncio.sleep(SCHEDULE_RECONCILE_SECONDS)
            try:
                await self._reconcile_schedules()
            except Exception as e:  # noqa: BLE001 — the loop must survive DB blips
                logger.warning(f"Automation schedule reconcile failed: {e}")

    async def _reconcile_schedules(self) -> None:
        """Diff enabled automations against local jobs; apply the changes."""
        if not self._scheduler:
            return

        want: Dict[str, str] = {}
        # One read off the loop; the scheduling below awaits per automation, so
        # nothing further folds into this hop.
        for auto in await run_sync(database.get_enabled_automations):
            triggers = auto.get("triggers") or []
            if any(t.get("type") in ("cron", "interval") for t in triggers):
                want[auto["id"]] = self._trigger_version(auto)

        for auto_id, version in want.items():
            if self._scheduled_versions.get(auto_id) != version:
                try:
                    await self.schedule_automation(auto_id)
                except Exception as e:
                    logger.error(f"Failed to schedule automation {auto_id}: {e}")

        for auto_id in list(self._scheduled_versions):
            if auto_id not in want:
                logger.info(f"Automation {auto_id} disabled or deleted — removing local schedule")
                await self.unschedule_automation(auto_id)

    def _remove_automation_jobs(self, automation_id: str) -> None:
        """Remove all APScheduler jobs for an automation."""
        if not self._scheduler:
            return
        for job in self._scheduler.get_jobs():
            if job.id == automation_id or job.id.startswith(f"{automation_id}__"):
                try:
                    self._scheduler.remove_job(job.id)
                except (KeyError, LookupError):
                    pass

    async def schedule_automation(self, automation_id: str) -> None:
        """Add or update APScheduler jobs for all cron/interval triggers in the automation."""
        if not self._scheduler:
            return

        # Single read; everything after it is APScheduler bookkeeping that
        # belongs on this loop.
        automation = await run_sync(database.get_automation, automation_id)
        if not automation:
            return

        # A disabled automation must never hold scheduler jobs: the reconcile
        # loop only loads enabled automations, so it would tear any job back
        # down within the interval anyway. No-op here so the direct path agrees
        # with reconcile and no caller can believe a disabled automation is live.
        if not automation.get("is_enabled"):
            logger.debug(
                "schedule_automation(%s): automation is disabled — not scheduling", automation_id
            )
            self._remove_automation_jobs(automation_id)
            self._scheduled_versions.pop(automation_id, None)
            return

        # Remove all existing jobs for this automation
        self._remove_automation_jobs(automation_id)

        triggers = automation.get("triggers") or []
        if any(t.get("type") in ("cron", "interval") for t in triggers):
            self._scheduled_versions[automation_id] = self._trigger_version(automation)
        else:
            self._scheduled_versions.pop(automation_id, None)
        for i, trig in enumerate(triggers):
            t_type = trig.get("type")
            t_config = trig.get("config", {})
            job_id = f"{automation_id}__trigger_{i}"

            # Event-driven trigger types don't need APScheduler jobs
            if t_type in ("connector_event", "connector_trigger", "webhook"):
                continue

            if t_type == "cron":
                from apscheduler.triggers.cron import CronTrigger
                expression = t_config.get("expression", "")
                timezone = t_config.get("timezone", "UTC")
                try:
                    parts = expression.strip().split()
                    if len(parts) != 5:
                        logger.error(f"Invalid cron expression for {automation_id} trigger {i}: {expression}")
                        continue
                    trigger = CronTrigger(
                        minute=parts[0], hour=parts[1],
                        day=parts[2], month=parts[3],
                        day_of_week=crontab_day_of_week(parts[4]),
                        timezone=timezone,
                    )
                    self._scheduler.add_job(
                        self._on_schedule_trigger,
                        trigger,
                        id=job_id,
                        args=[automation_id, t_type, t_config],
                        replace_existing=True,
                    )
                    logger.info(f"Scheduled cron trigger {i} for {automation_id}: {expression} ({timezone})")
                except Exception as e:
                    logger.error(f"Failed to create cron trigger {i} for {automation_id}: {e}")

            elif t_type == "interval":
                from apscheduler.triggers.interval import IntervalTrigger
                interval_seconds = t_config.get("seconds", 3600)
                try:
                    trigger = IntervalTrigger(seconds=interval_seconds)
                    self._scheduler.add_job(
                        self._on_schedule_trigger,
                        trigger,
                        id=job_id,
                        args=[automation_id, t_type, t_config],
                        replace_existing=True,
                    )
                    logger.info(f"Scheduled interval trigger {i} for {automation_id}: every {interval_seconds}s")
                except Exception as e:
                    logger.error(f"Failed to create interval trigger {i} for {automation_id}: {e}")

    async def unschedule_automation(self, automation_id: str) -> None:
        """Remove all APScheduler jobs for an automation."""
        self._remove_automation_jobs(automation_id)
        self._scheduled_versions.pop(automation_id, None)
        logger.info(f"Unscheduled automation {automation_id}")

    # ------------------------------------------------------------------
    # Trigger handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _fire_bucket(
        trigger_type: str, trigger_config: Optional[Dict[str, Any]], fired_at: datetime
    ) -> str:
        """Deterministic identity of one scheduled tick, identical on every install.

        The shared ``automation_fires`` claim keys on this, so it must name the
        SCHEDULED fire time rather than each install's wall-clock call time:

        * cron jobs fire on minute boundaries — rounding the callback time to
          the nearest minute recovers the scheduled tick even when the event
          loop delivers the callback late (or clocks are skewed) by <30s.
        * interval jobs have per-install phase (each scheduler starts at a
          different moment), so the tick is the epoch-aligned window the fire
          lands in — every install with the same interval config computes the
          same window.
        """
        if trigger_type == "interval":
            seconds = max(int((trigger_config or {}).get("seconds") or 3600), 60)
            window_start = int(fired_at.timestamp()) // seconds * seconds
            window_iso = datetime.fromtimestamp(window_start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return f"interval:{seconds}:{window_iso}"
        minute = (fired_at + timedelta(seconds=30)).replace(second=0, microsecond=0)
        return f"cron:{minute.strftime('%Y-%m-%dT%H:%MZ')}"

    async def _on_schedule_trigger(
        self,
        automation_id: str,
        trigger_type: str = "cron",
        trigger_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Called by APScheduler when a cron/interval job fires.

        Automations are shared across installs (one database), so every online
        install fires this job — the runner election picks exactly one to
        execute, preferring the install that has the automation's project. The
        election reads mutable registry state and deliberately fails open, so
        an atomic shared-database fire claim backs it up: exactly one install
        can insert the (automation_id, fire_bucket) row, and only that winner
        executes the tick.
        """
        if self._shutdown_begun():
            # Before the cross-install fire claim, so a live install can still
            # take this tick.
            logger.info(f"Automation {automation_id}: shutting down — scheduled fire skipped")
            return

        def _gate_blocking() -> Tuple[Optional[Dict[str, Any]], bool, str]:
            """Row read + runner election in ONE hop. ``should_run_scheduled_here``
            is itself a chain of blocking reads (install registry, project,
            placement), so offloading only ``get_automation`` would have left
            the bigger stall on the loop. The stale-schedule teardown stays on
            the caller — it drives APScheduler."""
            automation = database.get_automation(automation_id)
            if not automation:
                return None, False, ""
            from app.core.automation_service import should_run_scheduled_here
            return (automation, *should_run_scheduled_here(automation))

        try:
            automation, should_run, reason = await run_sync(_gate_blocking)
            if not automation:
                # Deleted (possibly on another install) while our job survived.
                logger.warning(f"Automation {automation_id} no longer exists — removing stale schedule")
                await self.unschedule_automation(automation_id)
                return

            if not should_run:
                logger.debug(f"Automation {automation_id}: scheduled fire skipped on this install ({reason})")
                return
        except Exception as e:
            # Fail open: a transient DB/registry blip must not silently drop
            # the fire — _execute records its own failure if things are broken.
            logger.warning(f"Automation {automation_id}: scheduled-fire gate failed ({e}) — running locally")

        fired_at = datetime.now(timezone.utc)
        fire_bucket = self._fire_bucket(trigger_type, trigger_config, fired_at)

        def _claim_blocking() -> bool:
            """The atomic cross-install fire claim and, only when we win it, the
            fire-table prune — ONE hop for both. The prune ran immediately after
            the claim's ``if not claimed: return`` before, so it still runs
            exactly when the claim is won, with the same fail-open handling."""
            try:
                claimed = database.claim_automation_fire(automation_id, fire_bucket)
            except Exception as e:  # noqa: BLE001 — claim trouble must not drop the fire
                # Same fail-open stance as the election: if the claim table is
                # unreachable here it is unreachable on the other installs too.
                logger.warning(f"Automation {automation_id}: fire claim {fire_bucket} failed ({e}) — running locally")
                claimed = True
            if claimed:
                try:
                    database.prune_automation_fires()
                except Exception:  # noqa: BLE001 — hygiene must never block a fire
                    pass
            return claimed

        claimed = await run_sync(_claim_blocking)
        if not claimed:
            logger.info(
                f"Automation {automation_id}: fire {fire_bucket} already claimed by another install — skipping"
            )
            return

        # The claimed tick stays consumed even if _execute's budget check then
        # blocks the run: budget counters live in the shared database, so a
        # fire that is over budget here is over budget on every install.
        trigger_data = {"trigger": "schedule", "fired_at": fired_at.isoformat()}
        self._create_tracked_task(self._execute(automation_id, trigger_data), name=f"auto-schedule-{automation_id}")

    async def handle_connector_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Check for automations triggered by connector events and fire them."""
        # Lifecycle delivery includes automation-owned turns too, for webhook
        # observers. Do not let those events recursively start more model runs;
        # intentional automation chaining belongs in explicit action steps.
        if data.get("automation_id") or data.get("source") in ("automation", "automation_step"):
            return
        automations = await run_sync(
            database.get_automations_for_event,
            trigger_type="connector_event",
            event_type=event_type,
        )
        # The trigger is configured against a connector, so it fires only for
        # sessions a connector is linked to. Every turn on every session emits
        # lifecycle events now; without this gate an unfiltered trigger would
        # start a run after each wake, queue and subagent turn app-wide.
        connector_ids = set(data.get("connector_ids") or [])
        if data.get("connector_id"):
            connector_ids.add(data["connector_id"])
        if not connector_ids:
            return
        for auto in automations:
            triggers = auto.get("triggers") or [{
                "type": "connector_event", "config": auto.get("trigger_config") or {},
            }]
            # Match a single trigger's event, connector and filter together.
            # Multiple matching triggers still create only one run.
            matches = False
            for trigger in triggers:
                config = trigger.get("config") or {}
                if trigger.get("type") != "connector_event" or config.get("event_type", event_type) != event_type:
                    continue
                if config.get("connector_id") and config["connector_id"] not in connector_ids:
                    continue
                if all(data.get(k) == v for k, v in (config.get("filter") or {}).items()):
                    matches = True
                    break
            if not matches:
                continue

            trigger_data = {"trigger": "connector_event", "event_type": event_type, "event": data}
            self._create_tracked_task(self._execute(auto["id"], trigger_data), name=f"auto-event-{auto['id']}")

    async def handle_connector_trigger(self, connector_id: str, trigger_id: str, data: Dict[str, Any]) -> None:
        """Fire automations that match a specific connector trigger."""
        automations = await run_sync(
            database.get_automations_for_event,
            trigger_type="connector_trigger",
            event_type=trigger_id,
            connector_id=connector_id,
        )
        for auto in automations:
            trigger_data = {
                "trigger": "connector_trigger",
                "connector_id": connector_id,
                "trigger_id": trigger_id,
                "data": data,
            }
            self._create_tracked_task(
                self._execute(auto["id"], trigger_data),
                name=f"auto-trigger-{auto['id']}"
            )

    async def trigger_webhook(
        self, automation_id: str, payload: Dict[str, Any], signature: Optional[str] = None
    ) -> Optional[Dict]:
        """Handle an inbound webhook trigger.

        Verifies HMAC signature if the automation has a secret configured.
        Returns the created run dict, or None if validation fails.
        """
        # Single read; the HMAC verification below is CPU work and the fire is
        # a task creation that needs this loop.
        automation = await run_sync(database.get_automation, automation_id)
        if not automation:
            return None
        if not automation.get("is_enabled"):
            return None

        # Find webhook trigger in triggers array
        triggers = automation.get("triggers") or []
        webhook_trigger = None
        for trig in triggers:
            if trig.get("type") == "webhook":
                webhook_trigger = trig
                break
        if not webhook_trigger:
            return None

        # Verify HMAC signature
        trigger_config = webhook_trigger.get("config", {})
        webhook_secret = trigger_config.get("secret")
        if webhook_secret:
            from app.core.automation_service import decrypt_webhook_secret
            decrypted = decrypt_webhook_secret(trigger_config)
            plain_secret = decrypted.get("secret", "")
            if plain_secret and signature:
                import json as json_mod
                body = json_mod.dumps(payload, separators=(",", ":"), sort_keys=True)
                expected = hmac.new(
                    plain_secret.encode(), body.encode(), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(f"sha256={expected}", signature or ""):
                    logger.warning(f"Webhook signature mismatch for automation {automation_id}")
                    return None
            elif plain_secret and not signature:
                logger.warning(f"Webhook missing signature for automation {automation_id}")
                return None

        trigger_data = {"trigger": "webhook", "payload": payload}
        self._create_tracked_task(self._execute(automation_id, trigger_data), name=f"auto-webhook-{automation_id}")

        # Return a placeholder — the actual run is created inside _execute
        return {"automation_id": automation_id, "status": "triggered"}

    async def trigger_manual(self, automation_id: str) -> Optional[Dict]:
        """Manual trigger from the UI."""
        automation = await run_sync(database.get_automation, automation_id)
        if not automation:
            return None

        trigger_data = {"trigger": "manual", "fired_at": datetime.now(timezone.utc).isoformat()}
        self._create_tracked_task(self._execute(automation_id, trigger_data), name=f"auto-manual-{automation_id}")
        return {"automation_id": automation_id, "status": "triggered"}

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _execute(self, automation_id: str, trigger_data: Dict[str, Any]) -> None:
        """Core execution flow: budget → approval → query → output."""
        # Own hop: the concurrency latch below is an ``asyncio.Lock`` and has
        # to be taken on this loop.
        automation = await run_sync(database.get_automation, automation_id)
        if not automation:
            logger.error(f"Automation not found for execution: {automation_id}")
            return

        if not automation.get("is_enabled"):
            logger.debug(f"Automation {automation_id} is disabled — skipping")
            return

        # Concurrency check — only one run at a time per automation
        async with self._lock:
            # Shutdown has begun: the scheduler kick is already suppressed,
            # so a run minted now would wait out its timeout (or be cancelled
            # mid-wait, leaving its row ``running``) while its queued turn ran
            # unwatched on the next boot. A webhook or connector event that
            # lands in that window starts nothing.
            if self._shutdown_begun():
                logger.info(f"Automation {automation_id}: shutting down — run not started")
                return
            if automation_id in self._running:
                logger.warning(f"Automation {automation_id} already running — skipping")
                return
            # Mark as running with a placeholder
            self._running[automation_id] = asyncio.current_task()

        run_id = f"run-{secrets.token_hex(4)}"

        try:

            def _admit_blocking() -> Tuple[bool, str, Optional[str]]:
                """Budget gate + run/approval records — ONE hop, ONE atomic block.

                This is the file's most important read-decide-write: ``check_budget``
                READS the shared daily-spend aggregate and the rest of this block
                WRITES the run row that the next read will count. Splitting the
                read from the write across thread hops would let two concurrent
                fires of the same automation both see the pre-run total and both
                admit themselves — a double-spend of the daily/lifetime budget.
                Everything here is contiguous in the original (no awaits between
                any of it), so keeping it in one closure changes nothing but
                where it runs.

                Returns ``(admitted, rendered_prompt, approval_id)``. The
                approval NOTIFICATION deliberately stays with the caller: it is
                an ``await`` that talks to live connector clients.
                """
                # Budget check
                from app.core.automation_service import check_budget, render_prompt
                ok, reason = check_budget(automation)
                if not ok:
                    logger.info(f"Automation {automation_id} budget exceeded: {reason}")
                    database.create_automation_run(
                        run_id=run_id,
                        automation_id=automation_id,
                        status="budget_exceeded",
                        trigger_data=trigger_data,
                    )
                    database.update_automation(
                        automation_id, last_error=reason, failure_count=automation.get("failure_count", 0) + 1
                    )
                    return False, "", None

                # Render prompt
                rendered_prompt = render_prompt(automation.get("prompt") or "", trigger_data)

                # Create run record
                database.create_automation_run(
                    run_id=run_id,
                    automation_id=automation_id,
                    status="pending",
                    trigger_data=trigger_data,
                    prompt_rendered=rendered_prompt,
                )

                # Approval check
                approval_id = None
                if automation.get("requires_approval"):
                    approval_id = f"appr-{secrets.token_hex(4)}"
                    database.create_automation_approval(
                        approval_id=approval_id,
                        run_id=run_id,
                        automation_id=automation_id,
                        prompt_preview=rendered_prompt[:2000],
                    )
                    database.update_automation_run(run_id, status="pending")
                    database.update_automation(automation_id, status="idle")
                return True, rendered_prompt, approval_id

            admitted, rendered_prompt, approval_id = await run_sync(_admit_blocking)
            if not admitted:
                return

            if approval_id is not None:
                # Notify via connectors
                await self._notify_approval_needed(automation, approval_id, rendered_prompt)

                logger.info(f"Automation {automation_id} run {run_id} awaiting approval")
                return

            # Execute
            await self._execute_run(automation, run_id, rendered_prompt, trigger_data)

        except Exception as e:
            logger.error(f"Automation {automation_id} execution error: {e}", exc_info=True)
            # Bound now: ``e`` is unbound at the end of this except clause, and
            # the closure below runs inside a nested try.
            error_text = str(e)

            def _record_failure() -> None:
                """The two failure writes are contiguous — one hop for the pair."""
                database.update_automation_run(
                    run_id, status="failed", error=error_text,
                    completed_at=datetime.now(timezone.utc).isoformat()
                )
                database.update_automation(
                    automation_id,
                    status="error",
                    last_error=error_text,
                    failure_count=automation.get("failure_count", 0) + 1,
                )

            try:
                await run_sync(_record_failure)
            except Exception as db_err:
                logger.error(
                    "Failed to record error status for automation %s run %s: %s (original error: %s)",
                    automation_id, run_id, db_err, e,
                )
        finally:
            async with self._lock:
                self._running.pop(automation_id, None)

    @staticmethod
    def _missing_project_message(project_id: str) -> str:
        """Explain why the automation's project can't run on this install."""
        project = None
        owner_name = None
        try:
            project = database.get_project(project_id)
        except Exception:
            pass
        owner_id = (project or {}).get("instance_id")
        if owner_id:
            try:
                owner = database.get_install(owner_id)
                owner_name = (owner or {}).get("device_name") or owner_id
            except Exception:
                owner_name = owner_id
        project_name = (project or {}).get("name") or project_id
        return (
            f"Project '{project_name}' lives on {owner_name or 'another install'} and this "
            f"install has no clone of it. Start that install, or clone the same git repository "
            f"here (matching remotes link automatically) so the automation can run anywhere."
        )

    def _resolve_v2_project_here(
        self, automation_id: str, project_id: str
    ) -> Optional[str]:
        """V2 (origin-keyed) project resolution for an automation run.

        When the automation's legacy ``project_id`` maps to a v2 (origin-keyed)
        project, the origin/checkout resolver — not the legacy link-group
        heuristic — owns where the run materializes. When the per-runner
        ``checkouts`` row is absent, a lazy self-seed is attempted (#607): the run
        is a pin to THIS install, so its base checkout is cloned on demand (behind
        ``AI_SHUFFLE_SELF_SEED`` + the per-project opt-out). A v2 project whose
        checkout is neither present nor seedable here (flag/opt-out off, no
        cloneable URL, or clone failure) is the SAME "project not runnable here"
        failure as a legacy missing clone.
        ``automations.project_id`` is left intact (hazard 7): the resolver derives
        project_v2 from it, and the legacy id keeps flowing downstream so an
        un-upgraded install can still read the row.

        Returns the ``project_v2`` id when the v2 path owns this run (checkout
        present here) so the caller binds the minted session to it; ``None`` when
        no v2 project maps to this legacy id — which, since resolve_project_v2
        self-seeds any live legacy row (F4), means there is no legacy project row
        on this shared DB at all, so the caller raises the missing-project error
        (the legacy link-group fallback died in PR-B). Raises ``ValueError``
        (missing-project message) directly when a v2 project maps but has no
        checkout on this runner.
        """
        from app.core import project_resolver

        session_row = {"project_id": project_id}
        try:
            v2_project = project_resolver.resolve_project_v2(session_row)
        except Exception:  # noqa: BLE001 — a resolver hiccup degrades to legacy
            logger.debug(
                "Automation %s: v2 project resolution failed for %s; using legacy path",
                automation_id, project_id, exc_info=True,
            )
            return None

        if not v2_project:
            return None

        checkout = project_resolver.resolve_local_checkout(session_row)
        if checkout is None:
            # No checkout materialized here yet. Attempt a lazy self-seed (#607):
            # an automation run is a pin to THIS install for this fire, so the base
            # is cloned on demand (behind AI_SHUFFLE_SELF_SEED + the per-project
            # opt-out) rather than spuriously failing "not on this device" for a
            # never-cloned pinned runner. Mirrors the send/resume seam
            # (project_resolver.resolve_cwd -> checkout_manager.ensure_checkout).
            # When self-seed cannot run (flag/opt-out off, or no cloneable URL) it
            # returns None and we fall through to the existing missing-project error.
            from app.core import checkout_manager

            if checkout_manager.ensure_checkout(v2_project, branch=None):
                checkout = project_resolver.resolve_local_checkout(session_row)
            if checkout is None:
                raise ValueError(self._missing_project_message(project_id))

        logger.info(
            "Automation %s: legacy project %s resolves to v2 project %s; running in "
            "checkout %s (runner %s)",
            automation_id, project_id, v2_project.get("id"),
            checkout.get("path"), checkout.get("runner_id"),
        )
        return v2_project.get("id")

    async def _execute_run(
        self,
        automation: Dict[str, Any],
        run_id: str,
        rendered_prompt: str,
        trigger_data: Dict[str, Any],
        *,
        claim_from: Optional[str] = None,
    ) -> None:
        """Execute a single automation run (after approval if needed).

        ``claim_from`` makes the ``running`` stamp a compare-and-set from that
        status: an approved run starts only while its row is still
        ``approved``, so the row itself is the claim for the whole run and a
        second install (or a second queued copy on this one) that reaches the
        same run later finds it taken and does nothing.
        """
        automation_id = automation["id"]
        action_steps = automation.get("action_steps") or []
        start_time = time.time()

        if claim_from is not None and self._shutdown_begun():
            # Leave the row ``claim_from`` (``approved``): the next boot's
            # re-queue starts it, instead of a turn nobody would run.
            logger.info(
                f"Automation {automation_id}: shutting down — {claim_from} run {run_id} left for the next start"
            )
            return

        def _cancel_ineligible_run() -> bool:
            # Approvals may outlive agent retirement, or wait behind another
            # run. Re-read at the common boundary, before ANY side effects.
            current = database.get_automation(automation_id)
            error = None
            if not current:
                error = "Automation no longer exists"
            elif not current.get("profile_id"):
                error = "Choose an agent before running this automation"
            elif not current.get("is_enabled"):
                error = "Enable this automation before running it"
            if error is None:
                return False
            database.update_automation_run(
                run_id, status="cancelled", error=error,
                completed_at=datetime.now(timezone.utc).isoformat(),
                expected_status=claim_from,
            )
            return True

        if await run_sync(_cancel_ineligible_run):
            return

        # If there's no prompt AND no action_steps, skip execution
        has_prompt = bool(rendered_prompt and rendered_prompt.strip())
        has_action_steps = bool(action_steps)
        if not has_prompt and not has_action_steps:
            logger.info(f"Automation {automation_id} has no prompt and no action_steps — skipping")

            def _settle_empty_run() -> None:
                """Terminal stamp + success accounting, contiguous — one hop."""
                settled = database.update_automation_run(
                    run_id, status="completed",
                    started_at=datetime.now(timezone.utc).isoformat(),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    expected_status=claim_from,
                )
                if claim_from is None or settled is not None:
                    database.record_automation_run_success(automation_id)

            await run_sync(_settle_empty_run)
            return

        def _mark_running() -> bool:
            """Both "now running" writes in one hop; they were already adjacent."""
            started = database.update_automation_run(
                run_id, status="running", started_at=datetime.now(timezone.utc).isoformat(),
                expected_status=claim_from,
            )
            if claim_from is not None and started is None:
                return False
            database.update_automation(automation_id, status="running")
            return True

        if not await run_sync(_mark_running):
            logger.info(
                f"Automation {automation_id}: run {run_id} is no longer {claim_from} "
                "(started elsewhere) — skipping"
            )
            return

        try:
            response_text = ""
            cost = 0.0
            tokens_in = 0
            tokens_out = 0

            # A stored model choice that is no longer available (provider
            # removed or disabled, model dropped from its list) fails the run
            # here, before anything starts, with the reason recorded on the
            # run. It must never fall through to some other model.
            from app.core.automation_service import model_choice_errors_for_run

            model_error = await run_sync(model_choice_errors_for_run, automation)
            if model_error:
                raise ValueError(model_error)

            # Execute prompt if present — as a REAL streaming chat session.
            # The run mints a fresh automation-badged session (AFTER the
            # cross-install fire claim in _on_schedule_trigger and the budget
            # check in _execute) and submits the rendered prompt through the
            # same server-initiated streaming seam user chats use, so the run
            # streams live to open tabs, is openable on the sessions board,
            # and persists normal history — instead of the old headless
            # one-shot collect that only surfaced in the run-history UI.
            if has_prompt:
                # The stored project row may belong to another install; run
                # against this install's checkout/clone of the same repo.
                project_id = automation.get("project_id")
                project_v2_id = None
                if project_id:
                    # V2 (origin-keyed) resolution is the ONLY path. resolve_project_v2
                    # self-seeds a fresh legacy project (F4), so "no v2 project maps to
                    # this legacy id" means there is no live legacy row on this shared DB
                    # at all → the project is genuinely missing here. When a v2 project
                    # DOES map but its checkout lives only on another runner,
                    # _resolve_v2_project_here raises the missing-here ValueError itself.
                    # Legacy project_id is left intact (hazard 7): the resolver derives
                    # project_v2 from it and the legacy id keeps flowing downstream.
                    # Off-loop: this resolver reads placement state and can
                    # LAZILY CLONE a checkout (#607) — seconds of blocking work,
                    # not milliseconds. It is plain sync code (no loop use), so
                    # a worker thread runs it unchanged; the ValueError it may
                    # raise still lands in this method's handlers below.
                    project_v2_id = await run_sync(
                        self._resolve_v2_project_here, automation_id, project_id
                    )
                    if not project_v2_id:
                        raise ValueError(self._missing_project_message(project_id))

                outcome = await self._run_prompt_as_session(
                    automation, run_id, rendered_prompt, project_id,
                    project_v2_id=project_v2_id,
                )
                response_text = outcome["response"]
                cost = outcome["cost_usd"]
                tokens_in = outcome["tokens_in"]
                tokens_out = outcome["tokens_out"]

                # Check per-run cost limit
                max_per_run = automation.get("max_cost_per_run")
                if max_per_run is not None and cost > max_per_run:
                    logger.warning(
                        f"Automation {automation_id} run {run_id} exceeded per-run cost "
                        f"(${cost:.4f} > ${max_per_run:.4f})"
                    )

            # Execute action steps
            if has_action_steps:
                await self._execute_action_steps(
                    automation_id, run_id, action_steps, trigger_data, response_text
                )

            duration_ms = int((time.time() - start_time) * 1000)

            # Update run record. The run's session was created and linked
            # BEFORE the turn started (the session is the primary artifact —
            # this row is a thin index into it), so completion only stamps
            # status/cost here and never re-writes session_id: the old
            # completion-time FK race (session deleted while the run looked
            # blank mid-turn) is gone by construction — the session carries
            # its user message from the start and streams like a user chat.
            # `response` keeps only a short tail for the runs-list preview;
            # the full transcript lives in the session history.
            def _settle_success() -> None:
                """Run completion + cost accounting in ONE hop.

                These two are a unit: the run row's terminal stamp and the SQL
                increment of the automation's spend counters are what the NEXT
                fire's budget check reads. Adjacent in the original, adjacent
                here — no await may open a window between them.
                """
                database.update_automation_run(
                    run_id,
                    status="completed",
                    response=self._response_tail(response_text),
                    cost_usd=cost,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    duration_ms=duration_ms,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

                # Update automation stats. The increments happen in SQL (one
                # atomic UPDATE) so a concurrent run on another install can't
                # make this stale in-memory row lose a count or a cost.
                database.record_automation_run_success(automation_id, cost)

            await run_sync(_settle_success)

            # Deliver output
            await self._deliver_output(automation, run_id, response_text)

        except _ShutdownBeforeTurn:
            logger.info(f"Automation {automation_id}: shutting down — run {run_id} cancelled before its turn")

            def _settle_cancelled() -> None:
                database.update_automation_run(
                    run_id, status="cancelled", error="Shutting down: the run's turn was not started",
                    duration_ms=int((time.time() - start_time) * 1000),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                database.update_automation(automation_id, status="idle")

            await run_sync(_settle_cancelled)

        except asyncio.TimeoutError:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Execution timed out after {automation.get('timeout_seconds', 300)}s"

            def _settle_timeout() -> None:
                """Both failure writes, contiguous — one hop."""
                database.update_automation_run(
                    run_id, status="failed", error=error_msg,
                    duration_ms=duration_ms, completed_at=datetime.now(timezone.utc).isoformat()
                )
                database.update_automation(
                    automation_id, status="error", last_error=error_msg,
                    failure_count=automation.get("failure_count", 0) + 1,
                )

            await run_sync(_settle_timeout)

        except (Exception, BaseExceptionGroup) as e:
            # Unwrap ExceptionGroup from SDK's anyio.TaskGroup (Python 3.11+)
            # to get the actual error message instead of the generic
            # "unhandled errors in a TaskGroup (N sub-exceptions)"
            actual = e
            if isinstance(e, BaseExceptionGroup):
                actual = e.exceptions[0] if e.exceptions else e
                logger.error(
                    "Automation %s: unwrapped ExceptionGroup with %d sub-exception(s)",
                    automation_id, len(e.exceptions), exc_info=e,
                )

            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = str(actual)

            def _settle_failure() -> None:
                """Both failure writes, contiguous — one hop."""
                database.update_automation_run(
                    run_id, status="failed", error=error_msg,
                    duration_ms=duration_ms, completed_at=datetime.now(timezone.utc).isoformat()
                )
                database.update_automation(
                    automation_id, status="error", last_error=error_msg,
                    failure_count=automation.get("failure_count", 0) + 1,
                )

            # Awaiting inside the handler keeps the active exception bound to
            # this frame, so the bare ``raise`` below still re-raises it.
            await run_sync(_settle_failure)
            if actual is e:
                raise
            # Propagate the unwrapped error: _execute records the failure a
            # second time from what it catches, and the group's text
            # ("unhandled errors in a TaskGroup") would overwrite this one.
            raise actual from e

    # ------------------------------------------------------------------
    # Prompt execution — automation runs ARE normal streaming sessions
    # ------------------------------------------------------------------

    #: The runs-list preview keeps only this much of the final assistant
    #: text; the full transcript lives in the run's session history.
    RESPONSE_TAIL_CHARS = 2000

    @classmethod
    def _response_tail(cls, text: str) -> str:
        return (text or "")[-cls.RESPONSE_TAIL_CHARS:]

    @staticmethod
    async def _discard_unused_session(automation_id: str, session_id: str) -> None:
        """Delete a session minted for a turn that will not start, and drop it
        from connected devices. Best-effort: a failure leaves the empty session
        behind, as before, and the run is still cancelled."""
        try:
            await run_sync(database.delete_session, session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Automation {automation_id}: could not delete unused session {session_id}: {e}")
            return
        try:
            from app.core import sync_engine as sync_engine_module

            await sync_engine_module.sync_engine.broadcast_user_session_deleted(
                user_id="admin", session_id=session_id, source_device_id=None
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Automation {automation_id}: session delete broadcast failed: {e}")

    async def _run_prompt_as_session(
        self,
        automation: Dict[str, Any],
        run_id: str,
        rendered_prompt: str,
        project_id: Optional[str],
        project_v2_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the automation prompt as a normal streaming chat session.

        Mints ONE FRESH automation-badged session per fire (no shared
        session, so overlapping fires can never race each other's
        history), links the run row to the session, then submits a durable
        ``source=automation`` turn through :mod:`app.core.turn_manager`. The
        scheduler kicks it into the ONE spawn path (``SessionRuntime``), so the
        run streams live to any open tab, is openable on the sessions board,
        and persists normal history — exactly like a user chat. The turn
        recorder persists the rendered prompt as a ``message.user`` event at
        turn start.
        """
        from app.core import turn_manager

        from app.core.automation_service import resolve_automation_model

        automation_id = automation["id"]
        profile_id = automation["profile_id"]
        if not profile_id:
            raise ValueError("Choose an agent before running this automation")
        timeout_seconds = automation.get("timeout_seconds", 300)
        # The fire passed the shutdown gates before the run moved to
        # ``running``; project resolution (possibly a clone) ran since.
        if self._shutdown_begun():
            raise _ShutdownBeforeTurn()

        # The model this run's session is bound to. An explicit choice is
        # stamped on the session row at birth, exactly like the composer's
        # pick on a new session (``create_session(model=...)``), so this turn
        # and every later one (failover retry, auto-continue) resolve from the
        # row. No choice leaves the row unbound, like a new chat with no pick:
        # the app default (or a legacy model still saved on the agent row).
        model_resolution = await run_sync(resolve_automation_model, automation)
        if model_resolution["error"]:
            raise ValueError(model_resolution["error"])
        model_selection = model_resolution["selection"]

        # 1. Fresh badged session. This runs only AFTER the cross-install
        #    fire claim (_on_schedule_trigger) and budget check (_execute),
        #    so a losing install never mints a session for a tick it
        #    didn't win, and a broke automation mints nothing.
        session_id = str(uuid.uuid4())
        fired_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        title = f"{automation.get('name') or 'Automation'} — {fired_label}"

        def _mint_session_blocking() -> None:
            """Mint the session, bind its project_v2, and link the run to it —
            ONE hop, and one indivisible unit.

            Steps 1 and 2 are contiguous in the original and must stay that
            way: a session row without its run link (or a run linked to a
            session whose project_v2 binding never landed) is exactly the
            half-built state this ordering exists to prevent. Nothing here
            needs the loop; the sidebar broadcast that does stays below.
            """
            # The agent's configured mode is this run's binding from birth,
            # so its turn and every successor (failover retry, auto-continue,
            # a wake) resolve their permissions from the row. An unattended
            # Accept Edits / Bypass automation must not drop to Ask.
            agent_config = (database.get_profile(profile_id) or {}).get("config") or {}
            database.create_session(
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                title=title,
                automation_id=automation_id,
                model=model_selection,
                permission_mode=agent_config.get("permission_mode"),
                # An explicit native pick must stay native even when the agent
                # still carries a legacy provider; a ``prov:`` pick binds its
                # own provider. Only an unbound run derives it from the agent.
                derive_provider_from_profile=model_selection is None,
            )
            # Bind the session to its project_v2 when the v2 path owns this run.
            # create_session does not take the column, so persist it directly (the
            # NULL-guarded self-heal write in project_resolver). Legacy project_id is
            # already set above and stays intact.
            if project_v2_id:
                from app.core import project_resolver

                project_resolver._persist_session_project_v2(session_id, project_v2_id)
            # Session starts empty until the recorder's first event: the turn
            # recorder persists the prompt as a message.user event at turn start.

            # 2. Link run → session BEFORE the turn starts: the session is the
            #    primary artifact, the run row a thin index into it. Completion
            #    never re-writes the link, so the old completion-time
            #    FK-violation race is unrepresentable.
            database.update_automation_run(run_id, session_id=session_id)

        await run_sync(_mint_session_blocking)

        # Best-effort: surface the new session on connected devices immediately
        # (scheduler-created sessions otherwise only appear on the next sidebar
        # poll). The prompt itself is delivered by the SSE event log.
        try:
            from app.core import sync_engine as sync_engine_module

            await sync_engine_module.sync_engine.broadcast_user_session_created(
                user_id="admin", session_id=session_id, source_device_id=None
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Automation {automation_id}: session broadcast failed: {e}")

        # 3. Submit a durable ``source=automation`` turn. ``submit`` enqueues
        #    then kicks the scheduler, which starts the turn in the ONE spawn
        #    path (SessionRuntime). No ``deferred_delivery`` concept survives —
        #    the v2 SSE typed-event feed is always live. Last shutdown gate:
        #    the session mint and broadcast above both awaited. The session was
        #    minted for this turn alone and is still empty, so it goes too
        #    (the run's link clears with it: ON DELETE SET NULL).
        if self._shutdown_begun():
            await self._discard_unused_session(automation_id, session_id)
            raise _ShutdownBeforeTurn()
        await turn_manager.submit_async(
            session_id,
            "automation",
            prompt=rendered_prompt,
            profile_id=profile_id,
            project_id=project_id,
            overrides={"max_turns": automation.get("max_turns", 1)},
        )

        # 4. Block until the session fully drains — no live turn AND no queued
        #    turn — under the automation timeout. A hand-off (account-failover
        #    retry, compact-boundary auto-continue) is enqueued by the scheduler
        #    as a NEW turn, so ``wait_session_idle`` naturally waits it out
        #    (it returns only once the whole queue has drained), covering the
        #    successor under the same deadline. On timeout, interrupt the wedged
        #    live turn (the command→runtime seam routes it to the runtime's SDK
        #    interrupt + escalation) and stamp the timeout.
        idle = await turn_manager.wait_session_idle(
            session_id, timeout=timeout_seconds
        )
        if not idle:
            await turn_manager.interrupt_async(session_id, "stop", source="automation_timeout")
            raise asyncio.TimeoutError()

        # 5. Read the final outcome from the durable event log (the v2 path no
        #    longer sniffs WS frames). The last terminal event of the session
        #    decides success / error / interruption; ``turn.completed`` carries
        #    the usage summary the run row is stamped from.
        # Off-loop: this loads the session's whole durable event log. Kept as
        # its own hop rather than batched with the text read below, because the
        # text read must still NOT happen when the outcome says error/interrupt.
        outcome = await run_sync(self._final_turn_outcome, session_id)
        if outcome["error"] is not None:
            raise RuntimeError(outcome["error"])
        if outcome["interrupted"]:
            raise RuntimeError("Turn was interrupted before completion")
        # Fail closed on a log with NO terminal turn event. We just submitted a
        # turn and drained the session, so a terminal event must exist — its
        # absence means the outcome is underivable (the recorder never wrote a
        # terminal, e.g. the turn crashed before the pump ran, or the log read
        # failed), and stamping the run "completed" from it would fabricate an
        # empty $0 success. Exactly the bogus run the 2026-08-28 idle-wait race
        # produced — the wait bug is fixed at the source, this is the backstop.
        if not outcome.get("terminal_found"):
            raise RuntimeError(
                "Automation turn produced no terminal event in the session log "
                "— outcome unknown, refusing to stamp an empty success "
                f"(session {session_id})"
            )

        usage = outcome["usage"]
        final_text = await run_sync(self._extract_final_assistant_text, session_id)
        return {
            "session_id": session_id,
            "response": final_text,
            "cost_usd": float(usage.get("total_cost_usd") or 0),
            "tokens_in": int(usage.get("tokens_in") or 0),
            "tokens_out": int(usage.get("tokens_out") or 0),
        }

    # The three readers below moved to :mod:`app.core.turn_outcome` — the REST
    # conversation endpoint and the connector intake path derive their outcome
    # from the same event log. They stay here as thin delegates because they are
    # part of this class's tested surface (tests patch them on the class).

    @staticmethod
    def _load_session_events(session_id: str) -> List[Dict[str, Any]]:
        """Delegate to :func:`app.core.turn_outcome.load_session_events`."""
        return turn_outcome.load_session_events(session_id)

    @classmethod
    def _final_turn_outcome(cls, session_id: str) -> Dict[str, Any]:
        """Delegate to :func:`app.core.turn_outcome.final_turn_outcome`."""
        return turn_outcome.final_turn_outcome(session_id)

    @staticmethod
    def _extract_final_assistant_text(session_id: str) -> str:
        """Delegate to :func:`app.core.turn_outcome.extract_final_assistant_text`."""
        return turn_outcome.extract_final_assistant_text(session_id)

    # ------------------------------------------------------------------
    # Action step execution
    # ------------------------------------------------------------------

    async def _execute_action_steps(
        self,
        automation_id: str,
        run_id: str,
        action_steps: List[Dict[str, Any]],
        trigger_data: Dict[str, Any],
        response_text: str,
    ) -> None:
        """Execute action steps for an automation run.

        Supports multiple action step types:
        - connector_action: Execute a connector action
        - send_prompt: Run an AI prompt
        - webhook_call: POST/GET to a URL
        - delay: Wait for a specified time
        """
        from app.core.automation_service import render_action_params, render_prompt

        for i, step in enumerate(action_steps):
            step_type = step.get("type")

            if step_type == "connector_action":
                from app.core.connectors.manager import connector_manager

                connector_id = step.get("connector_id")
                action_id = step.get("action_id")
                raw_params = step.get("params", {})

                if not connector_id or not action_id:
                    logger.warning(
                        f"Automation {automation_id} run {run_id}: "
                        f"action step {i} missing connector_id or action_id — skipping"
                    )
                    continue

                try:
                    instance = await connector_manager.get_running_connector(connector_id)
                    if not instance:
                        logger.error(
                            f"Automation {automation_id} run {run_id}: "
                            f"connector {connector_id} is not running for action step {i}"
                        )
                        continue

                    rendered_params = render_action_params(raw_params, trigger_data, response_text)
                    result = await instance.execute_action(action_id, rendered_params)
                    logger.info(
                        f"Automation {automation_id} run {run_id}: "
                        f"action step {i} ({connector_id}/{action_id}) completed: {result}"
                    )
                except Exception as e:
                    logger.error(
                        f"Automation {automation_id} run {run_id}: "
                        f"action step {i} ({connector_id}/{action_id}) failed: {e}",
                        exc_info=True,
                    )

            elif step_type == "send_prompt":
                prompt_text = step.get("prompt", "")
                if not prompt_text.strip():
                    logger.warning(
                        f"Automation {automation_id} run {run_id}: "
                        f"send_prompt step {i} has empty prompt — skipping"
                    )
                    continue
                try:
                    rendered = render_prompt(prompt_text, {**trigger_data, "response": response_text})
                    from app.core import account_failover

                    from app.core.automation_service import resolve_automation_model

                    def _load_step_context():
                        """Automation re-read + the step's model resolution — one hop."""
                        row = database.get_automation(automation_id)
                        return row, resolve_automation_model(row or {}, step)

                    automation, step_model = await run_sync(_load_step_context)
                    profile_id = step.get("profile_id") or (automation.get("profile_id") if automation else None)
                    max_turns = step.get("max_turns", 1)
                    if step_model["error"]:
                        raise ValueError(step_model["error"])
                    step_overrides: Dict[str, Any] = {"max_turns": max_turns}
                    # The step's own model, else the automation's. No explicit
                    # choice leaves the override out, so the step runs on the
                    # app default (or a legacy model saved on the agent).
                    if step_model["selection"]:
                        step_overrides["model"] = step_model["selection"]

                    result = await account_failover.execute_query_with_failover(
                        prompt=rendered,
                        profile_id=profile_id,
                        overrides=step_overrides,
                        automation_id=automation_id,
                    )
                    # Update response_text for subsequent steps
                    response_text = result.get("response", response_text)
                    logger.info(
                        f"Automation {automation_id} run {run_id}: "
                        f"send_prompt step {i} completed"
                    )
                except Exception as e:
                    logger.error(
                        f"Automation {automation_id} run {run_id}: "
                        f"send_prompt step {i} failed: {e}",
                        exc_info=True,
                    )

            elif step_type == "webhook_call":
                url = step.get("url", "")
                if not url:
                    logger.warning(
                        f"Automation {automation_id} run {run_id}: "
                        f"webhook_call step {i} has no URL — skipping"
                    )
                    continue
                try:
                    method = (step.get("method") or "POST").upper()
                    headers = step.get("headers") or {}
                    body_template = step.get("body", "")
                    rendered_body = render_prompt(body_template, {**trigger_data, "response": response_text}) if body_template else ""

                    from app.core.http_client import get_http_client
                    client = get_http_client()
                    resp = await client.request(
                        method, url,
                        content=rendered_body if rendered_body else None,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    logger.info(
                        f"Automation {automation_id} run {run_id}: "
                        f"webhook_call step {i} completed ({method} {url} → {resp.status_code})"
                    )
                except Exception as e:
                    logger.error(
                        f"Automation {automation_id} run {run_id}: "
                        f"webhook_call step {i} ({url}) failed: {e}",
                        exc_info=True,
                    )

            elif step_type == "delay":
                delay_seconds = step.get("seconds", 0)
                if delay_seconds <= 0:
                    continue
                # Cap delay at 5 minutes for safety
                delay_seconds = min(delay_seconds, 300)
                logger.info(
                    f"Automation {automation_id} run {run_id}: "
                    f"delay step {i} — waiting {delay_seconds}s"
                )
                await asyncio.sleep(delay_seconds)

            else:
                logger.warning(
                    f"Automation {automation_id} run {run_id}: "
                    f"unknown action step type '{step_type}' at index {i} — skipping"
                )

    # ------------------------------------------------------------------
    # Approval execution
    # ------------------------------------------------------------------

    async def execute_approved_run(self, approval_id: str) -> Optional[Dict]:
        """Resume execution of an approved automation run."""
        def _load_approved() -> Tuple[Optional[Dict], Optional[Dict]]:
            """Approval → run → automation: three contiguous reads, ONE hop.

            Each early return is just "not found", so the caller only needs to
            know whether all three resolved. The background task creation stays
            on the loop.
            """
            approval = database.get_automation_approval(approval_id)
            if not approval or approval["status"] != "approved":
                return None, None

            run = database.get_automation_run(approval["run_id"])
            if not run:
                return None, None

            automation = database.get_automation(run["automation_id"])
            if not automation:
                return None, None
            return run, automation

        if self._shutdown_begun():
            logger.info(f"Approval {approval_id}: shutting down — run left approved for the next start")
            return None
        run, automation = await run_sync(_load_approved)
        if run is None or automation is None:
            return None

        # Execute in background
        async def _run():
            auto_id = automation["id"]
            me = asyncio.current_task()
            # Same one-run-per-automation latch as _execute: queue behind an
            # in-flight run rather than run beside it and overwrite (and later
            # pop) that run's latch entry.
            while True:
                async with self._lock:
                    if self._shutdown_begun():
                        self._waiting_approved.discard(me)
                        return
                    holder = self._running.get(auto_id)
                    if holder is None or holder.done():
                        self._waiting_approved.discard(me)
                        self._running[auto_id] = me
                        break
                    if me not in self._waiting_approved:
                        self._waiting_approved.add(me)
                        me.add_done_callback(self._waiting_approved.discard)
                await asyncio.wait({holder})
            try:
                await self._execute_run(
                    automation,
                    run["id"],
                    run.get("prompt_rendered") or automation.get("prompt") or "",
                    run.get("trigger_data", {}),
                    claim_from="approved",
                )
            finally:
                async with self._lock:
                    if self._running.get(auto_id) is me:
                        self._running.pop(auto_id, None)

        self._create_tracked_task(_run(), name=f"auto-approved-{run['id']}")
        return run

    async def _requeue_approved_runs(self) -> None:
        """Start again the approved runs a previous shutdown cancelled.

        ``execute_approved_run`` queues an approved run behind a run of the
        same automation already in flight; shutdown cancels that wait and the
        row stays ``approved`` (the approval itself is spent, so approving
        again is refused). Every install sharing the database sees the row, so
        the same runner election as a scheduled fire picks one install.

        Only runs of an automation that still exists and is enabled, approved
        within ``APPROVED_RUN_REQUEUE_MAX_AGE_SECONDS`` (24 h), are re-queued;
        an older approved run is stamped ``cancelled`` (compare-and-set, so a
        run another install just started is untouched). A disabled
        automation's run is left ``approved`` for a later start once it is
        enabled again (inside the same 24 h). Running once is guaranteed by
        the run row itself: ``_execute_run(claim_from="approved")`` moves it
        ``approved -> running`` as a compare-and-set, so a second install, a
        second start, or a copy still queued in memory on a live install that
        reaches it afterwards skips it.
        """
        def _select_approved_blocking() -> List[str]:
            from app.core.automation_service import should_run_scheduled_here
            from app.db.base import get_db, rows_to_list

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT a.id AS approval_id, a.decided_at, r.id AS run_id,
                              r.automation_id
                       FROM automation_approvals a
                       JOIN automation_runs r ON r.id = a.run_id
                       WHERE a.status = 'approved' AND r.status = 'approved'
                       ORDER BY r.created_at"""
                )
                rows = rows_to_list(cursor.fetchall())
            now = datetime.now(timezone.utc)
            selected: List[str] = []
            for row in rows:
                decided = _parse_utc(row.get("decided_at"))
                if decided is None or (now - decided).total_seconds() > APPROVED_RUN_REQUEUE_MAX_AGE_SECONDS:
                    expired = database.update_automation_run(
                        row["run_id"], status="cancelled",
                        error="Approval expired: approved more than 24 h before a restart could run it",
                        completed_at=now.isoformat(), expected_status="approved",
                    )
                    if expired is not None:
                        logger.info(f"Approved run {row['run_id']} expired before a restart re-queued it")
                    continue
                automation = database.get_automation(row["automation_id"])
                if not automation or not automation.get("is_enabled"):
                    continue
                try:
                    should_run, reason = should_run_scheduled_here(automation)
                except Exception as e:  # noqa: BLE001 — same fail-open stance as a fire
                    should_run, reason = True, f"election failed ({e})"
                if not should_run:
                    logger.info(f"Approved run {row['run_id']} left to another install ({reason})")
                    continue
                selected.append(row["approval_id"])
            return selected

        if self._shutdown_begun():
            return
        try:
            approval_ids = await run_sync(_select_approved_blocking)
        except Exception as e:  # noqa: BLE001 — a DB blip must not break start()
            logger.warning(f"Could not re-queue approved automation runs: {e}")
            return
        for approval_id in approval_ids:
            if self._shutdown_begun():
                return
            run = await self.execute_approved_run(approval_id)
            if run is not None:
                logger.info(f"Re-queued approved automation run {run['id']} after restart")

    # ------------------------------------------------------------------
    # Output delivery
    # ------------------------------------------------------------------

    async def _deliver_output(
        self, automation: Dict[str, Any], run_id: str, response: str
    ) -> None:
        """Deliver the automation result to the configured output."""
        output_config = automation.get("output_config", {})
        output_type = output_config.get("type", "session")

        def _gate_and_settle_blocking() -> bool:
            """Every delivery decision that needs no network, in ONE hop.

            The suppress check, the dedup check and the two no-op output types
            are contiguous in the original — no awaits anywhere between them —
            and the dedup leg in particular is a read-decide-write (read the
            recent runs, compare, stamp ``deduplicated``) that has to stay
            atomic: split across hops, two runs finishing together could each
            read the other as "not yet delivered" and both deliver. Returns
            True when the delivery is fully settled here and the caller is
            done.
            """
            # --- Suppress matching responses (heartbeat pattern) ---
            suppress_pattern = output_config.get("suppress_pattern")
            if suppress_pattern and response:
                cleaned = response.strip()
                if cleaned.upper() == suppress_pattern.strip().upper():
                    logger.info(
                        f"Automation {automation['id']} run {run_id}: "
                        f"output suppressed (matched suppress_pattern)"
                    )
                    database.update_automation_run(run_id, output_status="suppressed")
                    return True

            # --- Deduplicate identical responses within window ---
            dedup_window = output_config.get("deduplicate_window_seconds")
            if dedup_window and isinstance(dedup_window, (int, float)) and dedup_window > 0 and response:
                recent = database.get_automation_runs(automation["id"], limit=5)
                now = datetime.now(timezone.utc)
                # Run rows store only the response tail, so compare tails.
                stored_response = self._response_tail(response)
                for prev in recent:
                    if prev["id"] == run_id:
                        continue
                    if (
                        prev.get("response") == stored_response
                        and prev.get("output_status") == "delivered"
                    ):
                        completed = prev.get("completed_at")
                        if completed:
                            try:
                                completed_dt = datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
                                if completed_dt.tzinfo is None:
                                    completed_dt = completed_dt.replace(tzinfo=timezone.utc)
                                age = (now - completed_dt).total_seconds()
                                if 0 < age < dedup_window:
                                    logger.info(
                                        f"Automation {automation['id']} run {run_id}: "
                                        f"deduplicated (same response {age:.0f}s ago)"
                                    )
                                    database.update_automation_run(
                                        run_id, output_status="deduplicated"
                                    )
                                    return True
                            except (ValueError, TypeError) as parse_err:
                                logger.debug("Skipping dedup check for run %s: timestamp parse error: %s", prev["id"], parse_err)

            if output_type == "session":
                # Already saved via execute_query
                database.update_automation_run(run_id, output_status="delivered")
                return True

            if output_type == "none":
                database.update_automation_run(run_id, output_status="delivered")
                return True
            return False

        if await run_sync(_gate_and_settle_blocking):
            return

        try:
            if output_type == "connector":
                await self._deliver_to_connector(output_config, response)
            elif output_type == "webhook":
                await self._deliver_to_webhook(output_config, automation, response)

            await run_sync(
                database.update_automation_run, run_id, output_status="delivered"
            )

        except Exception as e:
            logger.error(f"Output delivery failed for run {run_id}: {e}")
            await run_sync(
                database.update_automation_run,
                run_id, output_status="failed", output_error=str(e),
            )

    async def _deliver_to_connector(
        self, output_config: Dict[str, Any], response: str
    ) -> None:
        """Send response via a connector."""
        from app.core.connectors.base import OutgoingMessage
        from app.core.connectors.manager import connector_manager

        connector_id = output_config.get("connector_id")
        platform_chat_id = output_config.get("platform_chat_id")

        if not connector_id or not platform_chat_id:
            raise ValueError("Output config missing connector_id or platform_chat_id")

        instance = await connector_manager.get_running_connector(connector_id)

        if not instance:
            raise RuntimeError(f"Connector {connector_id} is not running")

        await instance.send_message(
            OutgoingMessage(platform_chat_id=platform_chat_id, text=response)
        )

    async def _deliver_to_webhook(
        self, output_config: Dict[str, Any], automation: Dict[str, Any], response: str
    ) -> None:
        """POST response to an outbound webhook URL."""
        url = output_config.get("url")
        if not url:
            raise ValueError("Output config missing url")

        headers = output_config.get("headers", {}) or {}
        headers["Content-Type"] = "application/json"

        import json as json_mod
        payload = json_mod.dumps({
            "automation_id": automation["id"],
            "automation_name": automation["name"],
            "response": response,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        from app.core.http_client import get_http_client
        client = get_http_client()
        resp = await client.post(url, content=payload, headers=headers)
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Approval notifications
    # ------------------------------------------------------------------

    async def _notify_approval_needed(
        self, automation: Dict[str, Any], approval_id: str, prompt_preview: str
    ) -> None:
        """Notify admin via connectors that an automation needs approval."""
        try:
            from app.core.connectors.manager import connector_manager

            # Get all active connectors to notify about pending approval. One
            # hop; the per-connector work below is all awaits.
            connectors = await run_sync(database.get_active_connectors)
            for conn_row in connectors:
                cid = conn_row["id"]
                instance = await connector_manager.get_running_connector(cid)
                if not instance:
                    continue

                # Get active connector sessions to notify. Its own cursor, so
                # the scanner does not see it — but it is the same blocking
                # psycopg2 round trip and gets the same treatment.
                def _load_sessions(connector_id: str = cid) -> List[Dict[str, Any]]:
                    from app.db.base import get_db, rows_to_list
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "SELECT * FROM connector_sessions WHERE connector_id = %s AND is_active = TRUE",
                            (connector_id,),
                        )
                        return rows_to_list(cursor.fetchall())

                sessions = await run_sync(_load_sessions)

                for sess in sessions:
                    chat_id = sess.get("platform_chat_id")
                    if chat_id:
                        try:
                            await instance.send_notification(
                                chat_id,
                                "automation.approval_needed",
                                {
                                    "automation_name": automation["name"],
                                    "approval_id": approval_id,
                                    "prompt_preview": prompt_preview[:500],
                                },
                            )
                        except Exception as e:
                            logger.warning(f"Failed to notify connector {cid} about approval: {e}")

        except Exception as e:
            logger.error(f"Failed to send approval notifications: {e}")


# Module-level singleton
automation_manager = AutomationManager()
