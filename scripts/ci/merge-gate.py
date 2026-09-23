"""Aggregate merge gate: one "Merge gate" commit status for a pull request head.

The path filters in each workflow's own `on.pull_request` block decide which
suites apply, so there is no second list to keep in sync. For the PR head SHA
the gate reads the newest run of every applicable workflow and reports:

- success: every applicable workflow's newest run for this SHA succeeded;
- pending: an applicable run is queued or in progress, or not created yet;
- failure: an applicable run failed, was cancelled, skipped or timed out, no
  run exists for this SHA once the start-up grace period ends, or a suite
  gated behind the ci:full label applies and the label is missing.

Runs are triggered by pull_request_target (every push, reopen and label change)
and by workflow_run (each watched workflow completing). Both run the default
branch's copy of this script and never check out or execute PR code: the PR's
workflow files are read through the API as data.

Local dry run (read-only, prints the verdict, writes nothing):
    GITHUB_TOKEN=$(gh auth token) python scripts/ci/merge-gate.py --pr 1234 --dry-run
"""
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

CONTEXT = "Merge gate"
FULL_LABEL = "ci:full"
READINESS_SCRIPT = "scripts/ci/require-full-ci.py"
DEFAULT_TYPES = ("opened", "synchronize", "reopened")
# A manual dispatch of a suite on the PR branch counts as evidence for the same
# SHA (used when GitHub skipped a path-filtered run, for example over 300 files).
ACCEPTED_EVENTS = ("pull_request", "workflow_dispatch")
LABEL_ACTIONS = ("labeled", "unlabeled")
GRACE_SECONDS = 180
POLL_SECONDS = 15
CLOCK_SKEW = timedelta(seconds=5)
WORKFLOW_DIR = ".github/workflows"


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------

def glob_regex(pattern):
    """Translate a GitHub Actions filter pattern into an anchored regex.

    `*` stops at `/`, `**` crosses directories, `?` and `+` quantify the
    preceding character, and `[...]` is a character class, as documented in
    GitHub's filter pattern cheat sheet.
    """
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char in "?+":
            out.append(char)
            i += 1
        elif char == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(char))
                i += 1
            else:
                out.append(pattern[i:end + 1])
                i = end + 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


def filter_matches(patterns, value):
    """Ordered include/exclude matching; a later `!pattern` excludes again."""
    matched = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        if glob_regex(pattern[1:] if negated else pattern).match(value):
            matched = not negated
    return matched


def as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def pull_request_trigger(config):
    """Return the workflow's pull_request block as a dict, or None."""
    if not isinstance(config, dict):
        return None
    on = config.get("on", config.get(True))
    if isinstance(on, str):
        on = {on: {}}
    elif isinstance(on, list):
        on = {name: {} for name in on}
    if not isinstance(on, dict) or "pull_request" not in on:
        return None
    trigger = on["pull_request"]
    return trigger if isinstance(trigger, dict) else {}


