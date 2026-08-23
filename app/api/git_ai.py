"""
Git AI generation API endpoints.

Provides AI-powered generation for:
- Commit messages from diffs
- PR titles and descriptions from branch commits
- Branch names from user messages
"""

import re
import time
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request, status

from app.core.git_service import git_service
from app.api.auth import require_auth
from app.api.common import check_project_access
from app.api.git_helpers import run_git, get_working_dir

from app.api.schemas.git import (
    GenerateCommitMessageRequest,
    GenerateCommitMessageResponse,
    GeneratePRDescriptionRequest,
    GeneratePRDescriptionResponse,
    GenerateBranchNameRequest,
    GenerateBranchNameResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# Branch Name Generation Helpers
# =============================================================================

# Common filler/stop words to strip from branch names
STOP_WORDS = {
    'a', 'an', 'the', 'is', 'it', 'in', 'on', 'at', 'to', 'for', 'of',
    'and', 'or', 'but', 'not', 'with', 'from', 'by', 'as', 'be', 'was',
    'are', 'been', 'has', 'have', 'had', 'do', 'does', 'did', 'will',
    'would', 'could', 'should', 'can', 'may', 'might', 'shall',
    'i', 'me', 'my', 'we', 'us', 'our', 'you', 'your', 'he', 'she',
    'they', 'them', 'their', 'this', 'that', 'these', 'those',
    'please', 'help', 'want', 'need', 'like', 'just', 'also',
    'hi', 'hello', 'hey', 'thanks', 'thank',
}

# Keyword hints for inferring prefix from the message when no explicit prefix is given
FIX_KEYWORDS = {'fix', 'bug', 'broken', 'crash', 'error', 'issue', 'wrong', 'fail', 'failing', 'regression', 'hotfix', 'patch'}
CHORE_KEYWORDS = {'chore', 'deps', 'dependency', 'upgrade', 'bump', 'ci', 'lint', 'format', 'cleanup', 'clean'}
DOCS_KEYWORDS = {'docs', 'documentation', 'readme', 'comment', 'comments', 'jsdoc', 'docstring'}
REFACTOR_KEYWORDS = {'refactor', 'restructure', 'reorganize', 'simplify', 'extract', 'rename', 'move', 'split'}
TEST_KEYWORDS = {'test', 'tests', 'testing', 'spec', 'coverage'}


def infer_prefix(msg: str) -> str:
    """Infer a conventional branch prefix from message keywords."""
    words = set(msg.lower().split())
    if words & FIX_KEYWORDS:
        return 'fix'
    if words & REFACTOR_KEYWORDS:
        return 'refactor'
    if words & CHORE_KEYWORDS:
        return 'chore'
    if words & DOCS_KEYWORDS:
        return 'docs'
    if words & TEST_KEYWORDS:
        return 'test'
    return 'feat'


def slugify_branch(msg: str, prefix: str) -> str:
    """Fallback: extract meaningful words from message into a branch name."""
    import unicodedata
    # Normalize unicode
    value = unicodedata.normalize('NFKD', msg).encode('ascii', 'ignore').decode('ascii')
    value = value.lower().strip()
    # Remove non-word chars (keep spaces and hyphens)
    value = re.sub(r'[^\w\s-]', '', value)
    # Filter out stop words and single-char words, take first 5 meaningful words
    words = [w for w in value.split() if len(w) > 1 and w not in STOP_WORDS][:5]
    slug = '-'.join(words)
    # Truncate and clean
    slug = slug[:50].rstrip('-')
    return f"{prefix}/{slug}" if slug else f"{prefix}/auto-{int(time.time())}"


# =============================================================================
# Shared Generation Helpers
# =============================================================================
# The core generation logic lives here so both the HTTP endpoints below and the
# server-side finish/ship orchestrator (app/api/git_operations.py) share a
# single source of truth for prompts and output cleanup — no HTTP self-calls.


async def generate_commit_message_for_dir(
    working_dir: str, include_unstaged: bool = True
) -> str:
    """Generate a commit message from the working directory's diff.

    Raises HTTPException(400) when the directory is not a git repository or has
    no changes to describe.
    """
    from app.api.generation import generate_with_claude

    t_start = time.perf_counter()
    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    t_diff_start = time.perf_counter()
    diff_text = await run_git(
        git_service.get_diff_text, working_dir, include_unstaged=include_unstaged
    )
    t_diff_ms = (time.perf_counter() - t_diff_start) * 1000
    if not diff_text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No changes to generate a commit message for"
        )

    prompt = f"""Based on the following git diff, write a concise commit message.
Use conventional commit format (e.g., "feat:", "fix:", "refactor:", "docs:", "chore:", "perf:", "style:", "test:").
The first line should be under 72 characters.
If the change is complex, add a blank line followed by bullet points covering all significant changes.
Return ONLY the commit message text, nothing else.

<diff>
{diff_text}
</diff>"""

    # Use the Default AI profile's model (no explicit override - respects user config)
    t_ai_start = time.perf_counter()
    response_text = await generate_with_claude(prompt)
    t_ai_ms = (time.perf_counter() - t_ai_start) * 1000

    # Clean up: remove any markdown code blocks the model may have wrapped around the message
    message = response_text.strip()
    if message.startswith("```"):
        lines = message.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        message = "\n".join(lines).strip()

    t_total_ms = (time.perf_counter() - t_start) * 1000
    logger.info(
        f"[ship-it-timing] generate-commit-message "
        f"total={t_total_ms:.0f}ms diff_fetch={t_diff_ms:.0f}ms "
        f"ai_call={t_ai_ms:.0f}ms diff_bytes={len(diff_text)}"
    )

    return message


