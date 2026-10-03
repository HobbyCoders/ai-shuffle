"""What the owner's assistant does on its own, and what it asks the owner first.

Scope (:func:`scope_for_session`): the assistant's home conversation, the chats
it starts, and the Task workers under either. Every other session is untouched.

Those sessions run in Bypass (``query_options``), so ordinary work goes ahead
without a prompt. Every tool has a risk level, declared once in
:data:`TOOL_LEVELS`:

* ``low`` goes ahead.
* ``high`` asks the owner in the assistant's conversation and waits for a firm
  yes (``app.core.assistant_confirm``), whatever the session's mode.

A tool with no declared level is high, so a new built-in, MCP or plugin tool
asks until someone declares it here. Tools whose risk depends on their
arguments (shell commands, Python cells, file reads and writes, starting work
on another worker, the browser) declare a rule that can raise them to high: the
fixed list is destructive git, deletes and overwrites outside version control,
merges to a default branch, deploys and releases, worker restarts, secrets,
private access, outward writes, spending, app security settings, and anything
outside the registered projects. A rule that cannot tell answers high.

The runtime seams decide from these levels (``harness_adapter``: the native
permission engine, the delegated PreToolUse policy and the bundled-tool
dispatch), and the approver puts a high call to the owner past any mode or
saved rule (``query_engine``). The model can ask when the app would not, but it
cannot lower a level or skip an ask, and the levels are not a setting.
"""
from __future__ import annotations

import logging
import ntpath
import os
import posixpath
import re
import tempfile
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Union  # noqa: F401

logger = logging.getLogger(__name__)

LOW, HIGH = "low", "high"

# What a scope dict carries (built by scope_for_session, stamped on the session
# options, read back by the runtime seams): plain data, so it survives the
# options rebuild and never needs a database read at decision time.
#   session_id       the session the call runs in
#   home_session_id  the assistant home it belongs to
#   roots            absolute paths of registered projects, their checkouts,
#                    the app workspace and the temp directory
#   cwd              the session's working directory, when known
Scope = Mapping[str, Any]

_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    """Absolute, normalized and (on Windows) case-folded spelling of ``path``."""
    path = os.path.expanduser(path.strip().strip("\"'"))
    if _WINDOWS:
        # Git Bash and WSL spellings of a drive path: /d/x and /mnt/d/x.
        match = re.match(r"^/(?:mnt/)?([a-zA-Z])(/.*)?$", path.replace("\\", "/"))
        if match:
            path = match.group(1) + ":" + (match.group(2) or "/")
        return ntpath.normcase(ntpath.normpath(ntpath.abspath(path)))
    return posixpath.normpath(posixpath.abspath(path))


def _is_absolute(path: str) -> bool:
    path = path.strip().strip("\"'")
    return (path.startswith(("/", "~", "\\\\"))
            or bool(re.match(r"^[a-zA-Z]:[\\/]", path)))


def _inside(path: Optional[str], scope: Scope) -> bool:
    """Whether ``path`` is inside a registered project (or the workspace/temp)."""
    if not isinstance(path, str) or not path.strip():
        return False
    cwd = scope.get("cwd")
    if not _is_absolute(path):
        if not cwd:
            return False
        path = os.path.join(cwd, path.strip().strip("\"'"))
    target = _norm(path)
    sep = "\\" if _WINDOWS else "/"
    for root in scope.get("roots") or ():
        base = _norm(root)
        if target == base or target.startswith(base.rstrip(sep) + sep):
            return True
    return False


# File names and folders that hold credentials. Example and template env files
# are not secrets.
_SECRET_NAME = re.compile(
    r"(^\.env($|\.(?!example$|sample$|template$|dist$)[\w.-]+$)|\.pem$|\.key$|\.p12$|\.pfx$|"
    r"\.keystore$|\.jks$|^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$|^\.netrc$|^\.pypirc$|^\.npmrc$|"
    r"^credentials(\.\w+)?$|^secrets?\.\w+$|^managed-postgres\.json$|^\.git-credentials$)",
    re.IGNORECASE,
)
_SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".azure", ".docker", ".config/gh", ".password-store"}
# Files that configure what runs: hooks, MCP servers, agent settings, git internals.
_CONTROL_FILE = re.compile(
    r"([\\/]|^)(\.git[\\/]|\.claude[\\/](settings[^\\/]*\.json|hooks[\\/])|\.mcp\.json$|"
    r"\.fluid[\\/](config\.toml|settings[^\\/]*\.json|hooks[\\/])|\.githooks[\\/]|\.husky[\\/])",
    re.IGNORECASE,
)


