"""Authorize edge publication from an owner's local full-suite pass on main.

CI does not test code. A worker runs every suite on the exact main revision
(``python scripts/local_ci.py full --attest``) and posts the commit status
``local/full-suite``. Edge publication waits for that status, created by the
owner's account, in state success, on the first-parent history of protected main.
Only the newest attested unpublished revision is eligible; later untested changes
wait for their own exact-revision attestation. One pass covers its ancestors.

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
    def __init__(self, repository=REPOSITORY):
        self.repository = repository

    def __call__(self, endpoint):
        raw = subprocess.check_output(
            ["gh", "api", f"repos/{self.repository}/{endpoint}"], text=True, encoding="utf-8", timeout=20
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
    if manifest.get("channel") not in ("edge", "nightly"):
        raise ValueError("Expected a nightly manifest (or transition edge alias)")
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


def load_cutover():
    import importlib.util

    path = Path(__file__).with_name("public_cutover.py")
    spec = importlib.util.spec_from_file_location("publication_cutover", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def update_reach_readiness(revision, *, api=None, base=None, root=None, repository=REPOSITORY, bridge=True):
    """Strict backstop before local/full-suite is posted, independent of PR draft state.

    Inspect each first-parent main change since the *published* edge pointer.
    A label on an unrelated PR cannot accept another PR's unreachable change.
    In-reach changes do not need GitHub PR lookups at all.
    """
    reach = load_update_reach()
    api = api or GitHub(repository)
    git = reach.git if root is None else lambda *args: reach.git(*args, root=root)
    reader = reach.reader if root is None else lambda ref: reach.reader(ref, root=root)
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
                    if git("rev-parse", "--is-shallow-repository") != "false":
                        return False, warning + ("no verified cached publication and incomplete Git history; "
                                                 "run `git fetch --unshallow origin` and retry attestation.")
                    base = git("rev-list", "--first-parent", revision).splitlines()[-1]
                    if bridge and git("ls-tree", base, "--", "release/public-cutover.json"):
                        raise ValueError("Configured cutover requires a verified signed envelope")
                    warning += ("no verified cached publication; checking all first-parent changes "
                                "since repository bootstrap. Local tags are not publication evidence.\n")
        # Refuse unknown/non-ancestor pointers instead of silently checking an empty range.
        if base not in git("rev-list", "--first-parent", revision).splitlines():
            if not bridge:
                return False, "Update reach refused: published edge is not on this main history; fetch origin/main."
            envelope = edge_cache_path().read_bytes()
            if verify_edge_manifest(envelope) != base:
                raise ValueError("Cutover requires the exact reverified signed publication")
            base, _ = load_cutover().verify_bridge(
                root or reach.ROOT, revision, base, envelope, walk=update_reach_readiness)

        commits = git("rev-list", "--first-parent", "--reverse", f"{base}..{revision}").splitlines()
        refusals = []
        for commit in commits:
            parent = git("rev-parse", f"{commit}^1")
            changed = git("diff", "--name-only", parent, commit).splitlines()
            groups = reach.classify(changed, reader(parent), reader(commit))
            failing = reach.verdict(groups)
            if not failing:
                continue
            # One request per offending commit, regardless of how many groups fail.
            prs = api(f"commits/{commit}/pulls?per_page=100")
            if not isinstance(prs, list) or len(prs) >= 100:
                raise ValueError("Incomplete PR evidence")
            merged = [pr for pr in prs if pr.get("merged_at") and pr.get("base", {}).get("ref") == "main"
                      and pr.get("base", {}).get("repo", {}).get("full_name") == repository
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


def exact_main_readiness(api, revision):
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


def main_history(api):
    """Refresh objects, then walk protected main's first parents (not merged PR heads)."""
    subprocess.check_output(["git", "fetch", "--quiet", "origin", "main"], timeout=60)
    branch = api("branches/main")
    if not branch.get("protected"):
        raise ValueError("main is not protected; refusing publication")
    return subprocess.check_output(
        ["git", "rev-list", "--first-parent", branch["commit"]["sha"]],
        text=True, timeout=20).splitlines()


def unpublished_history(history, published):
    if published in history:
        return history[:history.index(published)]
    # The only permitted unrelated-history baseline is the frozen signed public
    # cutover. Its owner's success already certifies the private exact-tree/reach
    # bridge; a public runner must not need credentials for the archived repo.
    import hashlib
    initial, record = load_cutover().mapping(Path.cwd(), history[0])
    envelope = edge_cache_path().read_bytes()
    if (record and initial == history[-1] and published == record["published_revision"]
            and verify_edge_manifest(envelope) == published
            and hashlib.sha256(envelope).hexdigest() == record["envelope_sha256"]):
        return history
    raise ValueError("Published revision is not on main or the frozen cutover baseline")