def is_full_gated(config):
    for job in (config.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if READINESS_SCRIPT in str((step or {}).get("run", "")):
                return True
    return False


@dataclass(frozen=True)
class Suite:
    path: str
    name: str
    types: tuple
    full_gated: bool


def applicable_suites(workflows, changed_files, base_ref):
    """Suites whose pull_request trigger fires for this PR.

    workflows maps ".github/workflows/x.yml" to parsed YAML (BaseLoader).
    changed_files includes previous names of renamed files.
    """
    suites = []
    for path in sorted(workflows):
        config = workflows[path]
        trigger = pull_request_trigger(config)
        if trigger is None:
            continue
        branches = as_list(trigger.get("branches"))
        if branches and not filter_matches(branches, base_ref):
            continue
        ignored_branches = as_list(trigger.get("branches-ignore"))
        if ignored_branches and filter_matches(ignored_branches, base_ref):
            continue
        paths = as_list(trigger.get("paths"))
        ignored = as_list(trigger.get("paths-ignore"))
        if paths and not any(filter_matches(paths, name) for name in changed_files):
            continue
        if ignored and all(filter_matches(ignored, name) for name in changed_files):
            continue
        suites.append(Suite(
            path=path,
            name=str(config.get("name") or path),
            types=tuple(as_list(trigger.get("types")) or DEFAULT_TYPES),
            full_gated=is_full_gated(config),
        ))
    return suites


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def run_path(run):
    return str(run.get("path") or "").split("@", 1)[0]


@dataclass
class SuiteResult:
    suite: Suite
    state: str
    detail: str
    url: str = ""
    not_started: bool = False


@dataclass
class Verdict:
    state: str
    description: str
    results: list = field(default_factory=list)

    @property
    def waiting_for_runs(self):
        return any(result.not_started for result in self.results)


def suite_result(suite, runs, head_sha, labels, grace_expired, label_action, label_changed_at):
    matching = [r for r in runs if run_path(r) == suite.path and r.get("event") in ACCEPTED_EVENTS]
    current = [r for r in matching if r.get("head_sha") == head_sha]
    if suite.full_gated and FULL_LABEL not in labels:
        return SuiteResult(suite, "failure", f"needs the {FULL_LABEL} label before merge")
    if not current:
        stale = sorted({str(r.get("head_sha", ""))[:7] for r in matching if r.get("head_sha")})
        detail = "no run for this revision"
        if stale:
            detail += " (runs exist only for " + ", ".join(stale) + ")"
        if grace_expired:
            return SuiteResult(suite, "failure", detail)
        return SuiteResult(suite, "pending", detail + "; waiting for it to start", not_started=True)
    latest = max(current, key=lambda r: (parse_time(r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
                                         int(r.get("run_number") or 0), int(r.get("run_attempt") or 0), int(r.get("id") or 0)))
    url = str(latest.get("html_url") or "")
    created = parse_time(latest.get("created_at"))
    if (label_action in LABEL_ACTIONS and label_action in suite.types and label_changed_at
            and created is not None and created < label_changed_at - CLOCK_SKEW and not grace_expired):
        return SuiteResult(suite, "pending", "waiting for the run started by the label change", url, not_started=True)
    if latest.get("status") != "completed":
        return SuiteResult(suite, "pending", str(latest.get("status") or "not completed"), url)
    conclusion = latest.get("conclusion")
    if conclusion == "success":
        return SuiteResult(suite, "success", "passed", url)
    return SuiteResult(suite, "failure", str(conclusion or "no conclusion"), url)


def decide(suites, runs, head_sha, labels, *, grace_expired, label_action=None, label_changed_at=None):
    results = [suite_result(s, runs, head_sha, set(labels), grace_expired, label_action, label_changed_at) for s in suites]
    if not results:
        return Verdict("success", "No path-filtered CI suite applies to this change.", results)
    failed = [r for r in results if r.state == "failure"]
    pending = [r for r in results if r.state == "pending"]
    if failed:
        return Verdict("failure", summarize("Blocked", failed), results)
    if pending:
        return Verdict("pending", summarize("Waiting", pending), results)
    return Verdict("success", f"All {len(results)} applicable suite(s) passed for this revision.", results)


def summarize(prefix, results):
    text = prefix + ": " + "; ".join(f"{r.suite.name} {r.detail}" for r in results)
    return text if len(text) <= 140 else text[:137] + "..."


def combine(verdicts):
    """Several open PRs can share one head SHA; the status is per SHA."""
    for state in ("failure", "pending", "success"):
        for verdict in verdicts:
            if verdict.state == state:
                return verdict
    return None


def render_summary(head_sha, verdict):
    lines = [f"### {CONTEXT}: {verdict.state}", "", f"Revision `{head_sha}`", "", verdict.description, ""]
    if verdict.results:
        lines += ["| Suite | Workflow | State | Detail |", "| --- | --- | --- | --- |"]
        for r in verdict.results:
            detail = f"[{r.detail}]({r.url})" if r.url else r.detail
            lines.append(f"| {r.suite.name} | `{r.suite.path}` | {r.state} | {detail} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# GitHub access
# ---------------------------------------------------------------------------

class GitHub:
    def __init__(self, token, api_url="https://api.github.com"):
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request(self, method, path, params=None, body=None):
        url = path if path.startswith("http") else f"{self.api_url}/{path.lstrip('/')}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-shuffle-merge-gate",
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read()
                    return json.loads(payload) if payload else None, response.headers
            except urllib.error.HTTPError as error:
                if error.code < 500 or attempt == 2:
                    raise RuntimeError(f"{method} {url}: HTTP {error.code} {error.read()[:300]!r}") from error
            except urllib.error.URLError:
                if attempt == 2:
                    raise
            time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def get(self, path, params=None):
        return self.request("GET", path, params)[0]

    def paginate(self, path, params=None, key=None):
        items, url, query = [], path, dict(params or {}, per_page=100)
        while url:
            payload, headers = self.request("GET", url, query)
            items.extend(payload[key] if key else payload)
            url, query = next_link(headers.get("Link", "")), None
        return items

    def graphql(self, query, variables):
        payload, _ = self.request("POST", "graphql", body={"query": query, "variables": variables})
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL error: {payload['errors']}")
        return payload["data"]

    def set_status(self, repo, sha, state, description, target_url):
        self.request("POST", f"repos/{repo}/statuses/{sha}", body={
            "state": state, "context": CONTEXT, "description": description[:140], "target_url": target_url or None,
        })


def next_link(header):
    for part in header.split(","):
        section = part.split(";")
        if len(section) > 1 and section[1].strip() == 'rel="next"':
            return section[0].strip().strip("<>")
    return None


WORKFLOW_QUERY = """
query($owner: String!, $name: String!, $commit: String!, $tree: String!) {
  repository(owner: $owner, name: $name) {
    commit: object(expression: $commit) { ... on Commit { parents(first: 2) { nodes { oid } } } }
    tree: object(expression: $tree) {
      ... on Tree { entries { name type object { ... on Blob { text isTruncated isBinary } } } }
    }
  }
}
"""


def fetch_workflows(api, repo, ref):
    owner, name = repo.split("/", 1)
    data = api.graphql(WORKFLOW_QUERY, {"owner": owner, "name": name, "commit": ref, "tree": f"{ref}:{WORKFLOW_DIR}"})
    repository = data["repository"]
    tree = repository.get("tree") or {}
    workflows = {}
    for entry in tree.get("entries") or []:
        if entry.get("type") != "blob" or not entry["name"].endswith((".yml", ".yaml")):
            continue
        blob = entry.get("object") or {}
        if blob.get("isTruncated") or blob.get("isBinary") or blob.get("text") is None:
            raise RuntimeError(f"Cannot read {WORKFLOW_DIR}/{entry['name']} at {ref}")
        workflows[f"{WORKFLOW_DIR}/{entry['name']}"] = yaml.load(blob["text"], Loader=yaml.BaseLoader)
    parents = [node["oid"] for node in ((repository.get("commit") or {}).get("parents") or {}).get("nodes", [])]
    return workflows, parents


def workflows_for(api, repo, pull):
    """Read workflow files from the PR's test merge commit, as pull_request runs do.

    The merge commit is used only when it was built from the current head;
    otherwise (not computed yet, or conflicting) the head commit is used.
    """
    head = pull["head"]["sha"]
    merge = pull.get("merge_commit_sha")
    if merge:
        workflows, parents = fetch_workflows(api, repo, merge)
        if head in parents:
            return workflows
    return fetch_workflows(api, repo, head)[0]


def with_known_runs(listed, known):
    """Add runs the event already delivered to the listed ones.

    The ``head_sha`` run search lags: evaluated straight after a run
    completed, it returned no run at all for that suite, and nothing fired
    afterwards to correct the resulting failure. The completed run in the
    event payload is authoritative for its own id.
    """
    known = [r for r in known if r.get("id") is not None]
    ids = {r["id"] for r in known}
    return [r for r in listed if r.get("id") not in ids] + known


def evaluate_pr(api, repo, number, *, expected_sha=None, label_action=None, label_changed_at=None,
                grace_seconds=0, poll_seconds=POLL_SECONDS, sleep=time.sleep, clock=time.monotonic,
                include_closed=False, known_runs=()):
    """Return (head_sha, Verdict), or None when the PR is closed or has moved on."""
    pull = api.get(f"repos/{repo}/pulls/{number}")
    if pull.get("state") != "open" and not include_closed:
        return None
    head_sha = pull["head"]["sha"]
    if expected_sha and head_sha != expected_sha:
        return None
    if pull.get("mergeable") is False:
        return head_sha, Verdict("failure", "Resolve merge conflicts; GitHub starts no pull_request workflows while the PR conflicts.")
    files = api.paginate(f"repos/{repo}/pulls/{number}/files")
    if len(files) < int(pull.get("changed_files") or 0):
        return head_sha, Verdict("failure", f"Only {len(files)} of {pull['changed_files']} changed files are listable; run full CI manually and review.")
    names = sorted({f["filename"] for f in files} | {f["previous_filename"] for f in files if f.get("previous_filename")})
    suites = applicable_suites(workflows_for(api, repo, pull), names, pull["base"]["ref"])
    labels = [label["name"] for label in pull.get("labels") or []]
    deadline = clock() + grace_seconds
    while True:
        expired = clock() >= deadline
        runs = api.paginate(f"repos/{repo}/actions/runs", {"head_sha": head_sha}, key="workflow_runs") if suites else []
        runs = with_known_runs(runs, [r for r in known_runs if r.get("head_sha") == head_sha]) if suites else []
        verdict = decide(suites, runs, head_sha, labels, grace_expired=expired,
                         label_action=label_action, label_changed_at=label_changed_at)
        if expired or not verdict.waiting_for_runs:
            return head_sha, verdict
        sleep(poll_seconds)


def open_prs_for_sha(api, repo, sha, hinted):
    numbers = [p["number"] for p in hinted or [] if p.get("head", {}).get("sha") in (None, sha)]
    if not numbers:
        numbers = [p["number"] for p in api.get(f"repos/{repo}/commits/{sha}/pulls") or []
                   if p.get("state") == "open" and p.get("head", {}).get("sha") == sha]
    return sorted(set(numbers))


def run(api, repo, event_name, payload, *, dry_run=False, run_url="", summary_path=None,
        sleep=time.sleep, clock=time.monotonic, grace_seconds=GRACE_SECONDS):
    """Evaluate the PR(s) named by an event and publish one status per SHA."""
    if event_name == "pull_request_target":
        pull = payload["pull_request"]
        sha, numbers = pull["head"]["sha"], [pull["number"]]
        action = payload.get("action")
        options = {"expected_sha": sha, "grace_seconds": grace_seconds}
        if action in LABEL_ACTIONS:
            options.update(label_action=action, label_changed_at=parse_time(pull.get("updated_at")))
        if not dry_run:
            api.set_status(repo, sha, "pending", "Evaluating applicable CI suites.", run_url)
    elif event_name == "workflow_run":
        workflow_run = payload["workflow_run"]
        if workflow_run.get("event") not in ACCEPTED_EVENTS:
            print(f"Ignoring {workflow_run.get('event')} run.")
            return None
        sha = workflow_run["head_sha"]
        numbers = open_prs_for_sha(api, repo, sha, workflow_run.get("pull_requests"))
        options = {"expected_sha": sha, "known_runs": [workflow_run]}
    elif event_name == "workflow_dispatch":
        sha, numbers, options = None, [int(payload["pr_number"])], {}
    elif event_name == "local":
        # Read-only replay; closed PRs are allowed so past PRs can be checked.
        sha, numbers, options = None, [int(payload["pr_number"])], {"include_closed": True}
        dry_run = True
    else:
        raise SystemExit(f"Unsupported event {event_name}")

    evaluated = [r for r in (evaluate_pr(api, repo, n, sleep=sleep, clock=clock, **options) for n in numbers) if r]
    if not evaluated:
        print(f"No open pull request has head {sha}; nothing to report.")
        return None
    sha = evaluated[0][0]
    verdict = combine([v for s, v in evaluated if s == sha])
    report = render_summary(sha, verdict)
    print(report)
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(report)
    if not dry_run:
        api.set_status(repo, sha, verdict.state, verdict.description, run_url)
    return sha, verdict


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, help="Evaluate this PR instead of the triggering event")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "HobbyCoders/ai-shuffle"))
    parser.add_argument("--dry-run", action="store_true", help="Print the verdict without writing a status")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    api = GitHub(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    if args.pr:
        event_name, payload = "local", {"pr_number": args.pr}
    else:
        event_name = os.environ["GITHUB_EVENT_NAME"]
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        if event_name == "workflow_dispatch":
            payload = {"pr_number": payload["inputs"]["pr_number"]}
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{args.repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    run(api, args.repo, event_name, payload, dry_run=args.dry_run, run_url=run_url,
        summary_path=os.environ.get("GITHUB_STEP_SUMMARY"))
    # The verdict lives in the commit status. This job succeeds unless the
    # evaluation itself errored, so workflow_run runs do not paint main red.
    return 0


if __name__ == "__main__":
    sys.exit(main())
