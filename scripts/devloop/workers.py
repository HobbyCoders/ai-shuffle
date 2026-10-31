"""A two-worker lease, reusing the single-worker supervisor and ingress rules.

Only generated environments reach the apps. Owner setup calls carry each app's
real per-launch desktop-listener credential; peers use the protected listener,
without that credential, cookies, Origin spoofing or a copied membership DB.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from scripts.devloop import __main__ as devloop

PAIR_TIMEOUT = 180
WORKER_NAMES = ("Dev worker A", "Dev worker B")


class WorkerStatus:
    """Serialize both reporters into one status; keep A's legacy aliases."""

    def __init__(self, args: argparse.Namespace, directory: Path, guardian: dict | None,
                 stopped: threading.Event) -> None:
        from app.core.temp_scratch import process_identity

        self.args, self.directory, self.stopped = args, directory, stopped
        self.guardian = guardian
        self.lock = threading.Lock()
        self.workers: list[dict] = [{"name": name, "state": "starting"} for name in WORKER_NAMES]
        self.network: dict | None = None
        self.identity = process_identity(os.getpid())
        self.retired = False

    def update(self, index: int, data: dict) -> None:
        with self.lock:
            self.workers[index] = {**json.loads(json.dumps(data)), "name": WORKER_NAMES[index]}
            self._write()

    def paired(self, network: dict) -> None:
        with self.lock:
            self.network = network
            self._write()

    def retire(self) -> None:
        with self.lock:
            self.retired = True
            self._write()

    def _write(self) -> None:
        states = [worker["state"] for worker in self.workers]
        if self.retired or self.stopped.is_set():
            state = "stopping"
        elif "app-exited" in states:
            state = "app-exited"
        elif all(value == "ready" for value in states):
            state = "ready" if self.network else "pairing"
        else:
            # Readiness of the pair must be proved again after either restart.
            self.network = None
            state = "restarting" if "restarting" in states else "starting"
        data = {**self.workers[0], "pid": os.getpid(), "pid_identity": self.identity,
                "checkout": str(devloop.ROOT), "state": state, "worker_count": 2,
                "workers": self.workers, "network": self.network, "guardian": self.guardian,
                "scratch_dir": str(self.directory), "remotes_dir": str(self.directory / "remotes")}
        devloop.write_private_json(self.args.status, data)


def worker_environment(args: argparse.Namespace, directory: Path, index: int,
                       fake: Any, fake_voice: Any, *, used_ports: set[int]) -> tuple[dict, int | None]:
    """One private home/data/workspace and one explicit peer endpoint per worker."""
    from app.preview_run import free_port

    def port(requested: int | None = None) -> int:
        value = requested or free_port(exclude=used_ports)
        used_ports.add(value)
        return value

    directory.mkdir()
    preview = port(args.port if index == 0 else None)
    protected = port()
    vite = None if args.no_vite else port(args.vite_port if index == 0 else None)
    extra = {"SHUFFLE_DEV_FAKE_AI_URL": fake.url, "SHUFFLE_DEV_FAKE_VOICE_URL": fake_voice.url,
             "SHUFFLE_DEV_VOICE_ENGINE": args.voice_engine,
             "SHUFFLE_DEV_ACCESS_FILE": str(directory / "access.json"),
             "SHUFFLE_DEV_SEED_FILE": str(directory / "seed.json"),
             "SHUFFLE_DEV_SEED": "default" if index == 0 else "empty"}
    if vite:
        extra["SHUFFLE_DEV_UI"] = "vite"
    env = devloop.application_environment(args, directory, preview, protected, extra=extra)
    # preview_environment also allocates the local-owner port. Reserve all ports
    # across both workers (including explicitly requested preview/Vite ports).
    while int(env["PORT"]) in used_ports:
        env["PORT"] = str(free_port(exclude=used_ports))
    used_ports.add(int(env["PORT"]))
    env["CLUSTER_ADVERTISE_URL"] = f"http://127.0.0.1:{protected}"
    env["SHUFFLE_DESKTOP_LISTENER_TOKEN"] = secrets.token_hex(32)
    env["SHUFFLE_DEV_REMOTES_DIR"] = str(directory.parent / "remotes")
    return env, vite


def owner_request(instance: devloop.Instance, method: str, path: str, body: dict | None = None) -> dict:
    """The app's supported local process credential, not a forged browser Origin."""
    connection = http.client.HTTPConnection("127.0.0.1", int(instance.env["PORT"]), timeout=5)
    headers = {"X-Shuffle-Desktop-Listener": instance.env["SHUFFLE_DESKTOP_LISTENER_TOKEN"]}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body)
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        if response.status >= 400:
            # Codes, poll secrets and credentials must never enter launcher logs.
            raise RuntimeError(f"{method} {path} answered HTTP {response.status}")
        return json.loads(raw) if raw else {}
    finally:
        connection.close()


