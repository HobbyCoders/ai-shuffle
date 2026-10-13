"""
GitHub integration service using gh CLI

This service wraps the GitHub CLI (gh) to provide pull request and
workflow management capabilities.
"""

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from fluid.tools.child_env import scrub_child_env

from app.core.export_redaction import redact_export_text
from app.core.platform import get_gh_executable
from app.core.ttl_cache import CACHE_MISS, TTLCache  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)


# =============================================================================
# Errors
# =============================================================================

class GitHubCliError(RuntimeError):
    """A ``gh`` invocation failed — the answer is *unknown*, not "empty".

    Raised by the read helpers (``list_pulls``, ``get_pull``, ``list_runs``,
    ``get_run``, ``get_repo_info``) when gh exits non-zero for a reason that is
    not "the thing does not exist", times out, or returns unparseable JSON.

    This exists because swallowing those failures into ``[]`` / ``None`` makes a
    rate-limited, offline or slow gh indistinguishable from "there is no pull
    request" — a lie that then gets cached and flips the UI between states.
    Callers that cache MUST NOT store a negative result derived from this error.
    """

    def __init__(self, message: str, *, stderr: Optional[str] = None):
        super().__init__(message)
        self.stderr = (stderr or "").strip()


# Substrings that mark a non-zero gh exit as an honest "it does not exist"
# rather than a failure to reach GitHub. Matched case-insensitively against
# stderr (gh writes API/GraphQL errors there).
_NOT_FOUND_MARKERS = (
    "could not resolve to",
    "not found",
    "no pull requests found",
    "could not find",
    "no such",
    "404",
)


def _looks_like_not_found(stderr: str) -> bool:
    """True when *stderr* says the object does not exist (vs. gh failing)."""
    low = (stderr or "").lower()
    return any(marker in low for marker in _NOT_FOUND_MARKERS)


# =============================================================================
# Small shared TTL cache
# =============================================================================





# ``gh auth status`` spawns a process and can hang for its full timeout. Being
# signed in is a machine-level fact that changes only on an explicit login or
# logout, both of which invalidate these caches — so a multi-minute TTL is
# safe. A *negative* answer gets a much shorter TTL so a login performed
# outside the app (``gh auth login`` in a terminal) is picked up quickly.
GH_AUTH_TTL_SECONDS = 300.0
GH_AUTH_NEGATIVE_TTL_SECONDS = 30.0

# The signed-in user's repositories (clone picker). Fetched a page of 100 at a
# time up to USER_REPOS_MAX_PAGES and then searched/paged in memory, so typing
# in the picker never costs a GitHub request. Repos change rarely; the picker
# offers a refresh for the one that was created a minute ago.
USER_REPOS_TTL_SECONDS = 120.0
USER_REPOS_PAGE_SIZE = 100
USER_REPOS_MAX_PAGES = 10
USER_REPOS_PAGE_TIMEOUT = 20
# Fields kept from each REST repository object. Never the owner's token: gh
# reads it from its own config and it never appears in the API payload.
_USER_REPOS_JQ = (
    '.[] | {full_name, name, owner: .owner.login, private, fork, archived,'
    ' description, clone_url, html_url, default_branch,'
    ' updated_at: (.pushed_at // .updated_at)}'
)



def user_repos_endpoint(page: int) -> str:
    """The REST path for one page of the signed-in user's repositories."""
    return (
        f"/user/repos?per_page={USER_REPOS_PAGE_SIZE}&page={page}"
        "&sort=pushed&affiliation=owner,collaborator,organization_member"
    )


def _parse_mergeable(value: Any) -> Optional[bool]:
    """Convert GitHub CLI mergeable string to Optional[bool].

    gh CLI returns 'MERGEABLE', 'CONFLICTING', or 'UNKNOWN' (not a bool).
    """
    if isinstance(value, bool):
        return value
    if value == 'MERGEABLE':
        return True
    if value == 'CONFLICTING':
        return False
    return None


@dataclass
class PullRequest:
    """Represents a GitHub Pull Request"""
    number: int
    title: str
    body: str
    state: str
    head_branch: str
    base_branch: str
    author: str
    url: str
    created_at: str
    updated_at: str
    mergeable: Optional[bool] = None
    head_sha: Optional[str] = None


