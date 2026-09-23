import base64
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import zipfile

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core import application_updates as policy
from docker.application_updates import (ContainerUpdates, UpdateError, build_environment, extract_source,
                                        tree_lines, verify_manifest)

A, B, C = "a" * 40, "b" * 40, "c" * 40
RUNTIME = "d" * 64


@pytest.fixture
def feed(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    responses, requests = {}, []
    def publish(revision=B, sequence=2, runtime=RUNTIME, entries=None, channel="stable", tree=None, **fields):
        output = io.BytesIO()
        entries = entries or {f"ai-shuffle-{revision}/source.txt": "content"}
        with zipfile.ZipFile(output, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        archive = output.getvalue()
        files = [(name.split("/", 1)[1], hashlib.sha256(value.encode()).hexdigest())
                 for name, value in entries.items() if not name.endswith("/")]
        release = {"format": 2, "revision": revision, "sequence": sequence,
                   "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive),
                   "container": {"sequence": sequence, "runtime": runtime},
                   "channel": channel, "tree": tree or hashlib.sha256(tree_lines(files)).hexdigest(), **fields}
        payload = json.dumps({key: value for key, value in release.items() if value is not None}).encode()
        envelope = json.dumps({"payload": base64.b64encode(payload).decode(),
                               "signature": base64.b64encode(key.sign(payload)).decode()}).encode()
        responses["/v1/desktop/manifest" + ("" if channel == "stable" else "/edge")] = envelope
        responses["/v1/desktop/sources/" + revision] = archive
        return envelope
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=responses[request.url.path])
    client = httpx.Client
    monkeypatch.setattr("docker.application_updates.httpx.Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    enabled = [True]
    image = {"revision": A, "sequence": 1, "runtime": RUNTIME}
    updater = ContainerUpdates(tmp_path / "updates", image, public, lambda: enabled[0], lambda action: {})
    def build(directory, revision, version=None):
        assert (directory / "source/source.txt").read_text() == "content"
        for name in ("venv/bin/python", "source/app/static/index.html", "complete"):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(revision)
    monkeypatch.setattr(updater, "build", build)
    return updater, publish, enabled, requests, responses


def test_signed_download_stages_then_explicit_restart_and_health_commit(feed):
    updater, publish, enabled, requests, _ = feed
    publish()
    updater.check()
    assert len(requests) == 1 and not updater.state.get("pending")
    updater.check(download=True)
    assert updater.state == {"pending": B}
    assert updater.launch({})[1] is None, "ordinary container restart must not activate staging"
    command, cwd, env = updater.launch({"PYTHONPATH": "wrong", "PATH": "/usr/bin"}, activate=True)
    assert Path(command[0]).parts[-5:] == ("versions", B, "venv", "bin", "python")
    assert "PYTHONPATH" not in env and env["GIT_SHA"] == B
    assert not updater.state.get("booting"), "validation must not mark old worker as new"
    updater.activating()
    updater.healthy()
    assert updater.state == {"active": B}
    offline = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: False, lambda _: {})
    assert offline.launch({})[1] == cwd
    assert all("authorization" not in request.headers for request in requests)


def test_lease_goes_to_hub_manifest_and_archive_requests_only(feed):
    updater, publish, _, requests, _ = feed
    publish()
    calls = []
    def lease():
        calls.append(1)
        return "eyJsZWFzZSI6MX0"
    updater.lease = lease
    updater.check(download=True)
    assert [request.url.path for request in requests] == ["/v1/desktop/manifest", "/v1/desktop/sources/" + B]
    for request in requests:
        assert request.url.scheme == "https" and request.url.host == "hobbycoders.com"
        assert request.headers["x-shuffle-lease"] == "eyJsZWFzZSI6MX0"
    assert len(calls) == 2


@pytest.mark.parametrize("value", [None, "", "not a lease!", "x" * 4097, 42, RuntimeError("app not ready")])
def test_missing_or_bad_lease_sends_no_header(feed, value):
    updater, publish, _, requests, _ = feed
    publish()
    def lease():
        if isinstance(value, Exception):
            raise value
        return value
    updater.lease = lease
    updater.check(download=True)
    assert updater.state == {"pending": B}
    assert requests and all("x-shuffle-lease" not in request.headers for request in requests)


def test_no_lease_provider_sends_no_header(feed):
    updater, publish, _, requests, _ = feed
    publish()
    updater.check()
    assert requests and "x-shuffle-lease" not in requests[0].headers


def test_disabled_before_download_and_after_staging_blocks_activation(feed):
    updater, publish, enabled, requests, _ = feed
    publish()
    enabled[0] = False
    with pytest.raises(UpdateError, match="disabled"):
        updater.check(download=True)
    assert requests == []
    enabled[0] = True
    updater.check(download=True)
    enabled[0] = False
    with pytest.raises(UpdateError, match="disabled"):
        updater.launch({}, activate=True)
    assert not updater.state.get("booting")


def test_policy_disabled_during_build_never_stages(feed, monkeypatch):
    updater, publish, enabled, _, _ = feed
    publish()
    monkeypatch.setattr(updater, "build", lambda *_: enabled.__setitem__(0, False))
    with pytest.raises(UpdateError, match="disabled"):
        updater.check(download=True)
    assert updater.state == {} and not updater.directory(B).exists()


def test_runtime_mismatch_requires_image_and_older_source_is_ignored(feed):
    updater, publish, _, requests, _ = feed
    publish(runtime="e" * 64)
    updater.check(download=True)
    assert json.loads((updater.root / "status.json").read_text())["phase"] == "image-required"
    assert len(requests) == 1
    publish(sequence=1)
    updater.check(download=True)
    assert updater.state == {}


def test_cleanup_retains_active_previous_staged_and_failed_revisions(feed):
    updater, _, _, _, _ = feed
    for revision in (A, B, C, "e" * 40, "f" * 40):
        updater.directory(revision).mkdir(parents=True)
    updater.state = {"active": A, "previous": B, "pending": C, "failed": ["e" * 40]}
    updater.prune()
    assert updater.directory(A).exists() and updater.directory(B).exists()
    assert updater.directory(C).exists() and updater.directory("e" * 40).exists()
    assert not updater.directory("f" * 40).exists()


def test_new_image_discards_obsolete_pending_and_never_downgrades(feed):
    updater, publish, _, _, _ = feed
    publish()
    updater.check(download=True)
    newer = ContainerUpdates(updater.root, dict(updater.image, revision=C, sequence=3), updater.public_key, lambda: True, lambda _: {})
    assert newer.state == {} and newer.launch({})[1] is None
    with pytest.raises(UpdateError, match="No application update"):
        newer.launch({}, activate=True)


def test_failed_first_boot_never_rolls_back_and_newer_milestone_can_recover(feed):
    updater, publish, _, _, _ = feed
    publish()
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.activating()
    with pytest.raises(UpdateError, match="failed first boot"):
        ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {})
    with pytest.raises(UpdateError, match="failed first boot"):
        ContainerUpdates(updater.root, dict(updater.image, revision=B, sequence=2), updater.public_key, lambda: True, lambda _: {})
    newer = ContainerUpdates(updater.root, dict(updater.image, revision=C, sequence=3), updater.public_key, lambda: True, lambda _: {})
    assert newer.state == {} and newer.launch({})[1] is None


