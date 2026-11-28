"""
GitLab integration: one account per worker, signed in with a personal access
token, on gitlab.com or a self-managed server.

The service mirrors the parts of :class:`app.core.github_service.GitHubService`
that source control uses (repository identity, merge requests as pull
requests, the clone picker's repository list, creating a repository), so the
Git panel's Create PR / Merge / Ship It flows run unchanged against a GitLab
remote. It talks to GitLab's REST API (v4) directly; no ``glab`` CLI.

Where the token lives
---------------------
``<data dir>/gitlab/account.json`` holds the server URL, the token and the
account name; ``<data dir>/gitlab/git-credential-<server hash>`` holds the
same token in git's credential format. Both are owner-only files on POSIX,
the same posture as gh's own ``hosts.yml``. ``<data dir>/gitlab/gitconfig``
holds a git credential helper scoped to that server
(``credential.<server>.helper``) that only answers ``get`` from that file,
and signing in adds one ``include.path`` line for it to the user's global git
config. So ``git clone/fetch/push`` over HTTPS works for the app and for
agents' shells, a failed push never erases the saved token (git's ``store``
helper would), and disconnecting deletes the app's own files without
rewriting any entry the owner wrote.

Merge requests are returned as :class:`PullRequest` objects with gh's
spelling of states (``OPEN``/``MERGED``/``CLOSED``) so every caller that
already understands pull requests needs no GitLab branch.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import httpx

from fluid.tools.child_env import scrub_child_env

from app.core.github_service import GitHubCliError, PullRequest
from app.core.ttl_cache import CACHE_MISS, TTLCache

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://gitlab.com"
API_TIMEOUT = 20.0
USER_REPOS_TTL_SECONDS = 120.0
USER_REPOS_PAGE_SIZE = 100
USER_REPOS_MAX_PAGES = 10
GITLAB_TOKENS_PATH = "/-/user_settings/personal_access_tokens?name=AI+Shuffle&scopes=api,write_repository"


class GitLabApiError(GitHubCliError):
    """GitLab could not answer: the result is unknown, not "empty".

    A subclass of :class:`GitHubCliError` on purpose: every source-control
    caller already treats that error as "could not ask the forge" (never cache
    it as "no pull request"), and GitLab failures need exactly that handling.
    """


def normalize_host(value: Optional[str]) -> str:
    """``https://gitlab.example.com[/prefix]`` from what the owner typed.

    Raises ValueError for anything that is not a plain http(s) server URL
    (no credentials, query or fragment). A bare host name means HTTPS.
    """
    raw = (value or "").strip() or DEFAULT_HOST
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("Enter the GitLab server address, such as https://gitlab.com.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("The GitLab server address must not contain a user name, query or fragment.")
    if any(c.isspace() for c in raw) or "'" in raw or '"' in raw:
        raise ValueError("The GitLab server address must not contain spaces or quotes.")
    host = parts.hostname.lower()
    netloc = f"{host}:{parts.port}" if parts.port else host
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{netloc}{path}"


def _state(value: Any) -> str:
    """GitLab MR state in gh's spelling."""
    low = str(value or "").lower()
    if low == "opened":
        return "OPEN"
    if low == "merged":
        return "MERGED"
    if low in ("closed", "locked"):
        return "CLOSED"
    return low.upper() or "UNKNOWN"


def _mergeable(mr: Dict[str, Any]) -> Optional[bool]:
    """True/False when GitLab has decided, None while it is still checking."""
    if mr.get("has_conflicts") is True:
        return False
    detailed = str(mr.get("detailed_merge_status") or "").lower()
    if detailed == "mergeable":
        return True
    if detailed in ("conflict", "need_rebase"):
        return False
    status = str(mr.get("merge_status") or "").lower()
    if status == "can_be_merged":
        return True
    if status == "cannot_be_merged":
        return False
    return None


