#!/usr/bin/env python3
"""Runtime-overlay acceptance for a Docker image (#194).

Run inside a disposable container of the image under test, as appuser. It
drives that image's own updater and supervisor (docker/application_updates.py
and docker/container_runtime.py beside --docker-dir) against a private
PostgreSQL 16 cluster in --work. Three paths, one install, in this order:

  apply     A signed release that requires the candidate Python runtime is
            downloaded, checked against its pins, unpacked, built on that
            interpreter, activated through the supervisor's restart path and
            reaches /health/ready on it. A cold restart (PostgreSQL stopped
            and started again, a new updater, network off) then starts it
            again from the retained, re-verified overlay, and the hand-over
            supervisor would run on it.
  rollback  A signed release whose overlay fails its first start (an injected
            start-up defect, after the new code has written to the database)
            is rolled back by the supervisor: the database copy is put back
            and the previous release runs on its own overlay again.
  refuse    Releases this image cannot run are refused before anything is
            activated, with the reason the user sees: a PostgreSQL major
            upgrade, an overlay published only for the other architecture,
            an overlay binary this image cannot execute (another processor),
            and an image whose bootstrap predates runtime-overlays-v1.

Nothing is published. Releases are signed with a throwaway key and served from
an in-process transport, so signature, checksum, size and archive checks are
the production ones. The stand-in application (app/main.py of each release)
uses only the standard library; the build runs the real venv, pip, optional
voice and npm steps and skips only ``pip install ./fluid``, which needs the
real source tree and does not depend on the overlay. Prints a JSON report and
exits 0 only when every path passed. See docs/ci/release-channels.md.

Example (amd64; for arm64 use the aarch64 archive in an arm64 container)::

    python3 -I /harness/overlay_acceptance.py \\
        --overlay /harness/cpython-3.13-x86_64-unknown-linux-gnu-install_only.tar.gz \\
        --capability python-3.13 --executable python/bin/python3
"""
from __future__ import annotations

import argparse
import base64
import bz2
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import Any

# Every release this run publishes, in feed order (sequence = index + 2).
RELEASES = ("baseline", "overlay", "defect", "postgres", "other-arch", "abi", "old-bootstrap")
REVISION = {name: str(index + 1) * 40 for index, name in enumerate(RELEASES)}
SEQUENCE = {name: index + 2 for index, name in enumerate(RELEASES)}
IMAGE_REVISION = "0" * 40
FEED_HOST = "hobbycoders.com"
OVERLAY_HOST = "overlays.acceptance.invalid"
DEFECT = "ACCEPTANCE-DEFECT"
PACKAGES = ("frontend", "tools", "browser-mcp")

# The stand-in for app.main. It records what the new code would do on its
# first start (write to the database), then serves the health endpoints with
# the interpreter it runs on, so the harness can tell which runtime started.
APP_MAIN = r'''"""Stand-in for app.main (scripts/release/overlay_acceptance.py)."""
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

revision = os.environ.get("GIT_SHA", "image")
runtime = sys.version.split()[0]
base = Path(sys._base_executable).resolve()
descriptor = json.loads((Path(os.environ["DATA_DIR"]) / "managed-postgres.json").read_text())
env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C", "PGCONNECT_TIMEOUT": "5",
       "PGHOST": descriptor["host"], "PGPORT": str(descriptor["port"]),
       "PGUSER": descriptor["user"], "PGPASSWORD": descriptor["password"],
       "PGDATABASE": descriptor["database"]}
insert = "INSERT INTO acceptance (revision, runtime) VALUES ('%s', '%s')" % (revision, runtime)
subprocess.run([os.environ["ACCEPTANCE_PSQL"], "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-c", insert],
               env=env, check=True)
if any((parent / "ACCEPTANCE-DEFECT").exists() for parent in base.parents[:3]):
    sys.exit("acceptance: this runtime overlay carries an injected start-up defect")
report = json.dumps({"revision": revision, "runtime": runtime, "base_executable": str(base),
                     "overlays": os.environ.get("SHUFFLE_RUNTIME_OVERLAYS")}).encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/health/ready", "/health/live"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(report)))
        self.end_headers()
        self.wfile.write(report)

    def log_message(self, *args: object) -> None:
        pass


ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
'''


