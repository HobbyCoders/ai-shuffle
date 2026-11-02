"""Main-only flaky-journey reporting, without a browser, database or public writes."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

from scripts.devloop import journey_issues as reporter
from scripts.devloop import journey_retry


RUN = reporter.Run("example/project", 401, "a" * 40)
CASE = reporter.FlakyCase("copy", "copy@phone-slow", "fail", "pass")
METADATA = {
    "id": RUN.id, "event": "push", "head_branch": "main", "name": "main-suite",
    "path": ".github/workflows/main-suite.yml", "head_sha": RUN.commit,
    "repository": {"full_name": RUN.repository}, "head_repository": {"full_name": RUN.repository},
}


def receipt(case=CASE, status="passed-on-retry"):
    return {"rows": [{"journey": case.journey, "case": case.case, "status": status,
                      "attempts": [{"status": case.first, "detail": "DO NOT PUBLISH: error text",
                                    "log": "logs/private-detail.log"},
                                   {"status": case.retry, "timeline": "DO NOT PUBLISH: evidence path"}]}]}


class FakeGitHub:
    def __init__(self, issues=(), comments=(), labels=("bug",)):
        self.existing = list(issues)
        self.history = list(comments)
        self.available_labels = list(labels)
        self.calls = []
        self.metadata = dict(METADATA)
        self.summaries = [receipt()]

    def issues(self):
        return self.existing.copy()

    def comments(self, number):
        return self.history

    def labels(self):
        return self.available_labels

    def receipts(self, run_id):
        return self.summaries

    def request(self, route, *, method="GET", data=None):
        self.calls.append((route, method, data))
        if route == f"actions/runs/{RUN.id}":
            return self.metadata
        if route == "issues":
            return {"number": 17, "state": "open", "user": {"login": "github-actions[bot]"}, **data}
        return {"body": data.get("body", "")}


def existing(state="open", body=None, *, number=17, login="github-actions[bot]"):
    return {"number": number, "state": state, "body": CASE.marker if body is None else body,
            "title": CASE.title, "user": {"login": login}}


def test_new_issue_contains_only_structured_fields_and_existing_label():
    api = FakeGitHub()
    reporter.report(api, RUN, [receipt()])
    route, method, data = api.calls[0]
    assert (route, method) == ("issues", "POST")
    assert data["title"] == "Flaky UI journey: copy / copy@phone-slow passed only on retry"
    assert data["labels"] == ["bug"]
    for value in [CASE.marker, RUN.marker, RUN.commit, RUN.url, "`fail`", "`pass`", CASE.case]:
        assert value in data["body"]
    assert "DO NOT PUBLISH" not in json.dumps(data)
    assert "private-detail" not in json.dumps(data)


def test_label_is_optional_and_no_new_label_is_created():
    api = FakeGitHub(labels=())
    reporter.report(api, RUN, [receipt()])
    assert api.calls[0][2]["labels"] == []


@pytest.mark.parametrize("state,methods", [("open", ["POST"]), ("closed", ["PATCH", "POST"])])
def test_recurrence_comments_and_reopens_closed_issue(state, methods):
    api = FakeGitHub([existing(state)], [{"body": "Previous occurrence " + reporter.Run(RUN.repository, 400, RUN.commit).marker}])
    reporter.report(api, RUN, [receipt()])
    assert [method for _, method, _ in api.calls] == methods
    if state == "closed":
        assert api.calls[0] == ("issues/17", "PATCH", {"state": "open"})
        assert "came back" in api.calls[-1][2]["body"]
    assert api.calls[-1][0] == "issues/17/comments"
    assert RUN.marker in api.calls[-1][2]["body"]
    assert RUN.url in api.calls[-1][2]["body"]


@pytest.mark.parametrize("state", ["open", "closed"])
@pytest.mark.parametrize("where", ["body", "comment"])
def test_same_run_dedupes_even_after_closure(state, where):
    issue = existing(state, CASE.marker + (RUN.marker if where == "body" else ""))
    api = FakeGitHub([issue], [{"body": RUN.marker, "user": {"login": "github-actions[bot]"}}]
                     if where == "comment" else [])
    reporter.report(api, RUN, [receipt()])
    assert api.calls == []


def test_stranger_issue_cannot_claim_case_or_run_markers():
    issue = existing(body=CASE.marker + RUN.marker, login="someone-else")
    api = FakeGitHub([issue])
    reporter.report(api, RUN, [receipt()])
    assert api.calls[0][0:2] == ("issues", "POST")
    assert reporter.decide(CASE, RUN, issue, []).action == "create"


@pytest.mark.parametrize("user", [{"login": "someone-else"}, {}, None])
def test_stranger_comment_cannot_suppress_reporting(user):
    comment = {"body": CASE.marker + RUN.marker, "user": user}
    api = FakeGitHub([existing()], [comment])
    reporter.report(api, RUN, [receipt()])
    assert api.calls[0][0:2] == ("issues/17/comments", "POST")
    assert reporter.decide(CASE, RUN, existing(), [comment]).action == "comment"


@pytest.mark.parametrize("issues,target,methods", [
    ([existing(number=30), existing(number=12)], 12, ["POST"]),
    ([existing(number=30), existing("closed", number=12)], 30, ["POST"]),
    ([existing("closed", number=30), existing("closed", number=12)], 12, ["PATCH", "POST"]),
])
@pytest.mark.parametrize("remove_marker", [False, True])
def test_multiple_bot_matches_warn_and_choose_deterministically(issues, target, methods, remove_marker, capsys):
    issues = [dict(issue) for issue in issues]
    if remove_marker:
        next(issue for issue in issues if issue["number"] == 12)["body"] = "Case marker removed; exact title retained"
    api = FakeGitHub(issues)
    reporter.report(api, RUN, [receipt()])
    assert [method for _, method, _ in api.calls] == methods
    assert api.calls[-1][0] == f"issues/{target}/comments"
    assert "::warning" in capsys.readouterr().out


@pytest.mark.parametrize("before,after,first_target", [
    (("open", "open"), ("closed", "open"), 12),
    (("closed", "open"), ("closed", "closed"), 30),
    (("open", "open"), ("open", "open"), 12),
])
def test_duplicate_bot_issues_dedupe_across_state_changes(before, after, first_target, monkeypatch, capsys):
    issues = [existing(before[0], number=12), existing(before[1], number=30)]
    api = FakeGitHub(issues)
    history = {12: [], 30: []}
    reads = []
    original_request = api.request
    def comments(number):
        reads.append(number)
        return history[number]
    def request(route, *, method="GET", data=None):
        response = original_request(route, method=method, data=data)
        if method == "POST" and route.endswith("/comments"):
            number = int(route.split("/")[1])
            history[number].append({"body": data["body"], "user": {"login": "github-actions[bot]"}})
        return response
    monkeypatch.setattr(api, "comments", comments)
    monkeypatch.setattr(api, "request", request)
    reporter.report(api, RUN, [receipt()])
    assert api.calls[-1][0] == f"issues/{first_target}/comments"
    for issue, state in zip(issues, after):
        issue["state"] = state
    api.calls.clear()
    capsys.readouterr()
    reporter.report(api, RUN, [receipt()])
    assert api.calls == [], "the same run must not comment or reopen a different duplicate"
    assert reads == [12, 30, 12, 30], "every matching issue's history must be read before selecting"
    output = capsys.readouterr().out
    assert '"action": "skip"' in output and "::warning" in output


@pytest.mark.parametrize("other_state", ["open", "closed"])
def test_rerun_reads_title_only_duplicate_history_after_case_marker_removed(other_state, monkeypatch, capsys):
    issues = [existing(number=12), existing(other_state, number=30)]
    api = FakeGitHub(issues)
    history = {12: [], 30: []}
    reads = []
    def comments(number):
        reads.append(number)
        return history[number]
    monkeypatch.setattr(api, "comments", comments)
    reporter.report(api, RUN, [receipt()])
    assert api.calls[-1][0:2] == ("issues/12/comments", "POST")
    history[12].append({"body": api.calls[-1][2]["body"], "user": {"login": "github-actions[bot]"}})
    issues[0]["body"] = "Case marker removed; exact title retained"
    api.calls.clear()
    capsys.readouterr()
    reporter.report(api, RUN, [receipt()])
    assert api.calls == [], "retained title must keep the already-reported duplicate in the match set"
    assert reads == [12, 30, 12, 30]
    output = capsys.readouterr().out
    assert '"action": "skip"' in output and "::warning" in output


def test_unselected_bot_issue_body_also_deduplicates_run():
    api = FakeGitHub([existing("closed", number=12, body=CASE.marker + RUN.marker), existing(number=30)])
    reporter.report(api, RUN, [receipt()])
    assert api.calls == []


def test_stranger_run_marker_on_unselected_issue_does_not_deduplicate(monkeypatch):
    api = FakeGitHub([existing("closed", number=12), existing(number=30)])
    monkeypatch.setattr(api, "comments", lambda number:
        [{"body": RUN.marker, "user": {"login": "someone-else"}}] if number == 12 else [])
    reporter.report(api, RUN, [receipt()])
    assert api.calls[-1][0:2] == ("issues/30/comments", "POST")


def test_removed_body_marker_falls_back_to_exact_bot_title():
    api = FakeGitHub([existing(body="Marker removed during editing")])
    reporter.report(api, RUN, [receipt()])
    assert api.calls[0][0:2] == ("issues/17/comments", "POST")


def test_duplicate_receipts_and_variants_have_stable_distinct_identities():
    api = FakeGitHub()
    other = reporter.FlakyCase(CASE.journey, "copy@desktop-fast", "fail", "pass")
    reporter.report(api, RUN, [receipt(), receipt(), receipt(other)])
    assert len(api.calls) == 2
    assert CASE.marker != other.marker
    assert CASE.marker == reporter.FlakyCase(CASE.journey, CASE.case, "crashed", "known-bug").marker


@pytest.mark.parametrize("status", ["fail", "crashed", "pass", "known-bug"])
def test_failed_without_accepted_retry_or_clean_first_pass_files_nothing(status):
    api = FakeGitHub()
    reporter.report(api, RUN, [receipt(status=status)])
    assert api.calls == []


@pytest.mark.parametrize("value", [None, {}, {"rows": None}, {"rows": [None]},
    {"rows": [{"status": "passed-on-retry", "journey": "flow", "case": "case@phone-slow"}]},
    receipt(reporter.FlakyCase("copy", "copy@phone-slow", "fail", "fail")),
    receipt(reporter.FlakyCase("copy", "@someone", "fail", "pass")),
    receipt(reporter.FlakyCase("copy\n@someone", "copy@phone-slow", "fail", "pass"))])
def test_malformed_receipt_is_nonfatal_and_never_files(value, capsys):
    api = FakeGitHub()
    reporter.report(api, RUN, [value])
    assert api.calls == []
    assert "::warning" in capsys.readouterr().out


def test_bad_rows_do_not_discard_valid_siblings(capsys):
    good = receipt()["rows"][0]
    invalid = receipt(reporter.FlakyCase("copy", "@someone", "fail", "pass"))["rows"][0]
    data = {"rows": [None, good, invalid, {"status": "passed-on-retry"}]}
    assert reporter.recovered(data) == [CASE]
    output = capsys.readouterr().out
    assert output.count("::warning") == 3
    assert "@someone" not in output
    api = FakeGitHub()
    reporter.report(api, RUN, [data])
    assert api.calls[0][0:2] == ("issues", "POST")


def test_missing_or_invalid_local_receipt_is_nonfatal(tmp_path, capsys):
    assert list(reporter.local_receipts(tmp_path)) == []
    directory = tmp_path / "ui-journeys-1"
    directory.mkdir()
    (directory / "summary.json").write_text("not JSON")
    assert list(reporter.local_receipts(tmp_path)) == []
    assert "::warning" in capsys.readouterr().out


@pytest.mark.parametrize("first,retry", [("crashed", "pass"), ("fail", "known-bug")])
def test_preserves_existing_runner_acceptance_semantics(first, retry):
    case = reporter.FlakyCase("copy", "(whole journey)" if first == "crashed" else CASE.case, first, retry)
    assert reporter.recovered(receipt(case)) == [case]


def test_api_failure_stays_nonfatal_and_does_not_expose_error(monkeypatch, capsys):
    api = FakeGitHub()
    def fail(*args, **kwargs):
        raise RuntimeError("DO NOT PUBLISH: API error details")
    monkeypatch.setattr(api, "request", fail)
    monkeypatch.setattr(reporter, "GitHub", lambda _: api)
    assert reporter.main(["--repository", RUN.repository, "--run-id", str(RUN.id)]) == 0
    output = capsys.readouterr().out
    assert "::warning" in output and "DO NOT PUBLISH" not in output


def test_one_failed_write_does_not_suppress_another_case(monkeypatch, capsys):
    api = FakeGitHub()
    original = api.request
    def fail_one(route, **kwargs):
        if kwargs["data"]["title"].startswith("Flaky UI journey: copy /"):
            raise RuntimeError("API unavailable")
        return original(route, **kwargs)
    monkeypatch.setattr(api, "request", fail_one)
    reporter.report(api, RUN, [receipt(), receipt(reporter.FlakyCase("other", CASE.case, "fail", "pass"))])
    assert len(api.calls) == 1
    assert "::warning" in capsys.readouterr().out


@pytest.mark.parametrize("key,value", [("event", "pull_request"), ("event", "workflow_dispatch"),
    ("head_branch", "topic"), ("name", "CI JavaScript"), ("path", ".github/workflows/ci-frontend.yml"),
    ("head_repository", {"full_name": "other/project"})])
def test_non_main_push_sources_are_rejected(key, value):
    assert reporter.eligible_run(RUN.repository, RUN.id, {**METADATA, key: value}) is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_ineligible_real_run_warns_but_dry_run_is_plain_log(dry_run, monkeypatch, capsys):
    api = FakeGitHub()
    api.metadata["name"] = "renamed-main-suite"
    monkeypatch.setattr(reporter, "GitHub", lambda _: api)
    args = ["--repository", RUN.repository, "--run-id", str(RUN.id)]
    assert reporter.main(args + (["--dry-run"] if dry_run else [])) == 0
    output = capsys.readouterr().out
    assert ("::warning" in output) is not dry_run
    assert "Not a push-to-main main-suite run" in output
    assert [method for _, method, _ in api.calls] == ["GET"]


def test_dry_run_uses_real_decisions_but_never_writes(monkeypatch, capsys):
    api = FakeGitHub([existing("closed")])
    monkeypatch.setattr(reporter, "GitHub", lambda _: api)
    assert reporter.main(["--repository", RUN.repository, "--run-id", str(RUN.id), "--dry-run"]) == 0
    assert [method for _, method, _ in api.calls] == ["GET"]
    assert '"action": "reopen"' in capsys.readouterr().out


def test_local_receipts_cannot_be_used_for_public_writes(tmp_path, monkeypatch, capsys):
    def unexpected_api(_):
        pytest.fail("must not contact GitHub for a writable local-receipt invocation")
    monkeypatch.setattr(reporter, "GitHub", unexpected_api)
    assert reporter.main(["--repository", RUN.repository, "--run-id", str(RUN.id),
                          "--receipts", str(tmp_path)]) == 0
    assert "::warning" in capsys.readouterr().out


def archive(receipt_data=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as data:
        if receipt_data is not None:
            data.writestr("summary.json", json.dumps(receipt_data))
        data.writestr("attempt-1/journeys/copy/summary.json", "untrusted nested summary")
        data.writestr("../../not-extracted", "untrusted path")
    return output.getvalue()


def test_artifacts_use_reusable_workflow_names_latest_id_and_only_root_receipt(monkeypatch):
    api = reporter.GitHub(RUN.repository)
    calls = []
    def request(route, **kwargs):
        calls.append((route, kwargs))
        if "artifacts?" in route:
            return [{"artifacts": [{"name": "ui-journeys-1", "id": 1, "expired": False},
                                   {"name": "unrelated", "id": 9}]},
                    {"artifacts": [{"name": "ui-journeys-1", "id": 2, "expired": False}]}]
        assert route == "actions/artifacts/2/zip" and kwargs == {"binary": True}
        return archive(receipt())
    monkeypatch.setattr(api, "request", request)
    assert list(api.receipts(RUN.id)) == [receipt()]
    assert len(calls) == 2
    assert calls[0][1] == {"pages": True}


def test_artifact_without_receipt_is_a_quiet_noop(monkeypatch, capsys):
    api = reporter.GitHub(RUN.repository)
    monkeypatch.setattr(api, "request", lambda route, **kwargs:
        [{"artifacts": [{"name": "ui-journeys-1", "id": 1, "expired": False}]}]
        if "artifacts?" in route else archive())
    assert list(api.receipts(RUN.id)) == []
    output = capsys.readouterr().out
    assert "no root retry receipt" in output and "::warning" not in output


@pytest.mark.parametrize("problem", ["corrupt", "oversized", "expired"])
def test_invalid_artifact_still_warns(problem, monkeypatch, capsys):
    api = reporter.GitHub(RUN.repository)
    def request(route, **kwargs):
        if "artifacts?" in route:
            return [{"artifacts": [{"name": "ui-journeys-1", "id": 1, "expired": problem == "expired"}]}]
        assert problem != "expired"
        return b"not a zip" if problem == "corrupt" else archive({"padding": "x" * 2_000_001})
    monkeypatch.setattr(api, "request", request)
    assert list(api.receipts(RUN.id)) == []
    assert "::warning" in capsys.readouterr().out


def test_gh_transport_is_bounded_paginated_and_uses_json_stdin_not_shell(monkeypatch):
    calls = []
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'[[{"body":"old"}], [{"body":"new"}]]')
    monkeypatch.setattr(reporter.subprocess, "run", command)
    api = reporter.GitHub(RUN.repository)
    assert api.comments(17) == [{"body": "old"}, {"body": "new"}]
    assert calls[0][0][-2:] == ["--paginate", "--slurp"]
    assert calls[0][1]["timeout"] == 45 and "shell" not in calls[0][1]
    api.request("issues", method="POST", data={"body": "$(not a shell command)"})
    assert json.loads(calls[-1][1]["input"]) == {"body": "$(not a shell command)"}
    assert calls[-1][0][-2:] == ["--input", "-"]


@pytest.mark.parametrize("crash_first", [False, True])
def test_existing_producer_writes_consumable_recovered_receipt(crash_first, tmp_path, monkeypatch, capsys):
    """Drive exact-case AND whole-file retry_failed -> write_receipt -> recovered."""
    first = [{"name": "copy.journey.mjs", "code": 1, "seconds": 1, "log": "logs/copy.log",
              "cases": None if crash_first else [{"id": CASE.case, "status": "fail", "detail": "first error"}],
              "error": "first attempt crashed"}]
    weights = tmp_path / "weights.json"
    weights.write_text(json.dumps({"seconds": {"copy.journey.mjs": 1}}))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    def retry_command(argv, env, log, timeout):
        path = Path(env["JOURNEY_OUT"]) / "copy"
        path.mkdir(parents=True)
        (path / "summary.json").write_text(json.dumps({"journey": "copy", "cases": [
            {"id": CASE.case, "status": "pass", "ok": True}]}))
        return 0, 1.0
    monkeypatch.setattr(journey_retry.time, "monotonic", lambda: 1.0)
    data = journey_retry.retry_failed(first, tmp_path, {}, retry_command, directory=tmp_path,
        timeout=60, started=0, alive=lambda: True, weights_path=weights)
    journey_retry.write_receipt(tmp_path, data)
    assert "::warning title=UI journey passed on retry::" in capsys.readouterr().out
    expected = reporter.FlakyCase("copy", "(whole journey)" if crash_first else CASE.case,
                                 "crashed" if crash_first else "fail", "pass")
    assert reporter.recovered(json.loads((tmp_path / "summary.json").read_text())) == [expected]


def test_all_real_journey_case_ids_including_dynamic_overlays_are_reportable(tmp_path):
    """--list plans actual cases without Playwright, an app, or a data directory."""
    root = Path(__file__).resolve().parents[2]
    journeys = sorted((root / "frontend/tests/journeys").glob("*.journey.mjs"))
    assert journeys
    overlays = []
    for path in journeys:
        # Mobile-shell planning expands the overlay-* template; no shell/app is launched.
        args = ["--mobile-shell"] if path.name == "transcript-reader.journey.mjs" else []
        result = subprocess.run(["node", str(path), "--list", *args], cwd=root,
            env={**os.environ, "JOURNEY_ONLY": "", "DEVLOOP_STATUS": str(tmp_path / "must-not-read-status.json")},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=True)
        ids = result.stdout.splitlines()
        assert ids, path.name
        journey = path.name.removesuffix(".journey.mjs")
        data = {"rows": [{"journey": journey, "case": id, "status": "passed-on-retry",
                          "attempts": [{"status": "fail"}, {"status": "pass"}]} for id in ids]}
        assert [case.case for case in reporter.recovered(data)] == ids, path.name
        overlays.extend(id for id in ids if id.startswith("overlay-"))
    assert overlays, "the dynamic overlay-* template must be included in the contract"
    assert "overlay-usage-opening-reading-growth@phone-slow" in overlays
