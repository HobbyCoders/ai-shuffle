"""Merge when green: watch a session's open PR and merge it once CI passes.

The owner typed "merge to main and push" / "merge on green" in 8+ sessions and
then waited for an agent to babysit CI. With this on for a session, the worker
polls the PR's checks; when every check has passed and GitHub reports it
mergeable, it merges (the same ``merge_pull`` the Git panel uses, never
``--delete-branch``: sibling worktrees make that fail after the merge lands),
deletes the remote branch separately, records the PR as merged and posts a
short row in the chat. Red checks, a conflict, a closed PR or a two-hour
budget stop the watch with a row saying why.

It never merges while another session is working in the PR's branch or
worktree: the existing idle-branch guard is checked first and the watch waits.
Watches live in memory on the worker that started them; a restart ends them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

POLL_S = 60
BUDGET_S = 2 * 60 * 60
SUBTYPE = "auto_merge"

_FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}


@dataclass
class Watch:
    session_id: str
    project_id: Optional[str]
    repo: str
    pr_number: int
    method: str
    working_dir: Optional[str]
    started: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task] = None
    state: str = "watching"


_watches: Dict[str, Watch] = {}


def checks_verdict(rollup: List[Dict[str, Any]]) -> str:
    """``passed`` / ``failed`` / ``pending`` from gh's ``statusCheckRollup``."""
    if not rollup:
        return "pending"
    verdicts = []
    for check in rollup:
        status = str(check.get("status") or check.get("state") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        if check.get("__typename") == "StatusContext":  # commit statuses use state only
            conclusion = status
            status = "COMPLETED" if status not in {"PENDING", "EXPECTED"} else status
        if status != "COMPLETED":
            verdicts.append("pending")
        elif conclusion in _FAILED:
            verdicts.append("failed")
        elif conclusion in _PASSED:
            verdicts.append("passed")
        else:
            verdicts.append("pending")
    if "failed" in verdicts:
        return "failed"
    return "passed" if all(v == "passed" for v in verdicts) else "pending"


def _pr_view(repo: str, number: int) -> Optional[Dict[str, Any]]:
    from app.core.github_service import github_service

    try:
        result = github_service._run_gh(["pr", "view", str(number), "--repo", repo, "--json",
                                         "state,mergeable,headRefName,url,statusCheckRollup"], timeout=30)
    except Exception:  # noqa: BLE001 — a gh blip is retried on the next poll
        logger.debug("auto-merge: gh pr view failed", exc_info=True)
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None


async def _record(session_id: str, content: str, data: Dict[str, Any]) -> None:
    try:
        from app.core.turn_recorder import record_drained_event

        await record_drained_event(session_id, {"type": "system", "subtype": SUBTYPE, "content": content,
                                                "data": {**data, "content": content}})
    except Exception:  # noqa: BLE001
        logger.warning("Could not record auto-merge row for %s", session_id, exc_info=True)


async def _idle(watch: Watch) -> bool:
    from fastapi import HTTPException

    from app.api.github import require_idle_pull_branches

    if not watch.project_id:
        return True
    try:
        await require_idle_pull_branches(watch.project_id, watch.repo, watch.pr_number,
                                         working_dir=watch.working_dir)
        return True
    except HTTPException:
        return False


async def _merge(watch: Watch, pr: Dict[str, Any]) -> str:
    from app.core.github_service import github_service
    from app.core.session_pr_state import persist_pr_state

    ok, error = await asyncio.to_thread(github_service.merge_pull, watch.repo, watch.pr_number, watch.method)
    if not ok and "already merged" not in str(error or "").lower():
        return f"Merge when green: GitHub refused the merge of PR #{watch.pr_number}: {error or 'unknown error'}"
    await persist_pr_state(session_id=watch.session_id, project_id=watch.project_id,
                           branch=pr.get("headRefName"), pr_number=watch.pr_number,
                           pr_url=pr.get("url"), pr_state="merged")
    branch = pr.get("headRefName")
    deleted = ""
    if branch:
        result = await asyncio.to_thread(github_service._run_gh, [
            "api", "-X", "DELETE", f"repos/{watch.repo}/git/refs/heads/{branch}"], 30)
        deleted = " and deleted its remote branch" if result.returncode == 0 else ""
    return f"Merge when green: CI passed, merged PR #{watch.pr_number}{deleted}."


async def _run(watch: Watch) -> None:
    outcome = None
    try:
        while time.monotonic() - watch.started < BUDGET_S:
            pr = await asyncio.to_thread(_pr_view, watch.repo, watch.pr_number)
            if pr is None:
                await asyncio.sleep(POLL_S)
                continue
            state = str(pr.get("state") or "").upper()
            if state == "MERGED":
                outcome = f"Merge when green: PR #{watch.pr_number} was already merged."
                break
            if state == "CLOSED":
                outcome = f"Merge when green stopped: PR #{watch.pr_number} was closed."
                break
            if str(pr.get("mergeable") or "").upper() == "CONFLICTING":
                outcome = f"Merge when green stopped: PR #{watch.pr_number} has merge conflicts."
                break
            verdict = checks_verdict(pr.get("statusCheckRollup") or [])
            if verdict == "failed":
                outcome = f"Merge when green stopped: a CI check failed on PR #{watch.pr_number}."
                break
            if verdict == "passed" and await _idle(watch):
                outcome = await _merge(watch, pr)
                break
            await asyncio.sleep(POLL_S)
        else:
            outcome = f"Merge when green stopped after {BUDGET_S // 3600} hours without a green, idle PR #{watch.pr_number}."
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Auto-merge watch failed for %s", watch.session_id, exc_info=True)
        outcome = f"Merge when green stopped: {exc}"
    finally:
        if _watches.get(watch.session_id) is watch:
            _watches.pop(watch.session_id, None)
    if outcome:
        watch.state = "done"
        await _record(watch.session_id, outcome, {"pr_number": watch.pr_number})


def status(session_id: str) -> Dict[str, Any]:
    watch = _watches.get(session_id)
    return {"enabled": watch is not None, "pr_number": watch.pr_number if watch else None}


def _session_target(session_id: str) -> Dict[str, Any]:
    """Blocking: the PR, repo and checkout a session's auto-merge would act on."""
    from app.core.github_service import github_service
    from app.core.project_utils import get_project_working_dir
    from app.db import database

    session = database.get_session(session_id)
    if not session:
        raise ValueError("Session not found")
    if not session.get("pr_number") or str(session.get("pr_state") or "").lower() != "open":
        raise ValueError("This session has no open pull request to merge.")
    working_dir = None
    if session.get("worktree_path"):
        working_dir = get_project_working_dir(session["worktree_path"])
    elif session.get("project_id"):
        project = database.get_project(session["project_id"])
        working_dir = get_project_working_dir(project) if project else None
    repo = github_service.get_repo_from_remote(working_dir) if working_dir else None
    if not repo:
        raise ValueError("Could not determine this session's GitHub repository.")
    return {"project_id": session.get("project_id"), "repo": repo,
            "pr_number": int(session["pr_number"]), "working_dir": working_dir}


async def enable(session_id: str, method: str = "squash") -> Dict[str, Any]:
    if session_id in _watches:
        return status(session_id)
    target = await asyncio.to_thread(_session_target, session_id)
    watch = Watch(session_id=session_id, method=method, **target)
    _watches[session_id] = watch
    watch.task = asyncio.get_running_loop().create_task(_run(watch))
    await _record(session_id, f"Merge when green is on for PR #{watch.pr_number}.", {"pr_number": watch.pr_number})
    return status(session_id)


async def disable(session_id: str) -> Dict[str, Any]:
    watch = _watches.pop(session_id, None)
    if watch and watch.task:
        watch.task.cancel()
    return status(session_id)