def test_bad_signature_and_checksum_rejected_before_build(feed):
    updater, publish, _, _, responses = feed
    signed = publish()
    envelope = json.loads(signed)
    envelope["signature"] = base64.b64encode(b"bad").decode()
    with pytest.raises(UpdateError, match="unsigned"):
        verify_manifest(json.dumps(envelope).encode(), updater.public_key)
    responses["/v1/desktop/sources/" + B] = b"corrupt"
    with pytest.raises(UpdateError, match="checksum"):
        updater.check(download=True)
    assert updater.state == {} and not updater.directory(B).exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", f"ai-shuffle-{B}/../escape", f"ai-shuffle-{B}/a\\b", f"ai-shuffle-{B}/C:drive"])
def test_archive_rejects_traversal_before_writes(tmp_path, monkeypatch, name):
    class ContainerZipInfo(zipfile.ZipInfo):
        def __init__(self, filename="NoName", *args, **kwargs):
            super().__init__(filename, *args, **kwargs)
            # Model the Linux container parser: Windows ZipInfo normalizes
            # backslashes on both write AND read, before our guard sees them.
            self.filename = self.orig_filename

    monkeypatch.setattr(zipfile, "ZipInfo", ContainerZipInfo)
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(name, "bad")
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.namelist() == [name]
    with pytest.raises(UpdateError):
        extract_source(archive, tmp_path / "source", B)
    assert not (tmp_path / "source").exists()


