"""
Agent Execution Engine

Manages the lifecycle of background autonomous agents:
- Launches agents in isolated git worktrees
- Tracks progress and streams updates via WebSocket
- Handles pause/resume/cancel operations
- Manages auto-PR creation and review workflows

Based on patterns from query_engine.py but adapted for long-running background execution.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Set, Coroutine
from dataclasses import dataclass, field
from enum import Enum

from claude_agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient, AgentDefinition
from claude_agent_sdk import (
    AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    ResultMessage, SystemMessage
)

from app.db import database
from app.core.config import settings
from app.core.profiles import get_profile
from app.core.worktree_manager import worktree_manager
from app.core.git_service import git_service
from app.core.query_engine import (
    build_options_from_profile,
    write_agents_to_filesystem,
    cleanup_agents_directory,
    truncate_large_payload,
    SECURITY_INSTRUCTIONS,
    DEFAULT_SDK_BUFFER_SIZE
)
from app.core.platform import detect_deployment_mode, DeploymentMode

logger = logging.getLogger(__name__)


class AgentStatus(str, Enum):
    """Agent run status values"""
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStatus(str, Enum):
    """Agent task status values"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentRunState:
    """Track state for an active agent run"""
    agent_run_id: str
    task: Optional[asyncio.Task] = None
    client: Optional[ClaudeSDKClient] = None
    sdk_session_id: Optional[str] = None
    is_paused: bool = False
    cancel_requested: bool = False
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    worktree_path: Optional[str] = None
    branch_name: Optional[str] = None
    written_agent_ids: List[str] = field(default_factory=list)
    agents_dir: Optional[Path] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self):
        # Start with pause event set (not paused)
        self.pause_event.set()


# Type for WebSocket broadcast callback (async function)
BroadcastCallback = Callable[[str, str, Dict[str, Any]], Coroutine[Any, Any, None]]


