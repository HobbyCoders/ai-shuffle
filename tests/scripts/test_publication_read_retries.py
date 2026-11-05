"""Temporary publication reads retry, without relaxing any publication evidence."""
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import urllib.error
from unittest.mock import Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40
PUBLISHED = "b" * 40
JOBS = "actions/runs/41/attempts/2/jobs?per_page=100&page=1"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/ci/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("edge_publication_gate")
nightly = load("nightly_changed")


@pytest.fixture(autouse=True)
def waits(monkeypatch):
    delays = []
    monkeypatch.setattr(time, "sleep", delays.append)
    return delays


def command_error(status):
    # gh puts its diagnostic on stderr, and the API's JSON error body on stdout.
    message = {403: "Forbidden", 404: "Not Found", 429: "Too Many Requests"}.get(status, "Server Error")
    return subprocess.CalledProcessError(
        1, ["gh", "api"], output=json.dumps({"status": str(status)}),
        stderr=f"gh: {message} (HTTP {status})\n",
    )


CLI_TIMEOUTS = [
    'Get "https://api.github.com/": net/http: TLS handshake timeout',
    'Get "https://api.github.com/": dial tcp 192.0.2.1:443: i/o timeout',
    'Get "https://api.github.com/": read tcp 192.0.2.2:1234->192.0.2.1:443: i/o timeout',
    'Get "https://api.github.com/": net/http: timeout awaiting response headers',
    'Get "https://api.github.com/": context deadline exceeded (Client.Timeout exceeded while awaiting headers)',
    'Get "https://api.github.com/": net/http: request canceled (Client.Timeout exceeded while awaiting headers)',
]


def cli_timeout(diagnostic):
    return subprocess.CalledProcessError(1, ["gh", "api"], stderr=diagnostic + "\n")


@pytest.mark.parametrize("error", [command_error(500), command_error(502), command_error(503), command_error(599),
                                 subprocess.TimeoutExpired(["gh", "api"], 20),
                                 *map(cli_timeout, CLI_TIMEOUTS)])
def test_github_temporary_failure_then_success(monkeypatch, waits, error, capsys):
    run = Mock(side_effect=[error, '{"jobs": []}'])
    monkeypatch.setattr(subprocess, "check_output", run)
    assert gate.GitHub()(JOBS) == {"jobs": []}
    assert run.call_count == 2
    assert waits == [1]
    args, kwargs = run.call_args
    assert args[0] == ["gh", "api", f"repos/{gate.REPOSITORY}/{JOBS}"]
    assert kwargs["timeout"] == 20
    assert kwargs["stderr"] == subprocess.PIPE
    assert capsys.readouterr().out == ""  # The signer's --json output stays clean.


@pytest.mark.parametrize("status", [403, 404, 429])
def test_github_permanent_failure_is_one_call(monkeypatch, waits, status, capsys):
    error = command_error(status)
    # A status mentioned in a body must not override gh's real HTTP diagnostic.
    error.output = '{"message": "upstream HTTP 502"}'
    run = Mock(side_effect=error)
    monkeypatch.setattr(subprocess, "check_output", run)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        gate.GitHub()(JOBS)
    assert caught.value is error
    assert run.call_count == 1
    assert waits == []
    assert capsys.readouterr().err == error.stderr


@pytest.mark.parametrize("diagnostic", [
    "not logged in",
    'Get "https://api.github.com/": tls: failed to verify certificate: x509: certificate expired',
    'Get "https://api.github.com/": dial tcp 192.0.2.1:443: connect: connection refused',
    'Get "https://api.github.com/": read tcp 192.0.2.1:443: connection reset by peer',
    'gh: Forbidden (HTTP 403)\nGet "https://api.github.com/": net/http: TLS handshake timeout',
])
def test_cli_non_timeout_transport_and_auth_failures_stay_permanent(monkeypatch, waits, diagnostic):
    error = cli_timeout(diagnostic)
    run = Mock(side_effect=error)
    monkeypatch.setattr(subprocess, "check_output", run)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        gate.GitHub()(JOBS)
    assert caught.value is error
    assert run.call_count == 1 and waits == []


def test_third_attempt_can_succeed_and_success_never_sleeps(monkeypatch, waits):
    run = Mock(side_effect=[command_error(502), command_error(503), "{}", "{}"])
    monkeypatch.setattr(subprocess, "check_output", run)
    assert gate.GitHub()(JOBS) == {}
    assert run.call_count == 3 and waits == [1, 2]
    assert gate.GitHub()(JOBS) == {}
    assert run.call_count == 4 and waits == [1, 2]


def test_unknown_cli_failure_and_invalid_json_are_not_retried(monkeypatch, waits):
    for result in [subprocess.CalledProcessError(1, ["gh"], stderr="not logged in"), "invalid JSON"]:
        run = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
        monkeypatch.setattr(subprocess, "check_output", run)
        with pytest.raises((subprocess.CalledProcessError, json.JSONDecodeError)):
            gate.GitHub()(JOBS)
        assert run.call_count == 1
    assert waits == []


@pytest.mark.parametrize("error", [command_error(503), subprocess.TimeoutExpired(["gh", "api"], 20),
                                 *map(cli_timeout, CLI_TIMEOUTS)])
