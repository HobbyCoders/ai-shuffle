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
            ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"], text=True, encoding="utf-8", timeout=20
        )
        return json.loads(raw)


def published_edge_revision() -> str:
    """Read the real signed edge pointer, never a successful-but-skipped Actions run."""
    import httpx
    # Match docker/application_updates.py::ContainerUpdates.fetch: no environment proxy,
    # no redirects, bounded streaming. Edge is public and needs no license lease.
    raw = bytearray()
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
        with client.stream("GET", "https://hobbycoders.com/v1/desktop/manifest/edge") as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 32768:
                    raise ValueError("Edge manifest exceeds limits")
    revision = verify_edge_manifest(raw)
    # Keep the envelope, not just its SHA: offline use must reverify the signature
    # and the updater's complete manifest contract with the current public key.
    try:
        cache_edge_manifest(raw)
    except OSError as error:
        print(f"WARNING: could not cache verified edge manifest ({type(error).__name__}).")
    return revision


def verify_edge_manifest(raw) -> str:
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("publication_manifest_verifier", root / "docker/application_updates.py")
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    manifest = updater.verify_manifest(raw, (root / "desktop/build/application-update-key.pem").read_bytes())
    if manifest.get("channel") != "edge":
        raise ValueError("Expected an edge manifest")
    return manifest["revision"]


def edge_cache_path() -> Path:
    return Path.home() / ".cache" / "ai-shuffle" / "verified-edge-manifest.json"


def cache_edge_manifest(raw) -> None:
    import tempfile

    path = edge_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".verified-edge-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cached_edge_revision() -> str:
    with edge_cache_path().open("rb") as stream:
        return verify_edge_manifest(stream.read(32769))


def load_update_reach():
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("publication_update_reach", root / "scripts/ci/update_reach.py")
    reach = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reach)
    return reach


def update_reach_readiness(revision, *, api=None, base=None):
    """Strict backstop before local/full-suite is posted, independent of PR draft state.

    Inspect each first-parent main change since the *published* edge pointer.
    A label on an unrelated PR cannot accept another PR's unreachable change.
    In-reach changes do not need GitHub PR lookups at all.
    """
    reach = load_update_reach()
    api = api or GitHub()
    try:
        warning = ""
        if base is None:
            try:
                base = published_edge_revision()
            except Exception as error:
                warning = f"WARNING: published edge pointer unavailable ({type(error).__name__}); "
                try:
                    base = cached_edge_revision()
                    warning += f"using the reverified cached signed publication ({base[:9]}).\n"
                except Exception:
                    # No publication evidence at all: never skip an unpublished
                    # change by guessing a baseline from local tags or HEAD^.
                    # A shallow boundary is not repository bootstrap.
                    if reach.git("rev-parse", "--is-shallow-repository") != "false":
                        return False, warning + ("no verified cached publication and incomplete Git history; "
                                                 "run `git fetch --unshallow origin` and retry attestation.")
                    base = reach.git("rev-list", "--first-parent", revision).splitlines()[-1]
                    warning += ("no verified cached publication; checking all first-parent changes "
                                "since repository bootstrap. Local tags are not publication evidence.\n")
        # Refuse unknown/non-ancestor pointers instead of silently checking an empty range.
        if base not in reach.git("rev-list", "--first-parent", revision).splitlines():
            return False, "Update reach refused: published edge is not on this main history; fetch origin/main."
        commits = reach.git("rev-list", "--first-parent", "--reverse", f"{base}..{revision}").splitlines()
        refusals = []
        for commit in commits:
            parent = reach.git("rev-parse", f"{commit}^1")
            changed = reach.git("diff", "--name-only", parent, commit).splitlines()
            groups = reach.classify(changed, reach.reader(parent), reach.reader(commit))
            failing = reach.verdict(groups)
            if not failing:
                continue
            # One request per offending commit, regardless of how many groups fail.
            prs = api(f"commits/{commit}/pulls?per_page=100")
            if not isinstance(prs, list) or len(prs) >= 100:
                raise ValueError("Incomplete PR evidence")
            merged = [pr for pr in prs if pr.get("merged_at") and pr.get("base", {}).get("ref") == "main"
                      and pr.get("base", {}).get("repo", {}).get("full_name") == REPOSITORY
                      and pr.get("merge_commit_sha") == commit]
            if not merged:
                refusals.append(f"{commit[:9]}: no merged main PR; needs "
                                + ", ".join(reach.LABEL + name for name in failing))
            for pr in merged:
                labels = ",".join(label["name"] for label in pr["labels"])
                missing = reach.verdict(groups, reach.allowed_by_labels(labels))
                if missing:
                    refusals.append(f"PR #{pr['number']} ({commit[:9]}): owner must add "
                                    + ", ".join(reach.LABEL + name for name in missing))
        if refusals:
            return False, warning + "OUT OF REACH — edge publication refused:\n" + "\n".join(refusals)
        return True, warning + f"Update reach accepted for {base[:9]}..{revision[:9]}."
    except Exception as error:
        # Network/auth/malformed evidence never authorizes an unreachable publication.
        # Do not echo subprocess output or response bodies (they may contain credentials).
        return False, f"Update reach refused: could not verify published edge/merged PR evidence ({type(error).__name__})."


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
