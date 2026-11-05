"""Harness unit regressions with synthetic proc/CDP and mocked Docker.

No candidate builds, container operations or live service probes run here.
"""
import json
import runpy
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from scripts import source_archive
from scripts import verify_container_postgres as harness
from scripts.verify_container_postgres import Check


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    build = Mock()
    monkeypatch.setattr(source_archive, "build", build)
    monkeypatch.setattr(harness.subprocess, "run", Mock(return_value=SimpleNamespace(stdout="a" * 40)))
    check = Check("candidate-only", tmp_path)
    # Keep real launch/volume methods so assertions cover the Docker commands,
    # not just arguments to a mocked launch helper. Never contact a daemon.
    check.docker = Mock(return_value=SimpleNamespace(stdout=b"0", stderr=b"", returncode=0))
    check.ready = Mock()
    check.state = Mock(return_value={"Running": False, "ExitCode": 0, "OOMKilled": False})
    check.stop = Mock(wraps=check.stop)  # real stop/wait/zero-exit contract, mocked daemon only
    check.system_id = Mock(return_value="same-cluster")
    check.sql = Mock(side_effect=["", "retained", "retained"])
    check.passed = Mock()
    return check, build


def mounts(command):
    return [command[index + 1] for index, value in enumerate(command) if value == "--mount"]


def test_candidate_lifecycle_installs_then_gracefully_restarts_into_signed_supervisor(candidate, tmp_path):
    check, build = candidate
    check.updater_lifecycle()
    build.assert_called_once_with("a" * 40, tmp_path / "updater-source.zip")
    initial, preparation, updated = check.containers
    assert check.stop.call_args_list[0].args == (initial,)
    calls = [call.args for call in check.docker.call_args_list]
    prepare = next(call for call in calls if call[-2:] == ("prepare", "/fixture-source.zip"))
    fixture = prepare[-3]
    assert prepare[:6] == ("run", "--name", preparation, "--network", "bridge", "--user")
    assert prepare[6:9] == ("99:100", "--entrypoint", "python")
    assert "DATA_DIR=/data" in prepare and "AI_SHUFFLE_UPDATER_FIXTURE=1" in prepare
    assert fixture == "/updater-candidate-fixture.py"
    fixture_source = (harness.ROOT / "scripts/ci/updater_candidate_fixture.py").resolve(strict=True)
    assert fixture_source.is_file()
    fixture_mount = f"type=bind,src={fixture_source},dst={fixture},readonly"
    assert fixture_mount in mounts(prepare)
    assert f"type=volume,src={check.volumes[0]},dst=/data" in mounts(prepare)
    assert f"type=bind,src={tmp_path / 'updater-source.zip'},dst=/fixture-source.zip,readonly" in mounts(prepare)

    # The same mount belongs to the long-lived lifecycle container, not a
    # temporary docker exec/cp; it is still present after supervisor execve.
    initial_run = next(call for call in calls if call[:4] == ("run", "-d", "--name", initial))
    updated_run = next(call for call in calls if call[:4] == ("run", "-d", "--name", updated))
    assert fixture_mount not in mounts(initial_run)
    assert fixture_mount in mounts(updated_run)
    assert f"type=bind,src={tmp_path / 'updater-ci-public.pem'},dst=/app/docker/application-update-key.pem,readonly" in mounts(updated_run)
    assert "AI_SHUFFLE_UPDATER_FIXTURE=1" in updated_run
    assert "START_CHROME=1" in updated_run
    assert "--network" in updated_run and updated_run[updated_run.index("--network") + 1] == "none"
    actions = [call for call in calls if len(call) >= 2 and call[-2] == fixture]
    assert [call[-1] for call in actions] == ["updater-ready", "install", "installed", "restarted", "diagnose"]
    assert all(call[:-2] == ("exec", "--user", "99:100", updated, "python") for call in actions)
    assert all("refresh-updater" not in call for call in calls)
    assert [call.args for call in check.stop.call_args_list] == [(initial,), (updated,), (updated,)]
    stopped = calls.index(("stop", "--time", "120", updated))
    waited = calls.index(("wait", updated))
    available_lock = next(call for call in calls if call[0] == "run" and "assert lock.acquire()" in call[-1])
    started = calls.index(("start", updated))
    installed = next(call for call in actions if call[-1] == "installed")
    restarted = next(call for call in actions if call[-1] == "restarted")
    held_lock = next(call for call in calls if call[0] == "exec" and "assert not lock.acquire()" in call[-1])
    assert calls.index(installed) < stopped < waited < calls.index(available_lock) < started < calls.index(restarted) < calls.index(held_lock)
    assert available_lock[:9] == ("run", "--rm", "--network", "none", "--user", "99:100", "--entrypoint", "python", "--mount")
    assert f"type=volume,src={check.volumes[0]},dst=/data" in available_lock
    assert (tmp_path / "updater-installed.json").read_bytes() == b"0"
    assert (tmp_path / "updater-restarted.json").read_bytes() == b"0"
    assert json.loads((tmp_path / "updater-container-stop.json").read_text()) == check.state.return_value
    assert (tmp_path / "updater-lifecycle.json").read_bytes() == b"0"
    chrome_log = ("cp", updated + ":/home/appuser/.config/chrome.log", tmp_path / "updater-chrome.log")
    assert chrome_log in calls
    assert calls.index(chrome_log) > calls.index(restarted)
    assert check.system_id.call_count == 3
    assert check.sql.call_count == 3
    check.passed.assert_called_once()