def _secret_path(path: str) -> bool:
    parts = re.split(r"[\\/]+", path.strip().strip("\"'"))
    if parts and _SECRET_NAME.search(parts[-1] or ""):
        return True
    joined = "/".join(p.lower() for p in parts)
    return any(f"/{d}/" in f"/{joined}/" or joined.endswith(f"/{d}") for d in _SECRET_DIRS)


def _read_level(path: Optional[str], scope: Scope) -> str:
    """Reading a file or folder: inside the projects and not a secret."""
    if path is None or (isinstance(path, str) and not path.strip()):
        path = scope.get("cwd")
    if not isinstance(path, str):
        return HIGH
    if _secret_path(path):
        return HIGH
    return LOW if _inside(path, scope) else HIGH


def _write_level(path: Optional[str], scope: Scope) -> str:
    """Writing a file: inside the projects, not a secret, not git or hook config."""
    if not isinstance(path, str) or not path.strip():
        return HIGH
    if _secret_path(path) or _CONTROL_FILE.search(path.replace("\\", "/")):
        return HIGH
    return LOW if _inside(path, scope) else HIGH


def _glob_root(pattern: Any) -> Optional[str]:
    """The fixed directory part of a glob pattern, or None for a relative one."""
    if not isinstance(pattern, str) or not _is_absolute(pattern):
        return None
    head = re.split(r"[*?\[{]", pattern, 1)[0]
    return head.rsplit("/", 1)[0] if "/" in head else head.rsplit("\\", 1)[0]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_SEP = r"[^|;&\n]*"  # The rest of one simple command.
