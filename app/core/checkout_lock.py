"""Checkout lock: an agent may not change the branch of another session's checkout.

A session bound to a branch runs in that branch's checkout, and the cwd
resolvers refuse to run once the folder is on another branch
(``query_options.WorktreeUnavailableError``). That refusal is the last line of
defence: by the time it fires, the session that owns the folder is already
stuck. This module stops the cause instead. Before a shell tool runs, its
command is scanned for branch-changing Git operations (``checkout``/``switch``
to another branch, ``checkout -b``, ``branch -f``/``-m``/``-d``, a moving or
``--hard`` ``reset``, ``rebase``, ``worktree move``/``remove``, ``gh pr
checkout``). Each one aimed at a checkout bound to a different session is
refused with a message telling the agent to use its own checkout.

Ownership is the same rule turn end uses when it declines to follow a branch
switch (``turn_finalize._checkout_shared_with_another_session``): a checkout
row on this runner records a branch, and every non-archived session on this
runner bound to that branch is bound to that checkout. The calling session and
its own workers do not count, so a session's own ``git checkout -b`` in its own
worktree keeps working (and turn end then follows it). The project's base
checkout records no branch and is not locked here.

Both harness legs enforce it: the native leg wraps the tool registry's
dispatch (which also serves the delegated leg's bridged Monitor), and the
delegated leg's PreToolUse policy covers the claude CLI's own Bash. The scan is
pure string work; Git and the database are consulted only for a command that
contains a candidate operation.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: Tool names (lower-cased, without any ``mcp__…__`` prefix) whose input runs
#: a shell command.
SHELL_TOOLS = frozenset({"bash", "monitor", "powershell"})

_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "\n", "(", ")", "|&", ";;"})
_WRAPPERS = frozenset({"sudo", "env", "command", "exec", "nohup", "time", "nice", "builtin"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# git's global options that take their value as the next word.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                                "--super-prefix", "--config-env", "--exec-path"})


@dataclass(frozen=True)
class BranchChange:
    """One branch-changing operation found in a command.

    ``directory`` is where it runs. ``ref`` is a word that changes the branch
    only if it names a commit (``git checkout <ref>``, ``git reset <ref>``).
    ``branch`` is a branch name whose checkout is affected (``git branch -f``);
    ``worktree`` is a worktree path argument (``git worktree move``).
    """

    text: str
    directory: str
    ref: Optional[str] = None
    kind: str = ""
    branch: Optional[str] = None
    current_branch: bool = False
    worktree: Optional[str] = None


def _short_name(tool_name: str) -> str:
    return str(tool_name or "").rsplit("__", 1)[-1].strip().lower()


def _lex(command: str) -> Optional[List[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes: the shell cannot run it either
        return None


_REDIRECT = re.compile(r"^\d*(?:>>?|<<?)")
_OPERATOR_CHARS = frozenset(";&|()\n")


def _segments(tokens: Iterable[str]) -> List[List[str]]:
    """Split on shell operators, dropping redirections (``2>&1``, ``> out``)."""
    segments: List[List[str]] = [[]]
    tokens = list(tokens)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _REDIRECT.match(token):
            # ``>out`` carries its target; a bare ``>`` or ``2>`` takes the
            # next word, and ``2>&1`` lexes as ``2>``, ``&``, ``1``.
            if token[-1] not in "<>":
                index += 1
            elif index + 1 < len(tokens) and tokens[index + 1] == "&":
                index += 3
            else:
                index += 2
            continue
        if token in _SEPARATORS or (token and set(token) <= _OPERATOR_CHARS):
            segments.append([])
        else:
            segments[-1].append(token)
        index += 1
    return [segment for segment in segments if segment]


def _program(word: str) -> str:
    name = os.path.basename(word.replace("\\", "/")).lower()
    return name[:-4] if name.endswith(".exe") else name


def _strip_wrappers(words: List[str]) -> List[str]:
    """Drop leading ``VAR=value`` assignments and wrappers like ``sudo``/``env``."""
    index = 0
    while index < len(words):
        word = words[index]
        if "=" in word and not word.startswith("-") and word.split("=", 1)[0].isidentifier():
            index += 1
            continue
        if _program(word) in _WRAPPERS:
            index += 1
            # ``env -i``, ``sudo -E``, ``nice -n 5``: skip their options.
            while index < len(words) and words[index].startswith("-"):
                index += 1
            continue
        break
    return words[index:]


def _join(base: str, path: str) -> str:
    return os.path.normpath(os.path.join(base, os.path.expanduser(path)))


def _positionals(args: List[str]) -> List[str]:
    return [arg for arg in args if not arg.startswith("-") or arg == "-"]


def _before_dashdash(args: List[str]) -> List[str]:
    return args[: args.index("--")] if "--" in args else args


def _git_changes(args: List[str], directory: str, text: str) -> List[BranchChange]:
    """The branch changes in one ``git`` invocation (``args`` after ``git``)."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        if option in _GIT_VALUE_OPTIONS and index + 1 < len(args):
            value = args[index + 1]
            if option == "-C":
                directory = _join(directory, value)
            elif option == "--work-tree":
                directory = _join(directory, value)
            index += 2
            continue
        if option.startswith("--work-tree="):
            directory = _join(directory, option.split("=", 1)[1])
        index += 1
    if index >= len(args):
        return []
    sub, rest = args[index], args[index + 1:]
    if "-h" in rest or "--help" in rest:
        return []
    head = _before_dashdash(rest)
    if sub == "switch":
        return [BranchChange(text, directory)] if rest else []
    if sub == "checkout":
        if any(arg in ("-b", "-B", "--orphan", "--detach", "-t", "--track", "-")
               or arg.startswith(("--orphan=", "--track="))
               or (arg.startswith("-") and not arg.startswith("--") and set(arg[1:]) & {"b", "B"})
               for arg in head):
            return [BranchChange(text, directory)]
        if "--" in rest or any(arg in ("-p", "--patch") or arg.startswith("--pathspec-from-file")
                               for arg in rest):
            return []  # restoring paths, not switching
        words = _positionals(head)
        # ``git checkout <commit> <path>…`` restores paths too.
        return [BranchChange(text, directory, ref=words[0], kind="checkout")] if len(words) == 1 else []
    if sub == "reset":
        if "--" in rest or len(_positionals(head)) > 1:
            return []  # resetting index entries for paths
        hard = any(arg in ("--hard", "--keep", "--merge") for arg in head)
        words = _positionals(head)
        if hard:
            return [BranchChange(text, directory)]
        return [BranchChange(text, directory, ref=words[0], kind="reset")] if words else []
    if sub == "rebase":
        return [BranchChange(text, directory)]
    if sub == "branch":
        flags = [arg for arg in head if arg.startswith("-")]
        short = set("".join(flag[1:] for flag in flags if not flag.startswith("--")))
        long = {flag.split("=", 1)[0] for flag in flags if flag.startswith("--")}
        words = _positionals(head)
        if short & {"m", "M"} or "--move" in long:
            if len(words) == 1:
                return [BranchChange(text, directory, current_branch=True)]
            return [BranchChange(text, directory, branch=words[0])] if words else []
        if short & {"f", "d", "D"} or long & {"--force", "--delete"}:
            names = words if (short & {"d", "D"} or "--delete" in long) else words[:1]
            return [BranchChange(text, directory, branch=name) for name in names]
        return []
    if sub == "worktree" and rest and rest[0] in ("move", "remove"):
        words = [arg for arg in rest[1:] if not arg.startswith("-")]
        return [BranchChange(text, directory, worktree=_join(directory, words[0]))] if words else []
    return []