def test_candidate_preparation_timeout_is_registered_for_cleanup(candidate):
    check, _ = candidate
    result = check.docker.return_value

    def docker(*args, **kwargs):
        if args[-2:] == ("prepare", "/fixture-source.zip"):
            raise subprocess.TimeoutExpired("docker", 900)
        return result

    check.docker.side_effect = docker
    with pytest.raises(subprocess.TimeoutExpired):
        check.updater_lifecycle()
    initial, preparation = check.containers
    assert check.docker.call_args.args[:2] == ("cp", initial + ":/data/application-updates/build.log")
    check.cleanup()
    calls = [call.args for call in check.docker.call_args_list]
    removed = [call[-1] for call in calls if call[:3] == ("rm", "--force", "--volumes")]
    assert removed == [preparation, initial]
    last_container_removal = max(index for index, call in enumerate(calls) if call[0] == "rm")
    first_volume_removal = next(index for index, call in enumerate(calls) if call[:2] == ("volume", "rm"))
    assert last_container_removal < first_volume_removal


def test_fixture_stays_out_of_packaged_image_and_source_update():
    ignore = (harness.ROOT / ".dockerignore").read_text().splitlines()
    assert "scripts/ci/" in ignore
    dockerfile = (harness.ROOT / "Dockerfile").read_text()
    assert "COPY --chown=root:root scripts/ ./scripts/" in dockerfile
    assert "updater_candidate_fixture" not in dockerfile
    assert not any(path == "scripts" or path.startswith("scripts/") for path in source_archive.RUNTIME_PATHS)


def test_direct_mounted_script_bootstraps_image_imports_without_cwd_or_pythonpath(tmp_path, monkeypatch):
    # Execute the actual __main__ bootstrap from a mount-like location and an
    # unrelated cwd. The unknown action exits before any producing operation.
    mounted = tmp_path / "mount" / "updater-candidate-fixture.py"
    mounted.parent.mkdir()
    mounted.write_bytes((harness.ROOT / "scripts/ci/updater_candidate_fixture.py").read_bytes())
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("AI_SHUFFLE_UPDATER_FIXTURE", "1")
    monkeypatch.setenv("DATA_DIR", "/data")
    monkeypatch.setattr(sys, "path", [str(mounted.parent), *(path for path in sys.path if path != "/app")])
    monkeypatch.setattr(sys, "argv", [str(mounted), "unknown"])
    with pytest.raises(SystemExit, match="Unknown fixture action"):
        runpy.run_path(str(mounted), run_name="__main__")
    assert sys.path[0] == "/app"  # parent of both the packaged app and local docker namespace


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return 1000 + self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now = round(self.now + seconds, 3)


