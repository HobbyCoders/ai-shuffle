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
        auto_pr: bool = False,
        auto_review: bool = False,
        max_duration_minutes: int = 30
    ) -> Dict[str, Any]:
        """
        Launch a new background agent.

        The agent will be queued and started when a slot is available.
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
            auto_review=auto_review,
            max_duration_minutes=max_duration_minutes
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

            # Build options from profile with custom cwd
            options, agents_dict = build_options_from_profile(
                profile=profile,
                project=project,
                overrides={"cwd": working_dir}
            )
            options.cwd = working_dir

            # Write agents to filesystem if needed (Windows workaround)
            if agents_dict and detect_deployment_mode() == DeploymentMode.LOCAL:
                state.agents_dir = write_agents_to_filesystem(agents_dict, working_dir)
                state.written_agent_ids = list(agents_dict.keys())

            # Build the enhanced prompt with context
            enhanced_prompt = self._build_agent_prompt(agent_run, state)

            # Set up timeout
            max_duration = agent_run.get("max_duration_minutes", 30)
            timeout_at = datetime.utcnow() + timedelta(minutes=max_duration)

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

                # Check timeout
                if datetime.utcnow() > timeout_at:
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
            # Get default branch
            default_branch = git_service.get_default_branch(project_path) or "main"

            # Create worktree (this also creates the branch)
            worktree, session = worktree_manager.create_worktree_session(
                project_id=project_id,
                branch_name=branch_name,
                create_new_branch=True,
                base_branch=default_branch,
                profile_id=agent_run.get("profile_id")
            )

            if worktree:
                state.worktree_path = str(settings.workspace_dir / worktree["worktree_path"])
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
        """Build the enhanced prompt for the agent"""
        base_prompt = agent_run["prompt"]

        # Add context about the agent environment
        context_parts = [
            "You are running as an autonomous background agent.",
            "Complete the following task thoroughly and independently.",
            "",
        ]

        if state.branch_name:
            context_parts.append(f"You are working on branch: {state.branch_name}")
            context_parts.append("Commit your changes as you make progress.")
            context_parts.append("")

        if agent_run.get("auto_pr"):
            context_parts.append("When complete, your changes should be ready for a pull request.")
            context_parts.append("")

        context_parts.append("## Task")
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

        await self._broadcast(agent_run_id, "agent_completed", {
            "status": AgentStatus.COMPLETED.value,
            "progress": 100,
            "result_summary": summary,
            "pr_url": agent_run_updated.get("pr_url") if agent_run_updated else None
        })

    async def _create_pull_request(
        self,
        agent_run_id: str,
        agent_run: Dict[str, Any],
        state: AgentRunState
    ):
        """Create a pull request for the agent's changes"""
        if not state.worktree_path or not state.branch_name:
            self._log(agent_run_id, "Cannot create PR: no worktree or branch", "warning")
            return

        self._log(agent_run_id, f"Creating pull request for branch {state.branch_name}")

        try:
            # Push the branch
            push_result = git_service.push(state.worktree_path, state.branch_name, set_upstream=True)
            if not push_result:
                self._log(agent_run_id, "Failed to push branch", "error")
                return

            # Get project for GitHub repo info
            project_id = agent_run.get("project_id")
            project = database.get_project(project_id) if project_id else None
            if not project:
                self._log(agent_run_id, "Cannot create PR: project not found", "warning")
                return

            repo = database.get_git_repository_by_project(project_id)
            if not repo or not repo.get("github_repo_name"):
                self._log(agent_run_id, "Cannot create PR: not a GitHub repository", "warning")
                return

            github_repo = repo["github_repo_name"]
            default_branch = repo.get("default_branch", "main")

            # Create PR using gh CLI
            import subprocess
            pr_title = f"Agent: {agent_run['name']}"
            pr_body = f"""## Automated Agent PR

**Agent:** {agent_run['name']}
**Branch:** {state.branch_name}

### Task Description
{agent_run['prompt'][:1000]}

---
*This PR was created automatically by an AI agent.*
"""
            result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--repo", github_repo,
                    "--head", state.branch_name,
                    "--base", default_branch,
                    "--title", pr_title,
                    "--body", pr_body
                ],
                capture_output=True,
                text=True,
                cwd=state.worktree_path,
                timeout=60
            )

            if result.returncode == 0:
                pr_url = result.stdout.strip()
                database.update_agent_run(agent_run_id, pr_url=pr_url)
                self._log(agent_run_id, f"Pull request created: {pr_url}")
            else:
                self._log(agent_run_id, f"Failed to create PR: {result.stderr}", "error")

        except Exception as e:
            self._log(agent_run_id, f"PR creation failed: {e}", "error")

    async def _trigger_auto_review(self, agent_run_id: str, agent_run: Dict[str, Any]):
        """Trigger an automatic code review for the PR"""
        pr_url = agent_run.get("pr_url")
        if not pr_url:
            return

        self._log(agent_run_id, f"Triggering auto-review for PR: {pr_url}")

        # Launch a new review agent
        review_prompt = f"""Review this pull request and provide feedback:
{pr_url}

Check for:
1. Code quality and best practices
2. Potential bugs or issues
3. Security concerns
4. Test coverage
5. Documentation

If the PR looks good, approve it. If there are issues, leave comments on the PR."""

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
        except Exception as e:
            self._log(agent_run_id, f"Failed to launch review agent: {e}", "error")

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