_DEFAULT_BRANCH = r"\b(main|master|trunk|develop|production|release)\b"
# Each pattern names one class on the fixed list. Matched case-insensitively
# against the whole command text, so a quoted inner command (bash -c '...')
# is read too.
_HIGH_COMMANDS = [
    # Destructive git and history rewrites.
    rf"\bgit\b{_SEP}\bpush\b{_SEP}(\s--force\b|\s--force-with-lease\b|\s-f\b|\s--delete\b|\s-d\b|\s--mirror\b|\s--prune\b|\s\+\S|\s:\S)",
    rf"\bgit\b{_SEP}\bpush\b{_SEP}(:|\s){_DEFAULT_BRANCH}",
    rf"\bgit\b{_SEP}\b(reset\s+{_SEP}--hard|clean\s+-\w*f|filter-branch|filter-repo|rebase\b|replace\b"
    rf"|commit\b{_SEP}--amend|update-ref\s+-d|reflog\s+(expire|delete)|gc\s+{_SEP}--prune"
    rf"|branch\s+{_SEP}(-\w*[dD]\b|--delete)|tag\s+{_SEP}(-d\b|--delete)|stash\s+(drop|clear)"
    rf"|checkout\s+{_SEP}(\s--\s|\s\.(\s|$)|--force|-f\b)|restore\b|worktree\s+(remove|prune)"
    rf"|remote\s+(remove|rm|set-url)|config\s+{_SEP}--global|submodule\s+deinit)",
    # GitHub: merges, releases, workflow runs, deletions, writes on others' repos, credentials.
    r"\bgh\s+(pr\s+(merge|close|comment|review|edit|lock|ready\s+--undo)|release\b|workflow\s+(run|enable|disable)"
    r"|run\s+(rerun|cancel|delete)|repo\s+(delete|archive|rename|edit|transfer|fork|sync)|issue\s+(create|comment|close|edit|delete|lock|transfer)"
    r"|gist\s+(create|edit|delete)|secret\b|variable\b|auth\b|ssh-key\b|gpg-key\b|extension\s+install|label\s+(create|delete|edit)"
    r"|cache\s+delete|api\b" + _SEP + r"(\s-X\s*(POST|PUT|PATCH|DELETE)|--method\s+(POST|PUT|PATCH|DELETE)|\s-[fF]\s|--field|--raw-field|--input))",
    # Deploys, releases, publishing, cloud and remote machines.
    r"\bwrangler\s+(deploy|publish|secret|d1\s+(execute|migrations\s+apply)|r2|kv|pages\s+deploy|versions\s+deploy|rollback|delete)",
    r"\b(npm|pnpm|yarn|bun)\s+(publish|unpublish|deprecate|login|token|owner)\b",
    r"\b(twine\s+upload|cargo\s+(publish|yank)|gem\s+push|poetry\s+publish|dotnet\s+nuget\s+push|mvn\s+deploy)\b",
    r"\bdocker\s+(push|login|logout|compose\s+" + _SEP + r"\b(up|down|restart|stop|rm|kill|pull)\b|restart|stop|kill|rm|rmi|system\s+prune|volume\s+(rm|prune)|swarm|stack|service|update)\b",
    r"\b(kubectl|helm|terraform|pulumi|ansible-playbook|flyctl|fly|vercel|netlify|firebase|heroku|railway|serverless|sls|eb|cdk|sam)\b"
    + _SEP + r"\b(apply|delete|destroy|deploy|upgrade|install|uninstall|rollout|scale|patch|edit|up|release|--prod)\b",
    r"\b(aws|az|gcloud|gsutil|doctl|oci|scp|sftp|rsync\s+" + _SEP + r":|ssh|ssh-copy-id|mosh)\b",
    r"\bfastlane\b|\bbundletool\b|\bplay\s+publish\b",
    # Worker restarts, updates and other people's processes.
    r"\b(shutdown|reboot|halt|poweroff|systemctl|launchctl|service\s+\S+\s+(start|stop|restart)|sc(\.exe)?\s+(stop|start|config|delete)"
    r"|taskkill|tskill|kill|pkill|killall|Stop-Process|Restart-Computer|Stop-Computer|Restart-Service|Stop-Service|Set-Service"
    r"|winget\s+(upgrade|install|uninstall)|choco\s+|apt(-get)?\s+(install|remove|purge|upgrade)|brew\s+(install|uninstall|upgrade)"
    r"|pip3?\s+install\s+" + _SEP + r"(--user|--break-system-packages)|npm\s+(i|install)\s+" + _SEP + r"\s-g\b)",
    # Deleting files (and their PowerShell and find spellings).
    r"(^|[\s;&|(`$])(rm|rmdir|rd|del|erase|unlink|shred|truncate|srm)(\s|$)",
    r"\b(Remove-Item|Clear-Content|Clear-Item|Remove-ItemProperty)\b|(^|[\s;&|])ri\s",
    r"\bfind\b" + _SEP + r"\s-(delete|exec\s+rm)\b|\bxargs\s+" + _SEP + r"\brm\b",
    # Secrets.
    r"(^|[\s;&|(`])(printenv|env|set|export\s+-p)\s*($|[|;&>)])|Get-ChildItem\s+env:|\bdir\s+env:|\$env:\w*(KEY|TOKEN|SECRET|PASS|CRED)",
    r"\b(security\s+find-(generic|internet)-password|cmdkey|keyring|pass\s+show|op\s+(read|item|signin)|vault\s+(read|kv|login)"
    r"|bw\s+(get|unlock)|secret-tool|gpg\s+--(decrypt|export-secret))\b",
    r"\b(cat|type|less|more|head|tail|Get-Content|gc|Select-String|grep|rg|strings|xxd|base64|cp|copy|mv|move|scp)\b" + _SEP
    + r"(\.env\b(?!\.(example|sample|template|dist)\b)|\bid_(rsa|dsa|ecdsa|ed25519)\b|\.ssh[\\/]|\.aws[\\/]|\.netrc\b|\.pypirc\b|\.npmrc\b"
    r"|\.git-credentials\b|managed-postgres\.json|\bcredentials\b|\.pem\b|\.key\b)",
    # Outward writes: HTTP methods that change something, mail, raw sockets.
    r"\bcurl\b" + _SEP + r"(\s-X\s*(POST|PUT|PATCH|DELETE)\b|--request\s+(POST|PUT|PATCH|DELETE)\b|\s(-d|--data[\w-]*|-F|--form[\w-]*|-T|--upload-file|--json)\b)",
    r"\bwget\b" + _SEP + r"--(post|method|body)",
    r"\b(Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b" + _SEP + r"(-Method\s+(Post|Put|Patch|Delete)|-Body\b|-InFile\b)",
    r"\b(sendmail|mailx?|mutt|swaks|msmtp|nc|ncat|netcat|socat|telnet)\b(\s|$)",
    # Databases (changes outside version control).
    r"\b(psql|mysql|mariadb|sqlite3|mongo|mongosh|redis-cli|sqlcmd|cqlsh)\b",
    # App and agent control: plugins that execute code, MCP servers, hooks.
    r"\b(claude|fluid)\s+(mcp|plugin|plugins|config|settings|hooks?)\b",
    # Hidden commands the text scan cannot read.
    r"\beval\b|\bInvoke-Expression\b|(^|[\s;&|(])iex(\s|$)|-EncodedCommand\b|-enc\s|FromBase64String"
    r"|\bbase64\s+(-d|--decode|-D)\b|\|\s*(ba|z|da|k)?sh\b|\|\s*(pwsh|powershell|python3?|node|perl|ruby)\b",
    # Python inline file deletes and moves (python -c, here-docs).
    r"\b(os\.(remove|unlink|rmdir|removedirs)|shutil\.(rmtree|move)|send2trash)\b|\.unlink\(|\.rmdir\(",
]
_HIGH_COMMAND_RE = re.compile("|".join(f"(?:{p})" for p in _HIGH_COMMANDS), re.IGNORECASE | re.MULTILINE)