def write_json(path, value):
    # Like the producer, publish via replacement so each generation is distinct.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def test_barrier_waits_for_real_watch_service_before_short_lived_install(tmp_path, monkeypatch):
    from docker import application_updates as updates
    from docker import container_runtime as runtime
    from scripts.ci import updater_candidate_fixture as fixture

    clock = Clock()
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setattr(fixture, "time", clock)
    monkeypatch.setattr(runtime, "time", clock)
    monkeypatch.setattr(updates, "time", clock)
    image_revision, revision = "a" * 40, "b" * 40
    write_json(tmp_path / "ci-fixture.json", {"image_revision": image_revision, "revision": revision})
    updater = updates.ContainerUpdates(tmp_path, {"revision": image_revision}, b"unused", lambda: True, lambda _: {})
    updater.state = {"pending": revision}  # unit-only input, not a candidate build
    updater.fetch = Mock(side_effect=updates.UpdateError("Offline fixture network"))
    service_times = []

    def tick():
        service_times.append(clock.now)
        updater.service()  # real consumer, including expiry/policy/check publication

    updater.tick = tick
    stop = SimpleNamespace(is_set=lambda: clock.now >= 31, wait=clock.sleep)
    monkeypatch.setattr(runtime, "STOP", stop)
    monitor = runtime.ReadinessMonitor("http://unused/health/ready", probe=lambda _: True)

    def first_sleep(seconds):
        # Drive the real supervisor loop from cold start on the same fake clock.
        monkeypatch.setattr(clock, "sleep", Clock.sleep.__get__(clock))
        runtime.watch(SimpleNamespace(poll=lambda: None), SimpleNamespace(process=None), None,
                      monitor=monitor, updates=updater)

    monkeypatch.setattr(clock, "sleep", first_sleep)
    fixture.wait_for_service()
    assert monitor.ever_ready
    assert service_times[0] >= runtime.READY_PROBE_INTERVAL == 30
    initial_request = json.loads((tmp_path / "ci-request.json").read_text())
    assert initial_request["action"] == "check"
    assert initial_request["expires_at"] < clock.time()  # boot-gated check expired
    assert not updater.install_requested.is_set()
    assert not (tmp_path / "request.json").exists()
    assert json.loads((tmp_path / "status.json").read_text())["error"] == "Offline fixture network"

    fixture.submit_request("install")
    queued = json.loads((tmp_path / "request.json").read_text())
    assert queued["expires_at"] - clock.time() == 10  # no TTL extension/requeue
    clock.sleep(1)
    updater.service()
    assert updater.install_requested.is_set()
    assert not (tmp_path / "request.json").exists()


@pytest.mark.parametrize("initial,fresh", [
    ({"phase": "ready", "checked_at": 900}, None),  # persisted preparation
    ({"phase": "ready"}, None),  # constructor publication
    ({"phase": "ready"}, {"phase": "ready"}),  # a new constructor-only publication
    ({"phase": "idle", "checked_at": 900}, None),  # old completed check
    ({"phase": "ready", "error": "old error"}, None),
    ({"phase": "ready"}, {"phase": "checking", "checked_at": 1001}),
    ({"phase": "ready"}, {"phase": "downloading", "error": "old error"}),
    ({"phase": "ready"}, {"phase": "idle", "checked_at": 1001, "updater": {"revision": "c" * 40}}),
])
def test_barrier_rejects_stale_unready_or_wrong_resident_publications(tmp_path, monkeypatch, initial, fresh):
    from scripts.ci import updater_candidate_fixture as fixture
    clock = Clock()
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setattr(fixture, "time", clock)
    submit = Mock()
    monkeypatch.setattr(fixture, "submit_request", submit)
    resident = {"revision": "a" * 40}
    write_json(tmp_path / "ci-fixture.json", {"image_revision": resident["revision"]})
    write_json(tmp_path / "status.json", {"updater": resident, **initial})

    def sleep(seconds):
        clock.now += 30
        if fresh:
            write_json(tmp_path / "status.json", {"updater": resident, **fresh})

    monkeypatch.setattr(clock, "sleep", sleep)
    with pytest.raises(TimeoutError, match="first service pass"):
        fixture.wait_for_service()
    submit.assert_called_once_with("check")


