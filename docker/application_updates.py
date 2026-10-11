"""Signed, staged application updates supervised inside the stable Docker image.

Never edits /app, installs OS packages, or opens the Docker socket. Builds run
unprivileged with a credential-free environment. State and versions live on the
existing data volume. Before an update first starts, the stopped application's
database is copied; if that first start fails, the copy is put back and the
previous version runs again. Without a copy, a failed first boot blocks
rollback over migrated data.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import zipfile

import httpx
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def atomic_json(path, value):
    # Manifest verification is also used by offline publication checks. Do not
    # initialize app settings/database imports until the updater writes state.
    from app.core.application_updates import atomic_json as write_json

    return write_json(path, value)

ORIGIN = "https://hobbycoders.com"
REVISION = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"[a-f0-9]{64}")
MAX_ARCHIVE = 512 * 1024**2
MAX_NOTES = 4000
# "0.9.0" on a release, "0.9.0-dev.12+gabc1234" on an edge build.
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][A-Za-z0-9.+-]{1,60})?")
# stable: tagged releases, signed only after the release image passed container
# verification. edge: every main revision, for the owner's own workers. The
# channel is inside the signed payload, so the hub cannot relabel an edge build.
CHANNELS = ("stable", "edge")
# D8: the hub's gated source feed (android/hub/src/license-gate.ts) reads this
# worker's license lease from this header. The value is the signed envelope as
# base64url (app/core/license.py lease_header), within the hub's 4 KiB limit.
LEASE_HEADER = "X-Shuffle-Lease"
LEASE_VALUE = re.compile(r"[A-Za-z0-9_-]{1,4096}")
# Image capabilities (docker/update_contract.py): a source update lists what it
# requires, an image what it provides.
CAPABILITY = re.compile(r"[a-z0-9][a-z0-9.-]{0,39}")
IMAGE_REQUIRED = "Needs a newer Docker image. This version keeps working."
IMAGE_NOTICE = ("A coming update needs a newer Docker image. This version keeps working; "
                "update the image when convenient.")


SNAPSHOT = re.compile(r"ai_shuffle_rollback_[0-9a-f]{12}")


class UpdateError(Exception):
    pass


def _log(message):
    print(f"container runtime: {message}", file=sys.stderr, flush=True)


def tree_lines(files):
    """Canonical listing signed as the manifest's tree (see scripts/source_archive.py)."""
    return b"".join(f"{path}\0{digest}\n".encode() for path, digest in sorted(files))


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_tree(directory, manifest):
    """Re-hash every signed source file before its code is built or run.

    The listing is kept beside the build and must hash to the signed tree
    digest, and every listed file must still match. Build outputs (venv,
    node_modules, dist, app/static) are not in the listing; they are rebuilt
    from these files and are not covered.
    """
    tree = manifest.get("tree")
    if not tree:
        raise UpdateError("This application build has no signed source tree; install a newer image")
    try:
        listing = (directory / "tree.txt").read_bytes()
    except FileNotFoundError:
        raise UpdateError("Application source listing is missing; download the update again") from None
    if hashlib.sha256(listing).hexdigest() != tree:
        raise UpdateError("Application source listing does not match the signed release")
    source = directory / "source"
    for line in listing.decode().splitlines():
        relative, digest = line.split("\0")
        path = source.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.is_file() or file_digest(path) != digest:
            raise UpdateError("Application source files changed after download; download the update again")


