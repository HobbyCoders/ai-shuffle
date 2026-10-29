"""Two paired dev instances in one worker network: ``up --pair``.

    python -m scripts.devloop up --pair [--ci] [--host H --browser-host IP] [--no-vite]
    python -m scripts.devloop pair offline|online a|b
    python -m scripts.devloop status --pair [--wait SECONDS]
    python -m scripts.devloop down --pair

``up --pair`` runs two ordinary ``up`` launchers as its children, worker A and
worker B: each has its own throwaway PostgreSQL, data, scratch, ports, fake
AI and guardian (``guardian.py``), so a SIGKILL anywhere still leaves nothing
behind. It then pairs them through the app's real invite flow, the way an
owner does it in Settings > Devices, as an owner Bearer on each instance's
loopback owner listener:

1. A seeds the default data on its first start; one restart turns that
   existing install into a network of one (the real upgrade path).
2. A creates a ``network:join`` invite for its own peer address; B enrolls
   with that link (``POST /network/enroll``), admits on contact, copies A's
   snapshot and starts syncing. Each instance's peer address is its own
   protected loopback listener (``CLUSTER_ADVERTISE_URL``), normal peer
   ingress, never the owner port.
3. B restarts and runs the ``pair-b`` seed: its own project and chats, owned
   by B and published to A.

Both instances get one random pair key and each other's address, and refuse
to pair with anything else (``app/network/dev_pair.py``). The pair status
file lists both instances' status files and device ids; ``pair offline a``
stops A's app (its database stays up) until ``pair online a``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from scripts.devloop.__main__ import (
    FAILED_LINE, OFFLINE_SIGNAL, ONLINE_SIGNAL, ROOT, StatusError, _iso, ensure_private_dir,
    load_status, remove_own_status, status_alive, write_private_json,
)

log = logging.getLogger("devloop")
ROLES = ("a", "b")
NAMES = {"a": "Dev worker A", "b": "Dev worker B"}
SEEDS = {"a": "default", "b": "pair-b"}
#: ``--ci``: one of these, then a JSON object, on one stdout line.
PAIR_READY_LINE = "DEVLOOP_PAIR_READY"
PAIR_FAILED_LINE = "DEVLOOP_PAIR_FAILED"
STEP_SECONDS = 240
#: Member output lines echoed to the pair's own output (the rest is in its log).
ECHOED = ("[devloop]", "DEVLOOP_", "Traceback", "  File ", "ValueError", "RuntimeError")
CHILD_STOP_SECONDS = 90


class PairError(RuntimeError):
    """The pair could not be set up (the message says which step)."""


class PairAbort(Exception):
    """A step failed for good: no point waiting for it any longer."""


def is_pair_status(path: Path) -> bool:
    try:
        status = load_status(path)
    except StatusError:
        return False
    return bool(status and status.get("kind") == "pair")


# ─── one member ────────────────────────────────────────────────────────────


class Member:
    """One child ``up`` launcher (worker A or B) and the app it runs."""

    def __init__(self, role: str, status: Path, args: argparse.Namespace, peer_url: str,
                 protected: int, key: str) -> None:
        self.role, self.status_path, self.protected = role, status, protected
        self.url = f"http://127.0.0.1:{protected}"
        command = [sys.executable, "-m", "scripts.devloop", "up", "--pair-role", role,
                   "--status", str(status), "--protected-port", str(protected),
                   "--ttl-seconds", str(args.ttl_seconds), "--seed", SEEDS[role]]
        if args.ci:
            command.append("--ci")
        else:
            command += ["--host", str(args.host)]
            if args.browser_host:
                command += ["--browser-host", str(args.browser_host)]
            if args.no_vite:
                command.append("--no-vite")
            if args.claude_cli:
                command += ["--claude-cli", args.claude_cli]
        env = {**os.environ, "SHUFFLE_DEV_PAIR_KEY": key, "SHUFFLE_DEV_PAIR_PEERS": peer_url}
        env.pop("DEVLOOP_CI", None)
        self.failure: str | None = None
        #: The member's whole output; only its launcher lines and errors are echoed.
        self.log_path = status.with_name(status.stem + ".log")
        self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.reader = threading.Thread(target=self._read, name=f"devloop-{role}", daemon=True)
        self.reader.start()

    def _read(self) -> None:
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as log_file:
            for line in self.process.stdout:
                log_file.write(line)
                log_file.flush()
                if line.startswith(ECHOED) or " ERROR " in line or " CRITICAL " in line:
                    print(f"[{self.role}] {line}", end="", flush=True)
                if line.startswith(FAILED_LINE):
                    self.failure = line[len(FAILED_LINE):].strip()

    # ── its status file ──
    def status(self) -> dict:
        try:
            return load_status(self.status_path) or {}
        except StatusError:
            return {}

    def wait_state(self, want: str, timeout: float, *, since_starts: int = 0) -> dict:
        """Its status reaches ``want`` (``ready`` after more than ``since_starts`` starts)."""
        deadline = time.monotonic() + timeout
        while True:
            if self.process.poll() is not None:
                raise PairError(f"worker {self.role.upper()}'s launcher exited "
                                f"({self.process.returncode}): {self.failure or 'see its log'}")
            if self.failure:
                raise PairError(f"worker {self.role.upper()} failed: {self.failure}")
            status = self.status()
            starts = int((status.get("app") or {}).get("starts") or 0)
            if status.get("state") == want and starts > since_starts:
                return status
            if status.get("state") == "app-exited":
                raise PairError(f"worker {self.role.upper()}'s app exited before it was {want}")
            if time.monotonic() >= deadline:
                raise PairError(f"worker {self.role.upper()} was not {want} within {timeout:.0f}s "
                                f"(it is {status.get('state')})")
            time.sleep(0.25)

    def signal(self, signum: int) -> None:
        os.kill(self.process.pid, signum)

    def restart(self, timeout: float = STEP_SECONDS) -> dict:
        """Stop the app and start it again (one more start), then wait for ready."""
        starts = int((self.status().get("app") or {}).get("starts") or 0)
        self.signal(OFFLINE_SIGNAL)
        self.wait_state("offline", timeout)
        self.signal(ONLINE_SIGNAL)
        return self.wait_state("ready", timeout, since_starts=starts)

    # ── its owner API (loopback owner listener, owner Bearer) ──
    def api(self, method: str, path: str, body: Any = None, *, ok: tuple = (200, 201, 202, 204),
            timeout: float = 60) -> Any:
        import httpx

        status = self.status()
        port = (status.get("app") or {}).get("local_port")
        owner = Path(status.get("scratch_dir") or "/nonexistent") / "owner.json"
        try:
            token = json.loads(owner.read_text(encoding="utf-8"))["token"]
        except (OSError, ValueError, KeyError) as exc:
            raise PairError(f"worker {self.role.upper()} wrote no owner session ({exc})") from None
        response = httpx.request(method, f"http://127.0.0.1:{port}/api/v1{path}", json=body,
                                 headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        if response.status_code not in ok:
            raise PairError(f"worker {self.role.upper()}: {method} {path} answered HTTP "
                            f"{response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    def wait_stopped(self, timeout: float) -> None:
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("worker %s did not stop within %ss; killing its launcher (its guardian "
                        "finishes the teardown)", self.role.upper(), timeout)
            self.process.kill()
            self.process.wait(timeout=10)
        self.reader.join(5)


# ─── pairing ───────────────────────────────────────────────────────────────


def _wait(what: str, check, timeout: float = STEP_SECONDS, step: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            result = check()
        except PairError as exc:
            result, last = None, str(exc)
        if result:
            return result
        if time.monotonic() >= deadline:
            raise PairError(f"{what} did not happen within {timeout:.0f}s"
                            + (f" (last: {last})" if last else ""))
        time.sleep(step)


def _member_view(network: dict, device_id: str) -> dict | None:
    return next((m for m in network.get("members") or [] if m.get("device_id") == device_id),
                None)


def pair_members(a: Member, b: Member) -> dict:
    """Pair B into A's network through the invite flow; return the network facts."""
    log.info("pair: worker A becomes a network of one (restart of an existing install)")
    a.restart()
    a_net = a.api("GET", "/network/status")
    if not a_net.get("enrolled"):
        raise PairError("worker A did not become a network of one after its restart")
    a_id = a_net["network"]["device_id"]
    a.api("PATCH", f"/network/members/{a_id}", {"name": NAMES["a"]})

    log.info("pair: worker A invites worker B (network:join at %s)", a.url)
    grant = a.api("POST", "/pairing/grants", {"purpose": "network:join", "endpoint_url": a.url,
                                               "device_label": NAMES["b"]})
    link = (grant.get("join") or {}).get("url")
    if not link:
        raise PairError(f"worker A's invite carried no join link: {sorted(grant)}")
    b.api("POST", "/network/enroll", {"link": link, "device_name": NAMES["b"]})

    def enrolled() -> dict | None:
        state = b.api("GET", "/network/enroll")
        job = state.get("enrollment") or {}
        if job.get("status") in ("rejected", "expired", "failed"):
            raise PairAbort(f"worker B's enrollment {job.get('status')}: {job.get('error')}")
        return state if state.get("enrolled") and job.get("status") == "complete" else None

    _wait("worker B's enrollment", enrolled)
    b_net = b.api("GET", "/network/status")
    b_id = b_net["network"]["device_id"]

    def both_online() -> bool:
        seen_by_a = _member_view(a.api("GET", "/network/status"), b_id) or {}
        seen_by_b = _member_view(b.api("GET", "/network/status"), a_id) or {}
        return bool(seen_by_a.get("online") and seen_by_b.get("online"))

    log.info("pair: waiting until each worker sees the other online")
    _wait("both workers seeing each other online", both_online)
    return {"network_id": b_net["network"]["network_id"], "a": a_id, "b": b_id}