class Failed(Exception):
    """An acceptance expectation that did not hold."""


def expect(condition: object, message: str) -> None:
    if not condition:
        raise Failed(message)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def read_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def until(predicate: Callable[[], Any], timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise Failed(f"timed out after {timeout:.0f}s waiting for {what}")


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decompressed_size(path: Path) -> int:
    """The signed unpacked_size: every byte the decompressor yields, tar headers
    included (the updater bounds the whole stream, not only file contents)."""
    with Path(path).open("rb") as raw:
        magic = raw.read(6)
    opener = (gzip.open if magic.startswith(b"\x1f\x8b") else bz2.open if magic.startswith(b"BZh")
              else lzma.open if magic.startswith(b"\xfd7zXZ\x00") else open)
    total = 0
    with opener(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
    return total


def describe(path: Path, kind: str, capability: str, platform_name: str, executable: str) -> dict:
    """The overlay descriptor a release would sign for this archive."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    return {"kind": kind, "capability": capability, "platform": platform_name,
            "url": f"https://{OVERLAY_HOST}/{sha256}.tar", "sha256": sha256,
            "size": Path(path).stat().st_size, "unpacked_size": decompressed_size(path),
            "executable": executable}


def with_defect(archive: Path, executable: str, target: Path) -> Path:
    """The candidate runtime plus a marker the stand-in app treats as a start-up
    defect: it builds like the real overlay and fails only on first start."""
    marker = str(PurePosixPath(executable).parent.parent / DEFECT)
    with tarfile.open(archive) as source, tarfile.open(target, "w:gz", compresslevel=1) as output:
        for member in source:
            output.addfile(member, source.extractfile(member) if member.isreg() else None)
        data = b"injected by scripts/release/overlay_acceptance.py\n"
        info = tarfile.TarInfo(marker)
        info.size, info.mode = len(data), 0o644
        output.addfile(info, io.BytesIO(data))
    return target


def foreign_binary(machine: str | None = None) -> bytes:
    """A 64-bit ELF header for the other processor: what an overlay built for
    the wrong architecture looks like to this kernel."""
    machine = (machine or platform.machine()).lower()
    e_machine = 183 if machine in ("x86_64", "amd64") else 62  # EM_AARCH64 / EM_X86_64
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    header = struct.pack("<HHIQQQIHHHHHH", 2, e_machine, 1, 0x400000, 64, 0, 0, 64, 56, 0, 64, 0, 0)
    return ident + header


def foreign_archive(executable: str, target: Path) -> Path:
    data = foreign_binary()
    with tarfile.open(target, "w:gz") as output:
        info = tarfile.TarInfo(executable)
        info.size, info.mode = len(data), 0o755
        output.addfile(info, io.BytesIO(data))
    return target


def package_files(name: str) -> dict[str, str]:
    """A dependency-free npm package whose build writes what the updater copies."""
    script = ("node -e \"const fs=require('fs');fs.mkdirSync('build',{recursive:true});"
              "fs.writeFileSync('build/index.html','<!doctype html>'+process.version)\"")
    package = f"acceptance-{name}"
    manifest = {"name": package, "version": "1.0.0", "private": True, "scripts": {"build": script}}
    lock = {"name": package, "version": "1.0.0", "lockfileVersion": 3, "requires": True,
            "packages": {"": {"name": package, "version": "1.0.0"}}}
    return {f"{name}/package.json": json.dumps(manifest),
            f"{name}/package-lock.json": json.dumps(lock)}


class Feed:
    """Signed releases and overlay archives, served in process."""

    def __init__(self, updates: ModuleType, contract: ModuleType) -> None:
        import httpx
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        self.updates, self.contract = updates, contract
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(Encoding.PEM,
                                                         PublicFormat.SubjectPublicKeyInfo)
        self.responses: dict[tuple[str, str], bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.offline = False
        self.httpx, self.client = httpx, httpx.Client
        client = httpx.Client

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append((request.url.host, request.url.path))
            if self.offline:
                raise httpx.ConnectError("acceptance: the network is off", request=request)
            body = self.responses.get((request.url.host, request.url.path))
            return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

        # The updater builds every client with trust_env=False and its own
        # timeouts; only the transport is replaced.
        def transport_client(**kwargs: Any) -> httpx.Client:
            return client(transport=httpx.MockTransport(handler), **kwargs)

        httpx.Client = transport_client

    def close(self) -> None:
        self.httpx.Client = self.client

    def serve_overlay(self, item: dict, path: Path) -> None:
        self.responses[(OVERLAY_HOST, f"/{item['sha256']}.tar")] = Path(path).read_bytes()

    def publish(self, name: str, files: dict[str, str | bytes],
                requires: Iterable[str] = ("base-1",), overlays: Iterable[dict] = ()) -> str:
        revision, sequence = REVISION[name], SEQUENCE[name]
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, value in files.items():
                archive.writestr(f"ai-shuffle-{revision}/{path}", value)
        data = output.getvalue()
        listing = self.updates.tree_lines(
            [(path, hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest())
             for path, value in files.items()])
        requires = sorted(set(requires))
        container = {"sequence": sequence, "runtime": self.contract.runtime(requires),
                     "requires": requires, "recommends": []}
        if overlays:
            container["overlays"] = list(overlays)
        release = {"format": 2, "revision": revision, "sequence": sequence,
                   "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                   "container": container, "channel": "stable",
                   "tree": hashlib.sha256(listing).hexdigest(),
                   "version": f"0.0.{sequence}-acceptance", "notes": f"overlay acceptance: {name}"}
        payload = json.dumps(release).encode()
        envelope = json.dumps({"payload": base64.b64encode(payload).decode(),
                               "signature": base64.b64encode(self.key.sign(payload)).decode()})
        self.responses[(FEED_HOST, "/v1/desktop/manifest")] = envelope.encode()
        self.responses[(FEED_HOST, f"/v1/desktop/sources/{revision}")] = data
        return revision


class Acceptance:
    """One disposable install that the three paths run on, in order."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.docker_dir = args.docker_dir.resolve()
        # The updater writes its state through app.core (atomic_json).
        sys.path.insert(0, str(self.docker_dir.parent))
        self.runtime = load_module("shuffle_container_runtime",
                                   self.docker_dir / "container_runtime.py")
        self.updates = self.runtime.load_updater()
        self.contract = load_module("shuffle_update_contract",
                                    self.docker_dir / "update_contract.py")
        if not Path(self.updates.IMAGE_PYTHON).exists():
            # Outside the image (a development host): its own base interpreter.
            self.updates.IMAGE_PYTHON = str(Path(sys._base_executable).resolve())
        # The supervisor probes every 30 s in production; the decisions are the
        # same at 1 s and the run takes minutes instead of an hour.
        self.runtime.READY_PROBE_INTERVAL = 1.0
        # A directory of this run's own, inside --work when given: removal at
        # the end never reaches the caller's files beside it.
        if args.work:
            Path(args.work).mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="overlay-acceptance-", dir=args.work)).resolve()
        self.platform = self.updates.runtime_platform()
        self.image_record = read_json(self.docker_dir / "image.json")
        self.provides = list(self.image_record.get("provides") or self.contract.PROVIDES)
        self.image = {"revision": IMAGE_REVISION, "sequence": 1,
                      "runtime": self.contract.LEGACY_RUNTIME, "provides": self.provides}
        self.feed = Feed(self.updates, self.contract)
        self.settings = SimpleNamespace(effective_data_dir=self.work / "data")
        self.root = self.settings.effective_data_dir / "application-updates"
        self.port, self.pg_port = free_port(), free_port()
        self.ready_url = f"http://127.0.0.1:{self.port}/health/ready"
        psql = shutil.which("psql") or "/usr/lib/postgresql/16/bin/psql"
        # A minimal environment: nothing of the caller's (keys, tokens) reaches
        # the stand-in application or the build.
        (self.work / "home").mkdir(exist_ok=True)
        self.env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": str(self.work / "home"), "LANG": "C.UTF-8",
                    "DATA_DIR": str(self.settings.effective_data_dir), "PORT": str(self.port),
                    "ACCEPTANCE_PSQL": psql}
        self.db = self.updater = self.app = self.thread = self.candidate = None
        self.watch_exit: list[int] = []

    # -- the install -------------------------------------------------------
    def source(self, name: str) -> dict[str, str | bytes]:
        files: dict[str, str | bytes] = {
            "app/__init__.py": "", "app/main.py": APP_MAIN, "VERSION.txt": name + "\n",
            "requirements.lock": "# overlay acceptance: the stand-in app has no dependencies\n"}
        for package in PACKAGES:
            files.update(package_files(package))
        # What a hand-over runs must be in the signed listing (handover_target).
        for relative in self.updates.HANDOVER_FILES:
            files[relative] = (self.docker_dir.parent / relative).read_bytes()
        return files

    def new_updater(self, root: Path | None = None, provides: list[str] | None = None,
                    database: object = "default") -> Any:
        updater = self.updates.ContainerUpdates(
            root or self.root, dict(self.image, provides=list(provides or self.provides)),
            self.feed.public, lambda: True, self.lifecycle, channel="stable",
            database=self.db if database == "default" else database)
        updater.next_check = float("inf")  # checks run only when asked, as from the UI
        original = updater.run_build

        def run_build(command: list[str], cwd: Path, env: dict[str, str]) -> None:
            if command[-1:] == ["./fluid"]:
                return None  # needs the real source tree; independent of the runtime
            return original(command, cwd, env)

        updater.run_build = run_build
        return updater

    @staticmethod
    def lifecycle(action: str) -> dict:
        return {"active-sessions": {"active_sessions": 0}, "stop-sessions": {"stopped": True},
                "update-policy": {"allow_updates": True}}[action]

    def open_database(self) -> None:
        self.runtime.STOP.clear()
        self.db = self.runtime.Database(self.settings, port=self.pg_port)
        self.db.open()
        self.db.publish_descriptor()

    def supervise(self, app: subprocess.Popen) -> None:
        self.app = app
        monitor = self.runtime.ReadinessMonitor(self.ready_url)

        def restart(current: subprocess.Popen) -> subprocess.Popen:
            self.app = self.runtime.restart_into_update(current, self.updater, self.env)
            return self.app

        def relaunch(current: subprocess.Popen) -> subprocess.Popen | None:
            self.app = self.runtime.start_current(current, self.updater, self.env)
            return self.app

        def run() -> None:
            self.watch_exit.append(self.runtime.watch(app, self.db, None, monitor, self.updater,
                                                      restart, relaunch))

        self.thread = threading.Thread(target=run, daemon=True, name="acceptance-supervisor")
        self.thread.start()

    def halt(self) -> None:
        """Stop as the container does: supervisor loop, updater, app, database."""
        self.runtime.STOP.set()
        if self.thread is not None:
            self.thread.join(timeout=90)
            self.thread = None
        if self.updater is not None:
            self.updater.close()
        if self.app is not None:
            self.runtime.stop_group(self.app, 30)
            self.app = None
        if self.db is not None:
            self.db.close()
            self.db = None

    def health(self) -> dict | None:
        from urllib.request import urlopen
        try:
            with urlopen(self.ready_url, timeout=3) as response:
                return json.loads(response.read()) if response.status == 200 else None
        except Exception:
            return None

    def state(self) -> dict:
        return read_json(self.root / "state.json")

    def status(self, root: Path | None = None) -> dict:
        return read_json((root or self.root) / "status.json")

    def request(self, action: str) -> None:
        self.updates.atomic_json(self.root / "request.json",
                                 {"action": action, "expires_at": time.time() + 300})

    def rows(self) -> list[str]:
        return self.db.query("SELECT revision FROM acceptance ORDER BY id").splitlines()

    def overlay_dirs(self) -> list[str]:
        root = self.root / "runtime-overlays"
        return sorted(path.name for path in root.iterdir()) if root.is_dir() else []

    def running(self, name: str, item: dict | None = None) -> dict | None:
        report = self.health()
        if not report or report.get("revision") != REVISION[name]:
            return None
        if item is not None and item["sha256"] not in report["base_executable"]:
            return None
        return report

    def evidence(self, report: dict) -> dict:
        base = Path(report["base_executable"])
        if base.is_relative_to(self.work):
            base = base.relative_to(self.work)
        return {"revision": report["revision"][:12], "runtime": report["runtime"],
                "base_executable": str(base), "SHUFFLE_RUNTIME_OVERLAYS": report["overlays"]}

    def settled(self, name: str) -> bool:
        state = self.state()
        return state.get("active") == REVISION[name] and "booting" not in state

    def download(self, name: str) -> None:
        self.request("download")
        revision = REVISION[name]

        def staged() -> bool:
            status = self.status()
            if status.get("phase") == "error" and status.get("version") in (None, revision[:12]):
                raise Failed(f"download of {name} failed: {status.get('error')}")
            return self.state().get("pending") == revision and status.get("phase") == "ready"

        until(staged, self.args.timeout, f"{name} to download and build")

    # -- setup ---------------------------------------------------------------
    def setup(self) -> None:
        args = self.args
        self.open_database()
        self.db.query("CREATE TABLE acceptance "
                      "(id serial PRIMARY KEY, revision text NOT NULL, runtime text NOT NULL)")
        self.updater = self.new_updater()
        self.candidate = describe(args.overlay, "python", args.capability, self.platform,
                                  args.executable)
        self.feed.serve_overlay(self.candidate, args.overlay)
        # The release the install already runs: no overlay, the image's Python.
        self.feed.publish("baseline", self.source("baseline"))
        self.updater.check(download=True)
        expect(self.state().get("pending") == REVISION["baseline"],
               f"baseline did not stage: {self.status()}")
        command, cwd, env = self.updater.launch(self.env, activate=True)
        self.updater.snapshot_room()
        self.updater.activating()
        self.supervise(self.runtime.start_app(command, cwd, env, self.root))
        until(lambda: self.settled("baseline"), args.timeout, "the baseline release to start")

    # -- paths -----------------------------------------------------------------
    def apply(self) -> dict:
        item = self.candidate
        requires = ("base-1", "runtime-overlays-v1", item["capability"])
        self.feed.publish("overlay", self.source("overlay"), requires=requires, overlays=[item])
        started = time.monotonic()
        self.download("overlay")
        expect(item["sha256"] in self.overlay_dirs(), "the overlay was not kept after the build")
        self.request("install")
        until(lambda: self.settled("overlay"), self.args.timeout,
              "the overlay release to become healthy")
        report = until(lambda: self.running("overlay", item), 60,
                       "the overlay release to answer on its runtime")
        activated = time.monotonic() - started
        expect(report["overlays"] == "1", "SHUFFLE_RUNTIME_OVERLAYS was not set for the release")
        expect(self.state().get("previous") == REVISION["baseline"],
               f"previous release not recorded: {self.state()}")
        # healthy() records the start, then drops the copy.
        until(lambda: self.db.update_databases() == [], 60, "the database copy to be dropped")
        expect(REVISION["overlay"] in self.rows(), "the overlay release never wrote to the DB")

        # Cold restart: everything stops, PostgreSQL starts again, a new
        # updater reads the retained state; no network for the overlay.
        self.halt()
        self.feed.offline = True
        requests = len(self.feed.requests)
        try:
            self.open_database()
            self.updater = self.new_updater()
            command, cwd, env = self.updater.launch(self.env)
            self.supervise(self.runtime.start_app(command, cwd, env, self.root))
            cold = until(lambda: self.running("overlay", item), self.args.timeout,
                         "the overlay release to start again after a cold restart")
            expect(len(self.feed.requests) == requests, "the cold restart used the network")
            target = self.updates.handover_target(self.root, self.updater.image, self.feed.public)
            expect(target is not None and target[0] == REVISION["overlay"],
                   f"no hand-over target: {target}")
            handover_base = subprocess.check_output(
                [str(target[1]), "-I", "-c", "import sys; print(sys._base_executable)"],
                text=True).strip()
            expect(item["sha256"] in handover_base, "the hand-over supervisor is off the overlay")
        finally:
            self.feed.offline = False
        state = {key: value[:12] for key, value in self.state().items() if isinstance(value, str)}
        return {"activation": self.evidence(report), "seconds_to_healthy": round(activated, 1),
                "cold_restart": dict(self.evidence(cold), offline=True),
                "handover_supervisor_on_overlay": True, "state": state}

    def rollback(self) -> dict:
        good = self.candidate
        defect = with_defect(self.args.overlay, self.args.executable,
                             self.work / "defect-overlay.tar.gz")
        item = describe(defect, "python", self.args.capability, self.platform, self.args.executable)
        self.feed.serve_overlay(item, defect)
        requires = ("base-1", "runtime-overlays-v1", item["capability"])
        self.feed.publish("defect", self.source("defect"), requires=requires, overlays=[item])
        self.download("defect")
        before = self.rows()
        self.request("install")

        def rolled_back() -> bool:
            return REVISION["defect"] in self.state().get("failed", []) and self.settled("overlay")

        until(rolled_back, self.args.timeout, "the failed first start to roll back")
        report = until(lambda: self.running("overlay", good), self.args.timeout,
                       "the previous release to run on its own overlay again")
        rows = self.rows()
        expect(REVISION["defect"] not in rows, "the defect release's write survived the rollback")
        expect(rows[:len(before)] == before, "rows written before the update changed")
        until(lambda: self.db.update_databases() == [], 60,
              "the rolled-back database to be dropped")
        status = self.status()
        message = status.get("error") or ""
        expect("failed to start" in message and "previous version" in message,
               f"no rollback message: {status}")
        # A rolled-back release is not offered again on its own.
        self.request("check")
        until(lambda: "failed to start on this worker" in (self.status().get("error") or ""), 60,
              "the rolled-back release to be reported instead of offered")
        return {"failed_first_start": REVISION["defect"][:12], "database_restored": True,
                "running_after": self.evidence(report), "message": message}

    def refusal(self, name: str, requires: tuple[str, ...], overlays: Iterable[dict] = (),
                phase: str = "image-required", contains: Iterable[str] = (),
                updater: Any = None) -> dict:
        root = updater.root if updater is not None else self.root
        state, dirs, requests = self.state(), self.overlay_dirs(), len(self.feed.requests)
        before = self.status(root)
        self.feed.publish(name, self.source(name), requires=requires, overlays=overlays)
        if updater is None:
            self.request("download")
        else:
            try:
                updater.check(download=True)
            except self.updates.UpdateError as exc:
                updater.publish("error", error=str(exc))

        def refused() -> dict | None:
            # A build-time refusal is published as an update error under the
            # running revision, so wait for a new final status, not a version.
            status = self.status(root)
            final = status.get("phase") in (phase, "error") and status != before
            return status if final and (status.get("message") or status.get("error")) else None

        status = until(refused, self.args.timeout, f"the {name} release to be refused")
        message = status.get("message") or status.get("error") or ""
        expect(status["phase"] == phase,
               f"{name}: phase {status['phase']!r}, not {phase!r}: {message}")
        for text in contains:
            expect(text in message, f"{name}: message lacks {text!r}: {message}")
        expect(self.state() == state, f"{name}: the install's state changed: {self.state()}")
        expect(not (root / "versions" / REVISION[name]).exists(),
               f"{name}: build files left behind")
        if updater is None:
            expect(self.overlay_dirs() == dirs,
                   f"{name}: overlay store changed: {self.overlay_dirs()}")
        expect(self.running("overlay", self.candidate), f"{name}: the running release stopped")
        downloads = [r for r in self.feed.requests[requests:] if r[0] == OVERLAY_HOST]
        return {"phase": phase, "message": message, "overlay_downloads": len(downloads)}

    def refuse(self) -> dict:
        cap = self.args.capability
        overlay_release = ("base-1", "runtime-overlays-v1", cap)
        other = "linux-aarch64" if self.platform == "linux-x86_64" else "linux-x86_64"
        results = {}
        results["postgres-major"] = self.refusal(
            "postgres", ("base-1", "postgres-17"),
            contains=("Needs a newer Docker image", "postgres-17", "PostgreSQL stays on the image"))
        results["other-architecture-only"] = self.refusal(
            "other-arch", overlay_release, overlays=[dict(self.candidate, platform=other)],
            contains=("No compatible runtime overlay for: " + cap,))
        for name in ("postgres-major", "other-architecture-only"):
            expect(results[name]["overlay_downloads"] == 0, f"{name}: an overlay was downloaded")
        foreign = foreign_archive(self.args.executable, self.work / "foreign-overlay.tar.gz")
        item = describe(foreign, "python", cap, self.platform, self.args.executable)
        self.feed.serve_overlay(item, foreign)
        results["wrong-processor-binary"] = self.refusal(
            "abi", overlay_release, overlays=[item], phase="error",
            contains=(f"{cap} runtime", "does not start on this image",
                      "This version keeps working"))
        expect(item["sha256"] not in self.overlay_dirs(), "the unusable overlay was kept")
        older = [name for name in self.provides if name != "runtime-overlays-v1"]
        old = self.new_updater(self.work / "old-bootstrap" / "application-updates",
                               provides=older, database=None)
        results["bootstrap-without-runtime-overlays-v1"] = self.refusal(
            "old-bootstrap", overlay_release, overlays=[self.candidate], updater=old,
            contains=("Needs a newer Docker image", "runtime-overlays-v1"))
        return results

    # -- driver ----------------------------------------------------------------
    def run(self) -> dict:
        image = {key: self.image_record.get(key) for key in ("revision", "sequence", "provides")}
        report = {"platform": self.platform, "machine": platform.machine(), "image": image,
                  "updater": str(self.docker_dir), "postgres": None, "paths": [], "passed": False}
        try:
            self.setup()
            report["postgres"] = self.db.query("SHOW server_version")
            report["overlay"] = {k: v for k, v in self.candidate.items() if k != "url"}
            for name, step in (("apply", self.apply), ("rollback", self.rollback),
                               ("refuse", self.refuse)):
                started = time.monotonic()
                entry: dict[str, Any] = {"path": name}
                try:
                    entry.update(evidence=step(), ok=True)
                except Exception as exc:
                    entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                    if not isinstance(exc, Failed):
                        entry["trace"] = traceback.format_exc(limit=8)
                entry["seconds"] = round(time.monotonic() - started, 1)
                report["paths"].append(entry)
                if not entry["ok"]:
                    break  # later paths build on this install's state
            report["passed"] = [entry.get("ok") for entry in report["paths"]] == [True] * 3
        except Exception as exc:
            report["setup_error"] = f"{type(exc).__name__}: {exc}"
            report["trace"] = traceback.format_exc(limit=8)
            report["status"] = self.status()
        finally:
            try:
                self.halt()
            finally:
                self.feed.close()
                if self.args.keep:
                    report["work"] = str(self.work)
                else:
                    self.remove_work()
        return report

    def remove_work(self) -> None:
        """Delete this run's own directory only, never the caller's --work."""
        remove_tree(self.work)