# Absolute-path tokens: POSIX (/x, ~/x), Windows (C:\x, C:/x) and UNC (\\host).
# A leading ':' or a word character means it is part of a URL or an option.
_PATH_TOKEN = re.compile(r"""(?<![\w:/\\.=-])(~(?=[\\/]|\s|$)[^\s'"|;&<>)`]*|/[^\s'"|;&<>)`]+|[a-zA-Z]:[\\/][^\s'"|;&<>)`]*|\\\\[^\s'"|;&<>)`]+)""")
_RELATIVE_ESCAPE = re.compile(r"""(?<![\w])(\.\.[\\/][^\s'"|;&<>)`]*)""")
# System locations a command may name without touching the owner's files.
_SYSTEM_PATHS = ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty", "/usr/bin/env",
                 "/bin/sh", "/bin/bash", "/usr/bin/bash", "/usr/bin/python", "/usr/bin/python3", "/usr/bin/node")


def _command_paths(text: str) -> Iterable[str]:
    for match in _PATH_TOKEN.finditer(text):
        token = match.group(1).rstrip(",.:")
        if re.fullmatch(r"/[A-Za-z?]{1,3}", token):
            continue  # A cmd.exe or PowerShell switch (/c, /s, /Q), not a path.
        if token.startswith(_SYSTEM_PATHS):
            continue
        yield token
    for match in _RELATIVE_ESCAPE.finditer(text):
        yield match.group(1)


_GIT_PUSH = re.compile(r"\bgit\b(?P<pre>[^|;&\n]*?)\bpush\b(?P<args>[^|;&\n]*)", re.IGNORECASE)
# Push options that take a value, so the value is not read as a remote or branch.
_PUSH_VALUE_OPTIONS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec", "--signed"}
_DEFAULT_BRANCH_NAMES = {"main", "master", "trunk", "develop", "production", "release"}