@pytest.mark.parametrize("row,allowed", [(None, True), (("true",), True), (("false",), False)])
def test_diagnostics_capture_actual_files_and_read_only_policy(tmp_path, monkeypatch, capsys, row, allowed):
    import psycopg2
    from app.core.config import settings
    from scripts.ci import updater_candidate_fixture as fixture
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    census = {"browser": []}  # diagnostic unit input, not an ownership proof
    monkeypatch.setattr(fixture, "lifecycle_processes", Mock(return_value=census))
    monkeypatch.setattr(fixture, "browser_ready", Mock(return_value=False))
    write_json(tmp_path / "state.json", {"pending": "a" * 40, "active": None, "private": "must-not-copy"})
    write_json(tmp_path / "request.json", {"action": "install", "expires_at": 1000})
    write_json(tmp_path / "status.json", {"phase": "ready", "updater": {"revision": "b" * 40}})
    (tmp_path / "handover.json").write_text("x" * 16385)
    monkeypatch.setattr(type(settings), "get_database_connection", lambda _: {"host": "synthetic", "password": "must-not-copy"})
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = row
    connect = Mock(return_value=connection)
    monkeypatch.setattr(psycopg2, "connect", connect)
    fixture.diagnose()
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["files"]["state.json"] == {"pending": "a" * 40, "active": None}
    assert report["files"]["request.json"]["action"] == "install"
    assert report["files"]["status.json"]["updater"]["revision"] == "b" * 40
    assert report["files"]["handover.json"] == {"unavailable": "ValueError"}
    assert report["files"]["ci-fixture.json"] == {"unavailable": "FileNotFoundError"}
    assert report["policy"] == {"allow_updates": allowed}
    assert report["processes"] == census
    assert report["browser_cdp_ready"] is False
    assert "must-not-copy" not in output
    assert connect.call_args.kwargs["connect_timeout"] == 5
    assert "default_transaction_read_only=on" in connect.call_args.kwargs["options"]
    cursor.execute.assert_called_once_with("SELECT value FROM system_settings WHERE key = %s", ("allow_updates",))
    connection.close.assert_called_once()


def test_policy_diagnostic_failure_reports_only_exception_type(tmp_path, monkeypatch, capsys):
    import psycopg2
    from app.core.config import settings
    from scripts.ci import updater_candidate_fixture as fixture
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setattr(fixture, "lifecycle_processes", Mock(return_value={}))
    monkeypatch.setattr(fixture, "browser_ready", Mock(return_value=False))
    monkeypatch.setattr(type(settings), "get_database_connection", lambda _: {})
    monkeypatch.setattr(psycopg2, "connect", Mock(side_effect=RuntimeError("must-not-copy connection details")))
    fixture.diagnose()
    output = capsys.readouterr().out
    assert json.loads(output)["policy"] == {"unavailable": "RuntimeError"}
    assert "must-not-copy" not in output