async def generate_pr_description_for_dir(
    working_dir: str, base_branch: str = "main"
) -> tuple[str, str]:
    """Generate a ``(title, body)`` PR description from the branch's commits.

    Raises HTTPException(400) when the directory is not a git repository or has
    no commits ahead of the base branch.
    """
    from app.api.generation import generate_with_claude, extract_json_from_response

    t_start = time.perf_counter()
    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    t_log_start = time.perf_counter()
    commit_log, diff_stat = await run_git(
        git_service.get_branch_log, working_dir, base_branch=base_branch
    )
    t_log_ms = (time.perf_counter() - t_log_start) * 1000

    if not commit_log.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No commits found ahead of the base branch"
        )

    prompt = f"""Based on the following git commits and change summary, generate a pull request title and description.
Return a JSON object with "title" and "body" keys.

The title should be concise (under 72 characters) and descriptive.
The body should have a "## Summary" section with bullet points describing what changed and why. Include as many bullet points as needed to cover all significant changes.

<commits>
{commit_log}
</commits>

<changes>
{diff_stat}
</changes>

Return ONLY the JSON object, no markdown code blocks or explanation."""

    # Use the Default AI profile's model (no explicit override - respects user config)
    t_ai_start = time.perf_counter()
    response_text = await generate_with_claude(prompt)
    t_ai_ms = (time.perf_counter() - t_ai_start) * 1000

    try:
        parsed = extract_json_from_response(response_text)
        title = parsed.get("title", "").strip()
        pr_body = parsed.get("body", "").strip()
    except (ValueError, KeyError) as e:
        logger.warning(f"Failed to parse AI PR description, using fallback: {e}")
        # Fallback: use first commit message as title
        first_line = commit_log.split("\n")[0] if commit_log else "Update"
        # Remove short SHA prefix if present
        title = " ".join(first_line.split()[1:]) if " " in first_line else first_line
        pr_body = ""

    if not title:
        first_line = commit_log.split("\n")[0] if commit_log else "Update"
        title = " ".join(first_line.split()[1:]) if " " in first_line else first_line

    t_total_ms = (time.perf_counter() - t_start) * 1000
    logger.info(
        f"[ship-it-timing] generate-pr-description "
        f"total={t_total_ms:.0f}ms branch_log={t_log_ms:.0f}ms "
        f"ai_call={t_ai_ms:.0f}ms log_bytes={len(commit_log)}"
    )

    return title, pr_body


# =============================================================================
# AI Generation Endpoints
# =============================================================================