@dataclass
class WorkflowRun:
    """Represents a GitHub Actions workflow run"""
    id: int
    name: str
    status: str
    conclusion: Optional[str]
    branch: str
    event: str
    url: str
    created_at: str



def _review_state(decision: Optional[str]) -> str:
    """gh's ``reviewDecision`` as the inbox's review filter value."""
    return {
        'APPROVED': 'approved',
        'CHANGES_REQUESTED': 'changes_requested',
        'REVIEW_REQUIRED': 'review_required',
    }.get(str(decision or '').upper(), 'none')


_FAILED_CONCLUSIONS = {'FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE'}


def summarize_checks(rollup: Any) -> str:
    """Fold a ``statusCheckRollup`` into ``passing``/``failing``/``pending``/``none``.

    The rollup mixes check runs (``status`` + ``conclusion``) and commit
    statuses (``state``). Any failure wins; then anything unfinished.
    """
    if not isinstance(rollup, list) or not rollup:
        return 'none'
    pending = False
    for item in rollup:
        if not isinstance(item, dict):
            continue
        if 'state' in item and 'conclusion' not in item:
            state = str(item.get('state') or '').upper()
            if state in ('FAILURE', 'ERROR'):
                return 'failing'
            if state in ('PENDING', 'EXPECTED', ''):
                pending = True
            continue
        if str(item.get('status') or '').upper() != 'COMPLETED':
            pending = True
            continue
        if str(item.get('conclusion') or '').upper() in _FAILED_CONCLUSIONS:
            return 'failing'
    return 'pending' if pending else 'passing'


