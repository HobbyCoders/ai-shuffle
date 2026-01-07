"""
GitHub integration service using gh CLI

This service wraps the GitHub CLI (gh) to provide pull request and
workflow management capabilities.
"""

import subprocess
import json
import logging
import re
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


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


class GitHubService:
    """
    Service for interacting with GitHub via the gh CLI.

    The gh CLI must be installed and authenticated for this service to work.
    All methods handle errors gracefully and return None or empty lists on failure.
    """

    def __init__(self):
        self.timeout = 60

    def _run_gh(self, args: List[str], timeout: int = None) -> subprocess.CompletedProcess:
        """
        Run a gh CLI command.

        Args:
            args: Command arguments (excluding 'gh')
            timeout: Optional timeout in seconds (defaults to self.timeout)

        Returns:
            CompletedProcess with stdout, stderr, and returncode

        Raises:
            subprocess.TimeoutExpired: If command times out
            subprocess.SubprocessError: If command fails to execute
        """
        cmd = ['gh'] + args
        timeout = timeout or self.timeout

        logger.debug(f"Running gh command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode != 0:
                logger.warning(f"gh command failed: {result.stderr}")

            return result
        except subprocess.TimeoutExpired:
            logger.error(f"gh command timed out after {timeout}s: {' '.join(cmd)}")
            raise
        except Exception as e:
            logger.error(f"gh command error: {e}")
            raise

    def is_authenticated(self) -> bool:
        """
        Check if gh CLI is authenticated.

        Returns:
            True if authenticated, False otherwise
        """
        try:
            result = self._run_gh(['auth', 'status'], timeout=10)
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"GitHub auth check failed: {e}")
            return False

    def get_auth_info(self) -> Dict[str, Any]:
        """
        Get detailed authentication information.

        Returns:
            Dict with 'authenticated', 'username', 'scopes' keys
        """
        info = {
            'authenticated': False,
            'username': None,
            'scopes': []
        }

        try:
            result = self._run_gh(['auth', 'status'], timeout=10)
            info['authenticated'] = result.returncode == 0

            # Parse output for username and scopes
            output = result.stdout + result.stderr

            # Look for "Logged in to github.com account <username>"
            username_match = re.search(r'account\s+(\S+)', output)
            if username_match:
                info['username'] = username_match.group(1)

            return info
        except Exception as e:
            logger.debug(f"Failed to get auth info: {e}")
            return info

    def get_repo_from_remote(self, working_dir: str) -> Optional[str]:
        """
        Extract owner/repo from git remote URL in a directory.

        Args:
            working_dir: Path to git repository

        Returns:
            Repository in "owner/repo" format, or None if not found
        """
        try:
            result = subprocess.run(
                ['git', 'remote', 'get-url', 'origin'],
                capture_output=True,
                text=True,
                cwd=working_dir,
                timeout=10
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
            Dict with repo info or None on failure
        """
        try:
            result = self._run_gh([
                'repo', 'view', repo,
                '--json', 'name,owner,description,url,defaultBranchRef'
            ])

            if result.returncode != 0:
                return None

            data = json.loads(result.stdout)
            return {
                'name': data.get('name'),
                'owner': data.get('owner', {}).get('login'),
                'description': data.get('description'),
                'url': data.get('url'),
                'default_branch': data.get('defaultBranchRef', {}).get('name', 'main')
            }
        except Exception as e:
            logger.debug(f"Failed to get repo info: {e}")
            return None

    # =========================================================================
    # Pull Requests
    # =========================================================================

    def list_pulls(self, repo: str, state: str = 'open', limit: int = 30) -> List[PullRequest]:
        """
        List pull requests for a repository.

        Args:
            repo: Repository in "owner/repo" format
            state: PR state filter - 'open', 'closed', 'merged', 'all'
            limit: Maximum number of PRs to return

        Returns:
            List of PullRequest objects
        """
        try:
            result = self._run_gh([
                'pr', 'list',
                '--repo', repo,
                '--state', state,
                '--limit', str(limit),
                '--json', 'number,title,body,state,headRefName,baseRefName,author,url,createdAt,updatedAt,mergeable'
            ])

            if result.returncode != 0:
                return []

            data = json.loads(result.stdout)

            pulls = []
            for pr_data in data:
                pulls.append(PullRequest(
                    number=pr_data.get('number', 0),
                    title=pr_data.get('title', ''),
                    body=pr_data.get('body', ''),
                    state=pr_data.get('state', 'unknown'),
                    head_branch=pr_data.get('headRefName', ''),
                    base_branch=pr_data.get('baseRefName', ''),
                    author=pr_data.get('author', {}).get('login', 'unknown'),
                    url=pr_data.get('url', ''),
                    created_at=pr_data.get('createdAt', ''),
                    updated_at=pr_data.get('updatedAt', ''),
                    mergeable=pr_data.get('mergeable')
                ))

            return pulls
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse PR list: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to list PRs: {e}")
            return []

    def get_pull(self, repo: str, pr_number: int) -> Optional[PullRequest]:
        """
        Get a specific pull request.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: Pull request number

        Returns:
            PullRequest object or None if not found
        """
        try:
            result = self._run_gh([
                'pr', 'view', str(pr_number),
                '--repo', repo,
                '--json', 'number,title,body,state,headRefName,baseRefName,author,url,createdAt,updatedAt,mergeable'
            ])

            if result.returncode != 0:
                return None

            pr_data = json.loads(result.stdout)

            return PullRequest(
                number=pr_data.get('number', 0),
                title=pr_data.get('title', ''),
                body=pr_data.get('body', ''),
                state=pr_data.get('state', 'unknown'),
                head_branch=pr_data.get('headRefName', ''),
                base_branch=pr_data.get('baseRefName', ''),
                author=pr_data.get('author', {}).get('login', 'unknown'),
                url=pr_data.get('url', ''),
                created_at=pr_data.get('createdAt', ''),
                updated_at=pr_data.get('updatedAt', ''),
                mergeable=pr_data.get('mergeable')
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse PR: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to get PR: {e}")
            return None

    def create_pull(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str
    ) -> Optional[PullRequest]:
        """
        Create a pull request.

        Args:
            repo: Repository in "owner/repo" format
            title: PR title
            body: PR body/description
            head: Head branch name
            base: Base branch name

        Returns:
            Created PullRequest object or None on failure
        """
        try:
            result = self._run_gh([
                'pr', 'create',
                '--repo', repo,
                '--title', title,
                '--body', body,
                '--head', head,
                '--base', base
            ])

            if result.returncode != 0:
                logger.error(f"Failed to create PR: {result.stderr}")
                return None

            # Parse the URL from output to get the PR number
            output = result.stdout.strip()
            pr_url_match = re.search(r'/pull/(\d+)', output)
            if pr_url_match:
                pr_number = int(pr_url_match.group(1))
                return self.get_pull(repo, pr_number)

            return None
        except Exception as e:
            logger.error(f"Failed to create PR: {e}")
            return None

    def merge_pull(
        self,
        repo: str,
        pr_number: int,
        method: str = 'merge'
    ) -> bool:
        """
        Merge a pull request.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: Pull request number
            method: Merge method - 'merge', 'squash', or 'rebase'

        Returns:
            True if merge succeeded, False otherwise
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
                logger.error(f"Failed to merge PR: {result.stderr}")
                return False

            return True
        except Exception as e:
            logger.error(f"Failed to merge PR: {e}")
            return False

    def close_pull(self, repo: str, pr_number: int) -> bool:
        """
        Close a pull request without merging.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: Pull request number

        Returns:
            True if close succeeded, False otherwise
        """
        try:
            result = self._run_gh([
                'pr', 'close', str(pr_number),
                '--repo', repo
            ])

            if result.returncode != 0:
                logger.error(f"Failed to close PR: {result.stderr}")
                return False

            return True
        except Exception as e:
            logger.error(f"Failed to close PR: {e}")
            return False

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
            List of WorkflowRun objects
        """
        try:
            result = self._run_gh([
                'run', 'list',
                '--repo', repo,
                '--limit', str(limit),
                '--json', 'databaseId,name,status,conclusion,headBranch,event,url,createdAt'
            ])

            if result.returncode != 0:
                return []

            data = json.loads(result.stdout)

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
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse workflow runs: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to list workflow runs: {e}")
            return []

    def get_run(self, repo: str, run_id: int) -> Optional[WorkflowRun]:
        """
        Get a specific workflow run.

        Args:
            repo: Repository in "owner/repo" format
            run_id: Workflow run ID

        Returns:
            WorkflowRun object or None if not found
        """
        try:
            result = self._run_gh([
                'run', 'view', str(run_id),
                '--repo', repo,
                '--json', 'databaseId,name,status,conclusion,headBranch,event,url,createdAt'
            ])

            if result.returncode != 0:
                return None

            run_data = json.loads(result.stdout)

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
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse workflow run: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to get workflow run: {e}")
            return None

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