@router.post("/generate-commit-message", response_model=GenerateCommitMessageResponse)
async def generate_commit_message(
    request: Request,
    project_id: str,
    body: GenerateCommitMessageRequest,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Generate a commit message using AI based on the current diff.

    When worktree_id is provided, generates message based on worktree's diff.
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id, session_id)
    message = await generate_commit_message_for_dir(working_dir, body.include_unstaged)
    return GenerateCommitMessageResponse(message=message)


@router.post("/generate-pr-description", response_model=GeneratePRDescriptionResponse)
async def generate_pr_description(
    request: Request,
    project_id: str,
    body: GeneratePRDescriptionRequest,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Generate a PR title and description using AI based on branch commits.

    When worktree_id is provided, generates description from worktree's branch.
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id, session_id)
    title, pr_body = await generate_pr_description_for_dir(working_dir, body.base_branch)
    return GeneratePRDescriptionResponse(title=title, body=pr_body)


@router.post("/generate-branch-name")
async def generate_branch_name(
    request: Request,
    project_id: str,
    body: GenerateBranchNameRequest,
    token: str = Depends(require_auth)
):
    """
    Generate a git branch name from a user message using AI.

    Falls back to a simple slugification if AI generation fails.
    """
    from app.api.generation import generate_with_claude

    check_project_access(request, project_id)

    fallback_prefix = body.prefix if body.prefix else infer_prefix(body.message)

    try:
        prefix_instruction = (
            f'Use the prefix "{body.prefix}/".'
            if body.prefix
            else 'Choose the most appropriate conventional prefix: feat/, fix/, refactor/, chore/, docs/, or test/.'
        )

        prompt = f"""Extract the core task from this chat message and generate a git branch name.

{prefix_instruction}
Format: prefix/lowercase-with-hyphens (e.g., feat/add-dark-mode, fix/login-timeout, refactor/extract-auth-service)
Rules:
- 3-5 descriptive words AFTER the prefix slash — focus on the technical task, not conversational filler
- Max 50 characters total
- Only lowercase letters, digits, hyphens, and one forward slash after prefix
- Use action verbs: fix, add, update, refactor, remove, improve, etc.
- Pick the prefix that best matches the intent:
  feat/ = new feature or enhancement
  fix/ = bug fix or correction
  refactor/ = code restructuring without behavior change
  chore/ = maintenance, deps, CI, formatting
  docs/ = documentation changes
  test/ = adding or updating tests

Message: "{body.message[:500]}"

Return ONLY the branch name, nothing else."""

        # Use the Default AI profile's model (no explicit override).
        # Keeps the generation pool client reusable across all AI calls — any
        # explicit model override here would force a pool recycle if the default
        # model differs.
        #
        # Shorter-than-default budget: branch naming sits in front of an
        # interactive "create branch" flow and has a perfectly good non-AI
        # fallback (slugify_branch, below), so a slow/stuck leg should degrade
        # to the fallback in seconds rather than stall the UI for the full
        # generation default. Commit messages and PR descriptions keep the
        # default — they have no equivalent fallback.
        response_text = await generate_with_claude(prompt, timeout_seconds=45)
        branch_name = response_text.strip().strip('"').strip("'").strip('`')

        # Validate the generated name
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9/_-]*$', branch_name) or len(branch_name) > 60:
            branch_name = slugify_branch(body.message, fallback_prefix)

        # If caller forced a prefix, ensure it's used
        if body.prefix and not branch_name.startswith(f"{body.prefix}/"):
            branch_name = f"{body.prefix}/{branch_name.split('/', 1)[-1] if '/' in branch_name else branch_name}"

        display_name = branch_name.split('/', 1)[-1].replace('-', ' ').title() if '/' in branch_name else branch_name

        return GenerateBranchNameResponse(branch_name=branch_name, display_name=display_name)

    except Exception as e:
        logger.warning(f"AI branch name generation failed, using fallback: {e}")
        branch_name = slugify_branch(body.message, fallback_prefix)
        display_name = branch_name.split('/', 1)[-1].replace('-', ' ').title()
        return GenerateBranchNameResponse(branch_name=branch_name, display_name=display_name)