def test_exhausted_jobs_read_never_writes_publication_permission(monkeypatch, tmp_path, waits, error):
    event, output = tmp_path / "event.json", tmp_path / "output"
    event.write_text('{"schedule": "0 6 * * *"}')
    for key, value in {
        "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(output),
        "GITHUB_EVENT_NAME": "schedule", "GITHUB_REPOSITORY": gate.REPOSITORY,
        "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": REVISION,
        "SOURCE_REVISION": REVISION,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(gate, "published_edge_revision", lambda: PUBLISHED)
    monkeypatch.setattr(gate, "main_history", lambda *a, **kw: [REVISION, PUBLISHED])
    run = {
        "id": 41, "workflow_id": 7, "path": gate.MAIN_SUITE_PATH,
        "head_sha": REVISION, "head_branch": "main", "event": "push",
        "status": "completed", "conclusion": "success", "run_attempt": 2,
        "repository": {"id": 42, "full_name": gate.REPOSITORY},
        "head_repository": {"id": 42, "full_name": gate.REPOSITORY},
    }
    jobs_calls = []

    def read(command, **kwargs):
        if command[0] == "git":
            return REVISION + "\n"
        endpoint = command[2].removeprefix(f"repos/{gate.REPOSITORY}").lstrip("/")
        if endpoint == JOBS:
            jobs_calls.append(endpoint)
            raise error
        if endpoint.startswith("commits/"):
            return "[]"
        if endpoint.startswith("actions/workflows/main-suite.yml/runs?"):
            return json.dumps({"workflow_runs": [run]})
        return json.dumps({
            "": {"id": 42, "full_name": gate.REPOSITORY},
            "actions/workflows/main-suite.yml": {"id": 7, "path": gate.MAIN_SUITE_PATH},
            "actions/runs/41": run,
        }[endpoint])

    monkeypatch.setattr(subprocess, "check_output", read)
    with pytest.raises(type(error)) as caught:
        gate.main([])
    assert caught.value is error
    assert len(jobs_calls) == 3
    assert waits == [1, 2]
    assert not output.exists()  # No ready=true, revision, or JSON pass after API failure.


@pytest.mark.parametrize("failure", [502, "timeout", 403, 404, "invalid", "signature"])
def test_signed_pointer_retries_transport_not_evidence(monkeypatch, waits, failure):
    calls = []
    verified, cached = Mock(return_value=REVISION), Mock()
    if failure == "signature":
        verified.side_effect = ValueError("invalid signature")
    monkeypatch.setattr(gate, "verify_edge_manifest", verified)
    monkeypatch.setattr(gate, "cache_edge_manifest", cached)

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("temporary", request=request)
            if isinstance(failure, int):
                return httpx.Response(failure)
        return httpx.Response(200, content=b"x" * 32769 if failure == "invalid" else b"signed envelope")

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    if failure in (502, "timeout"):
        assert gate.published_edge_revision() == REVISION
        assert len(calls) == 2 and waits == [1]
        verified.assert_called_once_with(b"signed envelope")
        cached.assert_called_once_with(b"signed envelope")
    else:
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            gate.published_edge_revision()
        assert len(calls) == 1 and waits == []
        cached.assert_not_called()


def test_pointer_stream_timeout_discards_partial_body(monkeypatch, waits):
    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield b"discard this partial body"
            raise httpx.ReadTimeout("temporary")

    calls = []
    def respond(request):
        calls.append(request)
        return (httpx.Response(200, stream=Partial()) if len(calls) == 1 else
                httpx.Response(200, content=b"complete signed envelope"))
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    verified = Mock(return_value=REVISION)
    monkeypatch.setattr(gate, "verify_edge_manifest", verified)
    monkeypatch.setattr(gate, "cache_edge_manifest", Mock())
    assert gate.published_edge_revision() == REVISION
    verified.assert_called_once_with(b"complete signed envelope")
    assert len(calls) == 2 and waits == [1]


@pytest.mark.parametrize("failure", [503, "timeout"])
def test_signed_pointer_exhaustion_raises(monkeypatch, waits, failure):
    calls = []
    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(failure)
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    verify = Mock()
    monkeypatch.setattr(gate, "verify_edge_manifest", verify)
    with pytest.raises((httpx.HTTPStatusError, httpx.TimeoutException)):
        gate.published_edge_revision()
    assert len(calls) == 3 and waits == [1, 2]
    verify.assert_not_called()


@pytest.mark.parametrize("failure", [502, "timeout", "wrapped timeout", 403, 404])
@pytest.mark.parametrize("exhausted", [False, True])
def test_cheap_pointer_read(monkeypatch, waits, failure, exhausted):
    error = (TimeoutError("temporary") if failure == "timeout" else
             urllib.error.URLError(TimeoutError("temporary")) if failure == "wrapped timeout" else
             urllib.error.HTTPError(nightly.URL, failure, "error", {}, None))
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b"envelope"
    read = Mock(side_effect=error if exhausted else [error, response])
    monkeypatch.setattr(nightly.urllib.request, "urlopen", read)
    temporary = failure in (502, "timeout", "wrapped timeout")
    if temporary and not exhausted:
        assert nightly.fetch() == b"envelope"
        assert read.call_count == 2 and waits == [1]
    else:
        with pytest.raises(type(error)) as caught:
            nightly.fetch()
        assert caught.value is error
        assert read.call_count == (3 if temporary else 1)
        assert waits == ([1, 2] if temporary else [])
