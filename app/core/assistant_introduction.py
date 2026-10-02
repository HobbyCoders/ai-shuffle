"""What the assistant knows about its owner, read from sources the install has.

Nobody types an introduction any more. A detection pass on the worker that
runs the assistant fills it from what this install already knows:

* name: the linked Shuffle account's username, else the local admin username;
* email: the address of a connected owner Gmail connector;
* GitHub login: ``gh api user`` when the gh CLI is signed in;
* repositories: the GitHub remotes of this worker's project checkouts;
* projects: the project list.

The owner sees the result read-only on the Assistant page and can correct a
field. A correction wins over detection until the owner clears it. The stored
value lives in the synced account row's ``introduction`` column::

    {"detected": {...}, "corrections": {...}}

An introduction saved before detection existed is a flat dict the owner typed;
it is read as corrections so nothing the owner said is lost.

Detection reads names and addresses only, never mail, and gives the model no
new access: the role context labels all of it as context, not grants.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Fields detection fills, in display order.
DETECTED = ("preferred_name", "email", "github_username", "repository_refs", "projects")
# Fields the owner may correct. notes and project_ids only come from older
# introductions; they are kept and still reach the role context.
CORRECTABLE = ("preferred_name", "email", "github_username", "repository_refs", "notes", "project_ids")
LIST_FIELDS = ("repository_refs", "projects", "project_ids")
MAX_ITEMS = 20

GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}")
EMAIL = re.compile(r"[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,63}")


def _empty(value):
    return value is None or value == "" or value == []


def split(stored):
    """(detected, corrections) from a stored introduction of either shape."""
    stored = stored if isinstance(stored, dict) else {}
    if "detected" in stored or "corrections" in stored:
        detected = stored.get("detected") if isinstance(stored.get("detected"), dict) else {}
        corrections = stored.get("corrections") if isinstance(stored.get("corrections"), dict) else {}
        return dict(detected), {k: v for k, v in corrections.items() if not _empty(v)}
    return {}, {k: v for k, v in stored.items() if not _empty(v)}


def effective(stored):
    """Detected values with the owner's corrections on top."""
    detected, corrections = split(stored)
    merged = {k: v for k, v in detected.items() if not _empty(v)}
    merged.update(corrections)
    return merged


def correct(stored, changes):
    """Apply owner corrections; an empty value goes back to the detected one."""
    detected, corrections = split(stored)
    for key, value in changes.items():
        if key not in CORRECTABLE:
            raise ValueError("Unknown introduction field")
        if _empty(value):
            corrections.pop(key, None)
        else:
            corrections[key] = value
    return {"detected": detected, "corrections": corrections}


def with_detected(stored, detected):
    """The stored introduction after a detection pass (corrections untouched)."""
    _old, corrections = split(stored)
    return {"detected": {k: v for k, v in detected.items() if not _empty(v)}, "corrections": corrections}


# ---------------------------------------------------------------------------
# Detection. Every reader is fail-soft: a source that cannot answer is left
# out, never guessed. All of these block; callers run them in a thread.
# ---------------------------------------------------------------------------

def _clean_text(value, limit=80):
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value if ch.isprintable()).strip()
    return value[:limit] or None


def owner_name():
    """The linked Shuffle account's username, else the local admin username."""
    try:
        from app.core import owner_auth, account_hub
        binding = owner_auth.linked_binding()
        if binding:
            name = _clean_text(((account_hub.account_display(binding) or {}).get("account") or {}).get("username"))
            if name:
                return name
    except Exception:  # noqa: BLE001 - the hub may be unreachable; fall back
        logger.debug("Assistant: account username unavailable", exc_info=True)
    try:
        from app.db import database
        return _clean_text((database.get_admin() or {}).get("username"))
    except Exception:  # noqa: BLE001
        return None


def github_login():
    """The gh CLI's signed-in login, or None when gh is absent or signed out."""
    try:
        from app.core.auth import auth_service, is_github_authenticated_cached
        if not is_github_authenticated_cached(auth_service.is_github_authenticated):
            return None
        login = auth_service.github_login()
    except Exception:  # noqa: BLE001
        return None
    return login if login and GITHUB_LOGIN.fullmatch(login) else None


def projects_and_repositories(runner_id):
    """Project names, and the GitHub repositories of this worker's checkouts."""
    from app.core.github_service import github_service
    from app.db.base import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM projects_v2 ORDER BY created_at DESC, id ASC LIMIT %s", (MAX_ITEMS,))
        names = [_clean_text(row["name"], 120) for row in cursor.fetchall()]
        cursor.execute("SELECT path FROM checkouts WHERE runner_id=%s AND status='active' ORDER BY updated_at DESC, id ASC LIMIT %s",
                       (runner_id, MAX_ITEMS * 2))
        paths = [row["path"] for row in cursor.fetchall()]
    repositories = []
    for path in paths:
        repo = github_service.get_repo_from_remote(path) if path else None
        if repo and REPOSITORY.fullmatch(repo) and repo not in repositories:
            repositories.append(repo)
        if len(repositories) >= MAX_ITEMS:
            break
    return [n for n in names if n], repositories


def owner_gmail_connectors(runner_id):
    """Connected owner Gmail connectors on this worker (ids only)."""
    from app.db import assistant as store, database
    from app.db.base import get_db
    result = []
    for connector in database.get_all_connectors():
        if (connector.get("type") != "gmail" or not connector.get("is_active")
                or connector.get("runner_id") != runner_id or connector.get("status") != "connected"):
            continue
        with get_db() as conn:
            if store.owner_connector(conn, connector):
                result.append(connector["id"])
    return result


def clean_email(value):
    value = _clean_text(value, 254)
    return value if value and EMAIL.fullmatch(value) else None


def detect(runner_id, emails=()):
    """Everything detection found. ``emails`` come from running connectors."""
    found = {"preferred_name": owner_name(), "github_username": github_login()}
    found["email"] = next((e for e in (clean_email(v) for v in emails) if e), None)
    try:
        found["projects"], found["repository_refs"] = projects_and_repositories(runner_id)
    except Exception:  # noqa: BLE001
        logger.warning("Assistant: could not read projects for the owner introduction", exc_info=True)
    return {k: v for k, v in found.items() if not _empty(v)}
