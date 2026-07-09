"""
Composite git-operation orchestrator.

Runs the multi-step "Finish" and "Ship It" git flows server-side so the
frontend fires a single request and polls one operation for step-by-step
progress instead of chaining half a dozen individual git/gh calls (which
strands the flow if the tab closes mid-way and races the git index).

Two flows:

* **Finish** — merge the reviewed PR for the current branch *as-is*. Aborts
  (400) on a dirty tree: the user must commit or discard first (the UI should
  prevent this; the server is the backstop). Steps: guard -> push -> merge ->
  optional cleanup.
* **Ship It** — take everything and ship it. Commits the current changes with
  an AI-generated message (when the tree is dirty), then push -> create PR
  (AI title/description) -> merge -> optional cleanup.

Operations run as in-memory asyncio background tasks tracked in a per-process
registry (one active op per project). No HTTP self-calls: the flows drive
``git_service`` / ``github_service`` / ``worktree_manager`` and the shared
generator helpers in ``git_ai`` directly.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.git_service import git_service
from app.core.github_service import github_service
from app.core.worktree_manager import worktree_manager
from app.api.auth import require_auth
from app.api.common import check_project_access
from app.api.git_helpers import run_git, get_working_dir
from app.api.git_ai import (
    generate_commit_message_for_dir,
    generate_pr_description_for_dir,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/projects/{project_id}/git", tags=["Git Operations"])


# =============================================================================
# Request / Response schemas
# =============================================================================

class FinishRequest(BaseModel):
    """Body for the Finish flow (merge the reviewed PR as-is)."""
    merge_method: str = Field(default="squash", pattern="^(merge|squash|rebase)$")
    cleanup: bool = Field(default=False, description="Remove the worktree / delete the local branch after merge")
    session_id: Optional[str] = Field(
        default=None, description="Requesting session, excluded from the cleanup liveness guard"
    )


class ShipRequest(BaseModel):
    """Body for the Ship It flow (commit everything, open + merge a PR)."""
    include_unstaged: bool = Field(default=True, description="Stage all changes before committing")
    merge_method: str = Field(default="squash", pattern="^(merge|squash|rebase)$")
    base_branch: Optional[str] = Field(default=None, description="PR base (defaults to the repo default branch)")
    draft: bool = Field(default=False, description="Open the pull request as a draft")
    cleanup: bool = Field(default=False, description="Remove the worktree / delete the local branch after merge")
    session_id: Optional[str] = Field(
        default=None, description="Requesting session, excluded from the cleanup liveness guard"
    )


# =============================================================================
# In-memory operation registry
# =============================================================================

_OP_TTL_SECONDS = 600.0  # 10 minutes

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_ERROR = "error"
STEP_WARNING = "warning"
STEP_SKIPPED = "skipped"


@dataclass
class Operation:
    """A running composite git operation tracked in the registry."""
    project_id: str
    kind: str  # "finish" | "ship"
    steps: List[Dict[str, Any]]
    session_id: Optional[str] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "running"  # running | done | error
    result: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)

    def set_step(
        self,
        step_id: str,
        step_status: str,
        error: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        for s in self.steps:
            if s["id"] == step_id:
                s["status"] = step_status
                if error is not None:
                    s["error"] = error
                if detail is not None:
                    s["detail"] = detail
                return

    def snapshot(self) -> Dict[str, Any]:
        steps = []
        for s in self.steps:
            entry = {"id": s["id"], "label": s["label"], "status": s["status"]}
            if s.get("error"):
                entry["error"] = s["error"]
            if s.get("detail"):
                entry["detail"] = s["detail"]
            steps.append(entry)
        return {
            "operation_id": self.id,
            "status": self.status,
            "steps": steps,
            "result": self.result or None,
        }


_operations: Dict[str, Operation] = {}


def _sweep_expired(now: Optional[float] = None) -> None:
    """Drop finished operations older than the TTL. Running ops are kept."""
    now = time.monotonic() if now is None else now
    stale = [
        op_id
        for op_id, op in _operations.items()
        if op.status in ("done", "error") and (now - op.created_at) > _OP_TTL_SECONDS
    ]
    for op_id in stale:
        _operations.pop(op_id, None)


def _has_active_op(project_id: str) -> bool:
    return any(
        op.project_id == project_id and op.status == "running"
        for op in _operations.values()
    )


def _register(op: Operation) -> None:
    _operations[op.id] = op


def _step(step_id: str, label: str) -> Dict[str, Any]:
    return {"id": step_id, "label": label, "status": STEP_PENDING}


# =============================================================================
# Shared helpers
# =============================================================================

def _is_already_merged(error: Optional[str]) -> bool:
    """gh reports an already-merged PR as an error — treat it as success."""
    if not error:
        return False
    low = error.lower()
    return "already merged" in low or "already been merged" in low


def _find_open_pr_for_branch(repo: str, branch: str):
    """Return the open PullRequest whose head is *branch*, or None."""
    try:
        pulls = github_service.list_pulls(repo, state="open", limit=100)
    except Exception:
        logger.exception("Failed to list pull requests for %s", repo)
        return None
    for pr in pulls:
        if pr.head_branch == branch:
            return pr
    return None


def _worktree_has_other_streaming_session(worktree_id: str, exclude_session_id: Optional[str]) -> bool:
    """True if a session other than *exclude_session_id* is bound to the
    worktree and currently streaming — the cleanup step must not yank a
    worktree out from under a live SDK turn.
    """
    from app.core.sync_engine import sync_engine
    from app.db import database

    try:
        sessions = database.get_sessions_for_worktree(worktree_id)
    except Exception:
        logger.exception("Failed to look up sessions for worktree %s", worktree_id)
        return False

    for s in sessions:
        sid = s.get("id")
        if sid and sid != exclude_session_id and sync_engine.is_session_streaming(sid):
            return True
    return False


async def _do_cleanup(
    op: Operation,
    working_dir: str,
    worktree_id: Optional[str],
    feature_branch: str,
    default_branch: str,
    session_id: Optional[str],
) -> None:
    """Run the cleanup step. Failures degrade to a warning — the overall
    operation still succeeds because the merge already landed.
    """
    op.set_step("cleanup", STEP_RUNNING)

    # Liveness guard (worktree mode only): never remove a worktree another
    # session is actively streaming in.
    if worktree_id and _worktree_has_other_streaming_session(worktree_id, session_id):
        op.set_step(
            "cleanup",
            STEP_WARNING,
            detail="Skipped: another session is active in this worktree",
        )
        return

    try:
        if worktree_id:
            ok = await worktree_manager.cleanup_worktree(worktree_id, keep_branch=False)
            if ok:
                op.set_step("cleanup", STEP_DONE)
                op.result["worktree_deleted"] = True
            else:
                op.set_step("cleanup", STEP_WARNING, detail="Worktree cleanup failed")
        else:
            # Project mode: return to the default branch, drop the feature
            # branch, and fast-forward the default branch.
            if not await run_git(git_service.checkout, working_dir, default_branch):
                op.set_step("cleanup", STEP_WARNING, detail="Failed to checkout default branch")
                return
            await run_git(git_service.delete_branch, working_dir, feature_branch, True)
            await run_git(git_service.pull, working_dir)
            op.set_step("cleanup", STEP_DONE)
    except Exception as e:  # noqa: BLE001
        logger.exception("Cleanup step failed for op %s", op.id)
        op.set_step("cleanup", STEP_WARNING, detail=str(e))


def _fail_running_step(op: Operation, error: str) -> None:
    """Mark whichever step is mid-flight as errored (used on unexpected crash)."""
    for s in op.steps:
        if s["status"] == STEP_RUNNING:
            s["status"] = STEP_ERROR
            s["error"] = error
            break


# =============================================================================
# Flow executors (awaitable — driven as background tasks by the endpoints)
# =============================================================================

async def _execute_finish(
    op: Operation,
    *,
    working_dir: str,
    worktree_id: Optional[str],
    repo: str,
    pr_number: int,
    pr_url: str,
    feature_branch: str,
    default_branch: str,
    merge_method: str,
    cleanup: bool,
    session_id: Optional[str],
) -> None:
    try:
        # --- push (only if the branch is ahead) ------------------------------
        op.set_step("push", STEP_RUNNING)
        st = await run_git(git_service.get_status, working_dir)
        ahead = (st or {}).get("ahead", 0) or 0
        if ahead > 0:
            ok, err = await run_git(
                git_service.push, working_dir, branch=feature_branch, set_upstream=True
            )
            if not ok:
                op.set_step("push", STEP_ERROR, error=err or "Failed to push")
                op.status = "error"
                return
            op.set_step("push", STEP_DONE, detail=f"Pushed {ahead} commit(s)")
        else:
            op.set_step("push", STEP_SKIPPED, detail="Branch already up to date")

        # --- merge -----------------------------------------------------------
        if not await _merge_step(op, repo, pr_number, merge_method):
            return
        op.result["pr_number"] = pr_number
        op.result["pr_url"] = pr_url

        # --- cleanup ---------------------------------------------------------
        if cleanup:
            await _do_cleanup(op, working_dir, worktree_id, feature_branch, default_branch, session_id)
        else:
            op.set_step("cleanup", STEP_SKIPPED)

        op.status = "done"
    except Exception as e:  # noqa: BLE001
        logger.exception("Finish operation %s crashed", op.id)
        _fail_running_step(op, str(e))
        op.status = "error"


async def _execute_ship(
    op: Operation,
    *,
    working_dir: str,
    worktree_id: Optional[str],
    repo: str,
    feature_branch: str,
    default_branch: str,
    base_branch: str,
    existing_pr: Any,
    dirty: bool,
    include_unstaged: bool,
    merge_method: str,
    draft: bool,
    cleanup: bool,
    session_id: Optional[str],
) -> None:
    try:
        # --- commit (only when the tree is dirty) ----------------------------
        if dirty:
            op.set_step("commit", STEP_RUNNING)
            try:
                message = await generate_commit_message_for_dir(working_dir, include_unstaged)
            except HTTPException as e:
                op.set_step("commit", STEP_ERROR, error=str(e.detail))
                op.status = "error"
                return
            if include_unstaged:
                if not await run_git(git_service.stage_files, working_dir, ["."]):
                    op.set_step("commit", STEP_ERROR, error="Failed to stage changes")
                    op.status = "error"
                    return
            ok, sha_or_err = await run_git(git_service.commit, working_dir, message)
            if not ok:
                op.set_step("commit", STEP_ERROR, error=sha_or_err or "Failed to commit")
                op.status = "error"
                return
            op.set_step("commit", STEP_DONE, detail=message.split("\n")[0])
        else:
            op.set_step("commit", STEP_SKIPPED, detail="Nothing to commit")

        # --- push ------------------------------------------------------------
        op.set_step("push", STEP_RUNNING)
        ok, err = await run_git(
            git_service.push, working_dir, branch=feature_branch, set_upstream=True
        )
        if not ok:
            op.set_step("push", STEP_ERROR, error=err or "Failed to push")
            op.status = "error"
            return
        op.set_step("push", STEP_DONE)

        # --- create PR (skip when one is already open) -----------------------
        if existing_pr is not None:
            pr_number = existing_pr.number
            pr_url = existing_pr.url
            op.set_step("create_pr", STEP_SKIPPED, detail=f"PR #{pr_number} already exists")
        else:
            op.set_step("create_pr", STEP_RUNNING)
            try:
                title, body = await generate_pr_description_for_dir(working_dir, base_branch)
            except HTTPException as e:
                op.set_step("create_pr", STEP_ERROR, error=str(e.detail))
                op.status = "error"
                return
            pr, err = await asyncio.to_thread(
                github_service.create_pull, repo, title, body, feature_branch, base_branch, draft
            )
            if not pr:
                op.set_step("create_pr", STEP_ERROR, error=err or "Failed to create pull request")
                op.status = "error"
                return
            pr_number = pr.number
            pr_url = pr.url
            op.set_step("create_pr", STEP_DONE, detail=pr.url)

        op.result["pr_number"] = pr_number
        op.result["pr_url"] = pr_url

        # --- merge -----------------------------------------------------------
        if not await _merge_step(op, repo, pr_number, merge_method):
            return

        # --- cleanup ---------------------------------------------------------
        if cleanup:
            await _do_cleanup(op, working_dir, worktree_id, feature_branch, default_branch, session_id)
        else:
            op.set_step("cleanup", STEP_SKIPPED)

        op.status = "done"
    except Exception as e:  # noqa: BLE001
        logger.exception("Ship operation %s crashed", op.id)
        _fail_running_step(op, str(e))
        op.status = "error"


async def _merge_step(op: Operation, repo: str, pr_number: int, merge_method: str) -> bool:
    """Run the merge step. Returns True to continue, False to stop (op errored).
    An already-merged PR counts as success.
    """
    op.set_step("merge", STEP_RUNNING)
    ok, err = await asyncio.to_thread(github_service.merge_pull, repo, pr_number, merge_method)
    if ok:
        op.set_step("merge", STEP_DONE)
        return True
    if _is_already_merged(err):
        op.set_step("merge", STEP_DONE, detail="Already merged")
        return True
    op.set_step("merge", STEP_ERROR, error=err or "Failed to merge pull request")
    op.status = "error"
    return False


# =============================================================================
# Endpoints
# =============================================================================

@router.post("/finish")
async def finish(
    request: Request,
    project_id: str,
    body: FinishRequest,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth),
):
    """Merge the reviewed PR for the current branch, then optionally clean up.

    Guard (synchronous, returns 400): must be a git repo, must have an open PR
    whose head is the current branch, and the tree must be clean.
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Project is not a git repository")

    if _has_active_op(project_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Another git operation is already running for this project")

    current_branch = await run_git(git_service.get_current_branch, working_dir)
    if not current_branch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Could not determine the current branch")

    repo = github_service.get_repo_from_remote(working_dir)
    if not repo:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Could not determine GitHub repository. Ensure the project has a GitHub remote.",
        )

    pr = _find_open_pr_for_branch(repo, current_branch)
    if pr is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"No open pull request found for branch '{current_branch}'",
        )

    st = await run_git(git_service.get_status, working_dir)
    if not (st or {}).get("is_clean", False):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Working tree has uncommitted changes — commit or discard changes first",
        )

    default_branch = await run_git(git_service.get_default_branch, working_dir)

    _sweep_expired()
    op = Operation(
        project_id=project_id,
        kind="finish",
        session_id=body.session_id,
        steps=[
            _step("guard", "Validate branch and pull request"),
            _step("push", "Push commits"),
            _step("merge", "Merge pull request"),
            _step("cleanup", "Clean up branch"),
        ],
    )
    op.set_step("guard", STEP_DONE)  # validated above
    _register(op)

    asyncio.create_task(
        _execute_finish(
            op,
            working_dir=working_dir,
            worktree_id=worktree_id,
            repo=repo,
            pr_number=pr.number,
            pr_url=pr.url,
            feature_branch=current_branch,
            default_branch=default_branch,
            merge_method=body.merge_method,
            cleanup=body.cleanup,
            session_id=body.session_id,
        )
    )
    return op.snapshot()