@pytest.mark.parametrize("chrome_timeout", [False, True])
def test_failed_install_captures_diagnostics_without_masking_failure(candidate, tmp_path, chrome_timeout):
    check, _ = candidate
    result = check.docker.return_value

    def docker(*args, **kwargs):
        if args[-1] == "installed":
            raise RuntimeError("original install timeout")
        if args[-1] == "diagnose" or (chrome_timeout and args[-1] == tmp_path / "updater-chrome.log"):
            raise subprocess.TimeoutExpired("docker", 20)
        return result

    check.docker.side_effect = docker
    with pytest.raises(RuntimeError, match="original install timeout"):
        check.updater_lifecycle()
    assert json.loads((tmp_path / "updater-lifecycle.json").read_text()) == {"unavailable": "diagnostic process timed out"}
    updated = check.containers[-1]
    assert check.docker.call_args.args == (
        "cp", updated + ":/home/appuser/.config/chrome.log", tmp_path / "updater-chrome.log",
    )


def process_fixture(root, revision, *, restarted=False, browser_title=True):
    directory = root / "versions" / revision
    resident = directory / "source/docker/container_runtime.py" if restarted else "/app/docker/container_runtime.py"
    argv = {
        # Entrypoint forwards empty argv; only source handover supplies "run".
        "10": ([str(directory / "venv/bin/python"), str(resident), "run"] if restarted else
               ["/usr/local/bin/python3", "-I", "-B", "-u", str(resident)]),
        "11": [str(directory / "venv/bin/python"), "-m", "app.main"],
        "12": ["postgres", "-D", "/data/postgres/data"],
        "13": ["/bin/bash", "/tmp/private/chrome-supervisor.sh"],
        "14": ["/usr/lib/chromium/chromium", "--ozone-platform=headless", "--remote-debugging-port=9221",
               "--user-data-dir=/tmp/synthetic-profile"],
        "15": ["postgres: checkpointer"],
        **{str(pid): ["/usr/lib/chromium/chromium", f"--type={kind}", "--user-data-dir=/tmp/synthetic-profile"]
           for pid, kind in enumerate(("renderer", "zygote", "gpu-process", "utility"), 16)},
    }
    if browser_title:
        # Read-only /proc evidence: the resident Debian Chromium root AND its
        # children expose one space-joined title, not the launcher's NUL argv.
        # Earlier fixtures used only the launch vector and missed both facts:
        # basename(title) is not chromium, and child --type= is inside title.
        for pid in ("14", "16", "17", "18", "19"):
            argv[pid] = [" ".join(argv[pid])]
    proc = root / "proc"
    for pid, args in argv.items():
        process = proc / pid
        process.mkdir(parents=True)
        (process / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
    return proc


@pytest.mark.parametrize("arguments", [[], ["run"]])
def test_census_recognizes_entrypoint_default_and_explicit_run(tmp_path, arguments):
    from scripts.ci import updater_candidate_fixture as fixture
    proc = process_fixture(tmp_path, "b" * 40)
    args = ["/usr/local/bin/python3", "-I", "-B", "-u", "/app/docker/container_runtime.py", *arguments]
    (proc / "10/cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
    assert fixture.lifecycle_processes(proc)["supervisor"] == [
        {"pid": 10, "executable": args[0], "entry": "/app/docker/container_runtime.py"},
    ]


@pytest.mark.parametrize("args", [
    ["/usr/local/bin/python3", "/app/docker/container_runtime.py", "dump", "--file", "-"],
    ["/usr/local/bin/python3", "/app/docker/container_runtime.py", "restore", "--file", "/tmp/dump"],
    ["/usr/local/bin/python3", "/app/docker/container_runtime.py", "validate-isolation"],
    ["/usr/local/bin/python3", "/app/docker/container_runtime.py", "remap-home"],
    ["/usr/local/bin/python3", "-c", "/app/docker/container_runtime.py", "run"],
    ["/usr/local/bin/python3", "-m", "helper", "/app/docker/container_runtime.py", "run"],
    ["/usr/local/bin/python3", "/tmp/helper.py", "/app/docker/container_runtime.py", "run"],
    ["/bin/echo", "/app/docker/container_runtime.py", "run"],
    ["/usr/bin/tini", "--", "/usr/local/bin/python3", "/app/docker/container_runtime.py", "run"],
    ["/usr/local/bin/python3", "/app/docker/container_runtime.py", "run", "dump"],
])
def test_census_does_not_mistake_maintenance_or_argument_mentions_for_supervisor(tmp_path, args):
    from scripts.ci import updater_candidate_fixture as fixture
    proc = process_fixture(tmp_path, "b" * 40)
    (proc / "10/cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
    assert fixture.lifecycle_processes(proc)["supervisor"] == []


@pytest.mark.parametrize("restarted", [False, True])
@pytest.mark.parametrize("browser_title", [False, True])
def test_lifecycle_census_counts_owned_roots_not_children(tmp_path, restarted, browser_title):
    from scripts.ci import updater_candidate_fixture as fixture
    revision = "b" * 40
    proc = process_fixture(tmp_path, revision, restarted=restarted, browser_title=browser_title)
    processes = fixture.lifecycle_processes(proc)
    assert all(len(owned) == 1 for owned in processes.values())
    assert processes["postgres"][0]["pid"] == 12
    # Receipts must not leak the title's flags, profile path or page URL.
    assert processes["browser"] == [{"pid": 14, "executable": "/usr/lib/chromium/chromium"}]
    assert (processes["supervisor"][0]["entry"].startswith(str(tmp_path))) is restarted


@pytest.mark.parametrize("args", [
    [f"/usr/lib/chromium/chromium --type={kind} --remote-debugging-port=9221"]
    for kind in ("renderer", "zygote", "gpu-process", "utility")
] + [
    ["/usr/lib/chromium/chromium", "--type=renderer", "--remote-debugging-port=9221"],
    ["node", "/opt/browser-mcp/dist/egress-launch.js", "--", "/usr/lib/chromium/chromium"],
    ["node /opt/browser-mcp/dist/egress-launch.js -- /usr/lib/chromium/chromium"],
    ["/bin/bash", "/usr/bin/chromium", "--remote-debugging-port=9221"],
    ["/bin/echo", "/usr/lib/chromium/chromium --remote-debugging-port=9221"],
    ["/bin/echo /usr/lib/chromium/chromium --remote-debugging-port=9221"],
    ["/usr/lib/chromium/chrome_crashpad_handler", "--monitor-self"],
    ["/usr/bin/chromium-helper --remote-debugging-port=9221"],
    ["   "],
])
def test_browser_census_rejects_child_wrapper_and_argument_mentions(tmp_path, args):
    from scripts.ci import updater_candidate_fixture as fixture
    proc = process_fixture(tmp_path, "b" * 40)
    (proc / "14/cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
    assert fixture.lifecycle_processes(proc)["browser"] == []


@pytest.mark.parametrize("scenario", [
    "ready", "connection-failed", "http-error", "invalid-json", "not-object", "missing-websocket", "wrong-listener", "empty-id",
])
def test_browser_readiness_checks_cdp_response_without_proxy_env(monkeypatch, scenario):
    import httpx
    from scripts.ci import updater_candidate_fixture as fixture
    client = httpx.Client
    options, requests = [], []
    prefix = "ws://127.0.0.1:9221/devtools/browser/"

    def respond(request):
        requests.append(request)
        if scenario == "connection-failed":
            raise httpx.ConnectError("synthetic unavailable listener", request=request)
        if scenario == "http-error":
            return httpx.Response(503)
        if scenario == "invalid-json":
            return httpx.Response(200, text="not json")
        body = {
            "ready": {"webSocketDebuggerUrl": prefix + "synthetic-id"},
            "not-object": [],
            "missing-websocket": {},
            "wrong-listener": {"webSocketDebuggerUrl": "ws://example.invalid/devtools/browser/id"},
            "empty-id": {"webSocketDebuggerUrl": prefix},
        }[scenario]
        return httpx.Response(200, json=body)

    def transport_client(**kwargs):
        options.append(kwargs)
        return client(transport=httpx.MockTransport(respond), **kwargs)

    # No service or browser is started by this protocol unit test.
    monkeypatch.setattr(httpx, "Client", transport_client)
    assert fixture.browser_ready() is (scenario == "ready")
    assert options == [{"trust_env": False, "timeout": 1}]
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "http://127.0.0.1:9221/json/version"


@pytest.mark.parametrize("mode", ["installed", "restarted"])
@pytest.mark.parametrize("ready_after", [0, 2])
def test_resident_proof_uses_status_process_entry_and_preserves_receipt(tmp_path, monkeypatch, capsys, mode, ready_after):
    from scripts.ci import updater_candidate_fixture as fixture
    revision, image = "b" * 40, "a" * 40
    restarted = mode == "restarted"
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    clock = Clock()
    monkeypatch.setattr(fixture, "time", clock)
    proc = process_fixture(tmp_path, revision, restarted=restarted)
    census = fixture.lifecycle_processes(proc)
    monkeypatch.setattr(fixture, "lifecycle_processes", lambda: census)
    ready = Mock(side_effect=[False] * ready_after + [True])  # CDP startup, not a real browser
    monkeypatch.setattr(fixture, "browser_ready", ready)
    write_json(tmp_path / "ci-fixture.json", {"revision": revision, "image_revision": image})
    write_json(tmp_path / "ci-request.json", {"action": "install", "expires_at": 1000})
    write_json(tmp_path / "state.json", {"active": revision})
    write_json(tmp_path / "status.json", {
        "updater": {"revision": revision if restarted else image},
        "diagnostics": {"revision": revision, "stages": [{"stage": "tools_build", "outcome": "hit"}]},
    })
    fixture.wait_for(mode)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["mode"] == mode
    assert receipt["updater"]["revision"] == (revision if restarted else image)
    assert receipt["app_revision"] == revision
    assert receipt["processes"] == census
    assert ready.call_count == ready_after + 1
    assert clock.now == ready_after
    assert "observed_absent_at" in json.loads((tmp_path / "ci-request.json").read_text())


@pytest.mark.parametrize("failure", [
    "old-resident", "booting", "trial", "image-app", "image-supervisor", "browser-unready",
    *[f"{fault}-{role}" for fault in ("missing", "duplicate")
      for role in ("supervisor", "application", "postgres", "browser_supervisor", "browser")],
    "missing-browser-unready", "duplicate-browser-unready",
])
def test_restart_proof_rejects_stale_unhealthy_or_duplicate_owners(tmp_path, monkeypatch, failure):
    from scripts.ci import updater_candidate_fixture as fixture
    revision, image = "b" * 40, "a" * 40
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setattr(fixture, "time", Clock())
    monkeypatch.setattr(fixture, "browser_ready", Mock(return_value=not failure.endswith("unready")))
    proc = process_fixture(tmp_path, revision, restarted=True)
    census = fixture.lifecycle_processes(proc)
    if failure.startswith(("missing-", "duplicate-")):
        role = failure.split("-")[1]
        cmdline = proc / str(census[role][0]["pid"]) / "cmdline"
        if failure.startswith("missing-"):
            cmdline.unlink()  # only this test's synthetic proc file
        else:
            (proc / "20").mkdir()
            (proc / "20/cmdline").write_bytes(cmdline.read_bytes())
    elif failure == "image-app":
        (proc / "11/cmdline").write_bytes(b"/usr/local/bin/python3\0-m\0app.main\0")
    elif failure == "image-supervisor":
        (proc / "10/cmdline").write_bytes(b"/usr/local/bin/python3\0/app/docker/container_runtime.py\0run\0")
    read_processes = fixture.lifecycle_processes
    monkeypatch.setattr(fixture, "lifecycle_processes", lambda: read_processes(proc))
    write_json(tmp_path / "ci-fixture.json", {"revision": revision, "image_revision": image})
    write_json(tmp_path / "ci-request.json", {"action": "install", "expires_at": 1000})
    write_json(tmp_path / "state.json", {"active": revision, **({"booting": revision} if failure == "booting" else {})})
    write_json(tmp_path / "status.json", {
        "updater": {"revision": image if failure == "old-resident" else revision},
        "diagnostics": {"revision": revision, "stages": [{"stage": "tools_build", "outcome": "hit"}]},
    })
    if failure == "trial":
        write_json(tmp_path / "handover.json", {"revision": revision})
    expected_error = TimeoutError if failure in (
        "old-resident", "booting", "trial", "browser-unready", "missing-browser-unready",
    ) else AssertionError
    with pytest.raises(expected_error):
        fixture.wait_for("restarted")


@pytest.mark.parametrize("role", ["supervisor", "application", "postgres", "browser_supervisor", "browser"])
@pytest.mark.parametrize("fault", ["missing", "duplicate"])
def test_final_census_rejects_owner_changes_after_cdp_ready(tmp_path, monkeypatch, role, fault):
    from scripts.ci import updater_candidate_fixture as fixture
    revision, image = "b" * 40, "a" * 40
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setattr(fixture, "time", Clock())
    proc = process_fixture(tmp_path, revision, restarted=True)
    before = fixture.lifecycle_processes(proc)
    cmdline = proc / str(before[role][0]["pid"]) / "cmdline"
    if fault == "missing":
        cmdline.unlink()  # only this test's synthetic proc file
    else:
        (proc / "20").mkdir()
        (proc / "20/cmdline").write_bytes(cmdline.read_bytes())
    after = fixture.lifecycle_processes(proc)
    census = Mock(side_effect=[before, after])
    monkeypatch.setattr(fixture, "lifecycle_processes", census)
    ready = Mock(return_value=True)  # synthetic CDP; the producing Docker path is CI-only
    monkeypatch.setattr(fixture, "browser_ready", ready)
    write_json(tmp_path / "ci-fixture.json", {"revision": revision, "image_revision": image})
    write_json(tmp_path / "ci-request.json", {"action": "install", "expires_at": 1000})
    write_json(tmp_path / "state.json", {"active": revision})
    write_json(tmp_path / "status.json", {
        "updater": {"revision": revision},
        "diagnostics": {"revision": revision, "stages": [{"stage": "tools_build", "outcome": "hit"}]},
    })
    with pytest.raises(AssertionError):
        fixture.wait_for("restarted")
    assert census.call_count == 2
    ready.assert_called_once_with()


def test_fixture_refuses_legacy_direct_refresh_without_writing_requests(tmp_path, monkeypatch):
    from scripts.ci import updater_candidate_fixture as fixture
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setenv("AI_SHUFFLE_UPDATER_FIXTURE", "1")
    monkeypatch.setenv("DATA_DIR", "/data")
    monkeypatch.setattr(sys, "argv", ["fixture", "refresh-updater"])
    with pytest.raises(SystemExit, match="Unknown fixture action"):
        fixture.main()
    with pytest.raises(SystemExit, match="Unknown fixture action"):
        fixture.submit_request("refresh-updater")
    assert not (tmp_path / "request.json").exists()


def test_fixture_refuses_to_run_outside_disposable_candidate(monkeypatch):
    from scripts.ci import updater_candidate_fixture as fixture
    monkeypatch.delenv("AI_SHUFFLE_UPDATER_FIXTURE", raising=False)
    monkeypatch.setenv("DATA_DIR", "/not-the-fixture")
    with pytest.raises(SystemExit, match="disposable"):
        fixture.main()