class GitHubService:
    """
    Service for interacting with GitHub via the gh CLI.

    The gh CLI must be installed and authenticated for this service to work.
    All methods handle errors gracefully and return None or empty lists on failure.
    """

    def __init__(self):
        self.timeout = 60
        # ``gh auth status`` result, keyed by a single constant. Per-instance so
        # tests get a clean cache with every fresh GitHubService().
        self._auth_cache = TTLCache(
            ttl=GH_AUTH_TTL_SECONDS, max_entries=1, name="gh-auth"
        )
        self._repos_cache = TTLCache(
            ttl=USER_REPOS_TTL_SECONDS, max_entries=1, name="gh-user-repos"
        )
        # One fetch at a time: concurrent cold-cache requests wait for the
        # first instead of each walking every page.
        self._repos_lock = threading.Lock()

    def _run_gh(
        self,
        args: List[str],
        timeout: int = None,
        stdin_data: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """
        Run a gh CLI command.

        Args:
            args: Command arguments (excluding 'gh')
            timeout: Optional timeout in seconds (defaults to self.timeout)
            stdin_data: Optional string to pass via stdin

        Returns:
            CompletedProcess with stdout, stderr, and returncode

        Raises:
            subprocess.TimeoutExpired: If command times out
            subprocess.SubprocessError: If command fails to execute
        """
        cmd = [get_gh_executable() or 'gh'] + args
        timeout = timeout or self.timeout

        logger.debug(f"Running gh command: {' '.join(cmd)}")

        try:
            # Force UTF-8 on stdin/stdout/stderr. Default `text=True` uses the
            # system locale (cp1252 on Windows), which fails to encode common
            # AI-generated unicode like → (\u2192) or ✓ (\u2713) in PR bodies.
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                input=stdin_data,
                env=scrub_child_env(),
            )

            if result.returncode != 0:
                # gh's error text is not expected to carry a credential, but
                # it is free text from another program: redact before logging.
                logger.warning("gh command failed: %s", redact_export_text(result.stderr or ""))

            return result
        except subprocess.TimeoutExpired:
            logger.error(f"gh command timed out after {timeout}s: {' '.join(cmd)}")
            raise
        except Exception as e:
            logger.error(f"gh command error: {e}")
            raise

    _AUTH_CACHE_KEY = "gh-auth-status"

    def is_authenticated(self, *, refresh: bool = False) -> bool:
        """
        Check if gh CLI is authenticated.

        The answer is cached (see :data:`GH_AUTH_TTL_SECONDS`) because this
        shells out to ``gh auth status`` with a 10s timeout and every PR /
        actions endpoint asks. Pass ``refresh=True`` to force the subprocess,
        or call :meth:`invalidate_auth_cache` after an explicit login/logout.

        Returns:
            True if authenticated, False otherwise
        """
        if not refresh:
            cached = self._auth_cache.get_or_miss(self._AUTH_CACHE_KEY)
            if cached is not CACHE_MISS:
                return cached

        try:
            result = self._run_gh(['auth', 'status'], timeout=10)
            value = result.returncode == 0
        except Exception as e:
            logger.debug(f"GitHub auth check failed: {e}")
            value = False

        self._auth_cache.set(
            self._AUTH_CACHE_KEY,
            value,
            ttl=None if value else GH_AUTH_NEGATIVE_TTL_SECONDS,
        )
        return value

    def invalidate_auth_cache(self, value: Optional[bool] = None) -> None:
        """Drop the cached ``gh auth status`` answer.

        Called by the login/logout endpoints so an explicit auth change is
        visible immediately instead of at the end of the TTL. Pass *value* to
        seed the cache with a freshly observed answer instead of clearing it.
        """
        if value is None:
            self._auth_cache.invalidate()
        else:
            self._auth_cache.set(
                self._AUTH_CACHE_KEY,
                bool(value),
                ttl=None if value else GH_AUTH_NEGATIVE_TTL_SECONDS,
            )

    def get_repo_from_remote(self, working_dir: str) -> Optional[str]:
        """
        Extract owner/repo from git remote URL in a directory.

        Args:
            working_dir: Path to git repository

        Returns:
            Repository in "owner/repo" format, or None if not found.

        Unlike the gh read helpers this stays fail-soft: it shells out to local
        ``git``, where "the command failed" and "there is no GitHub origin" are
        the same actionable answer for the caller ("no GitHub repo here").
        """
        try:
            result = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                capture_output=True,
                text=True,
                cwd=working_dir,
                timeout=10,
                env=scrub_child_env(),
            )

            if result.returncode != 0:
                return None

            remote_url = result.stdout.strip()

            # Parse various remote URL formats:
            # - https://github.com/owner/repo.git
            # - git@github.com:owner/repo.git
            # - https://github.com/owner/repo
            # - gh:owner/repo

            # SSH format: git@github.com:owner/repo.git
            ssh_match = re.search(r'github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$', remote_url)
            if ssh_match:
                return f"{ssh_match.group(1)}/{ssh_match.group(2)}"

            # HTTPS format: https://github.com/owner/repo
            https_match = re.search(r'github\.com/([^/]+)/([^/\s]+?)(?:\.git)?$', remote_url)
            if https_match:
                return f"{https_match.group(1)}/{https_match.group(2)}"

            return None
        except Exception as e:
            logger.debug(f"Failed to get repo from remote: {e}")
            return None

    def get_repo_info(self, repo: str) -> Optional[Dict[str, Any]]:
        """
        Get basic repository information.

        Args:
            repo: Repository in "owner/repo" format

        Returns:
            Dict with repo info, or None when the repository does not exist.

        Raises:
            GitHubCliError: gh could not answer (offline, rate-limited, timed
                out, unparseable output). Distinct from "repo not found".
        """
        try:
            result = self._run_gh([
                'repo', 'view', repo,
                '--json', 'name,owner,description,url,defaultBranchRef'
            ])
        except Exception as e:
            raise GitHubCliError(f"gh repo view failed: {e}") from e

        if result.returncode != 0:
            if _looks_like_not_found(result.stderr):
                return None
            raise GitHubCliError(
                f"gh repo view failed for {repo}", stderr=result.stderr
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise GitHubCliError(f"gh repo view returned invalid JSON: {e}") from e

        return {
            'name': data.get('name'),
            'owner': data.get('owner', {}).get('login'),
            'description': data.get('description'),
            'url': data.get('url'),
            'default_branch': data.get('defaultBranchRef', {}).get('name', 'main')
        }

    def create_repo(
        self,
        name: str,
        private: bool = True,
        description: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Create a new GitHub repository using gh CLI.

        Args:
            name: Repository name
            private: If True, create a private repository
            description: Optional repository description

        Returns:
            Dict with name, full_name, url, clone_url keys, or None on failure
        """
        # The name is a positional argv element: one starting with "-" would be
        # parsed by gh as a flag (e.g. "--source=." or "--template=<repo>").
        if not name or name.startswith('-'):
            logger.error(f"Refusing GitHub repo name that looks like an option: {name[:50]!r}")
            return None
        try:
            args = ['repo', 'create', name]
            args.append('--private' if private else '--public')
            if description:
                args.extend(['--description', description])
            args.extend(['--json', 'name,nameWithOwner,url,sshUrl'])

            result = self._run_gh(args, timeout=30)

            if result.returncode != 0:
                logger.error(f"Failed to create GitHub repo: {result.stderr}")
                return None

            data = json.loads(result.stdout)
            return {
                'name': data.get('name'),
                'full_name': data.get('nameWithOwner'),
                'url': data.get('url'),
                'clone_url': data.get('sshUrl') or data.get('url'),
            }
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse repo creation response: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to create GitHub repo: {e}")
            return None

    # =========================================================================
    # The signed-in user's repositories
    # =========================================================================

    _REPOS_CACHE_KEY = "user-repos"

    def invalidate_repos_cache(self) -> None:
        """Forget the cached repository list (sign-in changed, or refresh)."""
        self._repos_cache.invalidate()

    def list_user_repos(self, *, refresh: bool = False) -> Tuple[List[Dict[str, Any]], bool]:
        """Every repository the signed-in account can clone. Blocking.

        Owned, collaborator and organization-member repositories, most
        recently pushed first. Returns ``(repos, truncated)``; *truncated* is
        True when the account has more than
        ``USER_REPOS_PAGE_SIZE * USER_REPOS_MAX_PAGES`` and the oldest were
        left out (the URL field still clones any of them).

        The list is cached for :data:`USER_REPOS_TTL_SECONDS`; pass
        ``refresh=True`` to refetch. A failed fetch is never cached.

        Raises:
            GitHubCliError: gh could not answer (offline, rate-limited, token
                revoked, timed out, unparseable output).
        """
        with self._repos_lock:
            if not refresh:
                cached = self._repos_cache.get_or_miss(self._REPOS_CACHE_KEY)
                if cached is not CACHE_MISS:
                    return cached
            value = self._fetch_user_repos()
            self._repos_cache.set(self._REPOS_CACHE_KEY, value)
            return value

    def _fetch_user_repos(self) -> Tuple[List[Dict[str, Any]], bool]:
        repos: List[Dict[str, Any]] = []
        seen: set = set()
        truncated = False
        for page in range(1, USER_REPOS_MAX_PAGES + 1):
            try:
                result = self._run_gh(
                    ['api', '--hostname', 'github.com', user_repos_endpoint(page), '--jq', _USER_REPOS_JQ],
                    timeout=USER_REPOS_PAGE_TIMEOUT,
                )
            except Exception as e:
                raise GitHubCliError(f"gh api user/repos failed: {e}") from e
            if result.returncode != 0:
                raise GitHubCliError("gh api user/repos failed", stderr=result.stderr)
            count = 0
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as e:
                    raise GitHubCliError(f"gh api user/repos returned invalid JSON: {e}") from e
                count += 1
                full_name = item.get('full_name')
                if not isinstance(full_name, str) or full_name in seen:
                    continue
                seen.add(full_name)
                repos.append({
                    'full_name': full_name,
                    'name': item.get('name') or full_name.split('/')[-1],
                    'owner': item.get('owner') or full_name.split('/')[0],
                    'private': bool(item.get('private')),
                    'fork': bool(item.get('fork')),
                    'archived': bool(item.get('archived')),
                    'description': item.get('description') or None,
                    # HTTPS so a private clone uses the credential helper gh
                    # set up at sign-in; no token is ever put in the URL.
                    'clone_url': item.get('clone_url') or f"https://github.com/{full_name}.git",
                    'html_url': item.get('html_url') or f"https://github.com/{full_name}",
                    'default_branch': item.get('default_branch') or None,
                    'updated_at': item.get('updated_at') or None,
                })
            if count < USER_REPOS_PAGE_SIZE:
                break
        else:
            truncated = True
        repos.sort(key=lambda r: r['updated_at'] or '', reverse=True)
        return repos, truncated

    # =========================================================================
    # Pull Requests
    # =========================================================================

    def list_pulls(
        self,
        repo: str,
        state: str = 'open',
        limit: int = 30,
        head: Optional[str] = None,
    ) -> List[PullRequest]:
        """
        List pull requests for a repository.

        Args:
            repo: Repository in "owner/repo" format
            state: PR state filter - 'open', 'closed', 'merged', 'all'
            limit: Maximum number of PRs to return
            head: Optional head-branch filter (``gh pr list --head``)

        Returns:
            List of PullRequest objects. An empty list means "this repository
            genuinely has no matching pull requests" — never "we could not ask".

        Raises:
            GitHubCliError: gh could not answer. Callers MUST NOT treat this as
                "no PR exists" (a failure cached as a negative answer flips
                the UI between Merge-PR and Create-PR).
        """
        try:
            cmd = [
                'pr', 'list',
                '--repo', repo,
                '--state', state,
                '--limit', str(limit),
                '--json', 'number,title,body,state,headRefName,headRefOid,baseRefName,author,url,createdAt,updatedAt,mergeable',
            ]
            if head:
                cmd.extend(['--head', head])
            result = self._run_gh(cmd)
        except Exception as e:
            raise GitHubCliError(f"gh pr list failed: {e}") from e

        if result.returncode != 0:
            raise GitHubCliError(
                f"gh pr list failed for {repo}", stderr=result.stderr
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise GitHubCliError(f"gh pr list returned invalid JSON: {e}") from e

        pulls = []
        for pr_data in data:
            pulls.append(PullRequest(
                number=pr_data.get('number', 0),
                title=pr_data.get('title', ''),
                body=pr_data.get('body', ''),
                state=pr_data.get('state', 'unknown'),
                head_branch=pr_data.get('headRefName', ''),
                base_branch=pr_data.get('baseRefName', ''),
                author=(pr_data.get('author') or {}).get('login', 'unknown'),
                url=pr_data.get('url', ''),
                created_at=pr_data.get('createdAt', ''),
                updated_at=pr_data.get('updatedAt', ''),
                mergeable=_parse_mergeable(pr_data.get('mergeable')),
                head_sha=pr_data.get('headRefOid'),
            ))

        return pulls

    def list_inbox_pulls(self, repo: str, state: str = 'open', limit: int = 50) -> List[Dict[str, Any]]:
        """Pull requests with the review and check state the PR inbox shows.

        One ``gh pr list`` per repository. Returns plain dicts:
        ``number, title, url, author, head_branch, base_branch, state``
        (``open``/``draft``/``merged``/``closed``), ``review``
        (``approved``/``changes_requested``/``review_required``/``none``),
        ``checks`` (``passing``/``failing``/``pending``/``none``) and
        ``updated_at``.

        Raises:
            GitHubCliError: gh could not answer.
        """
        try:
            result = self._run_gh([
                'pr', 'list',
                '--repo', repo,
                '--state', state,
                '--limit', str(limit),
                '--json', 'number,title,url,author,headRefName,baseRefName,state,isDraft,reviewDecision,statusCheckRollup,updatedAt',
            ])
        except Exception as e:
            raise GitHubCliError(f"gh pr list failed: {e}") from e
        if result.returncode != 0:
            raise GitHubCliError(f"gh pr list failed for {repo}", stderr=result.stderr)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise GitHubCliError(f"gh pr list returned invalid JSON: {e}") from e

        pulls: List[Dict[str, Any]] = []
        for pr in data if isinstance(data, list) else []:
            raw_state = str(pr.get('state') or '').upper()
            pr_state = (
                'merged' if raw_state == 'MERGED'
                else 'closed' if raw_state == 'CLOSED'
                else 'draft' if pr.get('isDraft')
                else 'open'
            )
            pulls.append({
                'number': pr.get('number', 0),
                'title': pr.get('title', ''),
                'url': pr.get('url', ''),
                'author': (pr.get('author') or {}).get('login', 'unknown'),
                'head_branch': pr.get('headRefName', ''),
                'base_branch': pr.get('baseRefName', ''),
                'state': pr_state,
                'review': _review_state(pr.get('reviewDecision')),
                'checks': summarize_checks(pr.get('statusCheckRollup')),
                'updated_at': pr.get('updatedAt', ''),
            })
        return pulls

    def get_pull(self, repo: str, pr_number: int) -> Optional[PullRequest]:
        """
        Get a specific pull request.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: Pull request number

        Returns:
            PullRequest object, or None when the PR genuinely does not exist.

        Raises:
            GitHubCliError: gh could not answer (offline, rate-limited, timed
                out, unparseable output).
        """
        try:
            result = self._run_gh([
                'pr', 'view', str(pr_number),
                '--repo', repo,
                '--json', 'number,title,body,state,headRefName,baseRefName,author,url,createdAt,updatedAt,mergeable'
            ])
        except Exception as e:
            raise GitHubCliError(f"gh pr view failed: {e}") from e

        if result.returncode != 0:
            if _looks_like_not_found(result.stderr):
                return None
            raise GitHubCliError(
                f"gh pr view failed for {repo}#{pr_number}", stderr=result.stderr
            )

        try:
            pr_data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise GitHubCliError(f"gh pr view returned invalid JSON: {e}") from e

        return PullRequest(
            number=pr_data.get('number', 0),
            title=pr_data.get('title', ''),
            body=pr_data.get('body', ''),
            state=pr_data.get('state', 'unknown'),
            head_branch=pr_data.get('headRefName', ''),
            base_branch=pr_data.get('baseRefName', ''),
            author=(pr_data.get('author') or {}).get('login', 'unknown'),
            url=pr_data.get('url', ''),
            created_at=pr_data.get('createdAt', ''),
            updated_at=pr_data.get('updatedAt', ''),
            mergeable=_parse_mergeable(pr_data.get('mergeable'))
        )

    def _get_pull_or_none(self, repo: str, pr_number: int) -> Optional[PullRequest]:
        """:meth:`get_pull` for callers with a (value, error) contract.

        ``create_pull`` reports failure through its error string rather than by
        raising, so the read-back of a just-created / already-existing PR must
        not propagate :class:`GitHubCliError`.
        """
        try:
            return self.get_pull(repo, pr_number)
        except GitHubCliError as e:
            logger.warning("Could not read back PR %s#%s: %s", repo, pr_number, e)
            return None

    def create_pull(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False
    ) -> Tuple[Optional[PullRequest], Optional[str]]:
        """
        Create a pull request.

        Args:
            repo: Repository in "owner/repo" format
            title: PR title
            body: PR body/description
            head: Head branch name
            base: Base branch name
            draft: Open the pull request as a draft

        Returns:
            Tuple of (PullRequest or None, error message or None).
            On success: (pr, None). On failure: (None, error_detail).
        """
        try:
            # Last stop before the text is published on the PR. Strips only
            # unambiguous generation artifacts; prose is untouched.
            from app.core.git_text import sanitize_pr_body, sanitize_pr_title

            clean_title = sanitize_pr_title(title)
            clean_body = sanitize_pr_body(body)

            # Pass body via stdin (--body-file -) to avoid Windows command
            # line length limits (~8191 chars) with long PR descriptions.
            args = [
                'pr', 'create',
                '--repo', repo,
                '--title', clean_title,
                '--body-file', '-',
                '--head', head,
                '--base', base,
            ]
            if draft:
                args.append('--draft')
            result = self._run_gh(args, stdin_data=clean_body)

            if result.returncode != 0:
                stderr = result.stderr.strip()
                # If a PR already exists, extract its number and return it
                existing_match = re.search(r'already exists:\s*https?://\S+/pull/(\d+)', stderr)
                if existing_match:
                    existing_number = int(existing_match.group(1))
                    logger.debug(f"PR already exists (#{existing_number}), returning existing PR")
                    pr = self._get_pull_or_none(repo, existing_number)
                    return (pr, None) if pr else (None, f"PR #{existing_number} exists but could not be fetched")
                logger.error(f"Failed to create PR: {stderr}")
                return None, stderr or "gh pr create failed with no error output"

            # Parse the URL from output to get the PR number
            output = result.stdout.strip()
            pr_url_match = re.search(r'/pull/(\d+)', output)
            if pr_url_match:
                pr_number = int(pr_url_match.group(1))
                pr = self._get_pull_or_none(repo, pr_number)
                return (pr, None) if pr else (None, f"PR #{pr_number} created but could not be fetched")

            return None, "gh pr create succeeded but returned no PR URL"
        except Exception as e:
            logger.error(f"Failed to create PR: {e}")
            return None, str(e)

    def merge_pull(
        self,
        repo: str,
        pr_number: int,
        method: str = 'merge'
    ) -> Tuple[bool, Optional[str]]:
        """
        Merge a pull request.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: Pull request number
            method: Merge method - 'merge', 'squash', or 'rebase'

        Returns:
            Tuple of (success, error message or None).
            On success: (True, None). On failure: (False, error_detail).
        """
        try:
            method_flag = f'--{method}'

            result = self._run_gh([
                'pr', 'merge', str(pr_number),
                '--repo', repo,
                method_flag,
                '--delete-branch'
            ])

            if result.returncode != 0:
                stderr = result.stderr.strip()
                logger.error(f"Failed to merge PR: {stderr}")
                return False, stderr or "gh pr merge failed with no error output"

            return True, None
        except Exception as e:
            logger.error(f"Failed to merge PR: {e}")
            return False, str(e)

    def close_pull(self, repo: str, pr_number: int) -> Tuple[bool, Optional[str]]:
        """
        Close a pull request without merging.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: Pull request number

        Returns:
            Tuple of (success, error message or None).
        """
        try:
            result = self._run_gh([
                'pr', 'close', str(pr_number),
                '--repo', repo
            ])

            if result.returncode != 0:
                stderr = result.stderr.strip()
                logger.error(f"Failed to close PR: {stderr}")
                return False, stderr or "gh pr close failed with no error output"

            return True, None
        except Exception as e:
            logger.error(f"Failed to close PR: {e}")
            return False, str(e)

    # =========================================================================
    # GitHub Actions / Workflows
    # =========================================================================

    def list_runs(self, repo: str, limit: int = 10) -> List[WorkflowRun]:
        """
        List recent workflow runs.

        Args:
            repo: Repository in "owner/repo" format
            limit: Maximum number of runs to return

        Returns:
            List of WorkflowRun objects. Empty means "no runs", not "unknown".

        Raises:
            GitHubCliError: gh could not answer.
        """
        try:
            result = self._run_gh([
                'run', 'list',
                '--repo', repo,
                '--limit', str(limit),
                '--json', 'databaseId,name,status,conclusion,headBranch,event,url,createdAt'
            ])
        except Exception as e:
            raise GitHubCliError(f"gh run list failed: {e}") from e

        if result.returncode != 0:
            raise GitHubCliError(
                f"gh run list failed for {repo}", stderr=result.stderr
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise GitHubCliError(f"gh run list returned invalid JSON: {e}") from e

        runs = []
        for run_data in data:
            runs.append(WorkflowRun(
                id=run_data.get('databaseId', 0),
                name=run_data.get('name', ''),
                status=run_data.get('status', 'unknown'),
                conclusion=run_data.get('conclusion'),
                branch=run_data.get('headBranch', ''),
                event=run_data.get('event', ''),
                url=run_data.get('url', ''),
                created_at=run_data.get('createdAt', '')
            ))

        return runs

    def get_run(self, repo: str, run_id: int) -> Optional[WorkflowRun]:
        """
        Get a specific workflow run.

        Args:
            repo: Repository in "owner/repo" format
            run_id: Workflow run ID

        Returns:
            WorkflowRun object, or None when the run genuinely does not exist.

        Raises:
            GitHubCliError: gh could not answer.
        """
        try:
            result = self._run_gh([
                'run', 'view', str(run_id),
                '--repo', repo,
                '--json', 'databaseId,name,status,conclusion,headBranch,event,url,createdAt'
            ])
        except Exception as e:
            raise GitHubCliError(f"gh run view failed: {e}") from e

        if result.returncode != 0:
            if _looks_like_not_found(result.stderr):
                return None
            raise GitHubCliError(
                f"gh run view failed for {repo} run {run_id}", stderr=result.stderr
            )

        try:
            run_data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise GitHubCliError(f"gh run view returned invalid JSON: {e}") from e

        return WorkflowRun(
            id=run_data.get('databaseId', 0),
            name=run_data.get('name', ''),
            status=run_data.get('status', 'unknown'),
            conclusion=run_data.get('conclusion'),
            branch=run_data.get('headBranch', ''),
            event=run_data.get('event', ''),
            url=run_data.get('url', ''),
            created_at=run_data.get('createdAt', '')
        )

    def rerun_workflow(self, repo: str, run_id: int) -> bool:
        """
        Re-run a workflow.

        Args:
            repo: Repository in "owner/repo" format
            run_id: Workflow run ID

        Returns:
            True if rerun was triggered, False otherwise
        """
        try:
            result = self._run_gh([
                'run', 'rerun', str(run_id),
                '--repo', repo
            ])

            if result.returncode != 0:
                logger.error(f"Failed to rerun workflow: {result.stderr}")
                return False

            return True
        except Exception as e:
            logger.error(f"Failed to rerun workflow: {e}")
            return False


# Global singleton instance
github_service = GitHubService()