def begin_pairing(instances: list[devloop.Instance]) -> None:
    a, b = instances
    owner_request(a, "POST", "/api/v1/network/create",
                  {"name": "Dev network", "device_name": WORKER_NAMES[0]})
    endpoint = a.env["CLUSTER_ADVERTISE_URL"]
    invitation = owner_request(a, "POST", "/api/v1/pairing/grants",
                               {"purpose": "network:join", "endpoint_url": endpoint})
    owner_request(b, "POST", "/api/v1/network/enroll",
                  {"endpoint_url": endpoint, "code": invitation["code"],
                   "device_name": WORKER_NAMES[1]})
    # The app admits possession of the one-time invite code automatically. The
    # joiner supplies its actual effective_instance_id; no fixture alias here.


def online_pair(instances: list[devloop.Instance]) -> dict | None:
    """Ready means BOTH real status/installs APIs attest the approved online peer."""
    statuses = [owner_request(i, "GET", "/api/v1/network/status") for i in instances]
    networks = [status.get("network") or {} for status in statuses]
    network_id = networks[0].get("network_id")
    if (not network_id or networks[1].get("network_id") != network_id
            or any(n.get("bootstrap_state") != "ready" for n in networks)):
        return None
    observations = []
    for index, instance in enumerate(instances):
        peer_id = statuses[1 - index]["device"]["device_id"]
        members = statuses[index].get("members") or []
        peer = next((m for m in members if m["device_id"] == peer_id), {})
        installs = owner_request(instance, "GET", "/api/v1/cluster/installs")["installs"]
        install = next((row for row in installs if row["id"] == peer_id), {})
        if not (peer.get("online") and peer.get("status") == "active"
                and install.get("online") and install.get("approved") and not install.get("is_self")):
            return None
        observations.append({"name": WORKER_NAMES[index], "device_id": statuses[index]["device"]["device_id"],
                             "instance_id": statuses[index]["instance_id"],
                             "peer_device_id": peer_id, "peer_online": True, "peer_approved": True})
    return {"network_id": network_id, "workers": observations}


