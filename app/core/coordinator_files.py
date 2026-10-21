"""Read-only project broker for owner no-project sessions, on both harness legs.

Paths are authorized from the live project registry; contents and directory
enumeration use confined handles, not a permission callback's check-then-open.
"""
from pathlib import Path
import asyncio
import re
import time

from fluid.tools.registry import ToolOutcome
from fluid.tools.builtins._util import compile_glob, PRUNE_DIRS
from app.core.confined_files import list_confined, open_attachment
from app.core.session_modes import chat_uploads_dir

DENIAL = "No-project sessions cannot write worker files or run a shell. Start a session in that project instead."


def roots():
    from app.core.config import settings
    from app.db import projects_v2, checkouts, projects
    from app.core.project_utils import get_project_working_dir
    result = [chat_uploads_dir()]
    # Legacy registrations remain readable during the canonical-project migration.
    for project in projects.get_all_projects():
        if project.get("path"):
            path = Path(get_project_working_dir(project)).resolve()
            if path != Path(path.anchor):
                result.append(path)
    for project in projects_v2.list_projects_v2():
        for row in checkouts.list_checkouts(project["id"]):
            if row.get("runner_id") != settings.effective_instance_id or (row.get("status") or "active") != "active":
                continue
            path = Path(row["path"]).resolve()
            if path != Path(path.anchor):
                result.append(path)
    return list(dict.fromkeys(result))


def authorized_path(raw):
    path = Path(raw) if raw else chat_uploads_dir()
    if not path.is_absolute():
        path = chat_uploads_dir() / path
    path = path.resolve()
    for root in sorted(roots(), key=lambda p: len(p.parts), reverse=True):
        if path.is_relative_to(root):
            return root, path
    raise ValueError("Read-only access is limited to registered project roots and chat attachments.")


def permitted(name, arguments):
    name = name.removeprefix("mcp__attachments__")
    if name not in {"Read", "Glob", "Grep"}:
        return True
    try:
        raw = arguments.get("file_path") if name == "Read" else arguments.get("path")
        if name == "Read" and not raw:
            return False
        authorized_path(raw)
        return True
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        return False


def _search(name, args):
    root, path = authorized_path(args.get("path"))
    pattern = compile_glob(args["pattern"] if name == "Glob" else args.get("glob") or "**/*")
    regex = re.compile(args["pattern"]) if name == "Grep" else None
    from fluid.tools.hardening import is_sensitive_path
    limit = min(5000, max(1, int(args.get("limit") or 200)))
    pending = [path.relative_to(root).as_posix()]
    result = []
    visited = 0
    deadline = time.monotonic() + 5
    while pending and len(result) < limit and visited < 10000 and time.monotonic() < deadline:
        relative = pending.pop()
        target = root / relative
        if target.is_dir():
            for entry, directory in list_confined(root, "" if relative == "." else relative):
                if entry in PRUNE_DIRS or (entry.startswith(".") and not args.get("no_ignore")):
                    continue
                pending.append((Path(relative) / entry).as_posix())
            continue
        visited += 1
        rel = target.relative_to(path).as_posix() if target != path else target.name
        if not pattern.matches(rel):
            continue
        if name == "Glob":
            result.append(str(target))
            continue
        # Match the normal Grep tool's credential withholding convention.
        if is_sensitive_path(str(target)):
            continue
        try:
            with open_attachment(root, target) as stream:
                data = stream.read(2_000_001)
            if len(data) > 2_000_000 or b"\0" in data:
                continue
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    result.append(f"{target}:{number}:{line[:2000]}")
                    if len(result) == limit:
                        break
        except (OSError, ValueError):
            continue
    suffix = ["[search bounded; narrow the path or pattern]"] if pending else []
    return ToolOutcome(text="\n".join([f"{name} inside {path}", *(result or ["(no matches)"]), *suffix]))


def handler(name, session_supplier):
    async def run(arguments, ctx):
        from app.core.session_orchestration import is_owner_session
        if not is_owner_session(session_supplier()):
            return ToolOutcome(text="Project reads require an owner top-level session.", is_error=True)
        try:
            if name == "Read":
                from app.core.attachment_read import read_attachment
                root, path = authorized_path(arguments.get("file_path"))
                return await read_attachment({**arguments, "file_path": str(path)}, ctx, root=root)
            return await asyncio.to_thread(_search, name, arguments)
        except (OSError, ValueError, RuntimeError) as exc:
            return ToolOutcome(text=str(exc), is_error=True)
    return run
