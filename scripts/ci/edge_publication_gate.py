"""Authorize edge publication from an owner's local full-suite pass on main.

CI does not test code. A worker runs every suite on the exact main revision
(``python scripts/local_ci.py full --attest``) and posts the commit status
``local/full-suite``. Edge publication waits for that status, created by the
owner's account, in state success, on the current head of protected main.
One pass covers every PR merged up to that revision.

Run only from a checkout of protected main. This script reads GitHub
metadata; it does not receive signing or Cloudflare keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

REPOSITORY = "HobbyCoders/ai-shuffle"
OWNER_ID = "76577322"
CONTEXT = "local/full-suite"
REFUSAL_TITLE = "Edge publication refused"
# app/core/deploy_status.py shows this refusal, with the command, to the owner.
MAIN_SUITE_REFUSAL = "Edge publication waits for a local full-suite pass on main"
COMMAND = "python scripts/local_ci.py full --attest"


class GitHub:
    def __call__(self, endpoint):
        raw = subprocess.check_output(
            ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"], text=True, encoding="utf-8"
        )
        return json.loads(raw)


def owner_status(api, revision):
    """The newest local/full-suite status the owner posted on ``revision``, or None."""
    for page in range(1, 11):
        statuses = api(f"commits/{revision}/statuses?per_page=100&page={page}")
        if not isinstance(statuses, list):
            raise ValueError("Unexpected GitHub statuses response")
        # GitHub lists statuses newest first.
        for status in statuses:
            if (status.get("context") == CONTEXT
                    and str((status.get("creator") or {}).get("id")) == OWNER_ID):
                return status
        if len(statuses) < 100:
            return None
    raise ValueError("Too many statuses; refusing partial evidence")


def publication_readiness(api, revision):
    branch = api("branches/main")
    if not branch.get("protected"):
        return False, "main is not protected; refusing publication"
    if branch["commit"]["sha"] != revision:
        return False, "main moved; a superseded revision cannot publish"
    status = owner_status(api, revision)
    if status is None:
        return False, f"{MAIN_SUITE_REFUSAL}. Run on a clean checkout of main:\n{COMMAND}"
    if status.get("state") != "success":
        detail = status.get("description") or status.get("state")
        return False, f"{MAIN_SUITE_REFUSAL}. Latest pass is {status.get('state')}: {detail}\n{COMMAND}"
    # Recheck after the API reads. The publisher also checks main before writing its pointer.
    if api("branches/main")["commit"]["sha"] != revision:
        return False, "main moved during verification; skipping superseded revision"
    return True, f"Local full suite passed on {revision[:9]}: {status.get('description', '')}"


def warning_annotation(reason):
    # Workflow command data must escape %, CR and LF; each line stays readable.
    data = reason.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning title={REFUSAL_TITLE}::{data}"


def event_allowed(env, event):
    if env.get("GITHUB_REPOSITORY") != REPOSITORY or env.get("GITHUB_REF") != "refs/heads/main":
        return False
    kind = env.get("GITHUB_EVENT_NAME")
    if kind == "push":
        return not event.get("deleted", False)
    if kind == "workflow_dispatch":
        return (env.get("GITHUB_ACTOR_ID") == OWNER_ID and bool(env.get("GITHUB_ACTOR"))
                and env.get("GITHUB_TRIGGERING_ACTOR") == env["GITHUB_ACTOR"])
    if kind == "status":
        return (event.get("context") == CONTEXT and event.get("state") == "success"
                and str((event.get("sender") or {}).get("id")) == OWNER_ID
                and event.get("sha") == env.get("GITHUB_SHA"))
    return False


def main():
    env = os.environ
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if not event_allowed(env, event):
        raise ValueError("Event is not authorized for edge publication")
    revision = env["GITHUB_SHA"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid source revision")
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if checkout != revision:
        raise ValueError("Checkout does not match the main revision")
    ready, reason = publication_readiness(GitHub(), revision)
    print(reason)
    if not ready and reason.startswith(MAIN_SUITE_REFUSAL):
        # The gate job still succeeds; this is what makes the refusal visible.
        print(warning_annotation(reason))
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"ready={str(ready).lower()}\n")
    if env.get("GITHUB_STEP_SUMMARY"):
        with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(reason.replace("\n", "  \n") + "\n")


if __name__ == "__main__":
    main()