def _git_dirs(start: str) -> Optional[tuple]:
    """``(git dir, common dir)`` of the checkout containing ``start``, or None.

    A linked worktree's ``.git`` is a file naming its git dir, whose
    ``commondir`` names the shared repository.
    """
    path = os.path.abspath(start)
    while True:
        dot_git = os.path.join(path, ".git")
        if os.path.isdir(dot_git):
            return dot_git, dot_git
        if os.path.isfile(dot_git):
            with open(dot_git, encoding="utf-8") as handle:
                text = handle.read().strip()
            if not text.startswith("gitdir:"):
                return None
            git_dir = text.split(":", 1)[1].strip()
            if not os.path.isabs(git_dir):
                git_dir = os.path.join(path, git_dir)
            common = git_dir
            common_file = os.path.join(git_dir, "commondir")
            if os.path.isfile(common_file):
                with open(common_file, encoding="utf-8") as handle:
                    common = os.path.normpath(os.path.join(git_dir, handle.read().strip()))
            return git_dir, common
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _ref(path: str) -> Optional[str]:
    """The branch a symbolic ref file points at (``ref: refs/.../name``)."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    return text.rsplit("/", 1)[-1] if text.startswith("ref:") else None


def _on_default_branch(repo_dir: Optional[str]) -> bool:
    """True when the checkout at ``repo_dir`` is on its default branch, or
    when that cannot be read (detached head, no repository): doubt asks."""
    if not repo_dir:
        return True
    try:
        dirs = _git_dirs(repo_dir)
    except OSError:
        return True
    if dirs is None:
        return True
    git_dir, common = dirs
    branch = _ref(os.path.join(git_dir, "HEAD"))
    if not branch:
        return True
    remote_default = _ref(os.path.join(common, "refs", "remotes", "origin", "HEAD"))
    return branch == remote_default or branch.lower() in _DEFAULT_BRANCH_NAMES


def _pushes_current_branch(match: "re.Match[str]") -> bool:
    """True when a ``git push`` names no branch, so it pushes the current one
    (``git push``, ``git push origin``, ``git push origin HEAD``)."""
    tokens = match.group("args").split()
    positional = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token in _PUSH_VALUE_OPTIONS:
            skip = True
            continue
        if token.startswith("-"):
            continue
        positional.append(token.strip("\"'"))
    refspecs = positional[1:]
    return all(ref.upper() in ("HEAD", "@") for ref in refspecs)


def _push_repo_dir(match: "re.Match[str]", cwd: Optional[str]) -> Optional[str]:
    """The checkout a push runs in: ``git -C <dir>`` if given, else the cwd."""
    pre = match.group("pre").split()
    if "-C" in pre and pre.index("-C") + 1 < len(pre):
        target = pre[pre.index("-C") + 1].strip("\"'")
        return target if os.path.isabs(target) or not cwd else os.path.join(cwd, target)
    return cwd


def _command_level(command: Any, scope: Scope) -> str:
    """A shell command: high when it matches the fixed list or names a path
    outside the registered projects, low otherwise."""
    if not isinstance(command, str) or not command.strip():
        return HIGH
    if _HIGH_COMMAND_RE.search(command):
        return HIGH
    for push in _GIT_PUSH.finditer(command):
        # A push that names no branch pushes the current one: ask when that
        # is the default branch (or the branch cannot be read).
        if _pushes_current_branch(push) and _on_default_branch(_push_repo_dir(push, scope.get("cwd"))):
            return HIGH
    for path in _command_paths(command):
        if path in ("/", "~") or _secret_path(path) or not _inside(path, scope):
            return HIGH
    if not scope.get("cwd") or not _inside(scope["cwd"], scope):
        return HIGH  # It runs somewhere outside the projects.
    return LOW


_PYTHON_HIGH = re.compile(
    r"\b(subprocess|os\.(system|popen|exec\w*|spawn\w*|remove|unlink|rmdir|removedirs|rename|replace|chmod|chown|kill|environ|getenv)"
    r"|shutil|socket|ctypes|urllib|http\.client|requests\.(post|put|patch|delete|request)|httpx\.(post|put|patch|delete|request)"
    r"|aiohttp|smtplib|ftplib|paramiko|keyring|importlib|__import__|__builtins__|__subclasses__|globals\(|getattr\(|setattr\("
    r"|exec\(|eval\(|compile\(|send2trash|pty|signal\.)|\.write_(text|bytes)\(|\.unlink\(|\.rmdir\(|\.rename\(|\.replace\("
    r"|\bopen\([^)]*,\s*['\"]?\w*[wax+]",
)


def _python_level(code: Any, scope: Scope, spec_risk: Optional[str] = None) -> str:
    """A code-mode cell. Its tool calls are checked one by one by the engine;
    the cell itself is low when it runs behind the OS sandbox, or when it does
    nothing but compute and call those tools."""
    if spec_risk == "safe":
        return LOW
    if not isinstance(code, str):
        return HIGH
    if _PYTHON_HIGH.search(code) or _HIGH_COMMAND_RE.search(code):
        return HIGH
    for path in _command_paths(code):
        if _secret_path(path) or not _inside(path, scope):
            return HIGH
    return LOW


# ---------------------------------------------------------------------------
# The declared levels
# ---------------------------------------------------------------------------

Rule = Callable[[Mapping[str, Any], Scope], str]
Level = Union[str, Rule]


def _arg(*keys: str) -> Callable[[Mapping[str, Any]], Any]:
    def get(data: Mapping[str, Any]) -> Any:
        for key in keys:
            if data.get(key) not in (None, ""):
                return data.get(key)
        return None
    return get


_path = _arg("file_path", "path", "notebook_path", "filePath")


def _read_rule(data, scope):
    return _read_level(_path(data), scope)


def _glob_rule(data, scope):
    root = _glob_root(data.get("pattern"))
    if root is not None and _read_level(root, scope) == HIGH:
        return HIGH
    return _read_level(data.get("path"), scope)


def _write_rule(data, scope):
    return _write_level(_path(data), scope)


def _multi_write_rule(data, scope):
    edits = data.get("edits")
    paths = [_path(data)] + ([e.get("file_path") for e in edits if isinstance(e, dict) and e.get("file_path")]
                             if isinstance(edits, list) else [])
    return HIGH if any(_write_level(p, scope) == HIGH for p in paths if p) or not _path(data) else LOW


def _command_rule(data, scope):
    return _command_level(data.get("command"), scope)


def _check_plugin_rule(data, scope):
    """Checking a plugin reads a folder (a path) or the owner's plugin
    sources (a plugin id); a folder is judged like any other read."""
    path = data.get("path")
    return _read_level(path, scope) if path not in (None, "") else LOW


def _start_session_rule(data, scope):
    """Starting a chat here, or in a named registered project, is ordinary
    work. A chat on another worker with no project could land anywhere."""
    worker = data.get("worker")
    if worker in (None, "") or data.get("project") not in (None, ""):
        return LOW
    return HIGH


# Every tool reachable from an assistant session, by its canonical name
# (:func:`canonical`). A name that is not here is high.
TOOL_LEVELS: Dict[str, Level] = {
    # Reading and searching inside the projects.
    "Read": _read_rule, "NotebookRead": _read_rule, "LS": _read_rule, "LSP": _read_rule,
    "Grep": _read_rule, "Glob": _glob_rule,
    # Editing inside the projects.
    "Write": _write_rule, "Edit": _write_rule, "NotebookEdit": _write_rule, "MultiEdit": _multi_write_rule,
    # Commands.
    "Bash": _command_rule, "PowerShell": _command_rule, "Monitor": _command_rule,
    "BashOutput": LOW, "KillShell": LOW, "KillBash": LOW,
    # run_python is decided by _python_level (it needs the engine's sandbox verdict).
    "run_python": HIGH,
    # The harness itself: notes, skills, history, memory, questions.
    "Skill": LOW, "Note": LOW, "ToolSearch": LOW, "HistoryExpand": LOW, "HistorySearch": LOW,
    "Ledger": LOW, "SessionSearch": LOW, "SessionView": LOW, "AskUser": LOW, "AskUserQuestion": LOW,
    "Remember": LOW, "Recall": LOW, "Forget": HIGH, "TodoWrite": LOW, "TodoRead": LOW,
    "ReportFindings": LOW, "EnterWorktree": LOW, "ExitWorktree": LOW,
    # Web lookups.
    "WebSearch": LOW, "WebFetch": LOW,
    # Starting and steering work.
    "Task": LOW, "Agent": LOW, "Swarm": LOW, "TaskGet": LOW, "TaskList": LOW, "TaskOutput": LOW, "TaskStop": LOW,
    "ListAgents": LOW, "SendMessage": LOW, "ListSessions": LOW, "MessageSession": LOW,
    "StartSession": _start_session_rule, "SettleSession": LOW, "UnsettleSession": LOW, "ArchiveSession": LOW,
    "HandOffSession": HIGH, "RemoteTrigger": HIGH, "ScheduleWakeup": LOW,
    "CronList": LOW, "CronCreate": HIGH, "CronDelete": HIGH,
    # App status and the owner's own devices.
    "GetUsage": LOW, "DeployStatus": LOW, "WorktreeReport": LOW, "GetAppErrors": LOW, "GetAppLogs": LOW,
    "NotifyDevices": LOW, "PushNotification": LOW, "AndroidDevices": LOW, "AndroidAction": HIGH,
    "DesignSync": HIGH,
    # Plugins (app/mcp/plugin_tools.py): searching and checking read; an
    # install puts files on the worker (switched off, pending review), so it asks.
    "FindPlugins": LOW, "CheckPlugin": _check_plugin_rule, "InstallPlugin": HIGH,
    # Private access: folders, machines and service credentials.
    "mcp__chat-access__ChatResources": LOW,
    "mcp__chat-access__RequestChatAccess": HIGH, "mcp__chat-access__ChatListFiles": HIGH,
    "mcp__chat-access__ChatReadFile": HIGH, "mcp__chat-access__ChatWriteFile": HIGH,
    "mcp__chat-access__ChatRunCommand": HIGH, "mcp__chat-access__ChatHttpRequest": HIGH,
    # Paid generation (spending outside the model calls themselves).
    "mcp__ai-tools__searchModels": LOW,
    **{f"mcp__ai-tools__{name}": HIGH for name in (
        "generateImage", "editImage", "generateWithReference", "generateVideo", "imageToVideo",
        "extendVideo", "textTo3D", "imageTo3D", "generateAudio", "audioToAudio", "runFalModel")},
    # App configuration: reading is fine, changing it is a security setting.
    **{f"mcp__app-config__{name}": LOW for name in (
        "getAutomation", "getAutomationRuns", "getConfigCapabilities", "getConnectorStatus", "getHook",
        "getProfile", "getSubagent", "listAutomations", "listConnectors", "listHooks", "listMcpServers",
        "listProfiles", "listSubagents")},
    **{f"mcp__app-config__{name}": HIGH for name in (
        "addMcpServer", "createAutomation", "createHook", "createProfile", "createSubagent",
        "deleteAutomation", "deleteHook", "deleteProfile", "deleteSubagent", "removeMcpServer",
        "testHook", "toggleAutomation", "toggleConnector", "toggleHook", "toggleMcpServer",
        "triggerAutomation", "updateAutomation", "updateHook", "updateProfile", "updateSubagent")},
    # Computer use of the owner's desktop: the access request asks; the
    # actions it grants are gated by that grant. The clipboard can hold secrets.
    "mcp__computer-use__request_access": HIGH,
    "mcp__computer-use__read_clipboard": HIGH, "mcp__computer-use__write_clipboard": HIGH,
    **{f"mcp__computer-use__{name}": LOW for name in (
        "computer_batch", "cursor_position", "double_click", "focus_window", "hold_key", "key", "left_click",
        "left_click_drag", "left_mouse_down", "left_mouse_up", "list_displays", "list_granted_applications",
        "list_windows", "middle_click", "mouse_move", "open_application", "release_control", "right_click",
        "screenshot", "scroll", "switch_display", "triple_click", "type", "wait", "window_control", "zoom")},
    # The app's browser: looking is fine; acting on a page can act on a
    # signed-in account, so it asks.
    **{f"mcp__browser__{name}": LOW for name in (
        "browser_status", "browser_snapshot", "browser_screenshot", "browser_read_text", "browser_find",
        "browser_tabs", "browser_wait", "browser_console", "browser_network", "browser_resize",
        "browser_navigate")},
    **{f"mcp__browser__{name}": HIGH for name in (
        "browser_act", "browser_batch", "browser_eval", "browser_upload")},
}
# Connector gateways: listing a connector's actions is fine; invoking one
# reads private data or writes on the owner's behalf (mail, chat, money).
for _connector in ("bluebubbles", "discord", "gmail", "homeassistant", "hue", "imessage", "line", "msteams",
                   "plaid", "signal", "slack", "spotify", "telegram", "whatsapp"):
    TOOL_LEVELS[f"{_connector}_actions"] = LOW
    TOOL_LEVELS[f"{_connector}_invoke"] = HIGH


_PREFIXES = ("mcp__fluid_bundled__", "mcp__builtins__")


def canonical(name: Any) -> str:
    """The table's spelling of a tool name: transport prefixes off, so
    ``mcp__fluid_bundled__mcp__builtins__Task`` and ``Task`` are one tool."""
    text = str(name or "")
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if text.startswith(prefix):
                text, changed = text[len(prefix):], True
    return text


def declared(name: Any) -> bool:
    """Whether ``name`` has a declared level (tests guard the built-ins)."""
    return canonical(name) in TOOL_LEVELS


def level(name: Any, tool_input: Any, scope: Scope, *, spec_risk: Optional[str] = None) -> str:
    """``low`` or ``high`` for one call in an assistant session.

    ``spec_risk`` is the runtime's own risk verdict for the call, when it has
    one; only code mode's sandbox verdict is used. Never raises: a rule that
    fails answers high.
    """
    key = canonical(name)
    data = tool_input if isinstance(tool_input, Mapping) else {}
    try:
        if key == "run_python":
            return _python_level(data.get("code"), scope, spec_risk)
        declared_level = TOOL_LEVELS.get(key, HIGH)
        if callable(declared_level):
            return HIGH if declared_level(data, scope) != LOW else LOW
        return declared_level if declared_level in (LOW, HIGH) else HIGH
    except Exception:  # noqa: BLE001 — doubt asks
        logger.warning("Assistant risk rule failed for %s; asking the owner", key, exc_info=True)
        return HIGH


def needs_owner(name: Any, tool_input: Any, scope: Optional[Scope], *, spec_risk: Optional[str] = None) -> bool:
    """True when this call must wait for the owner's firm yes."""
    return bool(scope) and level(name, tool_input, scope, spec_risk=spec_risk) == HIGH