def verify_manifest(text, public_key):
    if len(text) > 32768:
        raise UpdateError("Source manifest exceeds limits")
    try:
        envelope = json.loads(text)
        payload = base64.b64decode(envelope["payload"], validate=True)
        key = load_pem_public_key(public_key)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("wrong key type")
        key.verify(base64.b64decode(envelope["signature"], validate=True), payload)
        result = json.loads(payload)
        if (result["format"] != 2 or not REVISION.fullmatch(result["revision"])
                or not DIGEST.fullmatch(result["sha256"])
                or type(result["size"]) is not int or not 0 < result["size"] <= MAX_ARCHIVE
                or type(result["sequence"]) is not int or not 0 < result["sequence"] < 2**53):
            raise ValueError("invalid fields")
        # Fields added after format 2 shipped. Older launchers ignore them; this
        # one requires channel and tree on every download and launch.
        if ("channel" in result and result["channel"] not in CHANNELS
                or "tree" in result and not (isinstance(result["tree"], str) and DIGEST.fullmatch(result["tree"]))
                or "version" in result and not (isinstance(result["version"], str) and VERSION.fullmatch(result["version"]))
                or "notes" in result and not (isinstance(result["notes"], str) and len(result["notes"]) <= MAX_NOTES)):
            raise ValueError("invalid release fields")
        container = result.get("container", {})
        if (type(container.get("sequence")) is not int or not 0 < container["sequence"] < 2**53
                or not DIGEST.fullmatch(container.get("runtime", ""))):
            raise UpdateError("This source update needs a newer container release")
        for field in ("requires", "recommends"):
            if field in container and not (isinstance(container[field], list) and len(container[field]) <= 32
                                           and all(isinstance(n, str) and CAPABILITY.fullmatch(n) for n in container[field])):
                raise ValueError("invalid container capabilities")
        return result
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError("Invalid or unsigned source manifest") from exc


def missing_capabilities(container, image):
    """What this image lacks to run the source, as a list; empty when it can.

    Manifests and images from before capabilities (#1821) compare the runtime
    string, as those images themselves still do. A base-1 release publishes
    the frozen string old images carry, so both sides agree.
    """
    requires, provides = container.get("requires"), image.get("provides")
    if requires is None or not isinstance(provides, list):
        return [] if container["runtime"] == image.get("runtime") else ["runtime"]
    return sorted(set(requires) - set(provides))


# What a hand-over runs from the update's source (docker/container_runtime.py).
HANDOVER_FILES = ("docker/container_runtime.py", "docker/application_updates.py",
                  "docker/chrome-supervisor.sh", "docker/start-chrome.sh")


def discard_name(snapshot):
    """What the database an update left behind is renamed to on rollback."""
    return snapshot.replace("_rollback_", "_failed_", 1)


def handover_target(root, image, public_key):
    """The active update whose own supervisor this image may start, or None.

    Read-only. Only a healthy, verified, compatible active update qualifies:
    never while a first boot is pending or blocked, never for a revision whose
    supervisor did not reach a healthy app last time (handover.json), and never
    when the image is newer than the update (the image's code then runs, and
    ContainerUpdates resets the state). Returns (revision, python, script).
    """
    root = Path(root)
    try:
        state = json.loads((root / "state.json").read_text())
    except FileNotFoundError:
        return None
    if not isinstance(state, dict):
        raise UpdateError("Invalid container update state")
    revision = state.get("active")
    if (not isinstance(revision, str) or not REVISION.fullmatch(revision) or state.get("booting")
            or state.get("blocked") or revision in (state.get("failed") or [])):
        return None
    try:
        trial = json.loads((root / "handover.json").read_text())
    except FileNotFoundError:
        trial = None
    if isinstance(trial, dict) and trial.get("revision") == revision:
        raise UpdateError(f"The supervisor of update {revision[:12]} did not reach a healthy start before")
    directory = root / "versions" / revision
    if directory.is_symlink():
        raise UpdateError("Linked application directory is not supported")
    manifest = verify_manifest((directory / "release.json").read_bytes(), public_key)
    if manifest["revision"] != revision:
        raise UpdateError("Installed revision mismatch")
    if image["sequence"] > manifest["container"]["sequence"] or missing_capabilities(manifest["container"], image):
        return None
    source, python = directory / "source", directory / "venv/bin/python"
    script = source / "docker/container_runtime.py"
    if not (directory / "complete").is_file() or not python.is_file() or not script.is_file():
        return None
    verify_tree(directory, manifest)
    # verify_tree checks the files the signed listing names; what the image
    # will run must be among them, not merely present beside them.
    listed = {line.split("\0")[0] for line in (directory / "tree.txt").read_text().splitlines()}
    if not set(HANDOVER_FILES) <= listed:
        return None
    return revision, python, script