class GitLabService:
    """GitLab REST client for one signed-in account on this worker."""

    def __init__(self, config_dir: Optional[Path] = None):
        self._config_dir_override = config_dir
        self._lock = threading.Lock()
        self._repos_lock = threading.Lock()
        self._repos_cache = TTLCache(ttl=USER_REPOS_TTL_SECONDS, max_entries=1, name="gitlab-user-repos")
        self._project_cache = TTLCache(ttl=60.0, max_entries=64, name="gitlab-projects")
        self._pinned_host: Optional[str] = None

    def pinned(self) -> "GitLabService":
        """A view bound to the server connected now (same caches and locks).

        A multi-step operation (Ship It: push, describe, create, merge) holds
        this view, so reconnecting to another server mid-way makes its next
        request fail instead of acting on a same-named project there.
        """
        view = copy.copy(self)
        view._pinned_host = self.host() or ""
        return view

    # ------------------------------------------------------------------ config

    @property
    def config_dir(self) -> Path:
        if self._config_dir_override is not None:
            return self._config_dir_override
        from app.core.config import settings
        data_dir = settings.data_dir
        if data_dir is None:  # set in model_post_init; never None at runtime
            raise RuntimeError("App data folder is not configured")
        return Path(data_dir) / "gitlab"

    @property
    def account_file(self) -> Path:
        return self.config_dir / "account.json"

    @property
    def git_config_file(self) -> Path:
        """App-owned git config holding the credential helper; the owner's
        global config only gains an ``include.path`` line pointing here."""
        return self.config_dir / "gitconfig"

    def credential_file_for(self, host: str) -> Path:
        """The token in git's credential format, one file per server, so a
        helper written for one server can never answer with another's token."""
        digest = hashlib.sha256(host.encode("utf-8")).hexdigest()[:16]
        return self.config_dir / f"git-credential-{digest}"

    @property
    def credential_file(self) -> Optional[Path]:
        account = self.load_account()
        return self.credential_file_for(account["host"]) if account else None

    def load_account(self) -> Optional[Dict[str, str]]:
        """The saved ``{host, token, user}``, or None when not signed in."""
        try:
            data = json.loads(self.account_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("GitLab account file is unreadable; treating GitLab as not connected")
            return None
        if not isinstance(data, dict):
            return None
        token, host = data.get("token"), data.get("host")
        if not isinstance(token, str) or not token or not isinstance(host, str) or not host:
            return None
        return {"host": host, "token": token, "user": str(data.get("user") or "")}

    def is_authenticated(self, *, refresh: bool = False) -> bool:  # noqa: ARG002 (GitHubService signature)
        """A token is saved. Local and cheap; the status endpoint verifies it live."""
        return self.load_account() is not None

    def host(self) -> Optional[str]:
        account = self.load_account()
        return account["host"] if account else None

    def _write_private(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(name, 0o600)
            except OSError:
                pass
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    # ------------------------------------------------------------- git helper

    def _helper_command(self, host: str) -> str:
        path = self.credential_file_for(host).as_posix()
        if "'" in path:
            raise RuntimeError("The app data folder path contains a quote; git credentials cannot be configured.")
        # Answers only "get": a rejected push makes git call "erase", which
        # must not delete the saved token.
        return f"!f() {{ test \"$1\" = get && cat '{path}'; }}; f"

    def _git_config_text(self, host: str) -> str:
        """The app-owned config: a helper scoped to *host*, nothing else."""
        def quoted(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        parts = urlsplit(host)
        return (
            "# Written by AI Shuffle for its GitLab connection; removed on disconnect.\n"
            f"[credential {quoted(f'{parts.scheme}://{parts.netloc}')}]\n"
            # The empty helper first clears helpers configured before this
            # file is included (a system credential manager would otherwise
            # answer first); the owner's own entries are never rewritten.
            "\thelper =\n"
            f"\thelper = {quoted(self._helper_command(host))}\n"
        )

    def _git_config(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "config", "--global", *args],
            capture_output=True, text=True, timeout=10, env=scrub_child_env(),
        )

    def _include_registered(self) -> bool:
        current = self._git_config("--get-all", "include.path")
        return self.git_config_file.as_posix() in (current.stdout or "").splitlines()

    def _register_include(self) -> None:
        if self._include_registered():
            return
        result = self._git_config("--add", "include.path", self.git_config_file.as_posix())
        if result.returncode:
            raise RuntimeError("git config failed")

    def _unregister_include(self) -> None:
        """Remove only our ``include.path`` line. Best effort: once the
        included file is gone git ignores a dangling include."""
        if self._include_registered():
            self._git_config("--fixed-value", "--unset-all", "include.path", self.git_config_file.as_posix())

    def _credential_files(self) -> List[Path]:
        try:
            return sorted(self.config_dir.glob("git-credential-*"))
        except OSError:
            return []

    # -------------------------------------------------------------- sign in/out

    def _api_base(self, host: str) -> str:
        return f"{host}/api/v4"

    def _fetch_user(self, host: str, token: str) -> Dict[str, Any]:
        try:
            resp = httpx.get(
                f"{self._api_base(host)}/user",
                headers={"PRIVATE-TOKEN": token}, timeout=API_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise GitLabApiError(f"Could not reach {host}: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise PermissionError("GitLab rejected this token.")
        if resp.status_code != 200:
            raise GitLabApiError(f"GitLab answered HTTP {resp.status_code} for /user")
        try:
            data = resp.json()
        except ValueError as exc:
            raise GitLabApiError(f"{host} did not answer like a GitLab server") from exc
        if not isinstance(data, dict) or not data.get("username"):
            raise GitLabApiError(f"{host} did not answer like a GitLab server")
        return data

    def login(self, token: str, host: Optional[str] = None) -> Dict[str, Any]:
        """Validate *token* against *host*, save it and set up git credentials."""
        token = (token or "").strip()
        if not token:
            return {"success": False, "error": "A personal access token is required."}
        if any(c.isspace() for c in token):
            return {"success": False, "error": "The token must not contain whitespace."}
        try:
            host = normalize_host(host)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        try:
            user = self._fetch_user(host, token)
        except PermissionError:
            return {"success": False, "error": "GitLab rejected this token. Check that it is correct, unexpired and has the api scope."}
        except GitLabApiError as exc:
            return {"success": False, "error": f"Could not validate the token: {exc}"}
        username = str(user["username"])
        credential_file = self.credential_file_for(host)
        with self._lock:
            # What a failure must put back. The account file is written last,
            # so the app never reports Connected for an unfinished sign-in.
            files = (credential_file, self.git_config_file, self.account_file)
            backups = {path: self._read_or_none(path) for path in files}
            was_registered = self._include_registered()
            try:
                self._write_private(credential_file, f"username={username}\npassword={token}\n")
                self._write_private(self.git_config_file, self._git_config_text(host))
                self._register_include()
                self._write_private(
                    self.account_file,
                    json.dumps({"host": host, "token": token, "user": username}),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("GitLab sign-in could not be saved (%s)", type(exc).__name__)
                self._restore(backups)
                if not was_registered:
                    self._unregister_include()
                return {"success": False, "error": "GitLab accepted the token, but it or the git credentials could not be saved. Retry connecting."}
            # The included config now names only this server; drop the
            # previous server's token.
            for stale in self._credential_files():
                if stale != credential_file:
                    stale.unlink(missing_ok=True)
        self.invalidate_repos_cache()
        self._project_cache.invalidate()
        return {"success": True, "user": username, "host": host}

    @staticmethod
    def _read_or_none(path: Path) -> Optional[bytes]:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def _restore(self, backups: Dict[Path, Optional[bytes]]) -> None:
        for path, data in backups.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    self._write_private(path, data.decode("utf-8"))
            except Exception:  # noqa: BLE001
                logger.warning("Could not restore %s after a failed GitLab sign-in", path.name)

    def logout(self) -> Dict[str, Any]:
        with self._lock:
            try:
                # The account goes first: from here on the app is signed out
                # even if a later step fails, and git's helper goes with its
                # config file (a dangling include.path is ignored by git).
                self.account_file.unlink(missing_ok=True)
                self.git_config_file.unlink(missing_ok=True)
                for path in self._credential_files():
                    path.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("GitLab sign-out failed (%s)", type(exc).__name__)
                return {"success": False, "error": "Could not remove the saved GitLab token."}
            self._unregister_include()
        self.invalidate_repos_cache()
        self._project_cache.invalidate()
        return {"success": True}

    def auth_info(self) -> Dict[str, Any]:
        """Status for Settings: saved account, verified live against the server."""
        account = self.load_account()
        if not account:
            return {"authenticated": False, "user": None, "host": None, "error": None}
        info: Dict[str, Any] = {"authenticated": True, "user": account["user"] or None,
                                "host": account["host"], "error": None}
        try:
            user = self._fetch_user(account["host"], account["token"])
            info["user"] = str(user["username"])
        except PermissionError:
            info["error"] = "GitLab no longer accepts the saved token. Connect again with a new one."
        except GitLabApiError as exc:
            info["error"] = f"Could not reach GitLab: {exc}"
        return info

    # --------------------------------------------------------------- REST core

    def _request(
        self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None,
    ) -> httpx.Response:
        account = self.load_account()
        if not account:
            raise GitLabApiError("GitLab is not connected on this worker")
        if self._pinned_host is not None and account["host"] != self._pinned_host:
            raise GitLabApiError("The GitLab connection changed during this operation; nothing was sent to the new server")
        try:
            return httpx.request(
                method, f"{self._api_base(account['host'])}{path}",
                params=params, json=json_body,
                headers={"PRIVATE-TOKEN": account["token"]},
                timeout=timeout or API_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise GitLabApiError(f"GitLab request failed: {type(exc).__name__}") from exc

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return f"HTTP {resp.status_code}"
        if isinstance(data, dict):
            msg = data.get("message") or data.get("error")
            if isinstance(msg, list):
                msg = "; ".join(str(m) for m in msg)
            elif isinstance(msg, dict):
                msg = "; ".join(f"{k} {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in msg.items())
            if msg:
                return f"HTTP {resp.status_code}: {msg}"
        return f"HTTP {resp.status_code}"

    def _json(self, resp: httpx.Response, what: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise GitLabApiError(f"GitLab returned invalid JSON for {what}") from exc

    @staticmethod
    def _pid(repo: str) -> str:
        return quote(repo, safe="")

    # ------------------------------------------------------ repository identity

    def get_repo_from_remote(self, working_dir: str) -> Optional[str]:
        """``group/subgroup/project`` when origin points at the connected server."""
        host = self.host()
        if not host:
            return None
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"], capture_output=True, text=True,
                cwd=working_dir, timeout=10, env=scrub_child_env(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("GitLab remote lookup failed: %s", exc)
            return None
        if result.returncode != 0:
            return None
        return repo_path_for_remote(result.stdout.strip(), host)

    def _project(self, repo: str) -> Optional[Dict[str, Any]]:
        cached = self._project_cache.get_or_miss(repo)
        if cached is not CACHE_MISS:
            return cached
        resp = self._request("GET", f"/projects/{self._pid(repo)}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise GitLabApiError(f"GitLab could not read {repo}", stderr=self._error_text(resp))
        data = self._json(resp, "project")
        self._project_cache.set(repo, data)
        return data

    def get_repo_info(self, repo: str) -> Optional[Dict[str, Any]]:
        data = self._project(repo)
        if data is None:
            return None
        return {
            "name": data.get("name"),
            "owner": (data.get("namespace") or {}).get("full_path"),
            "description": data.get("description"),
            "url": data.get("web_url"),
            "default_branch": data.get("default_branch") or "main",
        }

    # ------------------------------------------------------------ merge requests

    def _to_pull(self, mr: Dict[str, Any], repo: str, *, contains_commit: Optional[str] = None) -> PullRequest:
        same = mr.get("source_project_id") is not None and mr.get("source_project_id") == mr.get("target_project_id")
        return PullRequest(
            number=int(mr.get("iid") or 0),
            title=str(mr.get("title") or ""),
            body=str(mr.get("description") or ""),
            state=_state(mr.get("state")),
            head_branch=str(mr.get("source_branch") or ""),
            base_branch=str(mr.get("target_branch") or ""),
            author=str((mr.get("author") or {}).get("username") or "unknown"),
            url=str(mr.get("web_url") or ""),
            created_at=str(mr.get("created_at") or ""),
            updated_at=str(mr.get("updated_at") or ""),
            mergeable=_mergeable(mr),
            head_sha=mr.get("sha"),
            contains_commit=contains_commit,
            # A fork's MR is not this repository's own branch.
            head_repo=repo if same else f"fork:{mr.get('source_project_id')}",
        )

    def list_pulls(
        self, repo: str, state: str = "open", limit: int = 30,
        head: Optional[str] = None, timeout: Optional[int] = None,
    ) -> List[PullRequest]:
        gl_state = {"open": "opened", "closed": "closed", "merged": "merged", "all": "all"}.get(state, "opened")
        limit = max(1, min(int(limit), 100))
        # A head lookup asks for this checkout's branch. GitLab's source_branch
        # filter also matches forks' MRs from a same-named branch, so fetch a
        # full page, keep this project's own MRs and apply the limit after.
        params: Dict[str, Any] = {"per_page": 100 if head else limit, "order_by": "updated_at"}
        if gl_state != "all":
            params["state"] = gl_state
        if head:
            params["source_branch"] = head
        resp = self._request("GET", f"/projects/{self._pid(repo)}/merge_requests", params=params, timeout=timeout)
        if resp.status_code != 200:
            raise GitLabApiError(f"GitLab could not list merge requests for {repo}", stderr=self._error_text(resp))
        data = self._json(resp, "merge requests")
        pulls = [self._to_pull(mr, repo) for mr in data if isinstance(mr, dict)] if isinstance(data, list) else []
        if head:
            pulls = [pr for pr in pulls if pr.head_repo == repo]
        return pulls[:limit]

    def list_commit_pulls(self, repo: str, sha: str, timeout: Optional[int] = None) -> List[PullRequest]:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha or ""):
            raise GitLabApiError("list_commit_pulls needs a full commit SHA")
        resp = self._request(
            "GET", f"/projects/{self._pid(repo)}/repository/commits/{sha}/merge_requests", timeout=timeout,
        )
        if resp.status_code == 404:
            return []  # the commit was never pushed
        if resp.status_code != 200:
            raise GitLabApiError(f"GitLab commit merge-request lookup failed for {repo}", stderr=self._error_text(resp))
        data = self._json(resp, "commit merge requests")
        return [self._to_pull(mr, repo, contains_commit=sha) for mr in data if isinstance(mr, dict)] \
            if isinstance(data, list) else []

    def list_inbox_pulls(self, repo: str, state: str = "open", limit: int = 50) -> List[Dict[str, Any]]:
        return [
            {"number": pr.number, "title": pr.title, "url": pr.url, "author": pr.author,
             "head_branch": pr.head_branch, "base_branch": pr.base_branch,
             "state": pr.state.lower(), "review": "none", "checks": "none", "updated_at": pr.updated_at}
            for pr in self.list_pulls(repo, state=state, limit=limit)
        ]

    def get_pull(self, repo: str, pr_number: int) -> Optional[PullRequest]:
        resp = self._request("GET", f"/projects/{self._pid(repo)}/merge_requests/{int(pr_number)}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise GitLabApiError(f"GitLab could not read {repo}!{pr_number}", stderr=self._error_text(resp))
        data = self._json(resp, "merge request")
        return self._to_pull(data, repo) if isinstance(data, dict) else None

    def _get_pull_or_none(self, repo: str, pr_number: int) -> Optional[PullRequest]:
        try:
            return self.get_pull(repo, pr_number)
        except GitLabApiError as exc:
            logger.warning("Could not read back MR %s!%s: %s", repo, pr_number, exc)
            return None

    def create_pull(
        self, repo: str, title: str, body: str, head: str, base: str, draft: bool = False,
    ) -> Tuple[Optional[PullRequest], Optional[str]]:
        from app.core.git_text import sanitize_pr_body, sanitize_pr_title

        clean_title = sanitize_pr_title(title)
        if draft and not re.match(r"(?i)^(draft:|\[draft\]|\(draft\))", clean_title):
            clean_title = f"Draft: {clean_title}"
        try:
            resp = self._request("POST", f"/projects/{self._pid(repo)}/merge_requests", json_body={
                "source_branch": head, "target_branch": base,
                "title": clean_title, "description": sanitize_pr_body(body),
            }, timeout=60)
        except GitLabApiError as exc:
            return None, str(exc)
        if resp.status_code in (200, 201):
            data = self._json(resp, "created merge request")
            return self._to_pull(data, repo), None
        error = self._error_text(resp)
        existing = re.search(r"already exists[^!]*!(\d+)", error)
        if resp.status_code == 409 and existing:
            pr = self._get_pull_or_none(repo, int(existing.group(1)))
            return (pr, None) if pr else (None, f"MR !{existing.group(1)} exists but could not be read")
        logger.error("Failed to create GitLab merge request: %s", error)
        return None, f"GitLab could not create the merge request: {error}"

    def merge_pull(self, repo: str, pr_number: int, method: str = "merge") -> Tuple[bool, Optional[str]]:
        """Merge an MR. GitLab applies the project's own merge method; *squash*
        squashes, *merge* and *rebase* use that method."""
        try:
            resp = self._request(
                "PUT", f"/projects/{self._pid(repo)}/merge_requests/{int(pr_number)}/merge",
                json_body={"squash": method == "squash", "should_remove_source_branch": True},
                timeout=60,
            )
        except GitLabApiError as exc:
            return False, str(exc)
        if resp.status_code == 200:
            return True, None
        error = self._error_text(resp)
        pr = self._get_pull_or_none(repo, pr_number)
        if pr is not None and pr.state == "MERGED":
            return False, "Merge request was already merged"
        return False, f"GitLab could not merge !{pr_number}: {error}"

    def close_pull(self, repo: str, pr_number: int) -> Tuple[bool, Optional[str]]:
        try:
            resp = self._request(
                "PUT", f"/projects/{self._pid(repo)}/merge_requests/{int(pr_number)}",
                json_body={"state_event": "close"},
            )
        except GitLabApiError as exc:
            return False, str(exc)
        if resp.status_code == 200:
            return True, None
        return False, f"GitLab could not close !{pr_number}: {self._error_text(resp)}"

    # ------------------------------------------------------------ repositories

    def invalidate_repos_cache(self) -> None:
        self._repos_cache.invalidate()

    def list_user_repos(self, *, refresh: bool = False) -> Tuple[List[Dict[str, Any]], bool]:
        """Projects the account is a member of, most recently active first."""
        with self._repos_lock:
            if not refresh:
                cached = self._repos_cache.get_or_miss("repos")
                if cached is not CACHE_MISS:
                    return cached
            value = self._fetch_user_repos()
            self._repos_cache.set("repos", value)
            return value

    def _fetch_user_repos(self) -> Tuple[List[Dict[str, Any]], bool]:
        repos: List[Dict[str, Any]] = []
        seen: set = set()
        truncated = False
        for page in range(1, USER_REPOS_MAX_PAGES + 1):
            resp = self._request("GET", "/projects", params={
                "membership": "true", "order_by": "last_activity_at", "sort": "desc",
                "per_page": USER_REPOS_PAGE_SIZE, "page": page,
            })
            if resp.status_code != 200:
                raise GitLabApiError("GitLab could not list projects", stderr=self._error_text(resp))
            data = self._json(resp, "projects")
            if not isinstance(data, list):
                raise GitLabApiError("GitLab returned an unexpected project list")
            for item in data:
                if not isinstance(item, dict):
                    continue
                full_name = item.get("path_with_namespace")
                if not isinstance(full_name, str) or full_name in seen:
                    continue
                seen.add(full_name)
                repos.append({
                    "full_name": full_name,
                    "name": item.get("path") or full_name.rsplit("/", 1)[-1],
                    "owner": (item.get("namespace") or {}).get("full_path") or full_name.rsplit("/", 1)[0],
                    "private": item.get("visibility") != "public",
                    "fork": bool(item.get("forked_from_project")),
                    "archived": bool(item.get("archived")),
                    "description": item.get("description") or None,
                    # HTTPS so the clone uses the credential helper; never a token in the URL.
                    "clone_url": item.get("http_url_to_repo") or "",
                    "html_url": item.get("web_url") or "",
                    "default_branch": item.get("default_branch") or None,
                    "updated_at": item.get("last_activity_at") or None,
                })
            if len(data) < USER_REPOS_PAGE_SIZE:
                break
        else:
            truncated = True
        repos = [r for r in repos if r["clone_url"]]
        return repos, truncated

    def create_repo(self, name: str, private: bool = True, description: str = "") -> Optional[Dict[str, Any]]:
        if not name or name.startswith("-"):
            return None
        try:
            resp = self._request("POST", "/projects", json_body={
                "name": name, "visibility": "private" if private else "public",
                "description": description or None,
            }, timeout=30)
        except GitLabApiError as exc:
            logger.error("Failed to create GitLab project: %s", exc)
            return None
        if resp.status_code not in (200, 201):
            logger.error("Failed to create GitLab project: %s", self._error_text(resp))
            return None
        data = self._json(resp, "created project")
        return {
            "name": data.get("name"),
            "full_name": data.get("path_with_namespace"),
            "url": data.get("web_url"),
            "clone_url": data.get("http_url_to_repo") or data.get("web_url"),
        }


def _effective_port(scheme: str, port: Optional[int]) -> int:
    return port if port else (80 if scheme == "http" else 443)


def repo_path_for_remote(remote_url: str, host: str) -> Optional[str]:
    """The project path of *remote_url* when it is on GitLab server *host*.

    Accepts ``https://host[/prefix]/group/sub/repo(.git)``,
    ``git@host:group/repo.git`` and ``ssh://git@host[:port]/group/repo.git``.
    """
    remote = (remote_url or "").strip()
    if not remote:
        return None
    base = urlsplit(host)
    base_host = (base.hostname or "").lower()
    prefix = base.path.strip("/")
    path: Optional[str] = None
    scp = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)", remote)
    if "://" not in remote and scp:
        if scp.group(1).lower() != base_host:
            return None
        path = scp.group(2)
    else:
        parts = urlsplit(remote)
        if (parts.hostname or "").lower() != base_host:
            return None
        path = parts.path.strip("/")
        if parts.scheme in ("http", "https"):
            # Two servers can share a host name on different ports; only the
            # connected one's port is this account's server. (SSH runs on its
            # own port, so SSH remotes match by host name.)
            try:
                remote_port = _effective_port(parts.scheme, parts.port)
            except ValueError:
                return None
            if remote_port != _effective_port(base.scheme, base.port):
                return None
        if parts.scheme in ("http", "https") and prefix:
            if not (path == prefix or path.startswith(prefix + "/")):
                return None
            path = path[len(prefix):]
    path = (path or "").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2 or any(s in (".", "..") for s in segments) or "-" in segments:
        return None
    return "/".join(segments)


gitlab_service = GitLabService()
