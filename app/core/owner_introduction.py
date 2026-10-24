"""What the assistant knows about its owner, read from sources the install has.

Nobody types an introduction any more. A detection pass on the worker that
runs the assistant fills it from what this install already knows:

* name: the linked Shuffle account's username, else the local admin username;
* email: the address of a connected owner Gmail connector;
* GitHub login: ``gh api user`` when the gh CLI is signed in;
* repositories: the GitHub remotes of this worker's project checkouts;
* projects: the project list.

It sets the default git author for a new repository (``api/git.py``). A
correction an older release saved wins over detection until it is cleared. The stored
value lives in the synced account row's ``introduction`` column::

    {"detected": {...}, "corrections": {...}}

An introduction saved before detection existed is a flat dict the owner typed;
it is read as corrections so nothing the owner said is lost.

Detection reads account identity and connector metadata, never mail. Only
bounded names and GitHub logins feed ordinary owner memory; addresses, project
metadata and legacy free-form notes stay out. Facts do not grant access.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

# Fields detection fills, in display order.
DETECTED = ("preferred_name", "email", "github_username", "repository_refs", "projects")
# Fields the owner may correct. notes and project_ids only come from older
# introductions; they remain available to account compatibility consumers.
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


def with_detected(
    stored: dict[str, Any], detected: dict[str, Any], unknown: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """The stored introduction after a detection pass (corrections untouched).

    ``unknown`` names fields whose source could not answer this pass (gh or
    the account hub unreachable, Gmail not started yet). Those keep their last
    detected value, so a blip neither drops an account field nor publishes a
    change.
    """
    old, corrections = split(stored)
    fresh = {k: v for k, v in detected.items() if not _empty(v)}
    for key in unknown:
        if not _empty(old.get(key)):
            fresh[key] = old[key]
    return {"detected": fresh, "corrections": corrections}


def remember_identity(stored: dict[str, Any]) -> None:
    """Backfill safe effective identity through the ordinary owner's memory store.

    Do not copy arbitrary notes, email/street addresses, project names or remotes.
    The allowlist is intentionally smaller than the introduction/Git-author data.
    Structured owner configuration is recorded as session evidence with an explicit
    detection source, never as an owner-requested Remember or a permission grant.
    """
    from fluid.context.screening import looks_like_secret, screen_findings

    from app.core.fluid_memory import save_detected_owner_fact

    values = effective(stored)
    _, corrections = split(stored)
    for field, key, label in (
        ("preferred_name", "profile.name", "preferred name"),
        ("github_username", "profile.github_username", "GitHub username"),
    ):
        value = values.get(field)
        if not isinstance(value, str) or not value or len(value) > 80:
            continue
        if field == "preferred_name":
            # Human names only: reject addresses, paths, markup and token strings.
            if not all(ch.isalpha() or ch in " '-.’" for ch in value):
                continue
        elif not GITHUB_LOGIN.fullmatch(value):
            continue
        if looks_like_secret(value) or screen_findings(value):
            continue
        source = "correction" if field in corrections else "detected"
        try:
            save_detected_owner_fact(
                f"The user's {label} is {value}.", key, f"about-you:{source}:{field}",
            )
        except ValueError:
            # A forgotten/suppressed fact must not be restored by a periodic pass.
            continue
        except (OSError, TimeoutError):
            # Retry next pass without publishing private facts or exception text.
            logger.warning("Owner identity memory write unavailable")


# ---------------------------------------------------------------------------
# Detection. Every reader is fail-soft: a source that cannot answer is left
# out, never guessed. All of these block; callers run them in a thread.
# ---------------------------------------------------------------------------

def _clean_text(value, limit=80):
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value if ch.isprintable()).strip()
    return value[:limit] or None


def owner_name(unknown=None):
    """The linked Shuffle account's username, else the local admin username.

    When the account is linked but the hub cannot answer, the field goes into
    ``unknown`` so the last detected name is kept instead of the admin's.
    """
    try:
        from app.core import account_hub, owner_auth
        binding = owner_auth.linked_binding()
        if binding:
            name = _clean_text(((account_hub.account_display(binding) or {}).get("account") or {}).get("username"))
            if name:
                return name
    except Exception:  # The hub may be unreachable; fall back.
        logger.debug("Assistant: account username unavailable", exc_info=True)
        if unknown is not None:
            unknown.add("preferred_name")
    try:
        from app.db import database
        return _clean_text((database.get_admin() or {}).get("username"))
    except Exception:  # noqa: BLE001
        return None


def github_login(unknown=None):
    """The gh CLI's signed-in login, or None when gh is absent or signed out.

    Signed in but ``gh api user`` failed (offline, rate limited) puts the
    field in ``unknown``: the owner is signed in, the login just isn't known.
    """
    try:
        from app.core.auth import auth_service, is_github_authenticated_cached
        if not is_github_authenticated_cached(auth_service.is_github_authenticated):
            return None
        login = auth_service.github_login()
    except Exception:  # noqa: BLE001
        return None
    if login and GITHUB_LOGIN.fullmatch(login):
        return login
    if unknown is not None:
        unknown.add("github_username")
    return None


def projects_and_repositories(runner_id: str) -> tuple[list[str], list[str]]:
    """Project names, and the GitHub repositories of this worker's checkouts."""
    from app.core.github_service import github_service
    from app.db.base import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM projects_v2 ORDER BY created_at DESC, id ASC LIMIT %s", (MAX_ITEMS,))
        names = [_clean_text(row["name"], 120) for row in cursor.fetchall()]
        # Base checkouts only: worktrees share their base's remote and would
        # crowd other repositories out of the limit. Ordered by path, not by
        # updated_at, which every checkout write bumps.
        cursor.execute("SELECT path FROM checkouts WHERE runner_id=%s AND status='active' "
                       "AND (branch IS NULL OR branch = '') ORDER BY path ASC, id ASC LIMIT %s",
                       (runner_id, MAX_ITEMS * 2))
        paths = [row["path"] for row in cursor.fetchall()]
    repositories = []
    for path in paths:
        repo = github_service.get_repo_from_remote(path) if path else None
        if repo and REPOSITORY.fullmatch(repo) and repo not in repositories:
            repositories.append(repo)
        if len(repositories) >= MAX_ITEMS:
            break
    # Stable ordering avoids account publications when only query order changes.
    return [n for n in names if n], sorted(repositories, key=str.lower)


def owner_gmail_connectors(runner_id):
    """Connected owner Gmail connectors on this worker (ids only)."""
    from app.db import database
    from app.db import owner_state as store
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


def detect(runner_id, emails=(), unknown=None):
    """Everything detection found. ``emails`` come from running connectors.

    Fields whose source could not answer this pass are added to ``unknown``
    (see with_detected); a source that answered "nothing" is just left out.
    """
    unknown = set() if unknown is None else unknown
    found = {"preferred_name": owner_name(unknown), "github_username": github_login(unknown)}
    found["email"] = next((e for e in (clean_email(v) for v in emails) if e), None)
    if emails and not found["email"]:
        # A connected Gmail connector that has not reported its address yet
        # (still starting after a restart, or reconnecting).
        unknown.add("email")
    try:
        found["projects"], found["repository_refs"] = projects_and_repositories(runner_id)
    except Exception:
        logger.warning("Assistant: could not read projects for the owner introduction", exc_info=True)
        unknown.update(("projects", "repository_refs"))
    return {k: v for k, v in found.items() if not _empty(v)}