def extract_source(archive, target, revision):
    """Bound ZIP expansion, reject aliases, links, devices and traversal before writing."""
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > 100000 or sum(item.file_size for item in entries) > 2 * 1024**3:
            raise UpdateError("Source archive expansion exceeds limits")
        roots, seen = set(), set()
        for item in entries:
            name = item.filename.rstrip("/")
            parts = name.split("/")
            mode = item.external_attr >> 16
            if (not name or name.startswith("/") or "\\" in name
                    or any(part in ("", ".", "..") or ":" in part or "\0" in part for part in parts)
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                    or name.casefold() in seen or item.flag_bits & 1):
                raise UpdateError("Unsafe source archive entry")
            roots.add(parts[0])
            seen.add(name.casefold())
        if len(roots) != 1 or not re.fullmatch(
                rf"(?:ai-shuffle-{revision}|HobbyCoders-ai-shuffle-{revision[:7]}[a-f0-9]*)",
                next(iter(roots)), re.IGNORECASE):
            raise UpdateError("Unexpected source archive root")
        root = roots.pop()
        target.mkdir()
        files = []
        for item in entries:
            relative = PurePosixPath(item.filename).parts[1:]
            if not relative:
                continue
            destination = target.joinpath(*relative)
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with zipped.open(item) as source, destination.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
                files.append(("/".join(relative), digest.hexdigest()))
        return files


def build_environment(home):
    # No database descriptor, provider keys, cloud tokens, proxy or pip/npm config.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "CI": "true",
            "LANG": "C.UTF-8", "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1",
            "NPM_CONFIG_USERCONFIG": os.devnull, "ELECTRON_SKIP_BINARY_DOWNLOAD": "1"}


# Check the release manifest shortly after launch, then once a day. A
# check can also be requested from the app at any time.
FIRST_CHECK_SECONDS = 15
CHECK_INTERVAL_SECONDS = 24 * 3600


