"""Main-only flaky-journey reporting, without a browser, database or public writes."""
from __future__ import annotations

import io
import json
from pathlib import Path
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
            return {"number": 17, "state": "open", **data}
        return {"body": data.get("body", "")}


def existing(state="open", body=None):
    return {"number": 17, "state": state, "body": CASE.marker if body is None else body}


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
    api = FakeGitHub([issue], [{"body": RUN.marker}] if where == "comment" else [])
    reporter.report(api, RUN, [receipt()])
    assert api.calls == []


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


def test_artifact_without_receipt_warns_and_continues(monkeypatch, capsys):
    api = reporter.GitHub(RUN.repository)
    monkeypatch.setattr(api, "request", lambda route, **kwargs:
        [{"artifacts": [{"name": "ui-journeys-1", "id": 1, "expired": False}]}]
        if "artifacts?" in route else archive())
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


def test_existing_producer_writes_consumable_recovered_receipt(tmp_path, monkeypatch, capsys):
    """Drive retry_failed -> write_receipt, not just a hand-seeded consumer field."""
    first = [{"name": "copy.journey.mjs", "code": 1, "seconds": 1, "log": "logs/copy.log",
              "cases": [{"id": CASE.case, "status": "fail", "detail": "first error"}]}]
    def retry_command(argv, env, log, timeout):
        path = Path(env["JOURNEY_OUT"]) / "copy"
        path.mkdir(parents=True)
        (path / "summary.json").write_text(json.dumps({"journey": "copy", "cases": [
            {"id": CASE.case, "status": "pass", "ok": True}]}))
        return 0, 1.0
    monkeypatch.setattr(journey_retry.time, "monotonic", lambda: 1.0)
    data = journey_retry.retry_failed(first, tmp_path, {}, retry_command, directory=tmp_path,
        timeout=60, started=0, alive=lambda: True, weights_path=tmp_path / "unused.json")
    journey_retry.write_receipt(tmp_path, data)
    assert "::warning title=UI journey passed on retry::" in capsys.readouterr().out
    assert reporter.recovered(json.loads((tmp_path / "summary.json").read_text())) == [CASE]