def up_workers(args: argparse.Namespace) -> int:
    """One foreground parent/TTL owns both app+DB leases, Vites and fake providers."""
    from app.core.temp_scratch import process_identity, scratch_directory
    from app.preview_run import free_port, supervise
    from docker import container_runtime as runtime
    from scripts.devloop import fake_claude
    from scripts.devloop.fake_ai import FakeAI
    from scripts.devloop.fake_voice import FakeVoice
    from scripts.devloop.guardian import spawn_guardian

    if not args.no_vite:
        devloop._ensure_frontend()
    stopped = threading.Event()

    def stop(signum: int, frame: Any) -> None:
        stopped.set()
        runtime.STOP.set()

    old = {sig: signal.signal(sig, stop) for sig in devloop.STOP_SIGNALS}
    try:
        with scratch_directory("ai-shuffle-preview-") as raw:
            directory = Path(raw)
            guardian = spawn_guardian(args.status, directory)
            status = WorkerStatus(args, directory, guardian, stopped)
            instances: list[devloop.Instance] = []
            supervisors: list[threading.Thread] = []
            reporters: list[threading.Thread] = []
            watchers: list[devloop.Watcher] = []
            fake = voice = None
            errors: list[str] = []
            deadline = time.monotonic() + args.ttl_seconds
            owner_pid = os.getppid()

            def supervise_worker(instance: devloop.Instance) -> None:
                try:
                    code = supervise([sys.executable, "-m", "app.main"], instance.env, instance.database,
                                     ttl=max(0.001, deadline - time.monotonic()), owner_pid=owner_pid,
                                     stopped=stopped, restart=None if args.ci else instance.gate.restart,
                                     on_start=instance.on_app_start, on_exit=instance.on_app_exit,
                                     stop_timeout=devloop.APP_STOP_SECONDS)
                    if code:
                        errors.append(f"worker app exited with code {code}")
                        devloop.log.error("%s", errors[-1])
                except Exception as exc:  # noqa: BLE001 - thread boundary must stop both leases
                    errors.append(f"worker supervisor failed: {type(exc).__name__}")
                    devloop.log.error("%s", errors[-1])
                finally:
                    stopped.set()  # Either lease ending ends the whole pair.

            try:
                status.update(0, status.workers[0])
                fake = FakeAI(log_path=directory / "fake-ai-requests.jsonl",
                              latency_ms=args.latency_ms, tps=args.tps)
                voice = FakeVoice(log_path=directory / "fake-voice.jsonl", host=str(args.fake_voice_host))
                used = {p for p in (args.port, args.vite_port, urlsplit(fake.url).port, voice.port) if p}
                for index, name in enumerate(WORKER_NAMES):
                    home = directory / ("worker-a" if index == 0 else "worker-b")
                    env, vite_port = worker_environment(args, home, index, fake, voice, used_ports=used)
                    # Per-worker CI verdicts would announce readiness before pairing.
                    worker_args = argparse.Namespace(**{**vars(args), "ci": False})
                    instance = devloop.Instance(worker_args, home, env, fake, vite_port,
                                                status_writer=lambda data, n=index: status.update(n, data))
                    instances.append(instance)
                    instance.stopped = stopped
                    instance.data["mode"] = "ci" if args.ci else "dev"
                    if args.claude_cli == "fake":
                        bin_dir = fake_claude.install(home)
                        env["PATH"] = fake_claude.instance_path(env["PATH"], bin_dir)
                        fake_claude.cover_home(Path(env["HOME"]), bin_dir)
                        instance.data["claude_cli"] = {"mode": "fake", "path": str(bin_dir / "claude"),
                                                       "log": str(fake_claude.log_path(home))}
                    db_port = free_port(exclude=used)
                    used.add(db_port)
                    instance.database = runtime.Database(SimpleNamespace(effective_data_dir=Path(env["DATA_DIR"])),
                                                         port=db_port)
                    instance.data["urls"]["peer"] = env["CLUSTER_ADVERTISE_URL"]
                    control_host = instance.browser if str(args.fake_voice_host) == "0.0.0.0" else "127.0.0.1"
                    instance.data["fake_voice"] = {
                        "url": voice.url, "engine": args.voice_engine,
                        "control": f"http://{control_host}:{voice.port}/fake/", "log": str(voice.log_path),
                        "scenarios_dir": str(voice.scenarios.directory),
                        "scenarios": sorted(voice.scenarios.load())}
                    if vite_port:
                        api = f"http://{devloop._url_host(instance.connect)}:{instance.port}"
                        instance.vite = subprocess.Popen(devloop._vite_command(str(args.host), vite_port),
                                                         cwd=devloop.FRONTEND,
                                                         env=devloop.vite_environment(env, api),
                                                         start_new_session=True)
                        instance.data["vite"] = {"pid": instance.vite.pid,
                                                 "pid_identity": process_identity(instance.vite.pid),
                                                 "port": vite_port, "proxies_to": api}
                    instance.write()
                    reporter = threading.Thread(target=instance.report, name=f"{name}-report", daemon=True)
                    reporters.append(reporter)
                    reporter.start()
                    if not args.ci:
                        watcher = devloop.Watcher(instance.gate, poll=args.poll)
                        watchers.append(watcher)
                        watcher.start()
                    supervisor = threading.Thread(target=supervise_worker, args=(instance,), name=name)
                    supervisors.append(supervisor)
                    supervisor.start()

                begun = announced = False
                pair_deadline = None
                while not stopped.wait(0.5):
                    if not all(i.data["state"] == "ready" for i in instances) or status.network:
                        pair_deadline = None
                        continue
                    if pair_deadline is None:
                        pair_deadline = time.monotonic() + PAIR_TIMEOUT
                    if not begun:
                        for instance in instances:
                            if not (instance.data.get("seed") or {}).get("ok"):
                                raise RuntimeError("A worker seed failed; run down then up")
                        begin_pairing(instances)
                        begun = True
                    try:
                        network = online_pair(instances)
                    except (OSError, http.client.HTTPException):
                        network = None  # A file-change restart can race the readiness snapshot.
                    if network:
                        status.paired(network)
                        if not announced:
                            announced = True
                            devloop.log.info("PAIRED: each worker lists the other approved and ONLINE")
                            for instance, name in zip(instances, WORKER_NAMES):
                                devloop.log.info("%s — open once: %s", name, instance.data["access_link"])
                            if args.ci:
                                print(devloop.READY_LINE + " " + json.dumps({"status": str(args.status),
                                      "workers": [i.data["access_link"] for i in instances],
                                      "network": network}), flush=True)
                    elif time.monotonic() >= pair_deadline:
                        raise RuntimeError("Workers did not both report the approved peer ONLINE within 180s")
                if not announced and not errors:
                    errors.append("The lease ended before both workers were ready and paired")
                    devloop.log.error("%s", errors[-1])
            except Exception as exc:  # noqa: BLE001 - lease boundary reports failure after teardown
                errors.append(str(exc))
                devloop.log.error("Two-worker instance failed: %s", exc)
            finally:
                stopped.set()
                runtime.STOP.set()
                status.retire()
                for watcher in watchers:
                    watcher.stop()
                for instance in instances:
                    instance.done.set()
                for supervisor in supervisors:
                    supervisor.join()
                for reporter in reporters:
                    reporter.join(6)
                for instance in instances:
                    if instance.vite is not None:
                        runtime.stop_group(instance.vite, 10)
                if fake is not None:
                    fake.close()
                if voice is not None:
                    voice.close()
                devloop.remove_own_status(args.status, os.getpid())
                devloop.log.info("stopped; BOTH apps, PostgreSQL clusters and Vites are down")
            if errors and args.ci:
                print(devloop.FAILED_LINE + " " + json.dumps({"status": str(args.status), "error": errors[-1]}),
                      flush=True)
            return 1 if errors else 0
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
