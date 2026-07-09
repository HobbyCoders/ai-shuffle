"""
Git AI generation API endpoints.

Provides AI-powered generation for:
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
# AI Generation Endpoints
# =============================================================================

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
        response_text = await generate_with_claude(prompt)
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
