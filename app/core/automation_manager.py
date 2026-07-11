"""
AutomationManager — scheduler integration, event listener, execution lifecycle.

Singleton that manages the APScheduler instance, handles trigger firing,
budget enforcement, execution via execute_query(), and output delivery.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import psycopg2.errors

from app.db import database

logger = logging.getLogger(__name__)

# Automations are shared across installs but APScheduler jobs are per-process,
# so each install periodically re-syncs its jobs from the database to pick up
# automations created, changed, or deleted on other installs.
SCHEDULE_RECONCILE_SECONDS = 60


class AutomationManager:
    """Central hub for automation scheduling, execution, and output delivery."""

    def __init__(self):
        self._scheduler = None
        self._running: Dict[str, asyncio.Task] = {}  # automation_id → running task
        self._lock = asyncio.Lock()
        self._initialized = False
        self._scheduled_versions: Dict[str, str] = {}  # automation_id → trigger fingerprint
        self._reconcile_task: Optional[asyncio.Task] = None

    def _create_tracked_task(self, coro, *, name: str) -> asyncio.Task:
        """Create a background task with error logging on failure."""
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._on_task_done)
        return task

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

        # Cancel running tasks
        async with self._lock:
            for auto_id, task in self._running.items():
                task.cancel()
                logger.debug(f"Cancelled running automation {auto_id}")
            self._running.clear()

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
        for auto in database.get_enabled_automations():
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

        automation = database.get_automation(automation_id)
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
                        day_of_week=parts[4], timezone=timezone,
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
        try:
            automation = database.get_automation(automation_id)
            if not automation:
                # Deleted (possibly on another install) while our job survived.
                logger.warning(f"Automation {automation_id} no longer exists — removing stale schedule")
                await self.unschedule_automation(automation_id)
                return

            from app.core.automation_service import should_run_scheduled_here
            should_run, reason = should_run_scheduled_here(automation)
            if not should_run:
                logger.debug(f"Automation {automation_id}: scheduled fire skipped on this install ({reason})")
                return
        except Exception as e:
            # Fail open: a transient DB/registry blip must not silently drop
            # the fire — _execute records its own failure if things are broken.
            logger.warning(f"Automation {automation_id}: scheduled-fire gate failed ({e}) — running locally")

        fired_at = datetime.now(timezone.utc)
        fire_bucket = self._fire_bucket(trigger_type, trigger_config, fired_at)
        try:
            claimed = database.claim_automation_fire(automation_id, fire_bucket)
        except Exception as e:  # noqa: BLE001 — claim trouble must not drop the fire
            # Same fail-open stance as the election: if the claim table is
            # unreachable here it is unreachable on the other installs too.
            logger.warning(f"Automation {automation_id}: fire claim {fire_bucket} failed ({e}) — running locally")
            claimed = True
        if not claimed:
            logger.info(
                f"Automation {automation_id}: fire {fire_bucket} already claimed by another install — skipping"
            )
            return
        try:
            database.prune_automation_fires()
        except Exception:  # noqa: BLE001 — hygiene must never block a fire
            pass

        # The claimed tick stays consumed even if _execute's budget check then
        # blocks the run: budget counters live in the shared database, so a
        # fire that is over budget here is over budget on every install.
        trigger_data = {"trigger": "schedule", "fired_at": fired_at.isoformat()}
        self._create_tracked_task(self._execute(automation_id, trigger_data), name=f"auto-schedule-{automation_id}")

    async def handle_connector_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Check for automations triggered by connector events and fire them."""
        automations = database.get_automations_for_event(
            trigger_type="connector_event",
            event_type=event_type,
        )
        for auto in automations:
            trigger_config = auto.get("trigger_config", {})

            # Apply optional filter
            event_filter = trigger_config.get("filter", {})
            skip = False
            for fk, fv in event_filter.items():
                if data.get(fk) != fv:
                    skip = True
                    break
            if skip:
                continue

            trigger_data = {"trigger": "connector_event", "event_type": event_type, "event": data}
            self._create_tracked_task(self._execute(auto["id"], trigger_data), name=f"auto-event-{auto['id']}")

    async def handle_connector_trigger(self, connector_id: str, trigger_id: str, data: Dict[str, Any]) -> None:
        """Fire automations that match a specific connector trigger."""
        automations = database.get_automations_for_event(
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
        automation = database.get_automation(automation_id)
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
        automation = database.get_automation(automation_id)
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
        automation = database.get_automation(automation_id)
        if not automation:
            logger.error(f"Automation not found for execution: {automation_id}")
            return

        if not automation.get("is_enabled"):
            logger.debug(f"Automation {automation_id} is disabled — skipping")
            return

        # Concurrency check — only one run at a time per automation
        async with self._lock:
            if automation_id in self._running:
                logger.warning(f"Automation {automation_id} already running — skipping")
                return
            # Mark as running with a placeholder
            self._running[automation_id] = asyncio.current_task()

        run_id = f"run-{secrets.token_hex(4)}"

        try:
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
                return

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

                # Notify via connectors
                await self._notify_approval_needed(automation, approval_id, rendered_prompt)

                logger.info(f"Automation {automation_id} run {run_id} awaiting approval")
                return

            # Execute
            await self._execute_run(automation, run_id, rendered_prompt, trigger_data)

        except Exception as e:
            logger.error(f"Automation {automation_id} execution error: {e}", exc_info=True)
            try:
                database.update_automation_run(
                    run_id, status="failed", error=str(e),
                    completed_at=datetime.now(timezone.utc).isoformat()
                )
                database.update_automation(
                    automation_id,
                    status="error",
                    last_error=str(e),
                    failure_count=automation.get("failure_count", 0) + 1,
                )
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

    async def _execute_run(
        self,
        automation: Dict[str, Any],
        run_id: str,
        rendered_prompt: str,
        trigger_data: Dict[str, Any],
    ) -> None:
        """Execute a single automation run (after approval if needed)."""
        automation_id = automation["id"]
        action_steps = automation.get("action_steps") or []
        start_time = time.time()

        # If there's no prompt AND no action_steps, skip execution
        has_prompt = bool(rendered_prompt and rendered_prompt.strip())
        has_action_steps = bool(action_steps)
        if not has_prompt and not has_action_steps:
            logger.info(f"Automation {automation_id} has no prompt and no action_steps — skipping")
            database.update_automation_run(
                run_id, status="completed",
                started_at=datetime.now(timezone.utc).isoformat(),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            database.record_automation_run_success(automation_id)
            return

        database.update_automation_run(
            run_id, status="running", started_at=datetime.now(timezone.utc).isoformat()
        )
        database.update_automation(automation_id, status="running")

        try:
            response_text = ""
            cost = 0.0
            tokens_in = 0
            tokens_out = 0
            session_id = None

            # Execute prompt if present
            if has_prompt:
                from app.core.automation_service import resolve_local_project
                from app.core import account_failover

                # The stored project row may belong to another install; run
                # against this install's clone of the same repo (link group).
                project_id = automation.get("project_id")
                if project_id:
                    local_project = resolve_local_project(project_id)
                    if local_project is None:
                        raise ValueError(self._missing_project_message(project_id))
                    if local_project["id"] != project_id:
                        logger.info(
                            f"Automation {automation_id}: project {project_id} belongs to another "
                            f"install; using local clone {local_project['id']} ({local_project.get('path')})"
                        )
                        project_id = local_project["id"]

                result = await asyncio.wait_for(
                    account_failover.execute_query_with_failover(
                        prompt=rendered_prompt,
                        profile_id=automation["profile_id"],
                        project_id=project_id,
                        overrides={"max_turns": automation.get("max_turns", 1)},
                        automation_id=automation_id,
                    ),
                    timeout=automation.get("timeout_seconds", 300),
                )

                response_text = result.get("response", "")
                cost = result.get("total_cost_usd", 0)
                tokens_in = result.get("input_tokens", 0)
                tokens_out = result.get("output_tokens", 0)
                session_id = result.get("session_id")

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

            # Update run record. The run's session row can be deleted while a
            # long run is still executing (turn messages only persist at turn
            # end, so mid-run the session looks blank and gets cleaned up) —
            # linking it then violates the FK. Record the successful run
            # without the session link rather than marking it failed.
            run_result = dict(
                status="completed",
                response=response_text[:10000],  # Cap stored response
                cost_usd=cost,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                duration_ms=duration_ms,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            try:
                database.update_automation_run(run_id, session_id=session_id, **run_result)
            except psycopg2.errors.ForeignKeyViolation:
                logger.warning(
                    f"Automation {automation_id} run {run_id}: session {session_id} "
                    f"was deleted mid-run; recording completed run without session link"
                )
                database.update_automation_run(run_id, session_id=None, **run_result)

            # Update automation stats. The increments happen in SQL (one
            # atomic UPDATE) so a concurrent run on another install can't
            # make this stale in-memory row lose a count or a cost.
            database.record_automation_run_success(automation_id, cost)

            # Deliver output
            await self._deliver_output(automation, run_id, response_text)

        except asyncio.TimeoutError:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"Execution timed out after {automation.get('timeout_seconds', 300)}s"
            database.update_automation_run(
                run_id, status="failed", error=error_msg,
                duration_ms=duration_ms, completed_at=datetime.now(timezone.utc).isoformat()
            )
            database.update_automation(
                automation_id, status="error", last_error=error_msg,
                failure_count=automation.get("failure_count", 0) + 1,
            )

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
            database.update_automation_run(
                run_id, status="failed", error=error_msg,
                duration_ms=duration_ms, completed_at=datetime.now(timezone.utc).isoformat()
            )
            database.update_automation(
                automation_id, status="error", last_error=error_msg,
                failure_count=automation.get("failure_count", 0) + 1,
            )
            raise

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

                    automation = database.get_automation(automation_id)
                    profile_id = step.get("profile_id") or (automation.get("profile_id") if automation else None)
                    max_turns = step.get("max_turns", 1)

                    result = await account_failover.execute_query_with_failover(
                        prompt=rendered,
                        profile_id=profile_id,
                        overrides={"max_turns": max_turns},
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
        approval = database.get_automation_approval(approval_id)
        if not approval or approval["status"] != "approved":
            return None

        run = database.get_automation_run(approval["run_id"])
        if not run:
            return None

        automation = database.get_automation(run["automation_id"])
        if not automation:
            return None

        # Execute in background
        async def _run():
            async with self._lock:
                self._running[automation["id"]] = asyncio.current_task()
            try:
                await self._execute_run(
                    automation,
                    run["id"],
                    run.get("prompt_rendered") or automation.get("prompt") or "",
                    run.get("trigger_data", {}),
                )
            finally:
                async with self._lock:
                    self._running.pop(automation["id"], None)

        self._create_tracked_task(_run(), name=f"auto-approved-{run['id']}")
        return run

    # ------------------------------------------------------------------
    # Output delivery
    # ------------------------------------------------------------------

    async def _deliver_output(
        self, automation: Dict[str, Any], run_id: str, response: str
    ) -> None:
        """Deliver the automation result to the configured output."""
        output_config = automation.get("output_config", {})
        output_type = output_config.get("type", "session")

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
                return

        # --- Deduplicate identical responses within window ---
        dedup_window = output_config.get("deduplicate_window_seconds")
        if dedup_window and isinstance(dedup_window, (int, float)) and dedup_window > 0 and response:
            recent = database.get_automation_runs(automation["id"], limit=5)
            now = datetime.now(timezone.utc)
            for prev in recent:
                if prev["id"] == run_id:
                    continue
                if (
                    prev.get("response") == response
                    and prev.get("output_status") == "delivered"
                ):
                    completed = prev.get("completed_at")
                    if completed:
                        try:
                            completed_dt = datetime.fromisoformat(
                                str(completed).replace("Z", "+00:00").replace("+00:00", "")
                            )
                            age = (now - completed_dt).total_seconds()
                            if 0 < age < dedup_window:
                                logger.info(
                                    f"Automation {automation['id']} run {run_id}: "
                                    f"deduplicated (same response {age:.0f}s ago)"
                                )
                                database.update_automation_run(
                                    run_id, output_status="deduplicated"
                                )
                                return
                        except (ValueError, TypeError) as parse_err:
                            logger.debug("Skipping dedup check for run %s: timestamp parse error: %s", prev["id"], parse_err)

        if output_type == "session":
            # Already saved via execute_query
            database.update_automation_run(run_id, output_status="delivered")
            return

        if output_type == "none":
            database.update_automation_run(run_id, output_status="delivered")
            return

        try:
            if output_type == "connector":
                await self._deliver_to_connector(output_config, response)
            elif output_type == "webhook":
                await self._deliver_to_webhook(output_config, automation, response)

            database.update_automation_run(run_id, output_status="delivered")

        except Exception as e:
            logger.error(f"Output delivery failed for run {run_id}: {e}")
            database.update_automation_run(
                run_id, output_status="failed", output_error=str(e)
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

            # Get all active connectors to notify about pending approval
            connectors = database.get_active_connectors()
            for conn_row in connectors:
                cid = conn_row["id"]
                instance = await connector_manager.get_running_connector(cid)
                if not instance:
                    continue

                # Get active connector sessions to notify
                from app.db.base import get_db, rows_to_list
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT * FROM connector_sessions WHERE connector_id = %s AND is_active = TRUE",
                        (cid,),
                    )
                    sessions = rows_to_list(cursor.fetchall())

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
