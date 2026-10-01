"""Host-owned Git isolation for Task and Swarm workers.

Use the direct caller's committed HEAD, never an origin tip or the routing root.
No model-chosen paths, credential copies, shared runtime links, automatic merges,
or destructive cleanup. Retain checkouts (including failed launches) for review
and let the existing checkout/worktree lifecycle own their eventual removal.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re


class WorkerCheckoutError(RuntimeError):
    """Fail closed rather than launch a coding worker in the caller's tree."""


@dataclass(frozen=True)
class WorkerCheckout:
    path: str
    branch: str
    base_commit: str
    project_v2_id: str
    parent_branch: str | None

    def instructions(self) -> str:
        return (
            "# Host-provisioned worker checkout\n"
            + json.dumps({"path": self.path, "branch": self.branch,
                          "base_commit": self.base_commit,
                          "integration_target": self.parent_branch})
            + "\nThe app created and bound this dedicated checkout before launch. "
            "Use it instead of any checkout path in the assignment. Your default "
            "working directory is this checkout. Do not create another worktree "
            "or switch the parent's branch. Uncommitted parent changes and "
            "dependencies are not copied; install required dependencies here. "
            "Commit scoped changes and return their SHAs and this checkout path "
            "for the parent's review/integration. Do not merge into the parent "
            "checkout, remove this worktree, or push/open a PR unless assigned."
        )


def prepare_worker_checkout(parent_row, worker_session_id, profile_id, tool_allowlist=None):
    """Provision write-capable project workers; explicit research may share.

    Uses the same read-only declaration as Task mode selection, honoring file
    agents' effective allowlists. Unknown/unrestricted surfaces are isolated.
    A missing/mismatched project checkout is a refusal, not a fallback to main.
    Non-project and non-Git tasks retain their normal working-directory behavior.
    """
    from app.core import checkout_manager, project_resolver
    from app.core.builtin_subagents import is_read_only_tool_surface
    from app.core.git_service import git_service
    from app.db import database

    if parent_row is None:
        raise WorkerCheckoutError("The calling session is unavailable; refusing worker checkout inheritance.")
    tools = tool_allowlist
    if not tools:
        profile = database.get_profile(profile_id) or {}
        config = profile.get("config") or {}
        if config.get("tool_selection_mode", "allow") == "allow":
            tools = config.get("allowed_tools")
    # Normalize qualified builtin names without treating external MCP writers
    # as a known read-only surface. Wildcards and unknown tools isolate safely.
    if tools:
        from app.core.builtin_subagents import WRITE_CAPABLE_TOOLS
        from fluid.cli.prompt import BUILTIN_TOOL_NAMES
        known = {name.lower() for name in BUILTIN_TOOL_NAMES}
        normalized = [str(name).removeprefix("mcp__builtins__").strip().lower() for name in tools]
        if all(name in known or name in WRITE_CAPABLE_TOOLS for name in normalized):
            if is_read_only_tool_surface(normalized):
                return None
    if not (parent_row.get("project_v2_id") or parent_row.get("project_id")):
        return None
    project = project_resolver.resolve_project_v2(dict(parent_row))
    if not project:
        raise WorkerCheckoutError("The caller's project is unavailable; refusing shared coding checkout.")
    parent_path = project_resolver.resolve_existing_cwd(dict(parent_row))
    if not parent_path:
        raise WorkerCheckoutError("The caller's checkout is unavailable or its branch binding mismatches; refusing to launch in another checkout.")
    if not git_service.is_git_repo(parent_path):
        return None
    expected = parent_row.get("branch") or parent_row.get("worktree_branch")
    if expected:
        checkout_manager._require_branch_binding(parent_path, expected)
    tip = git_service._run_git(parent_path, ["rev-parse", "--verify", "HEAD^{commit}"], timeout=5)
    sha = tip.stdout.strip()
    if tip.returncode or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        raise WorkerCheckoutError("The caller has no valid committed HEAD to fork a worker from.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", worker_session_id):
        raise WorkerCheckoutError("Invalid worker identity for a dedicated branch.")
    branch = f"task/{worker_session_id}"
    # Creating the ref first prevents add_worktree's new-branch path fetching
    # origin and preferring an origin/<base> tip over the actual caller HEAD.
    if not git_service.create_branch(parent_path, branch, start_point=sha):
        raise WorkerCheckoutError(f"Could not create dedicated worker branch {branch}; refusing to reuse it.")
    path = checkout_manager.ensure_checkout(project, branch=branch, provision_runtime=False)
    if not path:
        raise WorkerCheckoutError(f"Could not provision dedicated checkout for {branch}; refusing shared-checkout fallback. The branch is retained for review.")
    checkout_manager._require_branch_binding(path, branch)
    actual = git_service._run_git(path, ["rev-parse", "HEAD"], timeout=5)
    if actual.returncode or actual.stdout.strip() != sha:
        raise WorkerCheckoutError(f"Dedicated checkout {path} does not match its recorded base commit; refusing launch.")
    if expected:
        checkout_manager._require_branch_binding(parent_path, expected)
    return WorkerCheckout(path, branch, sha, project["id"], expected)