def test_archive_rejects_links(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        info = zipfile.ZipInfo(f"ai-shuffle-{B}/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(info, "/etc/passwd")
    with pytest.raises(UpdateError):
        extract_source(archive, tmp_path / "source", B)


def test_build_environment_never_inherits_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "secret")
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("NODE_OPTIONS", "secret")
    env = build_environment(tmp_path)
    assert all("secret" not in value for value in env.values())
    assert env["PIP_CONFIG_FILE"] == os.devnull


@pytest.mark.parametrize("value, expected", [(None, True), ("true", True), ("false", False), ("invalid", False)])
def test_default_enabled_policy_and_desktop_mirror(tmp_path, monkeypatch, value, expected):
    monkeypatch.setattr(policy.database, "get_system_setting", lambda key: value)
    mirror = tmp_path / "allow-updates.json"
    monkeypatch.setenv("SHUFFLE_UPDATE_POLICY_FILE", str(mirror))
    assert policy.mirror_policy() is expected
    assert json.loads(mirror.read_text()) == {"allow_updates": expected}


def test_account_policy_is_shared():
    from app.network.policy import is_shared_setting_key
    assert is_shared_setting_key(policy.KEY)

@pytest.mark.parametrize("failure", [
    "busy", "policy", "files", "preflight-timeout", "flush", "stop-timeout",
    "stop-response", "stop-group", "activating", "popen", "none",
])
def test_supervisor_restart_failure_boundary(feed, monkeypatch, tmp_path, failure):
    """Drive run -> watch -> restart and the real shutdown-latch producer.

    Only transport, process/DB boundaries and the flush failure are injected;
    staging validation, lifecycle authorization, shutdown and update state are real.
    """
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from starlette.requests import Request
    from app.api import internal
    from app.core import session_runtime, startup, task_persistence
    from app.core.config import Settings
    from app.core.task_registry import task_registry
    pytest.importorskip("fcntl", reason="The container supervisor requires POSIX file locking")
    from docker import container_runtime as supervisor
    from docker import application_updates as update_module

    updater, publish, enabled, _, _ = feed
    publish()
    updater.check(download=True)
    if failure == "files":
        (updater.directory(B) / "complete").unlink()
    if failure == "policy":
        enabled[0] = False

    # run loads image provenance and the updater beside its own source file.
    packaging = tmp_path / "packaging"
    packaging.mkdir()
    (packaging / "image.json").write_text(json.dumps(updater.image))
    (packaging / "application-update-key.pem").write_bytes(updater.public_key)
    (packaging / "application_updates.py").write_text(
        "from docker.application_updates import ContainerUpdates, UpdateError\n"
    )
    monkeypatch.setattr(supervisor, "__file__", str(packaging / "container_runtime.py"))
    def factory(root, image, public_key, allowed, lifecycle, channel, lease):
        assert channel == "stable", "customers follow signed releases unless UPDATE_CHANNEL says otherwise"
        updater.allowed, updater.lifecycle, updater.lease = allowed, lifecycle, lease
        return updater
    monkeypatch.setattr(update_module, "ContainerUpdates", factory)
    monkeypatch.setattr(startup, "is_shutting_down", False)
    monkeypatch.setattr(internal, "_shutdown_task", None)
    monkeypatch.setattr(session_runtime, "_executions", {})
    monkeypatch.setattr(task_registry, "active_session_ids", lambda: ["busy"] if failure == "busy" else [])
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "DATA_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("AI_SHUFFLE_ENV_FILE", "/dev/null")
    monkeypatch.setenv("START_CHROME", "0")

    events, actions = [], []
    release_flush = asyncio.Event()
    async def flush():
        assert startup.is_shutting_down, "the real producer must set the latch first"
        events.append("flush")
        if failure == "flush":
            raise OSError("synthetic flush failure")
        if failure == "stop-timeout":
            await release_flush.wait()
    flush_mock = AsyncMock(side_effect=flush)
    monkeypatch.setattr(task_persistence, "flush", flush_mock)

    with asyncio.Runner() as runner:
        def handler(request):
            action = request.url.path.rsplit("/", 1)[-1]
            actions.append(action)
            local_request = Request({"type": "http", "shuffle.ingress": "local",
                                     "headers": [(k.encode(), v.encode()) for k, v in request.headers.items()]})
            if action == "update-policy":
                internal.require_desktop(local_request)
                return httpx.Response(200, json={"allow_updates": enabled[0]})
            if action == "source-lease":
                internal.require_desktop(local_request)
                return httpx.Response(200, json={"lease": "bGVhc2U"})
            if action == "active-sessions":
                if failure == "preflight-timeout":
                    raise httpx.ReadTimeout("synthetic preflight timeout", request=request)
                result = runner.run(internal.desktop_active_sessions(local_request))
            else:
                assert action == "stop-sessions"
                async def stop():
                    return await asyncio.wait_for(internal.desktop_stop_sessions(local_request), 0.01)
                try:
                    result = runner.run(stop())
                except TimeoutError as exc:
                    assert startup.is_shutting_down
                    assert not internal._shutdown_task.done(), "HTTP cancellation must not cancel cleanup"
                    raise httpx.ReadTimeout("synthetic stop timeout", request=request) from exc
                except OSError:
                    return httpx.Response(500)
                if failure == "stop-response":
                    result = {"stopped": False}
            return httpx.Response(200, json=result)

        # feed patches the shared httpx module: use a captured real client type.
        with httpx.Client() as client:
            client_type = type(client)
        monkeypatch.setattr(httpx, "Client", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
        db = Mock(process=None)
        db.close.side_effect = lambda: events.append("db-close")
        monkeypatch.setattr(supervisor, "Database", lambda settings: db)
        old, new = Mock(name="old"), Mock(name="new")
        old.poll.return_value = new.poll.return_value = None
        def popen(*args, **kwargs):
            if "initial-start" not in events:
                events.append("initial-start")
                monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", kwargs["env"]["SHUFFLE_DESKTOP_LIFECYCLE_TOKEN"])
                return old
            events.append("replacement-start")
            assert updater.state["booting"] == B
            if failure == "popen":
                raise OSError("synthetic spawn failure")
            return new
        spawn = Mock(side_effect=popen)
        monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
        def stop_group(process, timeout):
            if process is None:
                return
            events.append("stop-old" if process is old else "stop-new")
            if failure == "stop-group" and events.count("stop-old") == 1:
                raise OSError("synthetic process stop failure")
            process.poll.return_value = 0
            if failure == "stop-timeout":
                assert startup.is_shutting_down
                assert not internal._shutdown_task.done()
                release_flush.set()
                async def finish_stop():
                    await internal._shutdown_task
                runner.run(finish_stop())
        monkeypatch.setattr(supervisor, "stop_group", stop_group)
        if failure == "activating":
            monkeypatch.setattr(updater, "activating", Mock(side_effect=OSError("synthetic state write failure")))

        # Deterministic watch clock, with a guard against silently continuing.
        stop_event = Mock(wraps=threading.Event())
        stop_event.is_set.side_effect = [False, False, False, True]
        monkeypatch.setattr(supervisor, "STOP", stop_event)
        clock = iter(range(100))
        monkeypatch.setattr(supervisor, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        monkeypatch.setattr(supervisor, "READY_PROBE_INTERVAL", 1000)
        monitor_type = supervisor.ReadinessMonitor
        monkeypatch.setattr(supervisor, "ReadinessMonitor", lambda url: monitor_type(url, probe=lambda _: False))
        safe = failure in {"busy", "policy", "files", "preflight-timeout"}
        def still_supervising(_):
            if safe:
                assert not startup.is_shutting_down
                assert old.poll() is None
                assert "stop-old" not in events
        stop_event.wait.side_effect = still_supervising
        updater.install_requested.set()
        tick = Mock()
        monkeypatch.setattr(updater, "tick", tick)
        status = Mock(wraps=updater.publish)
        monkeypatch.setattr(updater, "publish", status)
        result = supervisor.run(Settings(_env_file=None), "run")
        # The supervisor hands the updater the app's lease for the hub feed,
        # read with the lifecycle token from /internal/desktop/source-lease.
        assert updater.lease() == "bGVhc2U" and actions[-1] == "source-lease"

        assert result == (0 if safe or failure == "none" else 1)
        if safe:
            assert not startup.is_shutting_down
            flush_mock.assert_not_awaited()
            assert "stop-sessions" not in actions
            assert status.call_args.args == ("ready",)
            assert stop_event.wait.call_count == 2, "old worker remains supervised until external shutdown"
        else:
            assert startup.is_shutting_down, "recovery must never clear a real shutdown latch"
            flush_mock.assert_awaited_once()
            status.assert_not_called()
            if failure != "none":
                stop_event.wait.assert_not_called(), "fatal restart must exit watch immediately"
        assert events[-1] == "db-close"
        assert "stop-old" in events
        if failure in {"popen", "none"}:
            assert spawn.call_count == 2
            assert updater.state == {"pending": B, "booting": B}
            if failure == "none":
                assert events[-2] == "stop-new", "finally owns the replacement process"
            # A potentially migrated boot never falls back to the image on restart.
            with pytest.raises(UpdateError, match="older code cannot safely run"):
                ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: False, lambda _: {})
        else:
            spawn.assert_called_once()
            assert updater.state == {"pending": B}


def test_only_a_signed_release_for_the_configured_channel_is_offered(feed):
    updater, publish, _, requests, responses = feed
    # A hub serving an edge build at the stable address cannot relabel it.
    responses["/v1/desktop/manifest"] = publish(channel="edge", version="0.8.18-dev.43+g3667297")
    with pytest.raises(UpdateError, match="signed stable release"):
        updater.check(download=True)
    assert updater.state == {} and [r.url.path for r in requests] == ["/v1/desktop/manifest"]
    edge = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {}, channel="edge")
    edge.build = updater.build
    edge.check(download=True)
    assert requests[-2].url.path == "/v1/desktop/manifest/edge" and edge.state == {"pending": B}
    # Switching the worker back to stable never activates the staged edge build.
    stable = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {})
    assert stable.state == {}
    with pytest.raises(UpdateError, match="Unknown update channel"):
        ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {}, channel="beta")


