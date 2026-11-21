"""Best-effort, main-only issue reporting from structured journey retry receipts.

One retry is recorded in a closed ledger issue, not reported. A case gets its
own issue (or a closed one reopens) only after THRESHOLD runs inside WINDOW_DAYS.

No logs, error details, paths or screenshots enter an issue. Decisions are pure;
GitHub access uses bounded gh calls, never a shell or artifact-supplied commands.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

# Distinct push-to-main runs, this one included, before a case without an open
# issue is reported. 1 reports every occurrence and uses no ledger.
THRESHOLD = 3
WINDOW_DAYS = 14
LEDGER_TITLE = "UI journey retries on main (occurrence ledger)"
LEDGER_MARKER = "<!-- ui-journey-flake-ledger:v1 -->"
LEDGER_BODY = "\n".join([
    "CI records here each UI journey case that passed only on its automatic retry in a push-to-main run.",
    f"A case gets its own issue once that happens in {THRESHOLD} runs within {WINDOW_DAYS} days.",
    "This issue stays closed: it is a record, not a task.", "", LEDGER_MARKER,
])
LEDGER_CHUNK = 50


@dataclass(frozen=True)
class FlakyCase:
    journey: str
    case: str
    first: str
    retry: str

    @property
    def title(self) -> str:
        return f"Flaky UI journey: {self.journey} / {self.case} passed only on retry"

    @property
    def identity_hash(self) -> str:
        identity = json.dumps([self.journey, self.case], separators=(",", ":"))
        return hashlib.sha256(identity.encode()).hexdigest()

    @property
    def marker(self) -> str:
        return f"<!-- ui-journey-flake:v1:{self.identity_hash} -->"


@dataclass(frozen=True)
class Run:
    repository: str
    id: int
    commit: str
    # When the workflow run was first created; a rerun keeps it.
    created: datetime | None = None

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repository}/actions/runs/{self.id}"


def run_marker(case: FlakyCase, run: Run) -> str:
    # Attempts of the same workflow run are NOT new occurrences of this case.
    return f"<!-- ui-journey-flake-run:{run.id}:{case.identity_hash} -->"


@dataclass(frozen=True)
class Decision:
    action: str
    title: str = ""
    body: str = ""


def _recovered_row(row: dict) -> FlakyCase | None:
    if not isinstance(row, dict):
        raise ValueError("malformed row")
    if row.get("status") != "passed-on-retry":
        return None
    journey, case = row.get("journey"), row.get("case")
    if not isinstance(journey, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", journey):
        raise ValueError("invalid journey ID")
    # @ is only allowed inside a case ID, not as an @-mention. Markdown,
    # controls and arbitrary receipt text cannot enter titles or bodies.
    if not isinstance(case, str) or not (case == "(whole journey)" or re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.:+/-]*@[a-zA-Z0-9][a-zA-Z0-9_.:+-]*", case)) or len(case) > 120:
        raise ValueError("invalid case ID")
    attempts = row.get("attempts")
    if (not isinstance(attempts, list) or len(attempts) != 2
            or any(not isinstance(a, dict) for a in attempts)):
        raise ValueError("missing attempt outcomes")
    first, retry = (a.get("status") for a in attempts)
    # known-bug is an accepted runner verdict, not a new definition of pass.
    if first not in {"fail", "crashed"} or retry not in {"pass", "known-bug"}:
        raise ValueError("inconsistent retry outcomes")
    return FlakyCase(journey, case, first, retry)


def recovered(receipt: dict) -> list[FlakyCase]:
    """Keep every valid recovered row even if a sibling row is malformed."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("rows"), list):
        raise ValueError("missing rows")
    cases = []
    for row in receipt["rows"]:
        try:
            case = _recovered_row(row)
        except (ValueError, TypeError):
            warning("A malformed journey retry row was skipped.")
            continue
        if case is not None:
            cases.append(case)
    return cases


