"""The main-suite watcher cancels the run on the first required failure."""
import importlib.util
import io
from pathlib import Path
import urllib.error

spec = importlib.util.spec_from_file_location("stop_on_failure", Path(__file__).resolve().parents[2] / "scripts/ci/stop_on_failure.py")
stop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stop)


def job(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion if status == "completed" else None,
            "html_url": f"https://example.test/{name}"}


class Api:
    def __init__(self, polls):
        self.polls = list(polls)
        self.calls = []

    def __call__(self, method, path, token):
        self.calls.append((method, path))
        if method == "POST":
            return {}
        poll = self.polls.pop(0)
        if isinstance(poll, Exception):
            raise poll
        return {"jobs": poll, "total_count": len(poll)}


def watch(api, tmp_path=None, attempt="2"):
    out = io.StringIO()
    summary = str(tmp_path / "summary.md") if tmp_path else None
    result = stop.watch("o/r", "7", attempt, "t", call=api, sleep=lambda _: None, out=out, summary_path=summary)
    return result, out.getvalue()


def test_first_required_failure_cancels_the_run_and_names_the_job(tmp_path):
    api = Api([
        [job("backend / backend-shard 1", "in_progress"), job("javascript / UI journeys (1/16)", "in_progress")],
        [job("backend / backend-shard 1", conclusion="failure"), job("javascript / UI journeys (1/16)", "in_progress"),
         job("fluid / fluid unit tests", conclusion="timed_out")],
    ])
    result, out = watch(api, tmp_path)
    assert result == "cancelled"
    assert api.calls[-1] == ("POST", "/repos/o/r/actions/runs/7/cancel")
    assert api.calls[0] == ("GET", "/repos/o/r/actions/runs/7/attempts/2/jobs?per_page=100&page=1")
    assert "backend / backend-shard 1" in out and "fluid / fluid unit tests" in out
    assert "backend / backend-shard 1" in (tmp_path / "summary.md").read_text()


def test_a_green_run_ends_with_the_gate_and_is_never_cancelled():
    api = Api([[job("backend / Backend tests"), job("Main suite", "in_progress")],
               [job("backend / Backend tests"), job("Main suite"), job("javascript / UI journeys (2/16)", conclusion="skipped")]])
    assert watch(api)[0] == "finished"
    assert all(method == "GET" for method, _ in api.calls)


def test_advisory_and_cancelled_jobs_do_not_stop_the_run():
    api = Api([[job("source-ui / build", conclusion="failure"),
                job("Report recovered UI journeys (non-blocking)", conclusion="failure"),
                job("Stop on first failure (non-blocking)", "in_progress"),
                job("windows / Windows desktop shard (1/3)", conclusion="cancelled"),
                job("Main suite")]])
    assert watch(api)[0] == "finished"


def test_an_unreadable_run_gives_up_without_cancelling():
    api = Api([urllib.error.URLError("down")] * stop.MAX_FAILED_READS)
    assert watch(api)[0] == "unreadable"
    assert all(method == "GET" for method, _ in api.calls)


def test_reads_recover_and_every_page_is_read():
    first = [job(f"backend / part {index}") for index in range(100)]

    class Paged(Api):
        def __call__(self, method, path, token):
            self.calls.append((method, path))
            if method == "POST":
                return {}
            if path.endswith("page=1"):
                return {"jobs": first, "total_count": 101}
            return {"jobs": [job("desktop / check", conclusion="failure")], "total_count": 101}

    api = Paged([])
    assert watch(api)[0] == "cancelled"
    assert [path[-6:] for method, path in api.calls if method == "GET"] == ["page=1", "page=2"]


def test_a_rerun_of_only_the_watcher_ends_instead_of_holding_the_lane():
    """Its attempt has no gate: the required suites passed in an earlier one."""
    alone = [job("Stop on first failure (non-blocking)", "in_progress")]
    api = Api([alone, alone, alone])
    assert watch(api)[0] == "finished"
    assert len(api.calls) == 2
    # A first attempt whose jobs have not registered yet keeps watching.
    api = Api([alone, alone, alone + [job("backend / backend-shard 1", conclusion="failure")]])
    assert watch(api, attempt="1")[0] == "cancelled"
    # In a re-run, one quiet poll between jobs is not the end.
    api = Api([alone, alone + [job("backend / backend-shard 1", "queued")], alone + [job("backend / backend-shard 1", conclusion="failure")]])
    assert watch(api)[0] == "cancelled"