@pytest.mark.parametrize("field, value", [("channel", "nightly"), ("tree", "short"),
                                          ("version", "latest; rm"), ("notes", "x" * 4001)])
def test_signed_manifest_with_malformed_release_fields_is_refused(feed, field, value):
    updater, publish, _, _, _ = feed
    with pytest.raises(UpdateError, match="Invalid or unsigned"):
        verify_manifest(publish(**{field: value}), updater.public_key)


def test_archive_that_differs_from_the_signed_tree_is_never_built(feed, monkeypatch):
    updater, publish, _, _, _ = feed
    publish(tree="0" * 64)
    built = []
    monkeypatch.setattr(updater, "build", lambda *args: built.append(args))
    with pytest.raises(UpdateError, match="do not match the signed release"):
        updater.check(download=True)
    assert built == [] and updater.state == {} and not updater.directory(B).exists()


def test_staged_tree_is_reverified_before_every_launch(feed):
    updater, publish, _, _, _ = feed
    publish(version="1.2.3", notes="Faster startup.")
    updater.check(download=True)
    _command, _cwd, env = updater.launch({}, activate=True)
    assert env["APP_VERSION"] == "1.2.3"
    source = updater.directory(B) / "source/source.txt"
    source.write_text("backdoor")
    with pytest.raises(UpdateError, match="changed after download"):
        updater.launch({}, activate=True)
    source.write_text("content")
    listing = updater.directory(B) / "tree.txt"
    listing.write_bytes(listing.read_bytes().replace(b"source.txt", b"sourcx.txt"))
    with pytest.raises(UpdateError, match="listing does not match"):
        updater.launch({}, activate=True)
    listing.unlink()
    with pytest.raises(UpdateError, match="listing is missing"):
        updater.launch({}, activate=True)