def remove_tree(path: Path) -> None:
    """rmtree that first gives back the write bits installed overlays drop
    (an image's own updater may predate its remove_tree)."""
    for directory, _, _ in os.walk(path):
        if not os.path.islink(directory):
            os.chmod(directory, 0o755)
    shutil.rmtree(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    image_docker = Path("/app/docker")
    checkout_docker = Path(__file__).resolve().parents[2] / "docker"
    parser.add_argument("--docker-dir", type=Path,
                        default=image_docker if image_docker.is_dir() else checkout_docker,
                        help="the updater and supervisor under test (default: /app/docker)")
    parser.add_argument("--overlay", type=Path, required=True,
                        help="candidate archive: a relocatable Python for this architecture")
    parser.add_argument("--capability", required=True,
                        help="the capability it provides, e.g. python-3.13")
    parser.add_argument("--executable", default="python/bin/python3",
                        help="interpreter path inside the archive")
    parser.add_argument("--work", type=Path,
                        help="where to create this run's own scratch directory "
                             "(default: the system temp dir); only that directory is removed")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    parser.add_argument("--timeout", type=float, default=600, help="seconds allowed per wait")
    args = parser.parse_args(argv)
    if not args.capability.startswith("python-"):
        parser.error("the candidate must be a Python runtime (capability python-*)")
    report = Acceptance(args).run()
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
