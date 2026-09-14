"""Signed, staged application updates supervised inside the stable Docker image.

Never edits /app, installs OS packages, or opens the Docker socket. Builds run
unprivileged with a credential-free environment. State and versions live on the
existing data volume. A failed first boot blocks rollback over migrated data.
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

from app.core.application_updates import atomic_json

ORIGIN = "https://hobbycoders.com"
REVISION = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"[a-f0-9]{64}")
MAX_ARCHIVE = 512 * 1024**2


class UpdateError(Exception):
    pass


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
        container = result.get("container", {})
        if (type(container.get("sequence")) is not int or not 0 < container["sequence"] < 2**53
                or not DIGEST.fullmatch(container.get("runtime", ""))):
            raise UpdateError("This source update needs a newer container release")
        return result
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError("Invalid or unsigned source manifest") from exc


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
        for item in entries:
            relative = PurePosixPath(item.filename).parts[1:]
            if not relative:
                continue
            destination = target.joinpath(*relative)
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(item) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)


def build_environment(home):
    # No database descriptor, provider keys, cloud tokens, proxy or pip/npm config.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "CI": "true",
            "LANG": "C.UTF-8", "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1",
            "NPM_CONFIG_USERCONFIG": os.devnull, "ELECTRON_SKIP_BINARY_DOWNLOAD": "1"}


class ContainerUpdates:
    def __init__(self, root, image, public_key, allowed, lifecycle):
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
        self.next_check = time.monotonic() + 15
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
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise UpdateError("Invalid container update state; files retained") from exc
        # An interrupted explicit restart must not become permission on next boot.
        self.request_path.unlink(missing_ok=True)
        if self.state.get("booting"):
            self.failed()
        pending = self.state.get("pending")
        if pending:
            staged = self.manifest(pending)["container"]
            if staged["sequence"] <= self.image["sequence"] or staged["runtime"] != self.image["runtime"]:
                self.state.pop("pending")
                self.save()
        retained = [self.manifest(revision) for revision in
                    {*self.state.get("failed", []), self.state.get("active")} if revision]
        if retained and all(self.image["sequence"] > m["container"]["sequence"] for m in retained):
            # A milestone strictly newer than every possibly migrated revision.
            self.state = {"pending": self.state["pending"]} if self.state.get("pending") else {}
            self.save()
        if self.state.get("blocked"):
            raise UpdateError("An application update failed first boot. Install a newer image; older code cannot safely run over this database. Files retained.")
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

    def publish(self, phase, message=None, error=None, version=None):
        atomic_json(self.root / "status.json", {"phase": phase, "message": message,
                    "error": error, "version": (version or self.state.get("pending") or self.running or "")[:12] or None})

    def failed(self):
        revision = self.state.pop("booting", None)
        if revision:
            self.state["failed"] = sorted({*self.state.get("failed", []), revision})
            self.state.pop("pending", None)
            self.state["blocked"] = True
            self.save()

    def healthy(self):
        if self.state.get("booting") == self.running and self.running:
            if self.state.get("active"):
                self.state["previous"] = self.state["active"]
            self.state["active"] = self.running
            self.state.pop("pending", None)
            self.state.pop("booting", None)
            self.save()
            self.publish("idle", "Application update is running.")
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
            return [sys.executable, "-m", "app.main"], None, env
        manifest = self.manifest(revision)
        if manifest["container"]["runtime"] != self.image["runtime"]:
            raise UpdateError("Stored application requires a different container runtime; install a newer image")
        directory = self.directory(revision)
        source = directory / "source"
        python = directory / "venv/bin/python"
        if not (directory / "complete").is_file() or not (source / "app/static/index.html").is_file() or not python.is_file():
            raise UpdateError("Application build is incomplete")
        env = dict(env, GIT_SHA=revision, PYTHONNOUSERSITE="1", TOOLS_DIR=str(source / "tools"),
                   BROWSER_MCP_ENTRY=str(source / "browser-mcp/dist/index.js"))
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            env.pop(key, None)
        env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
        if not activate:
            self.running = revision
        return [str(python), "-m", "app.main"], str(source), env

    def activating(self):
        self.running = self.state["pending"]
        self.state["booting"] = self.running
        self.save()  # after old app stopped, before anything could migrate the DB

    def fetch(self, endpoint, destination=None, manifest=None):
        if not re.fullmatch(r"manifest|sources/[a-f0-9]{40}", endpoint):
            raise UpdateError("Invalid source endpoint")
        limit = manifest["size"] if manifest else 32768
        chunks, size, digest = [], 0, hashlib.sha256()
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=120) as client:
            with client.stream("GET", ORIGIN + "/v1/desktop/" + endpoint) as response:
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

    def build(self, directory, revision):
        source = directory / "source"
        home = directory / "build-home"
        home.mkdir()
        env = build_environment(home)
        env["GITHUB_SHA"] = revision
        python = directory / "venv/bin/python"
        self.run_build([sys.executable, "-m", "venv", str(directory / "venv")], source, env)
        self.run_build([str(python), "-m", "pip", "install", "--prefer-binary", "-r", "requirements.txt", "./fluid"], source, env)
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
        signed = self.fetch("manifest")
        manifest = verify_manifest(signed, self.public_key)
        revision = manifest["revision"]
        active = self.manifest(self.state["active"])["container"] if self.state.get("active") else self.image
        if revision in (self.state.get("active"), self.image["revision"]) or manifest["container"]["sequence"] <= active["sequence"]:
            self.publish("idle", "You are up to date.")
            return
        if manifest["container"]["runtime"] != self.image["runtime"]:
            self.publish("image-required", "A new Docker image is required for this update.")
            return
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
            extract_source(archive, directory / "source", revision)
            archive.unlink()
            self.build(directory, revision)
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
                self.next_check = time.monotonic() + 600
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
