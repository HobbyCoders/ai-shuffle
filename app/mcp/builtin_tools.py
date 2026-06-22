"""
Claude Code builtin tools MCP server — adapts harness-level tools for AI Shuffle.

Claude Code CLI ships several "builtin" tools that normally require harness
cooperation (ScheduleWakeup, Monitor, CronCreate, EnterWorktree, etc). When
running under the Claude Agent SDK those tools don't exist natively, so we
expose equivalent implementations as MCP tools that bridge to AI Shuffle's
existing infrastructure (session pool, worktree manager, automation manager,
push notification service).

Tools are registered per-session so that session-scoped actions
(ScheduleWakeup firing a new prompt, EnterWorktree tying a worktree to the
calling session) have the context they need.

Tool naming convention: ``mcp__builtins__<toolName>``
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Helpers
# ============================================================================

def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Format a successful tool result for the SDK MCP envelope."""
    import json as _json
    return {"content": [{"type": "text", "text": _json.dumps({"success": True, **payload})}]}


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    """Format an error tool result for the SDK MCP envelope."""
    import json as _json
    return {"content": [{"type": "text", "text": _json.dumps({"success": False, "error": message, **extra})}]}


# ============================================================================
# Background task registry (Monitor / TaskOutput / TaskStop)
# ============================================================================

class _BackgroundTask:
    """A spawned subprocess whose stdout is buffered for later retrieval."""

    def __init__(self, task_id: str, command: str, cwd: Optional[str]) -> None:
        self.task_id = task_id
        self.command = command
        self.cwd = cwd
        self.lines: List[str] = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.exit_code: Optional[int] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None


_background_tasks: Dict[str, _BackgroundTask] = {}
_BACKGROUND_LINE_LIMIT = 5000  # avoid unbounded growth


def _registry_safe(fn) -> None:
    """Run a task-registry call, swallowing any error so it can never break a
    spawn/reader path. The registry is a best-effort observability surface."""
    try:
        fn()
    except Exception:
        logger.debug("task_registry update failed", exc_info=True)


async def _spawn_background(
    command: str,
    cwd: Optional[str],
    session_id: Optional[str] = None,
) -> _BackgroundTask:
    task_id = str(uuid.uuid4())[:8]
    task = _BackgroundTask(task_id=task_id, command=command, cwd=cwd)
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    task.process = proc

    # Register the shell task so TaskList / /live can surface it. Keyed by the
    # AI Shuffle session that spawned it; module-level spawns (no session) skip
    # registration.
    if session_id:
        from app.core.task_registry import task_registry
        _registry_safe(
            lambda: task_registry.start_task(session_id, task_id, "shell", command)
        )

    async def _reader() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            task.lines.append(decoded)
            if len(task.lines) > _BACKGROUND_LINE_LIMIT:
                # drop oldest
                del task.lines[: len(task.lines) - _BACKGROUND_LINE_LIMIT]
        task.exit_code = await proc.wait()
        if session_id:
            from app.core.task_registry import task_registry
            status = "completed" if task.exit_code == 0 else "error"

            def _finish() -> None:
                # Don't clobber a deliberate TaskStop "killed" with the reader's
                # "error" (terminate() yields a non-zero exit code).
                existing = (
                    task_registry._tasks.get(session_id, {}).get(task_id) or {}
                ).get("status")
                if existing == "killed":
                    return
                task_registry.complete_task(session_id, task_id, status)

            _registry_safe(_finish)

    task._reader_task = asyncio.create_task(_reader(), name=f"monitor-{task_id}")
    _background_tasks[task_id] = task
    return task


# ============================================================================
# Tool factory
# ============================================================================

