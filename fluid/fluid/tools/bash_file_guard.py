"""Conservative Bash-to-file-tool routing, mirrored by claude_mod/hooks/file_guard.ts.

Not a shell security parser: unknown syntax always passes. The shared corpus is
shipped with the mod and exercised by both implementations.
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


def enabled() -> bool:
    return os.environ.get("FLUID_BASH_FILE_GUARD", "true").strip().lower() not in (
        "", "0", "false", "off", "no",
    )


def _tokens(command: str) -> list[str] | None:
    # Deliberately smaller than shlex: quoted operators are words, unquoted
    # operators retain their identity. Backslash continuations/expansion pass.
    if len(command) > 16384 or any(c in command for c in ("$", "`", "\r")):
        return None
    parts = re.findall(r"'[^']*'|\"[^\"]*\"|[^\s'\";&|<>()]+|&&|>>|<<|[;&|<>()\n]|['\"]", command)
    words: list[str] = []
    for part in parts:
        if part in ("'", '"'):
            return None
        if part[0] in ("'", '"'):
            # Operators inside quotes cannot become shell syntax. Encoding a
            # marker is simpler than a full shell AST; a marker makes us allow.
            value = part[1:-1]
            if any(c in value for c in ";&|<>()\n") or not value:
                return None
            words.append(value)
        else:
            if "\\" in part:
                return None
            words.append(part)
    return words


def classify(command: str) -> FileWork | None:
    if not isinstance(command, str):
        return None
    command = command.strip()
    # Exactly one closed heredoc, with nothing after its terminator. Its body
    # is data, not more commands. Expansion anywhere remains an allow.
    if "$" in command or "`" in command:
        return None
    heredoc = "\n" in command
    if re.search(r"(?:^|\s)\d+[<>]", command):
        return None
    if heredoc:
        lines = command.split("\n")
        match = re.search(r"<<\s*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\1", lines[0])
        if not match or len(lines) < 3 or lines[-1] != match[2] or match[2] in lines[1:-1]:
            return None
        command = lines[0]
    words = _tokens(command)
    if not words or ("<<" in words and not heredoc):
        return None
    if len(words) >= 4 and words[0] == "cd" and words[2] == "&&" and not words[1].startswith("-"):
        words = words[3:]
    program, *args = words
    if any(x in words for x in (";", "&", "&&", "|", "(", ")", "\n")):
        return None
    if any(x.startswith(("/dev/", "/proc/", "/sys/")) for x in args):
        return None
    if program not in ("cat", "head", "tail", "sed", "grep", "rg", "find", "ls", "echo", "printf"):
        return None
    # One file output, not fd plumbing, process substitution or input files.
    redirects = [i for i, x in enumerate(args) if x in (">", ">>")]
    if redirects:
        i = redirects[0]
        if len(redirects) != 1 or i + 1 >= len(args) or args[i+1].startswith("-"):
            return None
        rest = args[:i] + args[i+2:]
        if program not in ("cat", "printf", "echo"):
            return None
        if (program == "cat" and any(a.startswith("-") for a in rest)) or (
            program == "printf" and rest and rest[0].startswith("-")
        ):
            return None
        if "<<" in rest:
            if program != "cat" or len(rest) != 2 or rest[0] != "<<":
                return None
        elif any(x in rest for x in ("<", ">", ">>")) or i != len(args)-2:
            return None
        return FileWork("file-write/redirect", "Write", program)
    if any(x in args for x in ("<", ">", ">>", "<<")):
        return None
    if not args:
        return FileWork("search", "Glob", program) if program == "ls" else None
    if program == "cat":
        options = {"-n", "-b", "-s", "-E", "-T", "-v", "-A", "--", "--number"}
        if all(not a.startswith("-") or a in options for a in args) and any(not a.startswith("-") for a in args):
            return FileWork("file-read", "Read", program)
    if program in ("head", "tail"):
        files = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-n", "-c", "--lines", "--bytes"):
                i += 1
                if i >= len(args) or not re.fullmatch(r"[+-]?\d+", args[i]):
                    return None
            elif a.startswith("-") and not re.fullmatch(r"-\d+|-[nc][+-]?\d+|--(?:lines|bytes)=[+-]?\d+|-q|-v|--", a):
                return None
            elif not a.startswith("-"):
                files.append(a)
            i += 1
        if files:
            return FileWork("file-read", "Read", program)
    if program == "sed":
        if len(args) >= 3 and args[0] == "-n":
            expr = 2 if args[1] == "-e" else 1
            if len(args) > expr+1 and all(not a.startswith("-") for a in args[expr+1:]) and re.fullmatch(r"\d+(?:,(?:\d+|\$))?p", args[expr]):
                return FileWork("file-read", "Read", program)
        if len(args) >= 3 and re.fullmatch(r"-i(?:\.[A-Za-z0-9]+)?|--in-place(?:=\.[A-Za-z0-9]+)?", args[0]):
            expr = 2 if args[1] == "-e" else 1
            # Only simple substitution/deletion, never sed's e/r/w commands.
            if len(args) > expr+1 and all(not a.startswith("-") for a in args[expr+1:]) and (re.fullmatch(r"s/[^/\n]*/[^/\n]*/[gI0-9]*", args[expr]) or re.fullmatch(r"\d+(?:,\d+)?d", args[expr])):
                return FileWork("in-place edit", "Edit", program)
    if program in ("grep", "rg"):
        operands = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-e", "-m", "-A", "-B", "-C", "--glob", "-g"):
                i += 1
                if i >= len(args):
                    return None
                if a == "-e":
                    operands.append(args[i])
            elif a.startswith("-") and not re.fullmatch(r"-[rnRiIlLEFvHhowscbqzU0]+|-n\d+|--(?:line-number|recursive|files|hidden|ignore-case|fixed-strings)|--", a):
                return None
            elif not a.startswith("-"):
                operands.append(a)
            i += 1
        if len(operands) >= 2:
            return FileWork("search", "Grep", program)
    if program == "find":
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-name", "-iname", "-path", "-ipath", "-type", "-maxdepth", "-mindepth"):
                i += 1
                if i >= len(args):
                    return None
            elif a.startswith("-") and a not in ("-print", "-print0"):
                return None
            i += 1
        return FileWork("search", "Glob", program)
    if program == "ls" and all(not a.startswith("-") or re.fullmatch(r"-[aAlLR1dhF]+|--", a) for a in args):
        return FileWork("search", "Glob", program)
    return None


def explicitly_requested(command: str, user_text: str) -> bool:
    # Err toward allowing: an exact literal in the latest user request is
    # enough, even if it might only be an example. Never inspect tool output
    # or trust a model-supplied "user asked" flag.
    command = command.strip()
    return bool(command and re.search(
        r"(?<![A-Za-z0-9_/.-])" + re.escape(command) + r"(?![A-Za-z0-9_/.-])", user_text,
    ))


def refusal(command: str, tools: Iterable[str], user_text: str = "") -> str | None:
    if not enabled():
        return None
    work = classify(command)
    if not work or work.tool not in tools or explicitly_requested(command, user_text):
        return None
    window = " (offset/limit for a window)" if work.tool == "Read" else ""
    return f"Use {work.tool}{window} instead of {work.program}."


def guard_input(name, arguments, registry, ctx, tool_filter=None):
    """Use the effective session surface, including still-deferred tools."""
    if name != "Bash" or not isinstance(arguments.get("command"), str):
        return None
    if not enabled() or classify(arguments["command"]) is None:
        return None
    tools = {s.name for s in registry.list_tools(loaded_only=False)
             if tool_filter is None or tool_filter(s.name)}
    return refusal(arguments["command"], tools, ctx.extra.get("bash_file_guard_user_text", ""))