ASK_REASON = (
    "This needs the owner's yes. The app is asking them in the assistant's conversation; "
    "wait for their answer and do not try another way to do the same thing."
)
NO_OWNER_DENIAL = (
    "Denied: this action needs the owner's yes in the assistant's conversation, and "
    "nobody can be asked from here. Tell the owner what you wanted to do."
)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

# How far up a chain of Task workers the home may be.
_MAX_DEPTH = 4


def home_of(session_id: Optional[str]) -> Optional[str]:
    """The assistant home ``session_id`` works for, or None.

    The home itself; a chat the home started; or a Task worker whose parent
    chain reaches either. Never raises.
    """
    try:
        from app.core import assistant_service
        from app.db import database

        if not assistant_service.get_primary_session_id():
            return None
        current = session_id
        for _ in range(_MAX_DEPTH):
            if not current:
                return None
            if assistant_service.is_primary_session(current):
                return current
            home = _started_by_home(current)
            if home:
                return home
            row = database.get_session(current)
            if not row or not row.get("is_worker"):
                return None
            current = row.get("parent_session_id")
    except Exception:  # noqa: BLE001 — an unknown session keeps ordinary behaviour
        logger.debug("assistant scope lookup failed for %s", session_id, exc_info=True)
    return None


def _started_by_home(session_id: str) -> Optional[str]:
    try:
        from app.core.session_orchestration import started_by_home
    except ImportError:
        return None
    return started_by_home(session_id)


