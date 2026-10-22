"""Conservative Bash routing, mirrored by claude_mod/hooks/file_guard.ts.

Not a shell security parser. Only line/literal/file operations representable by
both legs' dedicated tools are recognized; uncertain syntax always passes.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from collections.abc import Iterable


@dataclass(frozen=True)
class FileWork:
    category: str
    tool: str
    program: str
    path: str
    cwd: str = ""


def enabled() -> bool:
    return os.environ.get("FLUID_BASH_FILE_GUARD", "true").strip().lower() not in (
        "", "0", "false", "off", "no",
    )


def _tokens(command: str) -> list[str] | None:
    if len(command) > 16384 or any(c in command for c in ("$", "`", "\r", "\0")):
        return None
    parts = re.findall(r"'[^']*'|\"[^\"]*\"|[^\s'\";&|<>()]+|&&|>>|<<|[;&|<>()\n]|['\"]", command)
    words: list[str] = []
    for part in parts:
        if part in ("'", '"'):
            return None
        if part[0] in ("'", '"'):
            value = part[1:-1]
            if any(c in value for c in ";&|<>()\n") or not value:
                return None
            words.append(value)
        else:
            if "\\" in part:
                return None
            words.append(part)
    return words


def _plain_path(path: str) -> bool:
    # No shell expansion or unresolved set of paths for one dedicated call.
    return bool(path and not path.startswith(("-", "~")) and not any(c in path for c in "*?[]{}\\"))


def classify(command: str) -> FileWork | None:
    if not isinstance(command, str):
        return None
    command = command.strip()
    if any(c in command for c in ("$", "`", "\0")) or re.search(r"(?:^|\s)\d+[<>]", command):
        return None
    heredoc = "\n" in command
    if heredoc:
        lines = command.split("\n")
        match = re.search(r"<<\s*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\1", lines[0])
        if not match or len(lines) < 3 or lines[-1] != match[2] or match[2] in lines[1:-1]:
            return None
        command = lines[0]
    words = _tokens(command)
    if not words or ("<<" in words and not heredoc):
        return None
    cwd = ""
    if len(words) >= 4 and words[0] == "cd" and words[2] == "&&" and _plain_path(words[1]):
        cwd, words = words[1], words[3:]
    program, *args = words
    if any(x in words for x in (";", "&", "&&", "|", "(", ")", "\n", ">>")):
        return None  # Write overwrites; it can NEVER substitute for an append.
    if any(re.match(r"^/(?:dev|proc|sys)(?:/|$)", x) for x in [cwd, *args]):
        return None

    def work(category: str, tool: str, path: str) -> FileWork | None:
        return FileWork(category, tool, program, path, cwd) if _plain_path(path) else None

    if ">" in args:
        if args.count(">") != 1:
            return None
        i = args.index(">")
        if i+1 >= len(args):
            return None
        rest = args[:i] + args[i+2:]
        if program == "cat":
            if not heredoc or len(rest) != 2 or rest[0] != "<<":
                return None  # copying/concatenating files may be binary
        elif program in ("printf", "echo"):
            if i != len(args)-2 or not rest or not all(_plain_path(a) for a in rest):
                return None  # shell expansion/escape processing is not a text Write
        else:
            return None
        return work("file-write/redirect", "Write", args[i+1])
    if any(x in args for x in ("<", ">", "<<")) or not args:
        return None
    if program == "cat":
        paths = args[1:] if args[0] in ("--", "-n") else args
        if len(paths) == 1:
            return work("file-read", "Read", paths[0])
    if program in ("head", "tail"):
        if program == "head" and len(args) == 1:
            return work("file-read", "Read", args[0])
        numeric = None
        if len(args) == 3 and args[0] in ("-n", "--lines"):
            numeric = args[1]
        elif len(args) == 2:
            match = re.fullmatch(r"(?:-n|--lines=)(\+?\d+)", args[0])
            if match:
                numeric = match[1]
            elif program == "head" and re.fullmatch(r"-\d+", args[0]):
                numeric = args[0][1:]
        if numeric and re.fullmatch(r"\+\d+" if program == "tail" else r"\+?\d+", numeric):
            return work("file-read", "Read", args[-1])
    if program == "sed":
        expr = 2 if len(args) > 1 and args[1] == "-e" else 1
        if len(args) == expr+2:
            if args[0] == "-n" and re.fullmatch(r"\d+(?:,\d+)?p", args[expr]):
                return work("file-read", "Read", args[-1])
            if args[0] in ("-i", "--in-place"):
                subst = re.fullmatch(r"s/([^/\n]+)/([^/\n]*)/(g?)", args[expr])
                if subst and not re.search(r"[.\^$*+?\[\]{}()|\\]", subst[1]) and not re.search(r"[&\\]", subst[2]):
                    return work("in-place edit", "Edit", args[-1])
    if program == "grep":
        # Only non-recursive explicit-file grep. Recursive grep, all rg (which
        # may recurse implicitly), find and ls can see paths Grep/Glob prune.
        operands = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-e":
                i += 1
                if i >= len(args):
                    return None
                operands.append(args[i])
            elif a.startswith("-"):
                if not re.fullmatch(r"-[nE]+|--line-number|--", a):
                    return None
            else:
                operands.append(a)
            i += 1
        if len(operands) == 2:
            return work("search", "Grep", operands[1])
    return None


def resumed_user_text(nodes, sources: dict[str, str]) -> str:
    """Latest proven user intent; stored notices/peers cannot grant or erase it."""
    from fluid.messages import ToolResultBlock

    for node in reversed(nodes):
        if node.message.role != "user":
            continue
        results = [b for b in node.message.content if isinstance(b, ToolResultBlock)]
        for result in reversed(results):
            for steer in reversed(result.delivered_steers):
                if steer.origin == "user" and steer.sender is None:
                    return steer.text
        if not results and sources.get(node.node_id) == "user":
            return node.message.text()
    return ""


def explicitly_requested(command: str, user_text: str) -> bool:
    command = command.strip()
    return bool(command and re.search(
        r"(?<![A-Za-z0-9_/.-])" + re.escape(command) + r"(?![A-Za-z0-9_/.-])", user_text,
    ))


def refusal(command: str, tools: Iterable[str], user_text: str = "") -> str | None:
    if not enabled():
        return None
    found = classify(command)
    if not found or found.tool not in tools or explicitly_requested(command, user_text):
        return None
    return f"Use {found.tool} (or a pipeline if {found.tool} can't do this)."


def guard_input(name, arguments, registry, ctx, tool_filter=None, permissions=None):
    if name != "Bash" or not isinstance(arguments.get("command"), str) or not enabled():
        return None
    found = classify(arguments["command"])
    if found is None:
        return None
    spec = registry.get(found.tool)
    tools = {s.name for s in registry.list_tools(loaded_only=False)
             if tool_filter is None or tool_filter(s.name)}
    if spec is None or found.tool not in tools:
        return None
    # This is only a dry permission decision: no execution, prompts or doom-
    # loop mutations. Respect the same cwd-normalized path rules as dispatch.
    if permissions is not None:
        path = os.path.normpath(os.path.join(ctx.cwd, found.cwd, found.path))
        key = "file_path" if found.tool in ("Read", "Write", "Edit") else "path"
        if permissions.decide(spec, {key: path}, cwd=ctx.cwd).decision == "deny":
            return None
    return refusal(arguments["command"], tools, ctx.extra.get("bash_file_guard_user_text", ""))