def branch_changes(command: str, cwd: str, _depth: int = 0) -> List[BranchChange]:
    """Every branch-changing Git operation in ``command`` run from ``cwd``.

    Follows ``cd``/``pushd`` within the command, ``git -C``/``--work-tree``,
    leading assignments and wrappers (``sudo``, ``env``), and ``sh -c``/``eval``
    strings. Pure: no Git, no database.
    """
    if not command or ("git" not in command and "gh" not in command) or _depth > 3:
        return []
    tokens = _lex(command)
    if tokens is None:
        return []
    directory = os.path.normpath(cwd) if cwd else os.getcwd()
    found: List[BranchChange] = []
    for segment in _segments(tokens):
        words = _strip_wrappers(segment)
        if not words:
            continue
        program = _program(words[0])
        if program in ("cd", "pushd", "set-location", "sl"):
            targets = [word for word in words[1:] if not word.startswith("-")]
            if targets:
                directory = _join(directory, targets[0])
            elif program == "cd":
                directory = os.path.expanduser("~")
            continue
        flag = next((position for position, word in enumerate(words[1:], 1)
                     if word.startswith("-") and not word.startswith("--") and "c" in word), None)
        if program in _SHELLS and flag is not None:
            if flag + 1 < len(words):
                found += branch_changes(words[flag + 1], directory, _depth + 1)
            continue
        if program == "eval":
            found += branch_changes(" ".join(words[1:]), directory, _depth + 1)
            continue
        text = " ".join(segment)
        if program == "git":
            found += _git_changes(words[1:], directory, text)
        elif program == "gh" and len(words) > 2 and words[1] == "pr" and words[2] in ("checkout", "co"):
            found.append(BranchChange(text, directory))
    return found