class ContainerUpdates:
    def __init__(self, root, image, public_key, allowed, lifecycle, channel="stable", lease=None, database=None):
        if channel not in CHANNELS:
            raise UpdateError("Unknown update channel")
        self.channel = channel
        # The supervisor's managed database (container_runtime.Database), for
        # the copy taken before an update's first start. None keeps the
        # original contract: a failed first boot blocks.
        self.database = database
        # Returns the X-Shuffle-Lease value, or None (beta mode, no lease yet,
        # app not answering). The supervisor reads it from the app's
        # token-protected /api/v1/internal/desktop/source-lease.
        self.lease = lease
        self.offer = {}
        # Set when the feed's release recommends a capability this image lacks.
        self.notice = None
        self.root = Path(root)
        self.image = image
        self.public_key = public_key
        self.allowed = allowed
        self.lifecycle = lifecycle
        self.state = {}
        self.stop = threading.Event()
        self.thread = None
        self.install_requested = threading.Event()
        self.running = None
        # The revision whose failed first start was last rolled back, if any.
        self.rolled_back = None
        # When the feed was last read (epoch seconds). Workers compare it with
        # each other's to tell "checked and found nothing" from "not checked
        # since another worker found an update".
        self.checked_at = None
        self.next_check = time.monotonic() + FIRST_CHECK_SECONDS
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = self.root / "state.json"
        self.request_path = self.root / "request.json"
        try:
            self.state = json.loads(self.state_path.read_text())
            if not isinstance(self.state, dict):
                raise ValueError("state")
            for field in ("active", "previous", "pending", "booting"):
                if field in self.state and not REVISION.fullmatch(self.state[field]):
                    raise ValueError("revision")
            for revision in self.state.get("failed", []):
                if not REVISION.fullmatch(revision):
                    raise ValueError("failed revision")
            if "snapshot" in self.state and not SNAPSHOT.fullmatch(self.state["snapshot"]):
                raise ValueError("snapshot")
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise UpdateError("Invalid container update state; files retained") from exc
        # An interrupted explicit restart must not become permission on next boot.
        self.request_path.unlink(missing_ok=True)
        if self.state.get("booting"):
            # Power loss or a kill during an update's first start: put the
            # database copy back and run the previous version.
            self.failed()
        self.discard_copies()
        pending = self.state.get("pending")
        if pending:
            staged_manifest = self.manifest(pending)
            staged = staged_manifest["container"]
            if (staged["sequence"] <= self.image["sequence"] or missing_capabilities(staged, self.image)
                    or staged_manifest.get("channel") != self.channel):
                self.state.pop("pending")
                self.save()
            else:
                self.remember(staged_manifest)
        retained = [self.manifest(revision) for revision in
                    {*self.state.get("failed", []), self.state.get("active")} if revision]
        if retained and all(self.image["sequence"] > m["container"]["sequence"] for m in retained):
            # A milestone strictly newer than every possibly migrated revision.
            self.state = {"pending": self.state["pending"]} if self.state.get("pending") else {}
            self.save()
        if self.state.get("blocked"):
            raise UpdateError("An application update failed first boot. Install a newer image; older code cannot safely run over this database. Files retained.")
        if not self.rolled_back:
            self.publish("ready" if self.state.get("pending") else "idle")

    def directory(self, revision):
        if not REVISION.fullmatch(revision):
            raise UpdateError("Invalid revision")
        directory = self.root / "versions" / revision
        if directory.is_symlink():
            raise UpdateError("Linked application directory is not supported")
        return directory

    def manifest(self, revision):
        manifest = verify_manifest((self.directory(revision) / "release.json").read_bytes(), self.public_key)
        if manifest["revision"] != revision:
            raise UpdateError("Installed revision mismatch")
        return manifest

    def save(self):
        atomic_json(self.state_path, self.state)

    def remember(self, manifest):
        """The release name and notes shown beside the update button."""
        self.offer = {"release": manifest.get("version"), "notes": manifest.get("notes")}

    def publish(self, phase, message=None, error=None, version=None):
        offered = self.offer if phase in ("available", "ready", "downloading") else {}
        atomic_json(self.root / "status.json", {"phase": phase, "message": message,
                    "error": error, "version": (version or self.state.get("pending") or self.running or "")[:12] or None,
                    "release": offered.get("release"), "notes": offered.get("notes"), "channel": self.channel,
                    "checked_at": self.checked_at, "notice": self.notice})

    def release_name(self, revision):
        try:
            return self.manifest(revision).get("version") or revision[:12]
        except Exception:
            return revision[:12]

    def failed(self):
        """An update's first start failed. Returns True when its database copy
        was put back, so the previous version (state["active"], or the
        image's own code) may run again; the update stays in state["failed"]
        and is not offered automatically again. Without a copy, or when
        putting it back fails, the worker is blocked as before: older code
        never runs over a database the update may have migrated.

        The caller has stopped the failed application first."""
        revision = self.state.get("booting")
        if not revision:
            return False
        snapshot = self.state.get("snapshot")
        restored = False
        if snapshot and self.database is not None:
            try:
                self.database.restore_snapshot(snapshot, discard_name(snapshot))
                restored = True
            except Exception as exc:
                _log(f"could not put back the database copy taken before update {revision[:12]} "
                     f"({type(exc).__name__}); the copy {snapshot} is kept")
        self.state.pop("booting")
        self.state["failed"] = sorted({*self.state.get("failed", []), revision})
        self.state.pop("pending", None)
        if not restored:
            self.state["blocked"] = True
            self.save()
            return False
        self.state.pop("snapshot")
        self.save()
        self.rolled_back = revision
        self.running = None
        _log(f"application update {revision[:12]} failed to start; restored the database copy "
             "and starting the previous version")
        self.drop_copy(discard_name(snapshot))
        self.publish("error", error=(
            f"Version {self.release_name(revision)} failed to start, so this worker went back to the "
            "previous version. Your data is as it was before the update."), version=revision)
        return True

    def drop_copy(self, name):
        try:
            self.database.drop_update_database(name)
        except Exception as exc:
            _log(f"could not remove the database copy {name} ({type(exc).__name__}); "
                 "it is removed at the next start")

    def discard_copies(self):
        """Remove database copies no update state refers to (an interrupted
        activation or clean-up). Never blocks a start."""
        if self.database is None or self.state.get("blocked"):
            return  # a blocked worker keeps everything for the operator
        try:
            names = self.database.update_databases()
        except Exception as exc:
            _log(f"could not list update database copies ({type(exc).__name__})")
            return
        for name in names:
            if name != self.state.get("snapshot"):
                self.drop_copy(name)

    def healthy(self):
        if self.state.get("booting") == self.running and self.running:
            if self.state.get("active"):
                self.state["previous"] = self.state["active"]
            self.state["active"] = self.running
            self.state.pop("pending", None)
            self.state.pop("booting", None)
            snapshot = self.state.pop("snapshot", None)
            if self.running in self.state.get("failed", []):
                # An earlier failed attempt of this revision, retried by request.
                self.state["failed"] = [r for r in self.state["failed"] if r != self.running]
                if not self.state["failed"]:
                    self.state.pop("failed")
            self.save()
            if snapshot and self.database is not None:
                self.drop_copy(snapshot)
            self.publish("idle", "Updated version is running.")
            self.prune()

    def prune(self):
        # Keep one prior build for diagnosis, never for automatic rollback.
        # Failed revisions are retained until superseded by a newer image.
        keep = {self.state.get(field) for field in ("active", "previous", "pending", "booting")}
        keep.update(self.state.get("failed", []))
        try:
            for candidate in (self.root / "versions").iterdir():
                if (REVISION.fullmatch(candidate.name) and candidate.name not in keep
                        and candidate.is_dir() and not candidate.is_symlink()):
                    shutil.rmtree(candidate)
        except OSError:
            self.publish("idle", "Application updated. Some old build files could not be removed.")

    def launch(self, env, activate=False):
        revision = self.state.get("active")
        if activate:
            if not self.allowed():
                raise UpdateError("Application updates are disabled")
            revision = self.state.get("pending")
            if not revision:
                raise UpdateError("No application update is ready")
        if not revision:
            if not activate:
                self.running = None  # the image's own code
            return [sys.executable, "-m", "app.main"], None, env
        manifest = self.manifest(revision)
        if missing_capabilities(manifest["container"], self.image):
            raise UpdateError(self.runtime_mismatch(manifest))
        directory = self.directory(revision)
        source = directory / "source"
        python = directory / "venv/bin/python"
        if not (directory / "complete").is_file() or not (source / "app/static/index.html").is_file() or not python.is_file():
            raise UpdateError("Application build is incomplete")
        verify_tree(directory, manifest)
        env = dict(env, GIT_SHA=revision, PYTHONNOUSERSITE="1", TOOLS_DIR=str(source / "tools"),
                   BROWSER_MCP_ENTRY=str(source / "browser-mcp/dist/index.js"))
        if manifest.get("version"):
            env["APP_VERSION"] = manifest["version"]
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            env.pop(key, None)
        env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
        if not activate:
            self.running = revision
        return [str(python), "-m", "app.main"], str(source), env

    def runtime_mismatch(self, manifest):
        """Why the installed update cannot start on this image, and the way out.

        Reached only for the active update: a staged one built for another
        image is dropped at startup, and an image newer than the update never
        keeps it. So the image is at or behind the update on main, and its
        bundled code is refused over a database that update may have migrated.
        """
        container = manifest["container"]
        release = manifest.get("version") or manifest["revision"][:12]
        return (f"The installed application update {release} ({manifest.get('channel', 'stable')} channel, "
                f"main revision {container['sequence']}) was built for a different container image. "
                f"This image ({self.image['revision'][:12]}, main revision {self.image['sequence']}) is not newer "
                "than that update, so its own code is not run over the update's database. Change to an image "
                f"built from main revision {container['sequence']} or later, or, to run this image's code over "
                "the existing data anyway, stop the container and delete application-updates/state.json "
                "from the data volume; files retained.")

    def snapshot_room(self):
        """Refuse an install before stopping anything when the database copy
        would not fit; the message says how much space is needed."""
        if self.database is None:
            return
        try:
            free, need = self.database.snapshot_space()
        except Exception as exc:
            raise UpdateError("Could not check free disk space for the update; the current version keeps running") from exc
        if free < need:
            raise UpdateError(
                "Not enough free disk space for the copy of the database the update keeps until the new "
                f"version has started ({free / 1024**3:.1f} GB free, {need / 1024**3:.1f} GB needed). "
                "Free some space on the data volume and try again; the current version keeps running.")

    def activating(self):
        # After the old app stopped, before anything could migrate the DB: copy
        # it, then record the first start. A failed copy raises before booting
        # is saved, and the old version is started again over the same data.
        revision = self.state["pending"]
        snapshot = None
        if self.database is not None:
            snapshot = f"ai_shuffle_rollback_{revision[:12]}"
            self.database.snapshot(snapshot)
        self.running = revision
        self.state["booting"] = revision
        if snapshot:
            self.state["snapshot"] = snapshot
        self.save()

    def current_lease(self):
        """The lease header value, or None. Never raises: without a lease the
        request goes out as it always did, and the hub decides."""
        try:
            value = self.lease() if self.lease else None
        except Exception:
            return None
        return value if isinstance(value, str) and LEASE_VALUE.fullmatch(value) else None

    def fetch(self, endpoint, destination=None, manifest=None):
        if not re.fullmatch(r"manifest|manifest/edge|sources/[a-f0-9]{40}", endpoint):
            raise UpdateError("Invalid source endpoint")
        limit = manifest["size"] if manifest else 32768
        chunks, size, digest = [], 0, hashlib.sha256()
        # Every request here goes to the hub's own update endpoints (ORIGIN is a
        # constant and redirects are refused), so the lease reaches only the hub.
        lease = self.current_lease()
        headers = {LEASE_HEADER: lease} if lease else {}
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=120) as client:
            with client.stream("GET", ORIGIN + "/v1/desktop/" + endpoint, headers=headers) as response:
                if response.status_code != 200:
                    raise UpdateError(f"Source request failed ({response.status_code})")
                output = destination.open("xb") if destination else None
                try:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if self.stop.is_set() or size > limit:
                            raise UpdateError("Source download interrupted or exceeds signed size")
                        digest.update(chunk)
                        if output:
                            output.write(chunk)
                        else:
                            chunks.append(chunk)
                finally:
                    if output:
                        output.close()
        if manifest and (size != manifest["size"] or digest.hexdigest() != manifest["sha256"]):
            raise UpdateError("Source checksum or size mismatch")
        return b"".join(chunks)

    def run_build(self, command, cwd, env):
        with (self.root / "build.log").open("ab") as log:
            child = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 900
            try:
                while child.poll() is None:
                    if self.stop.wait(.2) or time.monotonic() >= deadline:
                        raise UpdateError("Application build interrupted or timed out")
                if child.returncode:
                    raise UpdateError("Application build failed; see application-updates/build.log on the data volume")
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()

    def build(self, directory, revision, version=None):
        source = directory / "source"
        home = directory / "build-home"
        home.mkdir()
        env = build_environment(home)
        env["GITHUB_SHA"] = revision
        if version:
            env["APP_VERSION"] = version  # the frontend stamps it into the UI
        python = directory / "venv/bin/python"
        if not (source / "requirements.lock").is_file():
            raise UpdateError("This source update has no hash-pinned requirements.lock; install a newer image")
        self.run_build([sys.executable, "-m", "venv", str(directory / "venv")], source, env)
        # pip's 15-second read timeout dropped an 85 MB wheel mid-download on
        # the Unraid host and failed the whole update; allow slow mirrors.
        # Every package, including fluid's build backend, must match a hash in
        # the signed source's lock, so a hostile index release cannot slip in.
        pip = [str(python), "-m", "pip", "install", "--prefer-binary", "--timeout", "120", "--retries", "10"]
        self.run_build(pip + ["--require-hashes", "-r", "requirements.lock"], source, env)
        self.run_build(pip + ["--no-deps", "--no-build-isolation", "./fluid"], source, env)
        for package in ("frontend", "tools", "browser-mcp"):
            self.publish("downloading", f"Building {package}…", version=revision)
            self.run_build(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], source / package, env)
            self.run_build(["npm", "run", "build"], source / package, env)
        shutil.copytree(source / "frontend/build", source / "app/static")
        (directory / "complete").write_text(revision)

    def check(self, download=False):
        if not self.allowed():
            raise UpdateError("Application updates are disabled")
        if self.state.get("pending"):
            self.publish("ready", "Update ready. Choose Update and restart.")
            return
        self.publish("checking")
        signed = self.fetch("manifest" if self.channel == "stable" else "manifest/edge")
        manifest = verify_manifest(signed, self.public_key)
        self.checked_at = time.time()
        if manifest.get("channel") != self.channel:
            raise UpdateError(f"The update feed did not return a signed {self.channel} release")
        if not manifest.get("tree"):
            raise UpdateError("The update has no signed source tree; install a newer image")
        revision = manifest["revision"]
        # Said ahead of the release that needs it, while this one still installs.
        lacking = set(manifest["container"].get("recommends") or []) - set(self.image.get("provides") or [])
        self.notice = IMAGE_NOTICE if lacking else None
        active = self.manifest(self.state["active"])["container"] if self.state.get("active") else self.image
        if revision in (self.state.get("active"), self.image["revision"]) or manifest["container"]["sequence"] <= active["sequence"]:
            self.publish("idle", "You are up to date.")
            return
        if missing_capabilities(manifest["container"], self.image):
            # The offered revision, not the running one: a status naming the
            # running revision reads as "Already up to date" (#1901).
            self.publish("image-required", IMAGE_REQUIRED, version=revision)
            return
        if revision in self.state.get("failed", []) and not download:
            # Rolled back once already: never offered on its own again. An
            # explicit download (the Update button) may try it once more.
            self.publish("error", error=(
                f"Version {manifest.get('version') or revision[:12]} failed to start on this worker and was "
                "rolled back. Choose Update to try it again, or wait for a newer release."), version=revision)
            return
        self.remember(manifest)
        if not download:
            self.publish("available", "An application update is available.", version=revision)
            return
        directory = self.directory(revision)
        if directory.exists():
            # Only unactivated, unretained partial builds can be discarded.
            shutil.rmtree(directory)
        directory.mkdir(parents=True, mode=0o700)
        archive = directory / "source.zip"
        try:
            self.publish("downloading", "Downloading verified source…", version=revision)
            self.fetch("sources/" + revision, archive, manifest)
            listing = tree_lines(extract_source(archive, directory / "source", revision))
            archive.unlink()
            if hashlib.sha256(listing).hexdigest() != manifest["tree"]:
                raise UpdateError("Source files do not match the signed release")
            (directory / "tree.txt").write_bytes(listing)
            self.build(directory, revision, manifest.get("version"))
            verify_tree(directory, manifest)
            (directory / "release.json").write_bytes(signed)
            if not self.allowed():
                raise UpdateError("Updates were disabled while preparing; no update was activated")
            self.state["pending"] = revision
            self.save()
            self.publish("ready", "Update ready. Choose Update and restart.")
        except Exception:
            shutil.rmtree(directory)
            raise

    def service(self):
        try:
            action = None
            try:
                request = json.loads(self.request_path.read_text())
                self.request_path.unlink()
                # Old queued requests are never replayed after a busy/offline period.
                action = request.get("action") if request.get("expires_at", 0) >= time.time() else None
            except FileNotFoundError:
                pass
            if action == "install":
                if not self.allowed():
                    raise UpdateError("Application updates are disabled")
                if not self.state.get("pending"):
                    raise UpdateError("No application update is ready")
                self.install_requested.set()
                return
            if action in ("check", "download") or time.monotonic() >= self.next_check:
                self.next_check = time.monotonic() + CHECK_INTERVAL_SECONDS
                if self.allowed():
                    self.check(download=action == "download")
        except Exception as exc:
            self.publish("ready" if self.state.get("pending") else "error",
                         error=str(exc) if isinstance(exc, UpdateError) else "Application update failed; current version is unchanged")

    def tick(self):
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.service, daemon=True, name="application-updates")
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=125)