def eligible_run(repository: str, run_id: int, metadata: dict) -> Run | None:
    if (metadata.get("id") != run_id or metadata.get("event") != "push"
            or metadata.get("head_branch") != "main" or metadata.get("name") != "CI"
            or metadata.get("path") != ".github/workflows/ci.yml"
            or metadata.get("repository", {}).get("full_name") != repository
            or metadata.get("head_repository", {}).get("full_name") != repository):
        return None
    commit = metadata.get("head_sha", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid commit")
    created = _time(metadata.get("created_at"))
    if created is None:
        raise ValueError("invalid creation time")
    return Run(repository, run_id, commit, created)


def bot_authored(entry: dict) -> bool:
    return (entry.get("user") or {}).get("login") == "github-actions[bot]"


def matches_case(case: FlakyCase, issue: dict) -> bool:
    """Trust complete body markers; otherwise recover only by exact title."""
    if not bot_authored(issue):
        return False
    markers = re.findall(r"<!-- ui-journey-flake:v1:[0-9a-f]{64} -->", issue.get("body") or "")
    return case.marker in markers if markers else issue.get("title") == case.title


def already_reported(case: FlakyCase, run: Run, entries: list[dict]) -> bool:
    marker = run_marker(case, run)
    return any(bot_authored(entry) and marker in (entry.get("body") or "") for entry in entries)


def _time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def ledger_issue(issues: list[dict]) -> dict | None:
    found = [issue for issue in issues if bot_authored(issue) and LEDGER_MARKER in (issue.get("body") or "")]
    return min(found, key=lambda issue: issue["number"]) if found else None


def ledger_runs(case: FlakyCase, entries: list[dict], after: datetime) -> set[int]:
    """Runs the bot's own ledger comments recorded for this case after `after`."""
    pattern = re.compile(rf"<!-- ui-journey-flake-run:([1-9][0-9]*):{case.identity_hash} -->")
    runs = set()
    for entry in entries:
        created = _time(entry.get("created_at"))
        if bot_authored(entry) and created is not None and created > after:
            runs.update(int(run_id) for run_id in pattern.findall(entry.get("body") or ""))
    return runs


def ledger_comment(run: Run, cases: list[FlakyCase]) -> str:
    return "\n".join([
        f"Main run [{run.id}]({run.url}) at `{run.commit}` accepted these UI journey cases only on retry:", "",
        *[f"- `{case.journey}` / `{case.case}`: `{case.first}` then `{case.retry}`" for case in cases], "",
        *[run_marker(case, run) for case in cases],
    ])


def decide(case: FlakyCase, run: Run, issue: dict | None, comments: list[dict],
           occurrences: int | None = None) -> Decision:
    """Only the bot's own issue body/comments can deduplicate a case in a run.

    `occurrences` is how many main runs of this case count toward THRESHOLD,
    this one included: recorded inside the window and, for a closed issue,
    after it was closed. None applies no threshold.
    """
    if issue is not None and not bot_authored(issue):
        issue = None
    if issue is not None and already_reported(case, run, [issue, *comments]):
        return Decision("skip")
    if occurrences is not None and occurrences < THRESHOLD and (issue is None or issue["state"] == "closed"):
        return Decision("record")
    action = "create" if issue is None else "reopen" if issue["state"] == "closed" else "comment"
    lead = ("This flaky UI journey came back after this issue was closed; reopening it."
            if action == "reopen" else "This UI journey failed initially and was accepted only on its automatic retry.")
    body = "\n".join([
        lead, "", f"- Journey: `{case.journey}`", f"- Case (including viewport/variant): `{case.case}`",
        f"- Attempt 1: `{case.first}`", f"- Attempt 2: `{case.retry}`",
        f"- Run: [{run.id}]({run.url})", f"- Commit: `{run.commit}`", "",
        "Only structured outcomes are reported here; logs and error text are not copied into public issues.",
        "", case.marker, run_marker(case, run),
    ])
    return Decision(action, case.title, body)


def warning(message: str) -> None:
    # Callers supply fixed messages, never API responses or untrusted receipt text.
    print(f"::warning title=UI journey issue reporting::{message}", flush=True)


class GitHub:
    def __init__(self, repository: str):
        self.repository = repository

    def request(self, route: str, *, method: str = "GET", data=None, pages: bool = False, binary: bool = False):
        argv = ["gh", "api", f"repos/{self.repository}/{route}", "--method", method]
        if pages:
            argv += ["--paginate", "--slurp"]
        if data is not None:
            argv += ["--input", "-"]
        result = subprocess.run(argv, input=None if data is None else json.dumps(data).encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=False)
        if result.returncode:
            raise RuntimeError("GitHub API request failed")
        return result.stdout if binary else json.loads(result.stdout)

    def issues(self) -> list[dict]:
        # REST listing (not eventually indexed search) also finds just-created,
        # closed and unlabelled matches. Fetch once for all cases in the run.
        return [issue for page in self.request("issues?state=all&per_page=100", pages=True)
                for issue in page if "pull_request" not in issue]

    def comments(self, number: int, since: str = "") -> list[dict]:
        route = f"issues/{number}/comments?per_page=100" + (f"&since={since}" if since else "")
        return [comment for page in self.request(route, pages=True) for comment in page]

    def labels(self) -> list[str]:
        names = {label["name"] for page in self.request("labels?per_page=100", pages=True) for label in page}
        return ["bug"] if "bug" in names else []

    def receipts(self, run_id: int):
        artifacts = {}
        for page in self.request(f"actions/runs/{run_id}/artifacts?per_page=100", pages=True):
            for artifact in page["artifacts"]:
                name = artifact["name"]
                if re.fullmatch(r"ui-journeys-[1-9][0-9]*", name):
                    if name not in artifacts or artifact["id"] > artifacts[name]["id"]:
                        artifacts[name] = artifact
        for artifact in artifacts.values():
            try:
                if artifact["expired"]:
                    raise ValueError("expired artifact")
                raw = self.request(f"actions/artifacts/{int(artifact['id'])}/zip", binary=True)
                # Read ONLY the root receipt, never nested per-attempt summaries,
                # never extract paths or execute anything supplied by an artifact.
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    if "summary.json" not in archive.namelist():
                        print("Journey artifact has no root retry receipt; no accepted retry was recorded.", flush=True)
                        continue
                    if archive.getinfo("summary.json").file_size > 2_000_000:
                        raise ValueError("oversized receipt")
                    yield json.loads(archive.read("summary.json"))
            except Exception:
                warning("A journey artifact could not supply a usable retry receipt; skipped it.")


def read_ledger(api: GitHub, run: Run, cases: list[FlakyCase], issues: list[dict],
                since: datetime, dry_run: bool) -> tuple[list[dict], set[str]]:
    """Record this run's cases in the ledger.

    Returns its comments since `since` and the markers of the cases this call
    recorded (or, on a dry run, would have recorded) for the first time.
    """
    ledger = ledger_issue(issues)
    if ledger is None:
        if dry_run:
            return [], {case.marker for case in cases}
        ledger = api.request("issues", method="POST", data={
            "title": LEDGER_TITLE, "body": LEDGER_BODY, "labels": []})
    if ledger.get("state") != "closed" and not dry_run:
        api.request(f"issues/{ledger['number']}", method="PATCH",
                    data={"state": "closed", "state_reason": "not_planned"})
    entries = api.comments(ledger["number"], since=since.strftime("%Y-%m-%dT%H:%M:%SZ"))
    # Attempts of the same workflow run are recorded once.
    new = [case for case in cases if not already_reported(case, run, entries)]
    if not dry_run:
        for start in range(0, len(new), LEDGER_CHUNK):
            api.request(f"issues/{ledger['number']}/comments", method="POST",
                        data={"body": ledger_comment(run, new[start:start + LEDGER_CHUNK])})
    return entries, {case.marker for case in new}


def report(api: GitHub, run: Run, receipts, *, dry_run: bool = False, now: datetime | None = None) -> None:
    cases = {}
    for receipt in receipts:
        try:
            for case in recovered(receipt):
                cases[case.marker] = case
        except (ValueError, TypeError):
            warning("A malformed journey retry receipt was skipped.")
    print(f"Recovered UI journey cases: {len(cases)}", flush=True)
    if not cases:
        return
    issues = api.issues()
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_DAYS)
    ledger, recorded = [], set()
    if THRESHOLD > 1:
        try:
            # A rerun's own entry can be older than the window but never older
            # than its run, so read back to whichever is earlier.
            ledger, recorded = read_ledger(api, run, list(cases.values()), issues,
                                           min(since, run.created) if run.created else since, dry_run)
        except Exception:
            # Without the ledger no occurrence counts, so nothing new is opened.
            warning("The journey retry ledger could not be read or updated; earlier occurrences are not counted.")
    labels = None
    for case in cases.values():
        try:
            matches = [issue for issue in issues if matches_case(case, issue)]
            if len(matches) > 1:
                warning("Multiple bot issues match a flaky case; using the lowest-numbered open issue, else the lowest-numbered closed issue.")
            # Closing a duplicate can change the preferred target between run
            # attempts. Deduplicate across ALL matches before choosing any target.
            histories = {match["number"]: api.comments(match["number"]) for match in matches}
            entries = [entry for match in matches for entry in [match, *histories[match["number"]]]]
            if already_reported(case, run, entries):
                issue, decision = None, Decision("skip")
            else:
                issue = min(matches, key=lambda item: (item["state"] != "open", item["number"])) if matches else None
                closed = _time(issue.get("closed_at")) if issue and issue["state"] == "closed" else None
                occurrences = None
                if THRESHOLD > 1:
                    # This run counts when it was recorded just now, or when its
                    # earlier ledger entry is itself after the cutoff (a rerun).
                    counted = ledger_runs(case, ledger, max(since, closed) if closed else since)
                    occurrences = len(counted | ({run.id} if case.marker in recorded else set()))
                decision = decide(case, run, issue, histories[issue["number"]] if issue else [], occurrences)
            print(json.dumps({"action": decision.action, "journey": case.journey, "case": case.case,
                              "run": run.url, "commit": run.commit, "dry_run": dry_run}), flush=True)
            if dry_run or decision.action in {"skip", "record"}:
                continue
            if decision.action == "create":
                if labels is None:
                    labels = api.labels()
                issues.append(api.request("issues", method="POST", data={
                    "title": decision.title, "body": decision.body, "labels": labels}))
            else:
                number = issue["number"]
                if decision.action == "reopen":
                    api.request(f"issues/{number}", method="PATCH", data={"state": "open"})
                api.request(f"issues/{number}/comments", method="POST", data={"body": decision.body})
        except Exception:
            warning("GitHub could not report a recovered journey case; suite and publication results are unchanged.")


def local_receipts(directory: Path):
    paths = list(directory.glob("ui-journeys-*/summary.json"))
    if not paths:
        warning("No local journey retry receipts were found.")
    for path in paths:
        try:
            yield json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            warning("A local journey retry receipt was missing or malformed; skipped it.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", type=int, default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--dry-run", action="store_true", help="Read real GitHub state but perform no writes")
    parser.add_argument("--receipts", type=Path, help="Use already-downloaded artifacts (dry-run only)")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", args.repository) or not args.run_id or args.run_id < 1:
            raise ValueError("missing repository or run ID")
        if args.receipts and not args.dry_run:
            raise ValueError("local receipts are only supported for read-only probes")
        api = GitHub(args.repository)
        run = eligible_run(args.repository, args.run_id, api.request(f"actions/runs/{args.run_id}"))
        if run is None:
            if args.dry_run:
                print("Not a push-to-main CI run; no issues reported.")
            else:
                warning("Not a push-to-main CI run; issue reporting is disabled.")
            return 0
        receipts = local_receipts(args.receipts) if args.receipts else api.receipts(run.id)
        report(api, run, receipts, dry_run=args.dry_run)
    except Exception:
        warning("Journey issue reporting failed; suite and publication results are unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