def newest_attested(api, history):
    # Bound API cost and fail closed, never silently choose a candidate after an
    # unchecked newer commit. A large backlog needs a fresh tip attestation.
    for revision in history[:100]:
        status = owner_status(api, revision)
        if status and status.get("state") == "success":
            return revision, status
    return None, None


def publication_readiness(api, revision, *, published=None):
    published = published if published is not None else published_edge_revision()
    # Repeat the complete decision: main or a newer attestation may change while
    # statuses are read. The writer repeats this and CASes the actual D1 pointer.
    for _ in range(2):
        history = main_history(api)
        if revision not in history:
            return False, "Revision is not on protected main's first-parent history"
        candidates = unpublished_history(history, published)
        if revision not in candidates:
            return False, "Revision is already published or older than the published revision"
        newest, status = newest_attested(api, candidates)
        if newest != revision:
            if newest:
                return False, f"Newer attested revision {newest[:9]} takes precedence"
            return False, f"{MAIN_SUITE_REFUSAL}. Run on a clean checkout of main:\n{COMMAND}"
    if published_edge_revision() != published:
        return False, "Published pointer moved during verification; retry selection"
    return True, f"Local full suite passed on {revision[:9]}: {status.get('description', '')}"


def warning_annotation(reason):
    # Workflow command data must escape %, CR and LF; each line stays readable.
    data = reason.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning title={REFUSAL_TITLE}::{data}"


def event_allowed(env, event):
    if env.get("GITHUB_REPOSITORY") != REPOSITORY or env.get("GITHUB_REF") != "refs/heads/main":
        return False
    kind = env.get("GITHUB_EVENT_NAME")
    if kind == "schedule":
        return event.get("schedule") == "0 6 * * *"
    if kind == "workflow_dispatch":
        return (env.get("GITHUB_ACTOR_ID") == OWNER_ID and bool(env.get("GITHUB_ACTOR"))
                and env.get("GITHUB_TRIGGERING_ACTOR") == env["GITHUB_ACTOR"])
    return False


def merge_readiness(api, *, now=None):
    """Optional merge preflight: refuse a recent owner pending marker, not a stale crash."""
    from datetime import datetime, timezone

    head = api("branches/main")["commit"]["sha"]
    status = owner_status(api, head)
    if api("branches/main")["commit"]["sha"] != head:
        return False, "main moved during merge preflight; run the check again"
    if not status or status.get("state") != "pending":
        return True, "No owner full-suite run is pending on current main."
    try:
        started = datetime.fromisoformat(status["created_at"].replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - started).total_seconds()
    except (KeyError, ValueError, TypeError):
        return False, "Owner full-suite is pending with unknown age; inspect the run before merging."
    if age > 2 * 60 * 60:
        return True, "WARNING: owner pending marker is over two hours old; inspect a possibly crashed run."
    return False, (f"Owner full-suite is pending on {head[:9]}; defer the merge until it finishes. "
                   "If main moves anyway, an eligible attested ancestor can still publish.")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--merge-check", action="store_true", help="read-only preflight before gh pr merge")
    args = parser.parse_args(argv)
    if args.merge_check:
        if args.select or args.json:
            parser.error("--merge-check cannot select or publish")
        ready, reason = merge_readiness(GitHub())
        print(reason)
        return 0 if ready else 1
    env = os.environ
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if not event_allowed(env, event):
        raise ValueError("Event is not authorized for edge publication")
    revision = env.get("SOURCE_REVISION") or env["GITHUB_SHA"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid source revision")
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if checkout != revision:
        raise ValueError("Checkout does not match the exact source revision")
    api = GitHub()
    published = published_edge_revision()
    if args.select:
        revision, _ = newest_attested(api, unpublished_history(main_history(api), published))
    ready, reason = (publication_readiness(api, revision, published=published) if revision else
                     (False, f"{MAIN_SUITE_REFUSAL}. {COMMAND}"))
    result = {"ready": ready, "revision": revision or "", "published": published, "reason": reason}
    if args.json:
        print(json.dumps(result))
        return
    print(reason)
    if not ready:
        print(warning_annotation(reason))
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"ready={str(ready).lower()}\nrevision={revision or ''}\n")
    if env.get("GITHUB_STEP_SUMMARY"):
        with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(reason.replace("\n", "  \n") + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