@router.post("/ship")
async def ship(
    request: Request,
    project_id: str,
    body: ShipRequest,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth),
):
    """Commit everything, open a PR (if needed), merge it, then optionally clean up."""
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Project is not a git repository")

    if _has_active_op(project_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Another git operation is already running for this project")

    current_branch = await run_git(git_service.get_current_branch, working_dir)
    if not current_branch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Could not determine the current branch")

    repo = github_service.get_repo_from_remote(working_dir)
    if not repo:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Could not determine GitHub repository. Ensure the project has a GitHub remote.",
        )

    st = await run_git(git_service.get_status, working_dir)
    dirty = not (st or {}).get("is_clean", False)

    default_branch = await run_git(git_service.get_default_branch, working_dir)
    base_branch = body.base_branch or default_branch

    existing_pr = _find_open_pr_for_branch(repo, current_branch)

    _sweep_expired()
    op = Operation(
        project_id=project_id,
        kind="ship",
        session_id=body.session_id,
        steps=[
            _step("commit", "Commit changes"),
            _step("push", "Push commits"),
            _step("create_pr", "Create pull request"),
            _step("merge", "Merge pull request"),
            _step("cleanup", "Clean up branch"),
        ],
    )
    _register(op)

    asyncio.create_task(
        _execute_ship(
            op,
            working_dir=working_dir,
            worktree_id=worktree_id,
            repo=repo,
            feature_branch=current_branch,
            default_branch=default_branch,
            base_branch=base_branch,
            existing_pr=existing_pr,
            dirty=dirty,
            include_unstaged=body.include_unstaged,
            merge_method=body.merge_method,
            draft=body.draft,
            cleanup=body.cleanup,
            session_id=body.session_id,
        )
    )
    return op.snapshot()


@router.get("/operations/{operation_id}")
async def get_operation(
    request: Request,
    project_id: str,
    operation_id: str,
    token: str = Depends(require_auth),
):
    """Poll the status and per-step progress of a running/finished operation."""
    check_project_access(request, project_id)
    op = _operations.get(operation_id)
    if not op or op.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Operation not found")
    return op.snapshot()
