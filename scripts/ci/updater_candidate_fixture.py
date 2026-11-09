"""CI-only synthetic signed release, built by the real candidate-image updater.

Mounted read-only by verify_container_postgres, never shipped in the image or
source update. It runs only in disposable volumes, not a worker installation.
A passing fixture is not a production update result.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import zipfile

ROOT = Path("/data/application-updates")


def prepare(archive):
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from docker import application_updates as updates
    from docker.update_contract import manifest_fields

    image = json.loads(Path("/app/docker/image.json").read_text())
    data = Path(archive).read_bytes()
    revision = hashlib.sha256(b"candidate-updater-lifecycle-v1\0" + data).hexdigest()[:40]
    # New test release identity, unchanged real candidate source bytes. UI/Fluid
    # are rebuilt for this identity by the ordinary per-revision build recipe.
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            relative = item.filename.split("/", 1)[1]
            if relative and not item.is_dir():
                target.writestr(f"ai-shuffle-{revision}/{relative}", source.read(item))
    data = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        listing = updates.tree_lines([(item.filename.split("/", 1)[1], hashlib.sha256(source.read(item)).hexdigest())
                                      for item in source.infolist()])
    sequence = image["sequence"] + 1
    manifest = {"format": 2, "revision": revision, "sequence": sequence, "version": "9.9.0-dev.ci",
                "channel": "stable", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "tree": hashlib.sha256(listing).hexdigest(), "container": {**manifest_fields(), "sequence": sequence}}
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    payload = json.dumps(manifest).encode()
    signed = json.dumps({"payload": base64.b64encode(payload).decode(), "signature": base64.b64encode(key.sign(payload)).decode()}).encode()
    client = httpx.Client
    def serve(request):
        if request.url.path == "/v1/desktop/manifest":
            return httpx.Response(200, content=signed)
        if request.url.path == "/v1/desktop/sources/" + revision:
            return httpx.Response(200, content=data)
        return httpx.Response(404)
    # Only feed transport is synthetic; signature/hash/tree checks, extraction,
    # runtime selection, pip/npm builds and staging are the production producer.
    updates.httpx.Client = lambda **kwargs: client(transport=httpx.MockTransport(serve), **kwargs)
    updater = updates.ContainerUpdates(ROOT, image, public, lambda: True, lambda _: {})
    updater.check(download=True)
    assert updater.state == {"pending": revision}
    assert updater.diagnostics["revision"] == revision
    # Exercise actual compiled tools/browser warm reuse, not seeded output markers.
    source = updater.directory(revision) / "source"
    with tempfile.TemporaryDirectory(prefix="updater-warm-probe-") as scratch:
        import shutil
        scratch = Path(scratch)
        home = scratch / "home"
        home.mkdir()
        env = updates.build_environment(home)
        identity = updater.dependency_identity("node", None, source, env)
        assert identity is not None
        for path in source.iterdir():
            if path.is_file() and (path.suffix == ".json" or path.name == ".npmrc"):
                shutil.copyfile(path, scratch / path.name)
        for package in ("tools", "browser-mcp"):
            shutil.copytree(source / package, scratch / package, ignore=shutil.ignore_patterns("node_modules", "dist"))
            updater.node_dependencies(package, scratch, env, identity)
            updater.node_output(package, scratch, env, identity)
            assert updater.diagnostics["stages"][-1]["outcome"] == "hit"
    updater.publish("ready")
    (ROOT / "ci-public.pem").write_bytes(public)
    (ROOT / "ci-fixture.json").write_text(json.dumps({"revision": revision, "image_revision": image["revision"]}))
    print(json.dumps({"fixture": "synthetic-signed-source", "revision": revision, "stages": updater.diagnostics["stages"]}))


def wait_for_service():
    """Prove a startup command is consumed within its unchanged 10s validity.

    HTTP readiness can precede the periodic supervisor probe. Queue exactly one
    check, require prompt consumption, then wait for its completed publication
    from the expected resident (including a network-isolated feed error).
    """
    fixture = json.loads((ROOT / "ci-fixture.json").read_text())
    path = ROOT / "status.json"
    before = path.stat()
    generation = (before.st_ino, before.st_mtime_ns)
    # A single read-only check also covers a container whose automatic first
    # check finished before this barrier. It is never renewed or replayed.
    submit_request("check", require_ack=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        stamp = path.stat()
        status = json.loads(path.read_text())
        if ((stamp.st_ino, stamp.st_mtime_ns) != generation
                and status.get("updater", {}).get("revision") == fixture["image_revision"]
                and status.get("phase") not in ("checking", "downloading")
                and (status.get("checked_at") is not None or status.get("error") is not None)):
            return
        time.sleep(1)
    raise TimeoutError("Resident updater did not complete its first service pass")


def diagnose():
    """Bounded CI-only evidence; never copy credentials, descriptors or env."""
    fields = {
        "ci-fixture.json": ("revision", "image_revision"),
        "ci-request.json": ("action", "queued_at", "expires_at", "observed_absent_at"),
        "request.json": ("action", "expires_at"),
        "state.json": ("pending", "active", "previous", "booting", "blocked", "failed"),
        "status.json": ("phase", "checked_at", "error", "updater", "diagnostics"),
        "handover.json": ("revision",),
    }
    report = {"observed_at": time.time(), "files": {}, "processes": lifecycle_processes(),
              "browser_cdp_ready": browser_ready()}
    for name, keys in fields.items():
        try:
            with (ROOT / name).open() as stream:
                text = stream.read(16385)
            if len(text) > 16384:
                raise ValueError("oversized diagnostic")
            value = json.loads(text)
            report["files"][name] = {key: value[key] for key in keys if key in value}
        except (OSError, ValueError, TypeError) as exc:
            report["files"][name] = {"unavailable": type(exc).__name__}
    try:
        import psycopg2
        from app.core.config import settings
        # Same saved policy read by the real API's allowed(); no pool/schema
        # initialization, policy changes, or database credentials in the report.
        options = dict(settings.get_database_connection(), connect_timeout=5,
                       options="-c statement_timeout=5000 -c default_transaction_read_only=on")
        connection = psycopg2.connect(**options)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT value FROM system_settings WHERE key = %s", ("allow_updates",))
                row = cursor.fetchone()
                report["policy"] = {"allow_updates": row is None or row[0] == "true"}
        finally:
            connection.close()
    except Exception as exc:
        report["policy"] = {"unavailable": type(exc).__name__}
    print(json.dumps(report))


def lifecycle_processes(proc=Path("/proc")):
    """Owned lifecycle roots, not PG backends/Chromium renderers or full argv."""
    processes = {role: [] for role in ("supervisor", "application", "postgres", "browser_supervisor", "browser")}
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            with (process / "cmdline").open("rb") as stream:
                args = [part.decode(errors="replace") for part in stream.read(8192).split(b"\0") if part]
        except OSError:
            continue  # a short-lived process may exit during the census
        if not args:
            continue
        # Debian Chromium rewrites /proc/cmdline as one space-joined title for
        # both the browser and its children. Normalize only that browser view:
        # basename(title) misses the root, and checking unsplit args misses
        # child --type= switches. This is not shell-quoted argv (do not shlex).
        browser_args = args[0].split() if len(args) == 1 else args
        # Entrypoint forwards empty argv (main defaults to run); signed source
        # handover supplies an explicit run. Only Python's script position is
        # an entry, not a mention in tini, maintenance, -c, -m or wrapper args.
        command = args[1:]
        if Path(args[0]).name in ("python", "python3"):
            while command and command[0] in ("-I", "-B", "-u"):
                command = command[1:]
            entry = (command[0] if command and Path(command[0]).name == "container_runtime.py"
                     and command[1:] in ([], ["run"]) else None)
        else:
            entry = None
        role = None
        if entry:
            role = "supervisor"
        elif any(arg == "-m" and args[index + 1] == "app.main" for index, arg in enumerate(args[:-1])):
            role = "application"
        elif Path(args[0]).name == "postgres" and "-D" in args:
            role = "postgres"
        elif any(Path(arg).name in ("chrome-supervisor.sh", "chrome-supervisor") for arg in args[:2]):
            role = "browser_supervisor"
        elif (browser_args and Path(browser_args[0]).name in ("chromium", "chrome", "google-chrome-stable")
              and not any(arg.startswith("--type=") for arg in browser_args)):
            role = "browser"
        if role:
            executable = browser_args[0] if role == "browser" else args[0]
            processes[role].append({"pid": int(process.name), "executable": executable, **({"entry": entry} if entry else {})})
    return processes


def browser_ready():
    """The shell eagerly launches Chrome; app readiness is not CDP readiness.

    NativeChrome's shutdown message only describes the app watchdog in Docker,
    not the separate shell owner. Probe the real listener, without launching a
    second browser or treating an absent browser as a successful census.
    """
    import httpx
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get("http://127.0.0.1:9221/json/version")
            response.raise_for_status()
            version = response.json()
        endpoint = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
        prefix = "ws://127.0.0.1:9221/devtools/browser/"
        return isinstance(endpoint, str) and endpoint.startswith(prefix) and len(endpoint) > len(prefix)
    except (httpx.HTTPError, ValueError):
        return False


def wait_for(mode):
    fixture = json.loads((ROOT / "ci-fixture.json").read_text())
    revision = fixture["revision"]
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        # Record disappearance separately from success: the consumer can also
        # remove an expired or refused request. Only the state proofs below pass.
        request = json.loads((ROOT / "ci-request.json").read_text())
        if not (ROOT / "request.json").exists() and "observed_absent_at" not in request:
            from app.core.application_updates import atomic_json
            atomic_json(ROOT / "ci-request.json", dict(request, observed_absent_at=time.time()))
        state = json.loads((ROOT / "state.json").read_text())
        status = json.loads((ROOT / "status.json").read_text())
        expected = revision if mode == "restarted" else fixture["image_revision"]
        if (state.get("active") == revision and not state.get("booting") and not state.get("blocked")
                and status.get("updater", {}).get("revision") == expected
                and (mode != "restarted" or not (ROOT / "handover.json").exists())):
            assert status["diagnostics"]["revision"] == revision
            assert any(item["stage"] == "tools_build" and item["outcome"] == "hit" for item in status["diagnostics"]["stages"])
            processes = lifecycle_processes()
            # Refuse duplicates and missing non-browser owners even while CDP
            # is starting/recovering. The existing 240s deadline is unchanged.
            assert all(len(owned) == 1 for role, owned in processes.items() if role != "browser"), processes
            assert len(processes["browser"]) <= 1, processes
            if browser_ready():
                # A responsive listener never substitutes for the owned roots.
                processes = lifecycle_processes()
                assert all(len(owned) == 1 for owned in processes.values()), processes
                directory = ROOT / "versions" / revision
                assert processes["application"][0]["executable"] == str(directory / "venv/bin/python")
                resident = directory / "source/docker/container_runtime.py" if mode == "restarted" else Path("/app/docker/container_runtime.py")
                assert processes["supervisor"][0]["entry"] == str(resident)
                print(json.dumps({"mode": mode, "app_revision": revision, "updater": status["updater"], "processes": processes}))
                return
        if state.get("blocked") or revision in state.get("failed", []):
            raise AssertionError("Candidate update failed or blocked")
        time.sleep(1)
    raise TimeoutError("Candidate updater lifecycle did not complete")


def submit_request(action, *, require_ack=False):
    if action not in ("check", "install"):
        raise SystemExit("Unknown fixture action")
    from app.core.application_updates import atomic_json, CONTAINER_REQUEST_SECONDS
    queued = time.time()
    request = {"action": action, "expires_at": queued + CONTAINER_REQUEST_SECONDS}
    atomic_json(ROOT / "ci-request.json", dict(request, queued_at=queued))
    atomic_json(ROOT / "request.json", request)
    if require_ack:
        # Disappearance after expiry proves nothing: the consumer also removes
        # expired commands. Keep this proof bounded, and never renew/requeue it.
        while (ROOT / "request.json").exists() and time.time() < request["expires_at"]:
            time.sleep(0.2)
        observed = time.time()
        if (ROOT / "request.json").exists() or observed >= request["expires_at"]:
            raise TimeoutError("Resident updater did not consume the startup command within its validity window")
        atomic_json(ROOT / "ci-request.json", dict(request, queued_at=queued, observed_absent_at=observed))


def main():
    if os.environ.get("AI_SHUFFLE_UPDATER_FIXTURE") != "1" or os.environ.get("DATA_DIR") != "/data":
        raise SystemExit("Only the disposable candidate-container harness may run this fixture")
    action = sys.argv[1]
    if action == "prepare":
        prepare(sys.argv[2])
    elif action == "updater-ready":
        wait_for_service()
    elif action == "diagnose":
        diagnose()
    elif action in ("installed", "restarted"):
        wait_for(action)
    elif action == "install":
        submit_request(action)
    else:
        raise SystemExit("Unknown fixture action")


if __name__ == "__main__":
    # Direct execution starts sys.path at the bind-mounted script's directory,
    # not WORKDIR. Use the packaged image's app/docker modules explicitly; do
    # not depend on PYTHONPATH (the supervisor drops it during source handover).
    sys.path.insert(0, "/app")
    main()
