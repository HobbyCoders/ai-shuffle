"""Pass records: main skips only what this repository's own pull request passed on the same files."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("proven", ROOT / "scripts/ci/proven.py")
proven = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proven)

TREE = "a" * 40
NAME = f"proven-backend-{TREE}"


SHA, HEAD, RUN, MERGE = "c" * 40, "d" * 40, 7, "9" * 40


BRANCH, NUMBER = "fix/thing", 12


def artifact(name=NAME, expired=False, repository=1, head=1, head_sha=HEAD, run=RUN, branch=BRANCH):
    return {"name": name, "expired": expired,
            "workflow_run": {"id": run, "repository_id": repository, "head_repository_id": head,
                             "head_sha": head_sha, "head_branch": branch}}


def used(commit=MERGE, ref=f"refs/pull/{NUMBER}/merge", name="record-pass.yml", repo="owner/repo"):
    """GitHub's note of the commit a run took a called workflow from."""
    return {"path": f"{repo}/.github/workflows/{name}@{commit}", "sha": commit, "ref": ref}


def the_run(**changes):
    return {"event": "pull_request", "head_sha": HEAD, "head_branch": BRANCH,
            "head_repository": {"full_name": "owner/repo"}, "path": ".github/workflows/ci-backend.yml",
            "referenced_workflows": [used()], **changes}


def job(status="completed", conclusion="success"):
    return {"status": status, "conclusion": conclusion}


def pull(merged="2026-01-01T00:00:00Z", commit=SHA, base="main", repo="owner/repo", head=HEAD, number=NUMBER,
         branch=BRANCH):
    return {"number": number, "merged_at": merged, "merge_commit_sha": commit, "base": {"ref": base},
            "head": {"sha": head, "ref": branch, "repo": {"full_name": repo}}}


def github(artifacts=(), pulls=None, run=None, jobs=None, asked=None, branch_pulls=None, events=None,
           merge_tree=TREE):
    """A fake GitHub: the record, the pull request merged as SHA, its run and that run's jobs."""
    pulls = [pull()] if pulls is None else pulls
    run = the_run() if run is None else run
    branch_pulls = [{"number": NUMBER}] if branch_pulls is None else branch_pulls
    jobs = [job(), job(conclusion="skipped"), job("in_progress", None)] if jobs is None else jobs
    events = [{"event": "labeled"}, {"event": "merged"}, {"event": "closed"}] if events is None else events

    def call(path, token):
        if asked is not None:
            asked.append(path)
        if "/actions/artifacts?" in path:
            return {"artifacts": list(artifacts)}
        if path.endswith(f"/commits/{SHA}/pulls?per_page=100"):
            return pulls
        if path.endswith("/pulls?state=all&per_page=100&head=owner%3Afix/thing"):
            return branch_pulls
        if path.endswith(f"/issues/{NUMBER}/events?per_page=100"):
            return events
        if path.endswith(f"/git/commits/{MERGE}"):
            return {"sha": MERGE, "tree": {"sha": merge_tree}}
        if path.endswith(f"/actions/runs/{RUN}"):
            return run
        if path.endswith(f"/actions/runs/{RUN}/jobs?filter=latest&per_page=100"):
            return {"total_count": len(jobs), "jobs": jobs}
        raise AssertionError(path)

    return call


def found(*artifacts, area="backend", **world):
    return proven.exists(NAME, "owner/repo", "token", call=github(artifacts, **world), area=area, tree=TREE,
                         commit=SHA)


def on_main_push(monkeypatch, event="push", ref="refs/heads/main", token="token"):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", token)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("GITHUB_SHA", SHA)


def test_record_names():
    assert proven.record_name("backend", TREE) == NAME
    assert proven.record_name("journeys", TREE, "all") == f"proven-journeys-{TREE}-all"