def test_release_name_and_notes_reach_the_status(feed):
    updater, publish, _, _, _ = feed
    publish(version="1.2.3", notes="Faster startup.")
    updater.check()
    status = json.loads((updater.root / "status.json").read_text())
    assert status["phase"] == "available" and status["release"] == "1.2.3"
    assert status["notes"] == "Faster startup." and status["channel"] == "stable"
    updater.check(download=True)
    reopened = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {})
    status = json.loads((reopened.root / "status.json").read_text())
    assert status["phase"] == "ready" and status["notes"] == "Faster startup."


def test_published_tree_digest_matches_what_the_updater_extracts(tmp_path):
    import subprocess
    from scripts import source_archive
    repo = tmp_path / "repo"
    for name, content in {"app/main.py": "print(1)", "fluid/fluid/__init__.py": "", "frontend/package.json": "{}",
                          "requirements.lock": "x", "requirements-native.lock": "x",
                          "docs/reviews/secret.md": "internal"}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)

    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x")
    revision = run("rev-parse", "HEAD")
    result = source_archive.build(revision, tmp_path / "source.zip", repo)
    files = extract_source(tmp_path / "source.zip", tmp_path / "source", revision)
    assert hashlib.sha256(tree_lines(files)).hexdigest() == result["tree"]
    assert not (tmp_path / "source/docs/reviews").exists()