def project_roots() -> list:
    """Registered projects, their checkouts, the workspace and the temp dir."""
    from app.core.config import settings
    from app.core.project_utils import get_project_working_dir, get_worktree_working_dir
    from app.db import projects
    from app.db.base import get_db

    roots = {str(settings.effective_workspace_dir), tempfile.gettempdir()}
    for project in projects.get_all_projects():
        if project.get("path"):
            roots.add(get_project_working_dir(project))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM worktrees")
        for row in cursor.fetchall():
            try:
                roots.add(get_worktree_working_dir(dict(row)))
            except (ValueError, TypeError):
                continue
    return sorted(roots)


def scope_for_session(session_id: Optional[str], cwd: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The scope dict for an assistant session, or None for any other session."""
    home = home_of(session_id)
    if not home:
        return None
    try:
        roots = project_roots()
    except Exception:  # noqa: BLE001 — no roots means every path asks
        logger.warning("Could not list project roots for %s", session_id, exc_info=True)
        roots = []
    return {"session_id": session_id, "home_session_id": home, "roots": roots, "cwd": cwd}


# Shown to the assistant in its instructions (static text, so the prompt
# prefix stays cached).
ASK_LIST_SUMMARY = (
    "spending money beyond the model calls themselves; deleting or overwriting anything outside a "
    "project's version control, force pushes, history rewrites and branch or repository deletion; "
    "merging into a default branch, deploys, releases, workflow runs and worker restarts or updates; "
    "private access to machines, folders or service credentials, and anything that reads or moves a "
    "secret; writes on the owner's behalf to email, chat, posts or repositories he does not own; "
    "computer use of his desktop and acting in the browser; changing app settings, permissions, "
    "hooks, MCP servers or automations; and anything outside his registered projects"
)