# --- Who owns a checkout -----------------------------------------------------


def _git(path: str, *args: str) -> Optional[str]:
    from app.core.git_service import git_service

    result = git_service._run_git(path, list(args), timeout=10)
    out = (result.stdout or "").strip()
    return out if result.returncode == 0 and out else None


def _key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _inside(path: str, root: str) -> bool:
    path, root = _key(path), _key(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _toplevel(directory: str) -> Optional[str]:
    probe = directory
    # A ``worktree move`` target or a ``cd`` into a subfolder that does not
    # exist yet still belongs to the checkout above it.
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    return _git(probe, "rev-parse", "--show-toplevel") if probe else None


def _worktree_of_branch(directory: str, branch: str) -> Optional[str]:
    """The checkout of the repository at ``directory`` that has ``branch`` out."""
    listing = _git(directory, "worktree", "list", "--porcelain")
    path = None
    for line in (listing or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path:
            return path
    return None


def _checkout_row(toplevel: str) -> Optional[Dict[str, Any]]:
    from app.core import checkout_manager
    from app.core.config import settings
    from app.db import checkouts as checkouts_db

    target = _key(toplevel)
    matches = [row for row in checkouts_db.list_branch_checkouts(settings.effective_instance_id)
               if row.get("path") and _key(checkout_manager._resolve_checkout_path(row["path"])) == target]
    active = [row for row in matches if (row.get("status") or "active") == "active"]
    return (active or matches or [None])[0]


def checkout_owners(toplevel: str, session_id: str) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(checkout row, other sessions bound to it)`` for the folder ``toplevel``.

    Others are non-archived sessions on this runner bound to the row's branch,
    other than ``session_id`` and its own workers.
    """
    from app.core.config import settings
    from app.db import database

    row = _checkout_row(toplevel)
    if not row:
        return None, []
    me = settings.effective_instance_id
    others = []
    for other in database.list_sessions_for_v2_branch(row["project_id"], row["branch"]):
        if other.get("id") == session_id or other.get("parent_session_id") == session_id:
            continue
        if (other.get("status") or "") == "archived":
            continue
        if other.get("instance_id") not in (None, "", me):
            continue
        others.append(other)
    return row, others


def _own_checkout(session_id: str) -> tuple[Optional[str], bool]:
    """``(the caller's own checkout folder, whether it is bound to a branch)``."""
    from app.core import project_resolver
    from app.db import database

    row = database.get_session(session_id)
    if not row:
        return None, False
    row = dict(row)
    bound = bool(row.get("branch") or row.get("worktree_branch"))
    return project_resolver.existing_checkout_path(row), bound


def _refusal(change: BranchChange, toplevel: str, row: Dict[str, Any],
             others: List[Dict[str, Any]], session_id: str) -> str:
    owner = others[0]
    title = str(owner.get("title") or "").strip()
    who = f"session {owner['id']}" + (f" (\u201c{title[:80]}\u201d)" if title else "")
    more = f" and {len(others) - 1} other session(s)" if len(others) > 1 else ""
    text = change.text if len(change.text) <= 200 else change.text[:197] + "..."
    own, bound = _own_checkout(session_id)
    if own and _inside(own, toplevel):
        where = ("This chat is bound to that checkout too, so its branch cannot change here. "
                 "Leave the branch as it is, or ask the owner for a chat with its own worktree.")
    elif own and bound:
        where = f"Use your own checkout ({own}) instead; create or switch branches there."
    else:
        where = ("Use your own checkout instead; a chat that needs its own branch can be "
                 "started with worktrees on.")
    return (f"Refused: `{text}` would change the branch of {toplevel}, the checkout of "
            f"{who}{more} on branch '{row['branch']}'. A session's checkout belongs to "
            f"that session, and switching it breaks that session's work. {where} "
            "Nothing was run.")


def _affected_toplevels(change: BranchChange) -> List[str]:
    if change.worktree:
        top = _toplevel(change.worktree)
        return [top] if top else []
    top = _toplevel(change.directory)
    if not top:
        return []
    if change.branch:
        holder = _worktree_of_branch(top, change.branch)
        return [holder] if holder else []
    return [top]


def _changes_branch(change: BranchChange, toplevel: str) -> bool:
    """Whether a ``ref``-dependent change really moves ``toplevel``'s branch."""
    if change.ref is None:
        return True
    target = _git(toplevel, "rev-parse", "--verify", "--quiet", f"{change.ref}^{{commit}}")
    if change.kind == "reset":
        # ``git reset <commit>`` moves the branch unless it names HEAD's commit.
        return target is not None and target != _git(toplevel, "rev-parse", "HEAD")
    if target is None:
        # Not a commit: a path to restore, unless it is not one either
        # (``git checkout <remote-only branch>`` creates and switches).
        return not os.path.exists(_join(change.directory, change.ref))
    # Checking out anything but the current branch, even its own commit,
    # leaves the branch (a detached HEAD is off the branch too).
    return change.ref != _git(toplevel, "symbolic-ref", "--quiet", "--short", "HEAD")


def denial_for_changes(session_id: str, changes: List[BranchChange]) -> Optional[str]:
    """The refusal for the first change aimed at another session's checkout."""
    for change in changes:
        for toplevel in _affected_toplevels(change):
            row, others = checkout_owners(toplevel, session_id)
            if row and others and _changes_branch(change, toplevel):
                return _refusal(change, toplevel, row, others, session_id)
    return None


def shell_command(tool_name: str, tool_input: Any) -> Optional[str]:
    """The shell command a tool call runs, or None for any other tool."""
    if _short_name(tool_name) not in SHELL_TOOLS or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    return command if isinstance(command, str) else None


def pending_changes(tool_name: str, tool_input: Any, cwd: Optional[str]) -> List[BranchChange]:
    """Cheap pre-check (no I/O): the candidate changes this call would make."""
    command = shell_command(tool_name, tool_input)
    if not command:
        return []
    base = tool_input.get("cwd") if isinstance(tool_input.get("cwd"), str) else None
    start = base or cwd
    if start and base and not os.path.isabs(base) and cwd:
        start = _join(cwd, base)
    return branch_changes(command, start or os.getcwd())


def tool_denial(session_id: Optional[str], tool_name: str, tool_input: Any,
                cwd: Optional[str]) -> Optional[str]:
    """Why this tool call is refused by the checkout lock, or None.

    Blocking (Git and database reads) only when the command holds a
    candidate operation. A lookup failure lets the call through with a
    warning: the resolver's wrong-branch refusal still stands behind it.
    """
    if not session_id:
        return None
    changes = pending_changes(tool_name, tool_input, cwd)
    if not changes:
        return None
    try:
        return denial_for_changes(session_id, changes)
    except Exception:
        logger.warning("checkout_lock: ownership check failed for %s", session_id, exc_info=True)
        return None


def install_registry_guard(registry: Any, session_id: Optional[str], default_cwd: Optional[str]) -> None:
    """Make ``registry.dispatch`` refuse shell calls that would switch another
    session's checkout. Covers native Bash/Monitor and bridged app tools."""
    if not session_id or registry is None:
        return
    import asyncio

    from fluid.tools.registry import ToolOutcome

    dispatch = registry.dispatch

    async def guarded_dispatch(name, input, ctx=None, **kwargs):
        cwd = getattr(ctx, "cwd", None) or default_cwd
        if pending_changes(name, input, cwd):
            denial = await asyncio.to_thread(tool_denial, session_id, name, input, cwd)
            if denial:
                return ToolOutcome(text=denial, is_error=True)
        return await dispatch(name, input, ctx, **kwargs)

    registry.dispatch = guarded_dispatch
