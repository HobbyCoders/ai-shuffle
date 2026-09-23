#!/usr/bin/env python3
"""Install AI Shuffle from docs/install/compose exactly as a buyer would.

Copies compose.yaml and env.example into an empty folder, fills in only the
values the buyer fills in, starts it with `docker compose up`, and checks what
a buyer depends on: the container becomes healthy, the UI answers on the
loopback address only, and the shared memory, stop timeout and persistent
volumes (including the browser profile) are what the file promises. Then it
removes everything it created.

--image names an image already present locally (built by CI, or pulled from
the private registry on a clean runner). It is re-tagged under the buyer's
registry path so the compose file itself is used unmodified, and the run never
pulls. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "docs" / "install" / "compose"
REGISTRY, TOKEN, VERSION = "ci.invalid", "ci-pull-token", "ci-candidate"
PROJECT = "ai-shuffle-buyer-check"
VOLUMES = {"/data", "/home/appuser/.fluid", "/home/appuser/.claude", "/home/appuser/.local",
           "/home/appuser/.config/gh", "/home/appuser/chrome-profile"}


def run(*args, cwd=None, check=True, timeout=900):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args[:3])} failed:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}")
    return result.stdout


def buyer_env(folder: Path, port: int) -> None:
    """env.example with only the lines a buyer edits changed."""
    values = {"AI_SHUFFLE_PULL_TOKEN": TOKEN, "AI_SHUFFLE_VERSION": VERSION, "PORT": str(port)}
    lines, seen = [], set()
    for line in (INSTALL / "env.example").read_text().splitlines():
        key = line.split("=", 1)[0]
        if not line.startswith("#") and key in values:
            line = f"{key}={values[key]}"
            seen.add(key)
        lines.append(line)
    missing = set(values) - seen
    if missing:
        raise RuntimeError(f"env.example no longer offers {', '.join(sorted(missing))}")
    # Not a buyer setting: points the unmodified compose file at the local image.
    lines.append(f"AI_SHUFFLE_REGISTRY={REGISTRY}")
    (folder / ".env").write_text("\n".join(lines) + "\n")


def wait_healthy(folder: Path, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        container = json.loads(run("docker", "inspect", "ai-shuffle"))[0]
        health = container["State"].get("Health", {}).get("Status")
        if health == "healthy":
            return container
        if not container["State"]["Running"] or time.monotonic() > deadline:
            logs = run("docker", "compose", "logs", "--no-color", "--tail", "200", cwd=folder, check=False)
            raise RuntimeError(f"container did not become healthy (state {health}):\n{logs}")
        time.sleep(5)


def check(image: str, port: int, timeout: int, artifacts: Path | None) -> None:
    folder = Path(tempfile.mkdtemp(prefix="ai-shuffle-buyer-"))
    shutil.copy(INSTALL / "compose.yaml", folder / "compose.yaml")
    buyer_env(folder, port)
    compose = ["docker", "compose", "-p", PROJECT]
    run("docker", "tag", image, f"{REGISTRY}/{TOKEN}/ai-shuffle:{VERSION}")
    try:
        config = json.loads(run(*compose, "config", "--format", "json", cwd=folder))
        service = config["services"]["ai-shuffle"]
        assert service["image"] == f"{REGISTRY}/{TOKEN}/ai-shuffle:{VERSION}", service["image"]
        run(*compose, "up", "-d", "--pull", "never", cwd=folder)
        container = wait_healthy(folder, timeout)
        host = container["HostConfig"]
        assert host["ShmSize"] == 1024**3, f"shm_size is {host['ShmSize']}"
        assert container["Config"].get("StopTimeout") == 90, f"stop timeout is {container['Config'].get('StopTimeout')}"
        bindings = host["PortBindings"]["8000/tcp"]
        assert bindings == [{"HostIp": "127.0.0.1", "HostPort": str(port)}], bindings
        mounts = {mount["Destination"]: mount for mount in container["Mounts"]}
        for target in VOLUMES:
            assert mounts.get(target, {}).get("Type") == "volume", f"{target} is not a named volume"
        assert mounts["/workspace"]["Type"] == "bind" and (folder / "workspace").is_dir()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=10) as response:
            assert response.status == 200, response.status
        # A replaced container keeps its data: recreate and come back healthy.
        run(*compose, "up", "-d", "--pull", "never", "--force-recreate", cwd=folder)
        wait_healthy(folder, timeout)
        print(f"buyer compose install: healthy on 127.0.0.1:{port}, 1g shm, 90 s stop, "
              f"{len(VOLUMES)} named volumes and a workspace folder, survives recreation")
    finally:
        if artifacts:
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "buyer-compose.log").write_text(
                run(*compose, "logs", "--no-color", cwd=folder, check=False))
        # This project owns every volume it names; a buyer never runs -v.
        run(*compose, "down", "-v", "--timeout", "120", cwd=folder, check=False)
        run("docker", "rmi", f"{REGISTRY}/{TOKEN}/ai-shuffle:{VERSION}", check=False)
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait for a healthy container")
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    try:
        check(args.image, args.port, args.timeout, args.artifacts)
    except (AssertionError, RuntimeError, OSError) as exc:
        print(f"buyer compose install failed: {exc}", file=sys.stderr)
        sys.exit(1)