def create_builtin_tools_server(
    session_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    is_admin: bool = False,
) -> Optional[Any]:
    """
    Build the ``mcp__builtins__*`` MCP server for a session.

    Args:
        session_id: The current AI Shuffle session (used by ScheduleWakeup to
            target the wakeup prompt back at the correct session).
        profile_id: Profile to use when firing scheduled/wake prompts.
        project_id: Project context for worktree and cron tools.
        is_admin: Whether the caller has admin permissions (gated for
            destructive operations like CronDelete).

    Returns:
        SDK MCP server config, or ``None`` if the SDK is unavailable.
    """
    try:
        from claude_agent_sdk import tool, create_sdk_mcp_server
    except ImportError:
        logger.debug("claude_agent_sdk not available — builtin tools disabled")
        return None

    from app.mcp.sdk_compat import patch_sdk_server

    all_tools: list = []

    # ------------------------------------------------------------------
    # ScheduleWakeup — pause + resume session after a delay
    # ------------------------------------------------------------------
    @tool(
        "ScheduleWakeup",
        "Schedule the session to resume after a delay by re-firing a prompt. "
        "Use when you want the agent to stop now and continue work later on a timer. "
        "delaySeconds is clamped to [60, 3600]. prompt is the /loop input to fire on wake-up.",
        {
            "type": "object",
            "properties": {
                "delaySeconds": {"type": "number", "description": "Seconds from now to wake up (60-3600)."},
                "prompt": {"type": "string", "description": "Prompt to fire on wake-up."},
                "reason": {"type": "string", "description": "Short explanation of the chosen delay."},
            },
            "required": ["delaySeconds", "prompt"],
        },
    )
    async def schedule_wakeup(args: dict) -> dict:
        if not session_id or not profile_id:
            return _err("ScheduleWakeup requires an active session context.")
        try:
            delay = float(args.get("delaySeconds") or 0)
        except (TypeError, ValueError):
            return _err("delaySeconds must be a number.")
        delay = max(60.0, min(3600.0, delay))
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return _err("prompt is required.")
        reason = (args.get("reason") or "").strip()

        async def _wake() -> None:
            try:
                await asyncio.sleep(delay)
                from app.core.query_engine import execute_query
                logger.info(f"ScheduleWakeup firing for session {session_id} after {delay}s")
                await execute_query(
                    prompt=prompt,
                    profile_id=profile_id,
                    project_id=project_id,
                    session_id=session_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ScheduleWakeup wake task failed")

        asyncio.create_task(_wake(), name=f"wakeup-{session_id}")
        fire_at = datetime.now(timezone.utc).timestamp() + delay
        return _ok({
            "scheduled": True,
            "delay_seconds": delay,
            "fire_at_unix": fire_at,
            "reason": reason,
        })

    all_tools.append(schedule_wakeup)

    # ------------------------------------------------------------------
    # PushNotification
    # ------------------------------------------------------------------
    @tool(
        "PushNotification",
        "Send a push notification to all registered mobile devices. "
        "Use for alerting the user to long-running task completion or important events.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "data": {"type": "object", "description": "Optional custom data payload."},
            },
            "required": ["title", "body"],
        },
    )
    async def push_notification(args: dict) -> dict:
        title = (args.get("title") or "").strip()
        body = (args.get("body") or "").strip()
        if not title or not body:
            return _err("title and body are required.")
        try:
            from app.services.push_notification import push_notification_service
            results = await push_notification_service.send_to_all_devices(
                title=title, body=body, data=args.get("data"),
            )
            successful = sum(1 for r in results if getattr(r, "success", False))
            return _ok({"sent": len(results), "successful": successful})
        except Exception as e:
            logger.exception("PushNotification failed")
            return _err(f"send failed: {e}")

    all_tools.append(push_notification)

    # ------------------------------------------------------------------
    # EnterWorktree / ExitWorktree
    # ------------------------------------------------------------------
    @tool(
        "EnterWorktree",
        "Create (or reuse) an isolated git worktree for the current project and return its details. "
        "The worktree path can be used as cwd for subsequent tool calls. Requires an active project.",
        {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string", "description": "Branch to check out."},
                "create_new_branch": {"type": "boolean", "description": "If true, create a new branch from base_branch."},
                "base_branch": {"type": "string", "description": "Base branch when creating a new one."},
            },
            "required": ["branch_name"],
        },
    )
    async def enter_worktree(args: dict) -> dict:
        if not project_id:
            return _err("EnterWorktree requires an active project context.")
        branch = (args.get("branch_name") or "").strip()
        if not branch:
            return _err("branch_name is required.")
        try:
            from app.core.worktree_manager import worktree_manager
            wt = await asyncio.to_thread(
                worktree_manager.create_worktree,
                project_id=project_id,
                branch_name=branch,
                create_new_branch=bool(args.get("create_new_branch")),
                base_branch=args.get("base_branch"),
            )
            return _ok({
                "worktree_id": wt.get("id"),
                "branch_name": wt.get("branch_name"),
                "worktree_path": wt.get("worktree_path"),
                "status": wt.get("status"),
            })
        except Exception as e:
            logger.exception("EnterWorktree failed")
            return _err(f"create worktree failed: {e}")

    all_tools.append(enter_worktree)

    @tool(
        "ExitWorktree",
        "Clean up a worktree by ID. Optionally keep the underlying branch.",
        {
            "type": "object",
            "properties": {
                "worktree_id": {"type": "string"},
                "keep_branch": {"type": "boolean", "description": "Keep the branch after removing the worktree (default true)."},
            },
            "required": ["worktree_id"],
        },
    )
    async def exit_worktree(args: dict) -> dict:
        wt_id = (args.get("worktree_id") or "").strip()
        if not wt_id:
            return _err("worktree_id is required.")
        try:
            from app.core.worktree_manager import worktree_manager
            keep = args.get("keep_branch", True)
            ok = await worktree_manager.cleanup_worktree(wt_id, keep_branch=bool(keep))
            return _ok({"removed": ok, "worktree_id": wt_id})
        except Exception as e:
            logger.exception("ExitWorktree failed")
            return _err(f"cleanup worktree failed: {e}")

    all_tools.append(exit_worktree)

    # ------------------------------------------------------------------
    # CronCreate / CronList / CronDelete (bridge to automations)
    # ------------------------------------------------------------------
    @tool(
        "CronCreate",
        "Create a recurring scheduled prompt (backed by an AI Shuffle automation). "
        "Use 5-field cron expressions (e.g. '0 9 * * 1-5' = weekdays at 9am).",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "expression": {"type": "string", "description": "5-field cron expression."},
                "prompt": {"type": "string", "description": "Prompt to run on each fire."},
                "timezone": {"type": "string", "description": "IANA timezone (default UTC)."},
                "max_cost_per_run": {"type": "number"},
                "max_runs_per_day": {"type": "integer"},
            },
            "required": ["name", "expression", "prompt"],
        },
    )
    async def cron_create(args: dict) -> dict:
        if not profile_id:
            return _err("CronCreate requires a profile context.")
        name = (args.get("name") or "").strip()
        expression = (args.get("expression") or "").strip()
        prompt = (args.get("prompt") or "").strip()
        if not name or not expression or not prompt:
            return _err("name, expression, and prompt are required.")
        try:
            from app.db import automations as auto_db
            from app.core.automation_manager import automation_manager
            automation_id = str(uuid.uuid4())
            trigger_config = {
                "expression": expression,
                "timezone": args.get("timezone") or "UTC",
            }
            row = await asyncio.to_thread(
                auto_db.create_automation,
                automation_id=automation_id,
                name=name,
                trigger_type="cron",
                trigger_config=trigger_config,
                profile_id=profile_id,
                prompt=prompt,
                project_id=project_id,
                max_cost_per_run=args.get("max_cost_per_run"),
                max_runs_per_day=args.get("max_runs_per_day"),
            )
            if not row:
                return _err("automation not created.")
            # Register with the scheduler so it actually runs.
            try:
                await automation_manager.schedule_automation(automation_id)
            except Exception:
                logger.exception("Failed to register cron with scheduler")
            return _ok({"automation_id": automation_id, "name": name, "expression": expression})
        except Exception as e:
            logger.exception("CronCreate failed")
            return _err(f"create failed: {e}")

    all_tools.append(cron_create)

    @tool(
        "CronList",
        "List existing scheduled automations (cron-triggered).",
        {"type": "object", "properties": {}},
    )
    async def cron_list(args: dict) -> dict:
        try:
            from app.db import automations as auto_db
            rows = await asyncio.to_thread(auto_db.get_all_automations)
            out = []
            for r in rows or []:
                triggers = r.get("triggers") or []
                cron_triggers = [t for t in triggers if t.get("type") == "cron"]
                if not cron_triggers:
                    continue
                out.append({
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "status": r.get("status"),
                    "expressions": [t.get("config", {}).get("expression") for t in cron_triggers],
                    "last_run_at": r.get("last_run_at"),
                })
            return _ok({"crons": out, "count": len(out)})
        except Exception as e:
            logger.exception("CronList failed")
            return _err(f"list failed: {e}")

    all_tools.append(cron_list)

    @tool(
        "CronDelete",
        "Delete a scheduled automation by ID. Requires confirm=true.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            "required": ["automation_id"],
        },
    )
    async def cron_delete(args: dict) -> dict:
        if not is_admin:
            return _err("CronDelete requires admin session.")
        if not args.get("confirm"):
            return _err("confirm=true required for delete.")
        aid = (args.get("automation_id") or "").strip()
        if not aid:
            return _err("automation_id is required.")
        try:
            from app.db import automations as auto_db
            from app.core.automation_manager import automation_manager
            try:
                await automation_manager.unschedule_automation(aid)
            except AttributeError:
                pass  # older impl
            except Exception:
                logger.exception("Failed to unregister cron from scheduler")
            ok = await asyncio.to_thread(auto_db.delete_automation, aid)
            return _ok({"deleted": ok, "automation_id": aid})
        except Exception as e:
            logger.exception("CronDelete failed")
            return _err(f"delete failed: {e}")

    all_tools.append(cron_delete)

    # ------------------------------------------------------------------
    # RemoteTrigger — fire an existing automation on demand
    # ------------------------------------------------------------------
    @tool(
        "RemoteTrigger",
        "Trigger an existing automation immediately. Use for ad-hoc runs of scheduled agents.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
            },
            "required": ["automation_id"],
        },
    )
    async def remote_trigger(args: dict) -> dict:
        aid = (args.get("automation_id") or "").strip()
        if not aid:
            return _err("automation_id is required.")
        try:
            from app.core.automation_manager import automation_manager
            result = await automation_manager.trigger_manual(aid)
            if result is None:
                return _err("automation not found.", automation_id=aid)
            return _ok(result)
        except Exception as e:
            logger.exception("RemoteTrigger failed")
            return _err(f"trigger failed: {e}")

    all_tools.append(remote_trigger)

    # ------------------------------------------------------------------
    # Monitor / TaskOutput / TaskStop — background shell
    # ------------------------------------------------------------------
    @tool(
        "Monitor",
        "Spawn a long-running shell command in the background and stream stdout into a buffer. "
        "Returns a task_id; use TaskOutput to read output and TaskStop to cancel.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Optional working directory."},
            },
            "required": ["command"],
        },
    )
    async def monitor(args: dict) -> dict:
        command = (args.get("command") or "").strip()
        if not command:
            return _err("command is required.")
        try:
            task = await _spawn_background(command, args.get("cwd"), session_id)
            return _ok({
                "task_id": task.task_id,
                "pid": task.process.pid if task.process else None,
                "started_at": task.started_at,
            })
        except Exception as e:
            logger.exception("Monitor spawn failed")
            return _err(f"spawn failed: {e}")

    all_tools.append(monitor)

    @tool(
        "TaskOutput",
        "Read buffered stdout/stderr from a background task spawned by Monitor. "
        "Optionally pass since_line to get only new lines.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "since_line": {"type": "integer", "description": "Line index to start from (exclusive)."},
                "tail": {"type": "integer", "description": "Return at most N most recent lines."},
            },
            "required": ["task_id"],
        },
    )
    async def task_output(args: dict) -> dict:
        tid = (args.get("task_id") or "").strip()
        task = _background_tasks.get(tid)
        if not task:
            return _err("task not found.", task_id=tid)
        lines = task.lines
        since = int(args.get("since_line") or 0)
        if since > 0:
            lines = lines[since:]
        tail = args.get("tail")
        if tail is not None:
            try:
                lines = lines[-int(tail):]
            except (TypeError, ValueError):
                pass
        return _ok({
            "task_id": tid,
            "running": task.is_running(),
            "exit_code": task.exit_code,
            "total_lines": len(task.lines),
            "lines": lines,
        })

    all_tools.append(task_output)

    @tool(
        "TaskStop",
        "Stop a background task spawned by Monitor.",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    )
    async def task_stop(args: dict) -> dict:
        tid = (args.get("task_id") or "").strip()
        task = _background_tasks.get(tid)
        if not task:
            return _err("task not found.", task_id=tid)
        if task.process and task.process.returncode is None:
            try:
                task.process.terminate()
                try:
                    await asyncio.wait_for(task.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    task.process.kill()
                    await task.process.wait()
            except ProcessLookupError:
                pass
        # Mark the task killed in the registry. The reader's completion path may
        # also fire (with exit_code from terminate); the last write wins and both
        # are terminal, so the surfaced status stays sane.
        if session_id:
            from app.core.task_registry import task_registry
            _registry_safe(
                lambda: task_registry.complete_task(session_id, tid, "killed")
            )
        return _ok({
            "task_id": tid,
            "running": task.is_running(),
            "exit_code": task.exit_code,
        })

    all_tools.append(task_stop)

    # ------------------------------------------------------------------
    # TaskList — live view of this session's background tasks
    # ------------------------------------------------------------------
    @tool(
        "TaskList",
        "List your currently-running background tasks for this session. "
        "Covers both background AGENT tasks (Task tool) and background shell "
        "commands spawned via Monitor. This is a LIVE query — it reflects task "
        "completions immediately, so call it whenever you need the current "
        "picture (e.g. before deciding whether to wait, follow up, or move on) "
        "rather than relying on an earlier snapshot.",
        {
            "type": "object",
            "properties": {
                "include_finished": {
                    "type": "boolean",
                    "description": "Include recently-finished tasks (default true).",
                },
            },
        },
    )
    async def task_list(args: dict) -> dict:
        if not session_id:
            return _err("TaskList requires an active session context.")
        from app.core.task_registry import task_registry

        include_finished = args.get("include_finished", True)
        if include_finished:
            tasks = task_registry.get_all(session_id)
        else:
            tasks = task_registry.get_active(session_id)
        running_count = sum(1 for t in tasks if t.get("status") == "running")
        return _ok({"tasks": tasks, "running_count": running_count})

    all_tools.append(task_list)

    # ------------------------------------------------------------------
    # ToolSearch — search local tool registry
    # ------------------------------------------------------------------
    @tool(
        "ToolSearch",
        "Search available tools by keyword or exact name. Returns tool metadata (name, category, description).",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords or 'select:Name1,Name2' for exact match."},
                "max_results": {"type": "integer", "description": "Default 5."},
            },
            "required": ["query"],
        },
    )
    async def tool_search(args: dict) -> dict:
        query = (args.get("query") or "").strip()
        if not query:
            return _err("query is required.")
        max_results = int(args.get("max_results") or 5)
        try:
            from app.api.system import BUILTIN_TOOLS, discover_tools_from_cli, get_tool_description, categorize_tool
            catalog = {t["name"]: t for t in BUILTIN_TOOLS}
            # Try to enrich from dynamic discovery if available
            try:
                discovered = (await discover_tools_from_cli()).get("tools") or []
                for name in discovered:
                    if name not in catalog:
                        catalog[name] = {
                            "name": name,
                            "category": categorize_tool(name),
                            "description": get_tool_description(name),
                        }
            except Exception:
                pass

            if query.startswith("select:"):
                names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
                hits = [catalog[n] for n in names if n in catalog]
            else:
                terms = [t for t in query.lower().split() if t]
                scored: list = []
                for t in catalog.values():
                    hay = f"{t['name']} {t.get('description', '')}".lower()
                    score = sum(1 for term in terms if term in hay)
                    if score:
                        scored.append((score, t))
                scored.sort(key=lambda x: (-x[0], x[1]["name"]))
                hits = [t for _, t in scored[:max_results]]
            return _ok({"results": hits, "count": len(hits)})
        except Exception as e:
            logger.exception("ToolSearch failed")
            return _err(f"search failed: {e}")

    all_tools.append(tool_search)

    # ------------------------------------------------------------------
    # GetAppErrors — read the durable app error log
    # ------------------------------------------------------------------
    @tool(
        "GetAppErrors",
        "Read recent application errors (frontend, backend, API, and WebSocket) "
        "from AI Shuffle's error log so you can debug what the user just hit. Use "
        "this when the user says something like 'I got an error, check the logs and "
        "fix it'. Each entry has source, level, message, detail (stack trace), url, "
        "status_code, occurrence_count, and timestamps.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max errors to return (default 20, max 200)."},
                "source": {"type": "string", "description": "Filter by source: frontend, backend, ws, or api."},
                "this_session_only": {"type": "boolean", "description": "Only errors tied to this chat session."},
                "since_minutes": {"type": "integer", "description": "Only errors seen within the last N minutes."},
            },
            "required": [],
        },
    )
    async def get_app_errors(args: dict) -> dict:
        try:
            from app.db import error_logs

            limit = max(1, min(int(args.get("limit") or 20), 200))
            source = (args.get("source") or "").strip() or None
            since = None
            since_minutes = args.get("since_minutes")
            if since_minutes:
                try:
                    since = datetime.now(timezone.utc) - timedelta(minutes=float(since_minutes))
                except (TypeError, ValueError):
                    since = None
            sess = session_id if args.get("this_session_only") else None

            errors = await asyncio.to_thread(
                error_logs.get_recent_errors,
                limit=limit,
                source=source,
                session_id=sess,
                since=since,
            )
            return _ok({"errors": errors, "count": len(errors)})
        except Exception as e:
            logger.exception("GetAppErrors failed")
            return _err(f"failed to read errors: {e}")

    all_tools.append(get_app_errors)

    # ------------------------------------------------------------------
    # Server assembly
    # ------------------------------------------------------------------
    logger.info(
        "Registering %d builtin tool(s) as MCP tools (session=%s, project=%s, admin=%s)",
        len(all_tools), session_id, project_id, is_admin,
    )
    return patch_sdk_server(
        create_sdk_mcp_server(name="builtins", version="1.0.0", tools=all_tools),
        all_tools,
    )


def get_builtin_tool_names() -> List[str]:
    """Return MCP tool names so they can be added to ``allowed_tools``."""
    return [
        "mcp__builtins__ScheduleWakeup",
        "mcp__builtins__PushNotification",
        "mcp__builtins__EnterWorktree",
        "mcp__builtins__ExitWorktree",
        "mcp__builtins__CronCreate",
        "mcp__builtins__CronList",
        "mcp__builtins__CronDelete",
        "mcp__builtins__RemoteTrigger",
        "mcp__builtins__Monitor",
        "mcp__builtins__TaskOutput",
        "mcp__builtins__TaskStop",
        "mcp__builtins__TaskList",
        "mcp__builtins__ToolSearch",
        "mcp__builtins__GetAppErrors",
    ]