def seed_b(a: Member, b: Member) -> dict:
    """Restart B so its ``pair-b`` seed runs, then wait for its chats to reach A."""
    log.info("pair: worker B restarts and seeds its own project and chats")
    b.restart()
    seed = _seed_result(b)
    if not seed.get("ok") or seed.get("deferred"):
        raise PairError(f"worker B's seed did not run: {seed}")
    a_seed = _seed_result(a)
    a_chat = a_seed["chats"]["long"]
    b_chat = seed["chats"]["b_loose"]

    def mirrored(member: Member, chat: str) -> bool:
        try:
            row = member.api("GET", f"/sessions/{chat}")
        except PairError:
            return False
        return bool(row and row.get("id") == chat)

    _wait("worker B's chats reaching worker A", lambda: mirrored(a, b_chat))
    _wait("worker A's chats reaching worker B", lambda: mirrored(b, a_chat))
    return seed


def _seed_result(member: Member) -> dict:
    path = Path(member.status().get("scratch_dir") or "/nonexistent") / "seed.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PairError(f"worker {member.role.upper()} has no seed result ({exc})") from None


# ─── up --pair ─────────────────────────────────────────────────────────────


def up_pair(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from app.core.temp_scratch import process_identity
    from app.preview_run import free_port
    from scripts.devloop.__main__ import _validate, default_pair_status_path

    if sys.platform != "linux":
        parser.error("The dev loop's owned PostgreSQL lease needs Linux")
    if args.pair_role or args.port or args.vite_port or args.protected_port:
        parser.error("--pair picks every port itself (no --port/--vite-port)")
    if args.seed != "default":
        parser.error("--pair seeds both workers itself (no --seed)")
    _validate(parser, args)
    if args.status is None:
        args.status = default_pair_status_path()
    try:
        ensure_private_dir(args.status.parent)
        existing = load_status(args.status)
    except StatusError as exc:
        parser.error(str(exc))
    if existing and status_alive(existing):
        parser.error(f"A dev pair for this checkout is already running (pid {existing['pid']}): "
                     "python -m scripts.devloop down --pair")
    started = time.time()
    data: dict[str, Any] = {
        "format": 1, "kind": "pair", "pid": os.getpid(),
        "pid_identity": process_identity(os.getpid()), "checkout": str(ROOT),
        "state": "starting", "mode": "ci" if args.ci else "dev", "started_at": _iso(started),
        "expires_at": _iso(started + args.ttl_seconds), "instances": {}, "network": None,
    }
    stopped = threading.Event()

    def write(**changes: Any) -> None:
        data.update(changes)
        write_private_json(args.status, data)

    def stop(signum: int, frame: Any) -> None:
        log.info("pair: stopping (signal %s)", signum)
        stopped.set()

    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    members: dict[str, Member] = {}
    owner, owner_identity = os.getppid(), process_identity(os.getppid())
    failure = None
    try:
        write()
        key = secrets.token_hex(32)
        ports = {"a": free_port(), "b": 0}
        ports["b"] = free_port(exclude=(ports["a"],))
        urls = {role: f"http://127.0.0.1:{ports[role]}" for role in ROLES}
        for role in ROLES:
            other = "b" if role == "a" else "a"
            child = args.status.with_name(f"{args.status.stem}.{role}.json")
            members[role] = Member(role, child, args, urls[other], ports[role], key)
            data["instances"][role] = {"name": NAMES[role], "status": str(child),
                                       "log": str(members[role].log_path),
                                       "peer_url": urls[role], "device_id": None}
        write()
        try:
            for role in ROLES:
                members[role].wait_state("ready", STEP_SECONDS * 2)
            network = pair_members(members["a"], members["b"])
            seed = seed_b(members["a"], members["b"])
        except (PairError, PairAbort, OSError) as exc:
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 — an HTTP error mid-pairing, say
            failure = f"{type(exc).__name__}: {exc}"
        if failure is None and not stopped.is_set():
            for role in ROLES:
                data["instances"][role]["device_id"] = network[role]
            write(state="ready", network={"network_id": network["network_id"]},
                  ready_seconds=round(time.time() - started, 1))
            log.info("pair ready in %.0fs: worker A %s, worker B %s; status %s",
                     time.time() - started, network["a"], network["b"], args.status)
            for role in ROLES:
                log.info("worker %s: open once: %s", role.upper(),
                         members[role].status().get("access_link"))
            if args.ci:
                print(PAIR_READY_LINE + " " + json.dumps({
                    "status": str(args.status), "seconds": data["ready_seconds"],
                    "a": network["a"], "b": network["b"], "b_seed": sorted(seed["chats"])}),
                    flush=True)
            while not stopped.is_set():
                gone = [role for role, member in members.items() if member.process.poll() is not None]
                if gone:
                    failure = f"worker {gone[0].upper()}'s launcher exited; stopping the pair"
                    break
                if process_identity(owner) != owner_identity:
                    log.info("pair: the parent process is gone; stopping")
                    break
                stopped.wait(0.5)
        if failure:
            log.error("pair: %s", failure)
            if args.ci and data["state"] != "ready":
                print(PAIR_FAILED_LINE + " " + json.dumps({"status": str(args.status),
                                                           "error": failure}), flush=True)
        return 1 if failure else 0
    finally:
        try:
            write(state="stopping")
        except (OSError, StatusError):
            pass
        for member in members.values():
            member.stop()
        for member in members.values():
            member.wait_stopped(CHILD_STOP_SECONDS)
        remove_own_status(args.status, os.getpid())
        for sig, handler in old.items():
            signal.signal(sig, handler)
        log.info("pair stopped; both instances are down")


# ─── status / down / pair offline|online ───────────────────────────────────


def _load_pair(path: Path) -> dict:
    status = load_status(path)
    if status is None or status.get("kind") != "pair":
        raise SystemExit(f"No dev pair recorded at {path} (start one: python -m scripts.devloop "
                         "up --pair)")
    return status


def show_pair(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + (args.wait or 0)
    while True:
        status = load_status(args.status)
        if status is not None and not status_alive(status):
            remove_own_status(args.status, status.get("pid"), status.get("pid_identity"))
            print(f"Stale dev pair status at {args.status}: launcher pid {status.get('pid')} is "
                  "gone; removed it")
            return 1
        if status is not None and (status.get("state") == "ready" or not args.wait):
            members = {}
            for role, info in (status.get("instances") or {}).items():
                try:
                    member = load_status(Path(info["status"])) or {}
                except (StatusError, KeyError):
                    member = {}
                members[role] = {"state": member.get("state"), "ui": (member.get("urls") or {}).get("ui"),
                                 "access_link": member.get("access_link")}
            print(json.dumps({**status, "members": members}, indent=2))
            return 0
        if time.monotonic() >= deadline:
            print(json.dumps(status, indent=2) if status else f"No dev pair at {args.status}")
            return 1
        time.sleep(0.5)


def down_pair(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    status = load_status(args.status)
    if status is None:
        print(f"No dev pair recorded at {args.status}")
        return 0
    if not status_alive(status):
        print(f"The recorded pair launcher (pid {status.get('pid')}) is gone; removing its stale "
              "status file")
        args.status.unlink(missing_ok=True)
        return 0
    try:
        os.kill(status["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + CHILD_STOP_SECONDS * 2
    while time.monotonic() < deadline:
        if not status_alive(status):
            print(f"Stopped the dev pair (pid {status['pid']})")
            return 0
        time.sleep(0.25)
    print(f"The dev pair (pid {status['pid']}) did not stop in time", file=sys.stderr)
    return 1


def switch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """``pair offline|online a|b``: stop one member's app (kept down) or start it again."""
    pair = _load_pair(args.status)
    if not status_alive(pair):
        raise SystemExit(f"The dev pair launcher (pid {pair.get('pid')}) is gone")
    info = (pair.get("instances") or {}).get(args.role) or {}
    member_path = Path(info.get("status") or "/nonexistent")
    member = load_status(member_path)
    if not member or not status_alive(member):
        raise SystemExit(f"Worker {args.role.upper()}'s launcher is not running")
    starts = int((member.get("app") or {}).get("starts") or 0)
    want = "offline" if args.action == "offline" else "ready"
    os.kill(member["pid"], OFFLINE_SIGNAL if args.action == "offline" else ONLINE_SIGNAL)
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        now = load_status(member_path) or {}
        now_starts = int((now.get("app") or {}).get("starts") or 0)
        if now.get("state") == want and (want == "offline" or now_starts > starts
                                         or member.get("state") == "ready"):
            print(f"worker {args.role.upper()} is {want}")
            return 0
        time.sleep(0.25)
    print(f"worker {args.role.upper()} did not become {want} within {args.wait:.0f}s",
          file=sys.stderr)
    return 1