class AgentExecutionEngine:
    """
    Manages background agent execution with concurrency control.

    Features:
    - Concurrent agent execution with configurable max slots
    - Queue management for overflow
    - Isolated git worktrees per agent
    - Real-time progress updates via WebSocket
    - Pause/resume/cancel support
    - Auto-PR creation on completion
    - Auto-review workflow
    """

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self._active_runs: Dict[str, AgentRunState] = {}
        self._broadcast_callback: Optional[BroadcastCallback] = None
        self._lock = asyncio.Lock()
        self._queue_processor_task: Optional[asyncio.Task] = None
        self._shutdown = False

    def set_broadcast_callback(self, callback: BroadcastCallback):
        """Set the WebSocket broadcast callback for real-time updates"""
        self._broadcast_callback = callback

    async def _broadcast(self, agent_run_id: str, event_type: str, data: Dict[str, Any]):
        """Broadcast an update to WebSocket subscribers"""
        if self._broadcast_callback:
            try:
                await self._broadcast_callback(agent_run_id, event_type, data)
            except Exception as e:
                logger.warning(f"Failed to broadcast update: {e}")

    def _log(self, agent_run_id: str, message: str, level: str = "info", metadata: Optional[Dict] = None):
        """Add a log entry for an agent run and broadcast it"""
        log_entry = database.add_agent_log(agent_run_id, message, level, metadata)
        # Fire and forget broadcast
        if self._broadcast_callback:
            asyncio.create_task(self._broadcast(agent_run_id, "agent_log", {
                "level": level,
                "message": message,
                "metadata": metadata,
                "timestamp": log_entry["timestamp"] if log_entry else datetime.utcnow().isoformat()
            }))

    async def start(self):
        """Start the engine and queue processor"""
        self._shutdown = False
        self._queue_processor_task = asyncio.create_task(self._process_queue_loop())
        logger.info("Agent execution engine started")

    async def stop(self):
        """Stop the engine and cancel all running agents"""
        self._shutdown = True

        # Cancel queue processor
        if self._queue_processor_task:
            self._queue_processor_task.cancel()
            try:
                await self._queue_processor_task
            except asyncio.CancelledError:
                pass

        # Cancel all running agents
        for agent_run_id in list(self._active_runs.keys()):
            await self.cancel_agent(agent_run_id, reason="Engine shutdown")

        logger.info("Agent execution engine stopped")

    async def _process_queue_loop(self):
        """Background task that processes queued agents when slots are available"""
        while not self._shutdown:
            try:
                await self._process_queue()
                await asyncio.sleep(2)  # Check every 2 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Queue processor error: {e}")
                await asyncio.sleep(5)

    async def _process_queue(self):
        """Start queued agents if slots are available"""
        async with self._lock:
            # Count active (running or paused) agents
            active_count = sum(
                1 for state in self._active_runs.values()
                if state.task and not state.task.done()
            )

            if active_count >= self.max_concurrent:
                return

            # Get queued agents
            queued = database.get_queued_agent_runs()
            slots_available = self.max_concurrent - active_count

            for agent_run in queued[:slots_available]:
                agent_run_id = agent_run["id"]
                if agent_run_id not in self._active_runs:
                    # Start the agent
                    await self._start_agent_run(agent_run)

    async def launch_agent(
        self,
        name: str,
        prompt: str,
        profile_id: Optional[str] = None,
        project_id: Optional[str] = None,
        auto_branch: bool = True,
        auto_pr: bool = True,
        auto_merge: bool = False,
        auto_review: bool = False,
        max_duration_minutes: int = 0,
        base_branch: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Launch a new background agent.

        The agent will be queued and started when a slot is available.

        Simplified workflow:
        - Always creates a new feature branch (auto_branch=True)
        - Always creates a PR on completion (auto_pr=True)
        - Optionally merge PR and cleanup (auto_merge=False)
        - Runs until completion (max_duration_minutes=0 = unlimited)
        - No auto-review (auto_review=False)
        """
        agent_run_id = f"agent-{uuid.uuid4().hex[:12]}"

        # Create database record
        agent_run = database.create_agent_run(
            agent_run_id=agent_run_id,
            name=name,
            prompt=prompt,
            profile_id=profile_id,
            project_id=project_id,
            auto_branch=auto_branch,
            auto_pr=auto_pr,
            auto_merge=auto_merge,
            auto_review=auto_review,
            max_duration_minutes=max_duration_minutes,
            base_branch=base_branch
        )

        self._log(agent_run_id, f"Agent '{name}' created and queued")

        # Broadcast launch event
        await self._broadcast(agent_run_id, "agent_launched", agent_run)

        # Trigger queue processing
        asyncio.create_task(self._process_queue())

        logger.info(f"Launched agent {agent_run_id}: {name}")
        return agent_run

    async def _start_agent_run(self, agent_run: Dict[str, Any]):
        """Start executing an agent run"""
        agent_run_id = agent_run["id"]

        # Create state tracker
        state = AgentRunState(agent_run_id=agent_run_id)
        self._active_runs[agent_run_id] = state

        # Update status to running
        database.update_agent_run(agent_run_id, status=AgentStatus.RUNNING.value)
        self._log(agent_run_id, "Agent execution starting")

        await self._broadcast(agent_run_id, "agent_started", {
            "status": AgentStatus.RUNNING.value
        })

        # Start the execution task
        state.task = asyncio.create_task(
            self._execute_agent(agent_run_id, agent_run, state)
        )

    async def _execute_agent(
        self,
        agent_run_id: str,
        agent_run: Dict[str, Any],
        state: AgentRunState
    ):
        """
        Main agent execution loop.

        This runs the Claude SDK query and handles:
        - Git worktree setup (if auto_branch enabled)
        - Progress tracking
        - Task detection from tool usage
        - Pause/resume handling
        - Timeout enforcement
        - Cleanup on completion/failure
        """
        try:
            # Setup phase
            await self._setup_agent_environment(agent_run_id, agent_run, state)

            # Get profile
            profile_id = agent_run.get("profile_id")
            profile = get_profile(profile_id) if profile_id else None
            if not profile:
                # Use default profile
                profiles = database.get_all_profiles()
                if profiles:
                    profile = profiles[0]
                else:
                    raise ValueError("No profile available for agent execution")

            # Get project for working directory
            project_id = agent_run.get("project_id")
            project = database.get_project(project_id) if project_id else None

            # Build SDK options
            # Use worktree path if we created one, otherwise project path
            working_dir = state.worktree_path
            if not working_dir and project:
                working_dir = str(settings.workspace_dir / project["path"])
            elif not working_dir:
                working_dir = str(settings.workspace_dir)

            # Build options from profile with worktree path override
            # The cwd override takes highest priority in build_options_from_profile
            options, agents_dict = build_options_from_profile(
                profile=profile,
                project=project,
                overrides={"cwd": working_dir}
            )

            # Write agents to filesystem if needed (Windows workaround)
            if agents_dict and detect_deployment_mode() == DeploymentMode.LOCAL:
                state.agents_dir = write_agents_to_filesystem(agents_dict, working_dir)
                state.written_agent_ids = list(agents_dict.keys())

            # Build the enhanced prompt with context
            enhanced_prompt = self._build_agent_prompt(agent_run, state)

            # Set up timeout (0 = unlimited)
            max_duration = agent_run.get("max_duration_minutes", 30)
            unlimited_duration = max_duration <= 0
            timeout_at = None if unlimited_duration else datetime.utcnow() + timedelta(minutes=max_duration)

            if unlimited_duration:
                self._log(agent_run_id, "Starting execution with unlimited duration")
            else:
                self._log(agent_run_id, f"Starting execution with {max_duration} minute timeout")
            self._log(agent_run_id, f"Working directory: {working_dir}")
            if state.branch_name:
                self._log(agent_run_id, f"Branch: {state.branch_name}")

            # Execute the query
            response_text = []
            sdk_session_id = None
            progress = 0
            task_count = 0

            async for message in query(prompt=enhanced_prompt, options=options):
                # Check for pause
                await state.pause_event.wait()

                # Check for cancel
                if state.cancel_requested:
                    self._log(agent_run_id, "Cancellation requested", "warning")
                    raise asyncio.CancelledError("User cancelled")

                # Check timeout (skip if unlimited)
                if timeout_at and datetime.utcnow() > timeout_at:
                    self._log(agent_run_id, f"Agent timed out after {max_duration} minutes", "warning")
                    raise TimeoutError(f"Agent exceeded maximum duration of {max_duration} minutes")

                state.last_activity = datetime.utcnow()

                # Process message
                if isinstance(message, SystemMessage):
                    if message.subtype == "init" and "session_id" in message.data:
                        sdk_session_id = message.data["session_id"]
                        state.sdk_session_id = sdk_session_id
                        database.update_agent_run(agent_run_id, sdk_session_id=sdk_session_id)

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            response_text.append(block.text)
                            # Log significant text (first 200 chars)
                            if len(block.text) > 10:
                                preview = block.text[:200] + "..." if len(block.text) > 200 else block.text
                                self._log(agent_run_id, preview, metadata={"type": "text"})

                        elif isinstance(block, ToolUseBlock):
                            # Track tool usage
                            tool_name = block.name
                            self._log(agent_run_id, f"Using tool: {tool_name}", metadata={
                                "type": "tool_use",
                                "tool": tool_name
                            })

                            # Detect task-like tools
                            if tool_name == "TodoWrite":
                                # Parse and track tasks
                                await self._handle_todo_write(agent_run_id, block.input)
                            elif tool_name == "Task":
                                # Subagent invocation
                                task_count += 1
                                subagent_type = block.input.get("subagent_type", "worker")
                                self._log(agent_run_id, f"Spawning subagent: {subagent_type}")

                            # Update progress estimate based on activity
                            progress = min(95, progress + 2)
                            database.update_agent_run(agent_run_id, progress=progress)
                            await self._broadcast(agent_run_id, "agent_progress", {
                                "progress": progress
                            })

                        elif isinstance(block, ToolResultBlock):
                            output = truncate_large_payload(block.content)
                            # Only log errors or significant results
                            if "error" in output.lower() or "failed" in output.lower():
                                self._log(agent_run_id, f"Tool result: {output[:500]}", "warning")

                elif isinstance(message, ResultMessage):
                    # Query completed
                    self._log(agent_run_id, f"Execution completed. Turns: {message.num_turns}, Cost: ${message.total_cost_usd:.4f}")

            # Execution completed successfully
            await self._complete_agent(agent_run_id, agent_run, state, "\n".join(response_text))

        except asyncio.CancelledError:
            await self._fail_agent(agent_run_id, state, "Cancelled by user")
        except TimeoutError as e:
            await self._fail_agent(agent_run_id, state, str(e))
        except Exception as e:
            logger.exception(f"Agent {agent_run_id} failed with error")
            await self._fail_agent(agent_run_id, state, str(e))
        finally:
            # Cleanup
            await self._cleanup_agent(agent_run_id, state)

    async def _setup_agent_environment(
        self,
        agent_run_id: str,
        agent_run: Dict[str, Any],
        state: AgentRunState
    ):
        """Set up the isolated environment for agent execution"""
        auto_branch = agent_run.get("auto_branch", True)
        project_id = agent_run.get("project_id")

        if not auto_branch or not project_id:
            self._log(agent_run_id, "Running without isolated worktree")
            return

        project = database.get_project(project_id)
        if not project:
            self._log(agent_run_id, "Project not found, running without worktree", "warning")
            return

        # Check if project is a git repo
        project_path = str(settings.workspace_dir / project["path"])
        if not git_service.is_git_repo(project_path):
            self._log(agent_run_id, "Project is not a git repository, running without worktree", "warning")
            return

        # Generate branch name
        safe_name = agent_run["name"].lower()
        safe_name = "".join(c if c.isalnum() or c == "-" else "-" for c in safe_name)[:30]
        branch_name = f"agent/{safe_name}-{agent_run_id[-8:]}"
        state.branch_name = branch_name

        self._log(agent_run_id, f"Creating worktree for branch: {branch_name}")

        try:
            # Get base branch - use provided one or get default from repo
            base_branch = agent_run.get("base_branch")
            if not base_branch:
                base_branch = git_service.get_default_branch(project_path) or "main"

            self._log(agent_run_id, f"Using base branch: {base_branch}")

            # Create worktree (this also creates the branch)
            worktree, session = worktree_manager.create_worktree_session(
                project_id=project_id,
                branch_name=branch_name,
                create_new_branch=True,
                base_branch=base_branch,
                profile_id=agent_run.get("profile_id")
            )

            if worktree:
                # Get absolute path to worktree - use same base as worktree_manager
                # The worktree is created relative to the project's git repo location
                project_path = str(settings.workspace_dir / project["path"])
                # Worktree path is stored as relative: .worktrees/{project_id}/{branch}
                # But the actual worktree is created relative to workspace_dir, not project
                state.worktree_path = str(settings.workspace_dir / worktree["worktree_path"])

                # Log for debugging
                self._log(agent_run_id, f"Workspace dir: {settings.workspace_dir}")
                self._log(agent_run_id, f"Worktree relative path: {worktree['worktree_path']}")

                database.update_agent_run(
                    agent_run_id,
                    worktree_id=worktree["id"],
                    branch=branch_name
                )
                self._log(agent_run_id, f"Worktree created at: {state.worktree_path}")
            else:
                self._log(agent_run_id, "Failed to create worktree, using main repository", "warning")

        except Exception as e:
            self._log(agent_run_id, f"Worktree creation failed: {e}", "error")
            # Continue without worktree

    def _build_agent_prompt(self, agent_run: Dict[str, Any], state: AgentRunState) -> str:
        """Build the prompt for the agent with minimal context injection.

        Note: GitHub workflow instructions should be in the profile's system prompt,
        not injected here. Use a dedicated "Background Agent" profile for GitHub workflows.
        """
        base_prompt = agent_run["prompt"]

        # Only add minimal context about the current environment
        context_parts = []

        if state.branch_name:
            context_parts.append(f"**Current branch:** `{state.branch_name}`")
            base_branch = agent_run.get("base_branch", "main")
            context_parts.append(f"**Base branch:** `{base_branch}`")
            context_parts.append("")

        context_parts.append("## Task")
        context_parts.append("")
        context_parts.append(base_prompt)

        return "\n".join(context_parts)

    async def _handle_todo_write(self, agent_run_id: str, todo_input: Dict[str, Any]):
        """Handle TodoWrite tool usage to track agent tasks"""
        todos = todo_input.get("todos", [])

        # Get existing tasks
        existing_tasks = database.get_agent_tasks(agent_run_id)
        existing_by_name = {t["name"]: t for t in existing_tasks}

        for i, todo in enumerate(todos):
            task_name = todo.get("content", "Unknown task")
            task_status = todo.get("status", "pending")

            # Map todo status to our task status
            status_map = {
                "pending": TaskStatus.PENDING.value,
                "in_progress": TaskStatus.IN_PROGRESS.value,
                "completed": TaskStatus.COMPLETED.value
            }
            mapped_status = status_map.get(task_status, TaskStatus.PENDING.value)

            if task_name in existing_by_name:
                # Update existing task
                task = existing_by_name[task_name]
                if task["status"] != mapped_status:
                    database.update_agent_task(task["id"], status=mapped_status)
                    await self._broadcast(agent_run_id, "agent_task_update", {
                        "task_id": task["id"],
                        "name": task_name,
                        "status": mapped_status
                    })
            else:
                # Create new task
                task_id = f"task-{uuid.uuid4().hex[:8]}"
                database.create_agent_task(
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    name=task_name,
                    status=mapped_status,
                    order_index=i
                )
                await self._broadcast(agent_run_id, "agent_task_update", {
                    "task_id": task_id,
                    "name": task_name,
                    "status": mapped_status
                })

    async def _complete_agent(
        self,
        agent_run_id: str,
        agent_run: Dict[str, Any],
        state: AgentRunState,
        response_text: str
    ):
        """Handle successful agent completion"""
        self._log(agent_run_id, "Agent completed successfully")

        # Generate summary (first 500 chars of response)
        summary = response_text[:500] + "..." if len(response_text) > 500 else response_text

        # Update database
        database.update_agent_run(
            agent_run_id,
            status=AgentStatus.COMPLETED.value,
            progress=100,
            completed_at=datetime.utcnow().isoformat(),
            result_summary=summary
        )

        # Handle auto-PR if enabled
        if agent_run.get("auto_pr") and state.branch_name:
            await self._create_pull_request(agent_run_id, agent_run, state)

        # Handle auto-review if enabled and PR was created
        agent_run_updated = database.get_agent_run(agent_run_id)
        if agent_run.get("auto_review") and agent_run_updated.get("pr_url"):
            await self._trigger_auto_review(agent_run_id, agent_run_updated)

        # Handle auto-merge if enabled and PR was created
        agent_run_updated = database.get_agent_run(agent_run_id)
        if agent_run.get("auto_merge") and agent_run_updated.get("pr_url"):
            await self._merge_and_cleanup(agent_run_id, agent_run, state, agent_run_updated.get("pr_url"))

        await self._broadcast(agent_run_id, "agent_completed", {
            "status": AgentStatus.COMPLETED.value,
            "progress": 100,
            "result_summary": summary,
            "pr_url": agent_run_updated.get("pr_url") if agent_run_updated else None
        })

    async def _commit_and_push_changes(
        self,
        agent_run_id: str,
        agent_run: Dict[str, Any],
        state: AgentRunState
    ) -> tuple[bool, Optional[str]]:
        """
        Commit any uncommitted changes and push to remote.

        This handles the git workflow so the agent doesn't have to:
        1. Stage all changes (git add -A)
        2. Commit with a descriptive message
        3. Fetch and rebase on base branch
        4. Push to remote

        Returns:
            tuple[bool, Optional[str]]: (success, conflict_info)
            - (True, None): Changes pushed successfully
            - (False, None): No changes to push or non-conflict error
            - (False, conflict_info): Merge conflict detected, conflict_info contains details
        """
        import subprocess

        if not state.worktree_path or not state.branch_name:
            self._log(agent_run_id, "Cannot commit: no worktree or branch", "warning")
            return False, None

        base_branch = agent_run.get('base_branch', 'main')

        try:
            # Step 1: Check for any changes (staged, unstaged, or untracked)
            self._log(agent_run_id, "Checking for uncommitted changes...")
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=state.worktree_path,
                timeout=30
            )

            has_uncommitted_changes = bool(status_result.stdout.strip())

            if has_uncommitted_changes:
                self._log(agent_run_id, f"Found uncommitted changes:\n{status_result.stdout.strip()}")

                # Step 2: Stage all changes
                self._log(agent_run_id, "Staging all changes...")
                add_result = subprocess.run(
                    ["git", "add", "-A"],
                    capture_output=True,
                    text=True,
                    cwd=state.worktree_path,
                    timeout=30
                )
                if add_result.returncode != 0:
                    self._log(agent_run_id, f"Failed to stage changes: {add_result.stderr}", "error")
                    return False, None

                # Step 3: Create commit with descriptive message
                commit_msg = f"feat: {agent_run['name']}\n\nAutomated changes by background agent.\n\nTask: {agent_run['prompt'][:200]}"
                self._log(agent_run_id, "Creating commit...")
                commit_result = subprocess.run(
                    ["git", "commit", "-m", commit_msg],
                    capture_output=True,
                    text=True,
                    cwd=state.worktree_path,
                    timeout=30
                )
                if commit_result.returncode != 0:
                    # Check if it's just "nothing to commit"
                    if "nothing to commit" in commit_result.stdout or "nothing to commit" in commit_result.stderr:
                        self._log(agent_run_id, "No changes to commit (already committed by agent)")
                    else:
                        self._log(agent_run_id, f"Failed to commit: {commit_result.stderr}", "error")
                        return False, None
                else:
                    self._log(agent_run_id, "Changes committed successfully")
            else:
                self._log(agent_run_id, "No uncommitted changes found")

            # Step 4: Check if we have any commits ahead of base branch
            # First fetch to ensure we have latest remote state
            self._log(agent_run_id, "Fetching from origin...")
            fetch_result = subprocess.run(
                ["git", "fetch", "origin"],
                capture_output=True,
                text=True,
                cwd=state.worktree_path,
                timeout=60
            )
            if fetch_result.returncode != 0:
                self._log(agent_run_id, f"Warning: git fetch failed: {fetch_result.stderr}", "warning")

            # Check for commits difference
            self._log(agent_run_id, f"Checking for commits relative to origin/{base_branch}")
            diff_check = subprocess.run(
                ["git", "rev-list", "--count", f"origin/{base_branch}..HEAD"],
                capture_output=True,
                text=True,
                cwd=state.worktree_path,
                timeout=30
            )

            commit_count = int(diff_check.stdout.strip()) if diff_check.stdout.strip().isdigit() else 0

            if commit_count == 0:
                self._log(agent_run_id, "No commits ahead of base branch - nothing to push", "warning")
                return False, None

            self._log(agent_run_id, f"Found {commit_count} commit(s) to push")

            # Step 5: Rebase on base branch to handle any parallel changes
            self._log(agent_run_id, f"Rebasing on origin/{base_branch}...")
            rebase_result = subprocess.run(
                ["git", "rebase", f"origin/{base_branch}"],
                capture_output=True,
                text=True,
                cwd=state.worktree_path,
                timeout=120
            )
            if rebase_result.returncode != 0:
                self._log(agent_run_id, f"Rebase failed, aborting and trying merge: {rebase_result.stderr}", "warning")
                # Abort rebase
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    capture_output=True,
                    cwd=state.worktree_path,
                    timeout=30
                )
                # Try merge instead
                merge_result = subprocess.run(
                    ["git", "merge", f"origin/{base_branch}", "-m", f"Merge {base_branch} into {state.branch_name}"],
                    capture_output=True,
                    text=True,
                    cwd=state.worktree_path,
                    timeout=60
                )
                if merge_result.returncode != 0:
                    # Check if this is a merge conflict
                    merge_output = merge_result.stdout + merge_result.stderr
                    if "CONFLICT" in merge_output or "Automatic merge failed" in merge_output:
                        self._log(agent_run_id, "Merge conflict detected - will ask agent to resolve", "warning")

                        # Get conflict details before aborting
                        conflict_info = self._get_conflict_details(state.worktree_path, base_branch)

                        # Abort the merge to leave working directory clean for agent
                        subprocess.run(
                            ["git", "merge", "--abort"],
                            capture_output=True,
                            cwd=state.worktree_path,
                            timeout=30
                        )

                        return False, conflict_info
                    else:
                        self._log(agent_run_id, f"Merge failed (not a conflict): {merge_result.stderr}", "error")
                        return False, None
                self._log(agent_run_id, "Merged base branch successfully")
            else:
                self._log(agent_run_id, "Rebased successfully")

            # Step 6: Push to remote
            self._log(agent_run_id, f"Pushing branch {state.branch_name} to origin...")
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", state.branch_name, "--force-with-lease"],
                capture_output=True,
                text=True,
                cwd=state.worktree_path,
                timeout=120
            )
            if push_result.returncode != 0:
                self._log(agent_run_id, f"Failed to push: {push_result.stderr}", "error")
                return False, None

            self._log(agent_run_id, "Branch pushed successfully")
            return True, None

        except subprocess.TimeoutExpired as e:
            self._log(agent_run_id, f"Git operation timed out: {e}", "error")
            return False, None
        except Exception as e:
            self._log(agent_run_id, f"Git operation failed: {type(e).__name__}: {e}", "error")
            return False, None

    def _get_conflict_details(self, worktree_path: str, base_branch: str) -> str:
        """Get details about merge conflicts for the agent to resolve."""
        import subprocess

        details = []

        # Get list of conflicted files
        status = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=30
        )

        conflicted_files = [f.strip() for f in status.stdout.strip().split('\n') if f.strip()]

        if conflicted_files:
            details.append(f"Files with conflicts: {', '.join(conflicted_files)}")

            # Get the actual conflict markers for each file (first 50 lines of diff)
            for file in conflicted_files[:5]:  # Limit to first 5 files
                diff = subprocess.run(
                    ["git", "diff", file],
                    capture_output=True,
                    text=True,
                    cwd=worktree_path,
                    timeout=30
                )
                if diff.stdout:
                    # Truncate if too long
                    diff_text = diff.stdout[:2000] + "..." if len(diff.stdout) > 2000 else diff.stdout
                    details.append(f"\nConflict in {file}:\n{diff_text}")

        # Get a summary of what changed on the base branch that caused the conflict
        log = subprocess.run(
            ["git", "log", "--oneline", f"HEAD..origin/{base_branch}", "-5"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=30
        )

        if log.stdout.strip():
            details.append(f"\nRecent commits on {base_branch} that may have caused conflict:\n{log.stdout.strip()}")

        return "\n".join(details) if details else "Merge conflict detected (no additional details available)"

    async def _create_pull_request(
        self,
        agent_run_id: str,
        agent_run: Dict[str, Any],
        state: AgentRunState,
        max_conflict_retries: int = 2
    ):
        """Create a pull request for the agent's changes"""
        import subprocess

        if not state.worktree_path or not state.branch_name:
            self._log(agent_run_id, "Cannot create PR: no worktree or branch", "warning")
            return

        self._log(agent_run_id, f"Preparing pull request for branch {state.branch_name}")

        try:
            # Step 1: Commit and push any changes (handles the full git workflow)
            # This may detect merge conflicts if another agent modified the same files
            success, conflict_info = await self._commit_and_push_changes(agent_run_id, agent_run, state)

            # Handle merge conflicts by asking the agent to resolve them
            conflict_retry_count = 0
            while not success and conflict_info and conflict_retry_count < max_conflict_retries:
                conflict_retry_count += 1
                self._log(agent_run_id, f"Attempting conflict resolution (attempt {conflict_retry_count}/{max_conflict_retries})")

                # Ask the agent to resolve the conflict
                resolved = await self._resolve_merge_conflict(agent_run_id, agent_run, state, conflict_info)
                if not resolved:
                    self._log(agent_run_id, "Agent could not resolve merge conflict", "error")
                    return

                # Try again
                success, conflict_info = await self._commit_and_push_changes(agent_run_id, agent_run, state)

            if not success:
                if conflict_info:
                    self._log(agent_run_id, f"Failed to resolve merge conflict after {max_conflict_retries} attempts", "error")
                else:
                    self._log(agent_run_id, "No changes to create PR for", "warning")
                return

            # Step 2: Check if gh is authenticated
            auth_check = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                cwd=state.worktree_path,
                timeout=30
            )
            if auth_check.returncode != 0:
                self._log(agent_run_id, f"GitHub CLI not authenticated: {auth_check.stderr}", "error")
                return

            # Get project for GitHub repo info
            project_id = agent_run.get("project_id")
            project = database.get_project(project_id) if project_id else None
            if not project:
                self._log(agent_run_id, "Cannot create PR: project not found", "warning")
                return

            repo = database.get_git_repository_by_project(project_id)
            github_repo = repo.get("github_repo_name") if repo else None

            # Fallback: try to extract github repo from git remote URL
            if not github_repo:
                self._log(agent_run_id, "github_repo_name not in database, attempting to extract from git remote")
                try:
                    remote_result = subprocess.run(
                        ["git", "remote", "get-url", "origin"],
                        capture_output=True,
                        text=True,
                        cwd=state.worktree_path,
                        timeout=10
                    )
                    if remote_result.returncode == 0:
                        url = remote_result.stdout.strip()
                        self._log(agent_run_id, f"Found remote URL: {url}")
                        # Parse: https://github.com/owner/repo.git or git@github.com:owner/repo.git
                        match = re.search(r'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$', url)
                        if match:
                            github_repo = match.group(1)
                            self._log(agent_run_id, f"Extracted GitHub repo: {github_repo}")
                        else:
                            self._log(agent_run_id, f"Could not parse GitHub repo from URL: {url}", "warning")
                    else:
                        self._log(agent_run_id, f"Failed to get git remote URL: {remote_result.stderr}", "warning")
                except Exception as e:
                    self._log(agent_run_id, f"Error extracting GitHub repo from remote: {e}", "warning")

            if not github_repo:
                self._log(agent_run_id, "Cannot create PR: not a GitHub repository", "warning")
                return

            # Use the base_branch the agent was started from, not the repo's default branch
            # This ensures PRs target the correct branch (e.g., ai-shuffle instead of main)
            target_branch = agent_run.get("base_branch") or repo.get("default_branch", "main") if repo else "main"

            # Create PR using gh CLI
            pr_title = f"Agent: {agent_run['name']}"
            # Escape special characters in PR body
            prompt_safe = agent_run['prompt'][:1000].replace('`', '\\`').replace('$', '\\$')
            pr_body = f"""## Automated Agent PR

**Agent:** {agent_run['name']}
**Branch:** {state.branch_name}

### Task Description
{prompt_safe}

---
*This PR was created automatically by an AI agent.*
"""
            self._log(agent_run_id, f"Creating PR: {pr_title} ({github_repo})")

            result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--repo", github_repo,
                    "--head", state.branch_name,
                    "--base", target_branch,
                    "--title", pr_title,
                    "--body", pr_body
                ],
                capture_output=True,
                text=True,
                cwd=state.worktree_path,
                timeout=60
            )

            if result.returncode == 0:
                output = result.stdout.strip()
                # Extract PR URL using regex to handle different gh CLI output formats
                url_match = re.search(r'https://github\.com/\S+/pull/\d+', output)
                if url_match:
                    pr_url = url_match.group(0)
                    database.update_agent_run(agent_run_id, pr_url=pr_url)
                    self._log(agent_run_id, f"Pull request created: {pr_url}")
                else:
                    self._log(agent_run_id, f"PR created but could not extract URL from output: {output}", "warning")
                    # Still update with the full output as fallback
                    database.update_agent_run(agent_run_id, pr_url=output)
            else:
                self._log(agent_run_id, f"Failed to create PR (exit code {result.returncode}): {result.stderr}", "error")
                if result.stdout:
                    self._log(agent_run_id, f"PR stdout: {result.stdout}", "debug")

        except subprocess.TimeoutExpired:
            self._log(agent_run_id, "PR creation timed out", "error")
        except Exception as e:
            self._log(agent_run_id, f"PR creation failed: {type(e).__name__}: {e}", "error")

    async def _resolve_merge_conflict(
        self,
        agent_run_id: str,
        agent_run: Dict[str, Any],
        state: AgentRunState,
        conflict_info: str
    ) -> bool:
        """
        Send a follow-up query to the agent to resolve merge conflicts.

        The agent is given the conflict details and asked to:
        1. Fetch and merge the base branch
        2. Resolve any conflicts
        3. Commit the resolution

        Returns True if the agent successfully resolved the conflict.
        """
        self._log(agent_run_id, "Asking agent to resolve merge conflict...")

        # Build the conflict resolution prompt
        base_branch = agent_run.get('base_branch', 'main')
        conflict_prompt = f"""
MERGE CONFLICT DETECTED - Please resolve it.

While trying to push your changes, a merge conflict was detected with the base branch ({base_branch}).
Another process has modified some of the same files you worked on.

Here are the conflict details:
{conflict_info}

Please:
1. Run `git fetch origin` to get the latest changes
2. Run `git merge origin/{base_branch}` to merge the base branch
3. Resolve any conflicts in the affected files (remove conflict markers, keep the correct code)
4. Run `git add -A` to stage resolved files
5. Run `git commit -m "Resolve merge conflict with {base_branch}"` to commit the resolution

After resolving, I will attempt to push your changes again.
"""

        try:
            # Get profile for the follow-up query
            profile_id = agent_run.get("profile_id")
            profile = get_profile(profile_id) if profile_id else None
            if not profile:
                profiles = database.get_all_profiles()
                if profiles:
                    profile = profiles[0]
                else:
                    self._log(agent_run_id, "No profile available for conflict resolution", "error")
                    return False

            # Get project
            project_id = agent_run.get("project_id")
            project = database.get_project(project_id) if project_id else None

            # Build options with the worktree path
            options, agents_dict = build_options_from_profile(
                profile=profile,
                project=project,
                overrides={"cwd": state.worktree_path},
                resume_session_id=state.sdk_session_id  # Resume the existing session
            )

            # Write agents to filesystem if needed (Windows workaround)
            if agents_dict and detect_deployment_mode() == DeploymentMode.LOCAL:
                write_agents_to_filesystem(agents_dict, state.worktree_path)

            self._log(agent_run_id, "Sending conflict resolution request to agent...")

            # Execute the follow-up query
            response_text = []
            async for message in query(prompt=conflict_prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            response_text.append(block.text)
                            # Log progress
                            if len(block.text) > 10:
                                preview = block.text[:200] + "..." if len(block.text) > 200 else block.text
                                self._log(agent_run_id, f"[Conflict Resolution] {preview}")
                        elif isinstance(block, ToolUseBlock):
                            self._log(agent_run_id, f"[Conflict Resolution] Using tool: {block.name}")

                elif isinstance(message, ResultMessage):
                    self._log(agent_run_id, f"Conflict resolution completed. Turns: {message.num_turns}")

            self._log(agent_run_id, "Agent finished conflict resolution attempt")
            return True

        except Exception as e:
            self._log(agent_run_id, f"Conflict resolution failed: {type(e).__name__}: {e}", "error")
            return False

    async def _merge_and_cleanup(
        self,
        agent_run_id: str,
        agent_run: Dict[str, Any],
        state: AgentRunState,
        pr_url: str
    ):
        """
        Merge the PR and cleanup:
        1. Merge the PR using gh CLI
        2. Delete the remote branch
        3. Delete the local branch
        4. Remove the worktree
        """
        import subprocess

        if not pr_url or not state.worktree_path or not state.branch_name:
            self._log(agent_run_id, "Cannot merge: missing PR URL, worktree, or branch", "warning")
            return

        self._log(agent_run_id, f"Auto-merging PR and cleaning up: {pr_url}")

        try:
            # Step 1: Merge the PR
            self._log(agent_run_id, "Merging PR...")
            merge_result = subprocess.run(
                ["gh", "pr", "merge", pr_url, "--squash", "--delete-branch"],
                capture_output=True,
                text=True,
                cwd=state.worktree_path,
                timeout=120
            )

            if merge_result.returncode != 0:
                self._log(agent_run_id, f"Failed to merge PR: {merge_result.stderr}", "error")
                return

            self._log(agent_run_id, "PR merged successfully")

            # Step 2: Remove the worktree
            self._log(agent_run_id, f"Removing worktree at {state.worktree_path}...")
            project_id = agent_run.get("project_id")
            if project_id:
                project = database.get_project(project_id)
                if project:
                    project_path = str(settings.workspace_dir / project["path"])

                    # Remove worktree using git command
                    remove_result = subprocess.run(
                        ["git", "worktree", "remove", state.worktree_path, "--force"],
                        capture_output=True,
                        text=True,
                        cwd=project_path,
                        timeout=60
                    )

                    if remove_result.returncode == 0:
                        self._log(agent_run_id, "Worktree removed successfully")
                    else:
                        self._log(agent_run_id, f"Failed to remove worktree: {remove_result.stderr}", "warning")

                    # Prune worktrees
                    subprocess.run(
                        ["git", "worktree", "prune"],
                        capture_output=True,
                        cwd=project_path,
                        timeout=30
                    )

                    # Step 3: Delete local branch if it still exists
                    self._log(agent_run_id, f"Deleting local branch {state.branch_name}...")
                    delete_result = subprocess.run(
                        ["git", "branch", "-D", state.branch_name],
                        capture_output=True,
                        text=True,
                        cwd=project_path,
                        timeout=30
                    )

                    if delete_result.returncode == 0:
                        self._log(agent_run_id, "Local branch deleted")
                    else:
                        # Branch might already be deleted by gh pr merge --delete-branch
                        self._log(agent_run_id, f"Local branch cleanup: {delete_result.stderr.strip()}", "debug")

            # Update worktree status in database
            worktree_id = agent_run.get("worktree_id")
            if worktree_id:
                database.update_worktree(worktree_id, status="removed")

            self._log(agent_run_id, "Merge and cleanup completed successfully")

        except subprocess.TimeoutExpired as e:
            self._log(agent_run_id, f"Merge/cleanup operation timed out: {e}", "error")
        except Exception as e:
            self._log(agent_run_id, f"Merge/cleanup failed: {type(e).__name__}: {e}", "error")

    async def _trigger_auto_review(self, agent_run_id: str, agent_run: Dict[str, Any]):
        """Trigger an automatic code review for the PR"""
        import subprocess

        pr_url = agent_run.get("pr_url")
        if not pr_url:
            return

        self._log(agent_run_id, f"Triggering auto-review for PR: {pr_url}")

        # Launch a new review agent with instructions to merge if approved
        review_prompt = f"""Review this pull request and provide feedback:
{pr_url}

Check for:
1. Code quality and best practices
2. Potential bugs or issues
3. Security concerns
4. Test coverage
5. Documentation

If the PR looks good with no critical issues:
1. Approve the PR using: gh pr review --approve {pr_url}
2. Then merge the PR using: gh pr merge --merge {pr_url}

If there are issues that need fixing, leave comments on the PR using:
gh pr review --comment --body "Your feedback here" {pr_url}

Important: Always approve before attempting to merge."""

        try:
            review_run = await self.launch_agent(
                name=f"Review: {agent_run['name']}",
                prompt=review_prompt,
                profile_id=agent_run.get("profile_id"),
                project_id=agent_run.get("project_id"),
                auto_branch=False,  # Reviewer doesn't need its own branch
                auto_pr=False,
                auto_review=False,
                max_duration_minutes=15  # Reviews should be quick
            )
            self._log(agent_run_id, f"Review agent launched: {review_run['id']}")

            # Note: Worktree cleanup will happen when the original agent task completes
            # If we wanted to wait for review completion, we'd need a callback mechanism

        except Exception as e:
            self._log(agent_run_id, f"Failed to launch review agent: {e}", "error")

    async def _cleanup_worktree(self, agent_run_id: str, agent_run: Dict[str, Any]):
        """Clean up the worktree directory after PR is merged"""
        import subprocess
        import shutil

        worktree_id = agent_run.get("worktree_id")
        branch_name = agent_run.get("branch")
        project_id = agent_run.get("project_id")

        if not worktree_id or not project_id:
            return

        project = database.get_project(project_id)
        if not project:
            return

        project_path = str(settings.workspace_dir / project["path"])
        worktree_data = database.get_worktree(worktree_id)

        if worktree_data:
            worktree_path = str(settings.workspace_dir / worktree_data["worktree_path"])

            try:
                # Remove the worktree using git
                self._log(agent_run_id, f"Removing worktree at {worktree_path}")
                result = subprocess.run(
                    ["git", "worktree", "remove", worktree_path, "--force"],
                    capture_output=True,
                    text=True,
                    cwd=project_path,
                    timeout=60
                )

                if result.returncode != 0:
                    self._log(agent_run_id, f"Git worktree remove failed: {result.stderr}", "warning")
                    # Try manual removal as fallback
                    if Path(worktree_path).exists():
                        shutil.rmtree(worktree_path, ignore_errors=True)

                # Update database
                database.delete_worktree(worktree_id)
                self._log(agent_run_id, "Worktree removed successfully")

            except Exception as e:
                self._log(agent_run_id, f"Failed to remove worktree: {e}", "warning")

        # Optionally delete the branch if it was merged
        if branch_name:
            try:
                # Check if branch was merged
                result = subprocess.run(
                    ["git", "branch", "-d", branch_name],
                    capture_output=True,
                    text=True,
                    cwd=project_path,
                    timeout=30
                )
                if result.returncode == 0:
                    self._log(agent_run_id, f"Branch {branch_name} deleted")
                else:
                    # Branch might not be merged yet or already deleted
                    self._log(agent_run_id, f"Could not delete branch {branch_name}: {result.stderr}", "debug")
            except Exception as e:
                self._log(agent_run_id, f"Failed to delete branch: {e}", "debug")

    async def _fail_agent(self, agent_run_id: str, state: AgentRunState, error: str):
        """Handle agent failure"""
        self._log(agent_run_id, f"Agent failed: {error}", "error")

        database.update_agent_run(
            agent_run_id,
            status=AgentStatus.FAILED.value,
            completed_at=datetime.utcnow().isoformat(),
            error=error
        )

        await self._broadcast(agent_run_id, "agent_failed", {
            "status": AgentStatus.FAILED.value,
            "error": error
        })

    async def _cleanup_agent(self, agent_run_id: str, state: AgentRunState):
        """Clean up agent resources"""
        # Clean up written agent files
        if state.agents_dir and state.written_agent_ids:
            cleanup_agents_directory(state.agents_dir, state.written_agent_ids)

        # Remove from active runs
        self._active_runs.pop(agent_run_id, None)

        # Note: We don't clean up the worktree here - it's preserved for inspection
        # Users can manually clean up worktrees when they're done

        self._log(agent_run_id, "Agent cleanup completed")

    async def pause_agent(self, agent_run_id: str) -> bool:
        """Pause a running agent"""
        state = self._active_runs.get(agent_run_id)
        if not state:
            return False

        agent_run = database.get_agent_run(agent_run_id)
        if not agent_run or agent_run["status"] != AgentStatus.RUNNING.value:
            return False

        state.is_paused = True
        state.pause_event.clear()

        database.update_agent_run(agent_run_id, status=AgentStatus.PAUSED.value)
        self._log(agent_run_id, "Agent paused")

        await self._broadcast(agent_run_id, "agent_paused", {
            "status": AgentStatus.PAUSED.value
        })

        return True

    async def resume_agent(self, agent_run_id: str) -> bool:
        """Resume a paused agent"""
        state = self._active_runs.get(agent_run_id)
        if not state:
            return False

        agent_run = database.get_agent_run(agent_run_id)
        if not agent_run or agent_run["status"] != AgentStatus.PAUSED.value:
            return False

        state.is_paused = False
        state.pause_event.set()

        database.update_agent_run(agent_run_id, status=AgentStatus.RUNNING.value)
        self._log(agent_run_id, "Agent resumed")

        await self._broadcast(agent_run_id, "agent_resumed", {
            "status": AgentStatus.RUNNING.value
        })

        return True

    async def cancel_agent(self, agent_run_id: str, reason: str = "Cancelled by user") -> bool:
        """Cancel a running or queued agent"""
        state = self._active_runs.get(agent_run_id)

        agent_run = database.get_agent_run(agent_run_id)
        if not agent_run:
            return False

        if agent_run["status"] in [AgentStatus.COMPLETED.value, AgentStatus.FAILED.value]:
            return False

        if state:
            # Running agent - signal cancellation
            state.cancel_requested = True
            state.pause_event.set()  # Unpause if paused

            if state.task and not state.task.done():
                state.task.cancel()

        # Update database
        database.update_agent_run(
            agent_run_id,
            status=AgentStatus.FAILED.value,
            completed_at=datetime.utcnow().isoformat(),
            error=reason
        )

        self._log(agent_run_id, f"Agent cancelled: {reason}", "warning")

        await self._broadcast(agent_run_id, "agent_cancelled", {
            "status": AgentStatus.FAILED.value,
            "error": reason
        })

        return True

    def get_agent_state(self, agent_run_id: str) -> Optional[Dict[str, Any]]:
        """Get the current state of an agent run"""
        state = self._active_runs.get(agent_run_id)
        agent_run = database.get_agent_run(agent_run_id)

        if not agent_run:
            return None

        # Enrich with runtime state
        result = dict(agent_run)
        result["tasks"] = database.get_agent_tasks_tree(agent_run_id)

        if state:
            result["is_active"] = state.task and not state.task.done()
            result["is_paused"] = state.is_paused
        else:
            result["is_active"] = False
            result["is_paused"] = False

        return result

    def get_active_count(self) -> int:
        """Get count of active (running/paused) agents"""
        return sum(
            1 for state in self._active_runs.values()
            if state.task and not state.task.done()
        )

    def get_queued_count(self) -> int:
        """Get count of queued agents"""
        return database.get_agent_runs_count(status=AgentStatus.QUEUED.value)


# Singleton instance
agent_engine = AgentExecutionEngine()
