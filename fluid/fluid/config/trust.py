"""Which project folders may configure the harness.

A project's own files can change what the harness does before the first
prompt: ``.fluid/config.toml`` sets the approval policy and adds hooks and
MCP servers (programs that start with the session), ``.fluid/settings.json``
adds permission rules, a command file runs shell code when it is called.
That is right for the owner's own project and wrong for a folder that was
just cloned or unpacked.

So a project's files are loaded only once its owner has said the folder is
trusted. The answer is kept in the user's home (``~/.fluid/
trusted_projects.json``), never in the project, and is keyed by the real
path of the project's top folder: the git checkout's root, or the working
folder outside a checkout. Trust is for that folder exactly. A checkout
cloned inside a trusted folder has its own root and is asked about again.

Trust is per path, not per content: a trusted project that later pulls a
changed config is still trusted, as with any tool that keeps this kind of
record.

The rule is the same on Linux, macOS and Windows: paths are compared after
links are followed and with the platform's own case folding, and a record
that cannot be read means "not trusted".
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

TRUST_FILE_NAME = "trusted_projects.json"

# The settings scopes a project's own files feed (see
# ``fluid.config.model.SETTING_SOURCES``): never loaded for an untrusted one.
PROJECT_SOURCES = frozenset({"project", "local"})


def trust_file() -> Path:
    return Path.home() / ".fluid" / TRUST_FILE_NAME


def project_root(cwd: str | os.PathLike[str] | None = None) -> Path | None:
    """The folder a trust answer is recorded for, or None when the working
    folder has no project files to load (it is the home folder)."""
    from fluid.context.settings_stack import project_dirs

    try:
        dirs = project_dirs(Path.cwd() if cwd is None else cwd)
    except (OSError, RuntimeError, ValueError):
        return None
    return dirs[0] if dirs else None


def _key(folder: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(folder)))


def _read() -> list[str]:
    try:
        data = json.loads(trust_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        return []
    return [item for item in projects if isinstance(item, str) and item]


def _write(projects: list[str]) -> None:
    path = trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "projects": projects}, indent=2) + "\n"
    # Written beside the file and moved over it, so a crash leaves the old
    # record and never half of a new one.
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".trusted_projects.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(payload)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def trusted_projects() -> list[str]:
    """Every trusted project folder, as recorded."""
    return _read()


def is_project_trusted(cwd: str | os.PathLike[str] | None = None) -> bool:
    """True when the project ``cwd`` belongs to may configure the harness.

    Also True when there is no project to load (:func:`project_root` is
    None): nothing is gated then.
    """
    root = project_root(cwd)
    if root is None:
        return True
    key = _key(root)
    return any(_key(item) == key for item in _read())


def trust_project(cwd: str | os.PathLike[str] | None = None) -> Path | None:
    """Record the project ``cwd`` belongs to as trusted. Returns its root."""
    root = project_root(cwd)
    if root is None:
        return None
    key = _key(root)
    projects = _read()
    if not any(_key(item) == key for item in projects):
        _write([*projects, os.path.realpath(root)])
    return root


def untrust_project(cwd: str | os.PathLike[str] | None = None) -> Path | None:
    """Forget the trust answer for the project ``cwd`` belongs to."""
    root = project_root(cwd)
    if root is None:
        return None
    key = _key(root)
    projects = _read()
    kept = [item for item in projects if _key(item) != key]
    if len(kept) != len(projects):
        _write(kept)
    return root


__all__ = [
    "PROJECT_SOURCES",
    "TRUST_FILE_NAME",
    "is_project_trusted",
    "project_root",
    "trust_file",
    "trust_project",
    "trusted_projects",
    "untrust_project",
]