def test_head_tree_is_the_same_for_the_same_files_on_different_commits(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                              cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    (tmp_path / "file.txt").write_text("one\n")
    git("add", "-A")
    git("commit", "-q", "-m", "first")
    first = proven.head_tree(tmp_path)
    git("commit", "-q", "--amend", "-m", "the same files under another commit")
    assert proven.head_tree(tmp_path) == first == git("rev-parse", "HEAD^{tree}")
    (tmp_path / "file.txt").write_text("two\n")
    git("commit", "-q", "-am", "changed")
    assert proven.head_tree(tmp_path) != first


def test_only_a_live_record_from_this_repositorys_own_run_counts():
    asked = []
    assert proven.exists(NAME, "owner/repo", "token", call=github([artifact()], asked=asked), area="backend",
                         tree=TREE, commit=SHA)
    assert asked == [f"/repos/owner/repo/actions/artifacts?per_page=100&name={NAME}",
                     f"/repos/owner/repo/commits/{SHA}/pulls?per_page=100",
                     "/repos/owner/repo/pulls?state=all&per_page=100&head=owner%3Afix/thing",
                     f"/repos/owner/repo/issues/{NUMBER}/events?per_page=100",
                     f"/repos/owner/repo/actions/runs/{RUN}",
                     f"/repos/owner/repo/git/commits/{MERGE}",
                     f"/repos/owner/repo/actions/runs/{RUN}/jobs?filter=latest&per_page=100"]
    assert found(artifact(expired=True), artifact(head=2), artifact())
    assert not found()
    assert not found(artifact(expired=True))
    assert not found(artifact(expired=None))
    assert not found(artifact(head=2))  # uploaded by a fork's run
    assert not found(artifact(repository=None, head=None))
    assert not found(artifact(name=NAME + "-other"))
    assert not found(artifact(run=None))
    assert not found({"name": NAME, "expired": False})
    assert not proven.exists(NAME, "owner/repo", "token", call=lambda path, token: {}, area="backend", tree=TREE,
                             commit=SHA)


def test_a_name_alone_proves_nothing():
    """Any run in the repository can upload an artifact with the right name."""
    record = artifact()
    assert found(record)
    # Another branch's run: its head is not the head of the pull request merged as this commit.
    assert not found(artifact(head_sha="e" * 40))
    assert not found(record, pulls=[])
    assert not found(record, pulls=[pull(merged=None)])
    assert not found(record, pulls=[pull(commit="f" * 40)])
    assert not found(record, pulls=[pull(base="other")])
    assert not found(record, pulls=[pull(repo="fork/repo")])
    # The merged pull request's head, but not the area's own pull request workflow.
    for run in (the_run(event="push"), the_run(event="workflow_dispatch"), the_run(event="pull_request_target"),
                the_run(path=".github/workflows/other.yml"), the_run(path=".github/workflows/ci-fluid.yml"),
                the_run(head_sha="e" * 40), the_run(head_branch="other"),
                the_run(head_repository={"full_name": "fork/repo"}), {}):
        assert not found(record, run=run), run
    assert not found(artifact(branch="other"))
    assert not found(record, pulls=[pull(number=None)])
    assert not found(record, pulls=[pull(branch="")])


def test_a_branch_with_a_second_pull_request_proves_nothing():
    """A pull request runs its base's workflows: the same head against an unreviewed base could forge a record."""
    record = artifact()
    assert found(record, branch_pulls=[{"number": NUMBER}])
    assert not found(record, branch_pulls=[{"number": NUMBER}, {"number": NUMBER + 1}])
    assert not found(record, branch_pulls=[{"number": NUMBER + 1}])
    assert not found(record, branch_pulls=[])
    assert not found(record, branch_pulls={"message": "error"})
    assert not found(record, area="fluid")
    assert not found(record, area="unknown")
    assert not proven.exists(NAME, "owner/repo", "token", call=github([record]), area="backend", tree=TREE, commit="")
    assert not proven.exists(NAME, "owner/repo", "token", call=github([record]), area="backend", tree="", commit=SHA)


def test_a_retargeted_pull_request_proves_nothing():
    """Opened against an unreviewed base, checked there, then pointed at main and merged."""
    record = artifact()
    assert found(record, events=[])
    assert not found(record, events=[{"event": "base_ref_changed"}, {"event": "merged"}])
    # No history, an odd answer, or more than one page of it: unknown, so not proven.
    assert not found(record, events={"message": "error"})
    assert not found(record, events=[{"event": "merged"}, "odd"])
    assert not found(record, events=[{"event": None}])
    assert not found(record, events=[{"event": "labeled"}] * 100)


def test_the_run_must_have_used_the_pull_requests_own_merge_with_exactly_these_files():
    """The record's name is written by the run; what GitHub notes about the run is not."""
    record = artifact()
    assert found(record)
    # The run merged the head into something else: its files differ whatever the name says.
    assert not found(record, merge_tree="b" * 40)
    for called in ([], [used(), used()], [used(ref="refs/heads/main")], [used(ref=f"refs/pull/{NUMBER + 1}/merge")],
                   [used(ref=None)], [used(name="other.yml")], [used(repo="fork/repo")],
                   [{**used(), "sha": "8" * 40}], [{**used(), "sha": None}], ["odd"]):
        assert not found(record, run=the_run(referenced_workflows=called)), called
    assert not found(record, run=the_run(referenced_workflows=None))


@pytest.mark.parametrize("bad", [
    job(conclusion="failure"), job(conclusion="cancelled"), job(conclusion="timed_out"),
    job(conclusion="action_required"), job(conclusion="neutral"), job(conclusion="new-kind"),
    job(conclusion=None), job("in_progress", "success"), job("new-status", None), {}, "odd"])
def test_a_job_that_failed_or_has_an_unknown_state_proves_nothing(bad):
    assert found(artifact(), jobs=[job()])
    assert not found(artifact(), jobs=[job(), bad])
    assert not found(artifact(), jobs=[])


def test_a_job_list_that_did_not_fit_one_page_proves_nothing():
    def call(path, token):
        answer = github([artifact()])(path, token)
        return {**answer, "total_count": 101} if "/jobs?" in path else answer

    assert not proven.exists(NAME, "owner/repo", "token", call=call, area="backend", tree=TREE, commit=SHA)


def test_every_recorded_area_names_its_workflow():
    for area, name in proven.WORKFLOWS.items():
        text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        assert "uses: ./.github/workflows/record-pass.yml" in text and f"area: {area}" in text, area


def test_green_main_commits_keeps_successful_main_runs_in_order():
    asked = []
    runs = [{"head_sha": "1", "head_branch": "main", "conclusion": "success"},
            {"head_sha": "2", "head_branch": "other", "conclusion": "success"},
            {"head_sha": "3", "head_branch": "main", "conclusion": "failure"},
            {"head_sha": "4", "head_branch": "main", "conclusion": "success"}]
    assert proven.green_main_commits("owner/repo", "token",
                                     call=lambda path, token: asked.append(path) or {"workflow_runs": runs}) == ["1", "4"]
    assert asked == ["/repos/owner/repo/actions/workflows/main-suite.yml/runs?branch=main&status=success&per_page=30"]


@pytest.mark.parametrize(("event", "ref", "token"), [
    ("schedule", "refs/heads/main", "token"), ("workflow_dispatch", "refs/heads/main", "token"),
    ("pull_request", "refs/pull/1/merge", "token"), ("push", "refs/heads/other", "token"),
    ("push", "refs/heads/main", ""),
])
def test_only_a_push_to_main_looks_for_records(event, ref, token, monkeypatch, tmp_path):
    on_main_push(monkeypatch, event, ref, token)
    assert proven.environment() is None
    assert proven.check(["backend"], tmp_path, call=lambda *args: pytest.fail("must not ask")) == {"backend": False}


def test_check_answers_per_area_and_never_fails(monkeypatch, capsys):
    on_main_push(monkeypatch)
    assert proven.environment() == ("owner/repo", "token")
    monkeypatch.setattr(proven, "head_tree", lambda root=ROOT: TREE)

    def call(path, token):
        if "proven-fluid-" in path:
            raise OSError("GitHub is down")
        return github([artifact()] if NAME in path or "/actions/artifacts?" not in path else [])(path, token)

    # A lookup that breaks part-way keeps what it found and runs the rest.
    assert proven.check(["backend", "windows", "fluid", "container"], call=call) == {
        "backend": True, "windows": False, "fluid": False, "container": False}
    monkeypatch.setattr(proven, "head_tree", lambda root=ROOT: 1 / 0)
    assert proven.check(["backend"], call=call) == {"backend": False}
    monkeypatch.setattr(proven, "check", lambda areas: {"backend": True, "javascript-checks": False})
    assert proven.main(["check", "--areas", "backend,javascript-checks"]) == 0
    assert capsys.readouterr().out == "backend=true\njavascript-checks=false\n"
