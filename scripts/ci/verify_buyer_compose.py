#!/usr/bin/env python3
"""Install AI Shuffle from docs/install/compose exactly as a buyer would.

Copies compose.yaml and env.example into an empty folder, fills in only the
values the buyer fills in, starts it with `docker compose up`, and checks what
a buyer depends on: the container becomes healthy, the UI answers on the
loopback address only, and the shared memory, stop timeout and persistent
volumes (including the browser profile) are what the file promises. Then it
drives the first minutes of a buyer's session through the HTTP API, from the
host like a browser would (Docker hands the app the connection from its own
network address, so the host is not loopback to the app):

- ``/health`` answers without signing in;
- with no owner password, a signed-out request gets no data and the owner
  status says the password step is required;
- the owner password can be created through the API, a wrong one is refused
  and the right one signs in;
- the license status reports the beta lease;
- ``/api/v1/auth/claude/cli`` reports no CLI login on a fresh container;
- an agent turn without any credential is refused with a setup message.

After a forced recreation the owner password still signs in. Then it removes
everything it created.

--image names an image already present locally (built by CI, or pulled from
the private registry on a clean runner). It is re-tagged under the buyer's
registry path so the compose file itself is used unmodified, and the run never
pulls. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
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


class Api:
    """The running container's HTTP API, reached from the host like a browser.

    Every request carries the page's own Origin (owner routes refuse
    cross-origin writes) and never goes through a proxy.
    """

    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def open(self, method: str, path: str, body=None, bearer: str | None = None, timeout: float = 30):
        headers = {"Origin": self.base, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error

    def call(self, method: str, path: str, body=None, bearer: str | None = None) -> tuple[int, object]:
        with self.open(method, path, body, bearer) as response:
            text = response.read().decode(errors="replace")
            try:
                return response.status, json.loads(text)
            except ValueError:
                return response.status, text


def detail(payload) -> str:
    if isinstance(payload, dict):
        value = payload.get("detail", payload.get("message", payload))
        return value if isinstance(value, str) else json.dumps(value)
    return str(payload)[:300]


# What a refused agent turn's setup message talks about. Today the turn routes
# answer "Claude CLI not authenticated. Please run 'claude login' in the
# container." The shared credential gate (app/core/credential_gate.py, being
# added) may word it differently, answer with another 4xx, or refuse inside the
# event stream instead; any of these counts, as long as no turn runs.
SETUP_MESSAGE = re.compile(r"sign(ed)?[ -]?in|log ?in|credential|api key|set ?up|settings|not authenticated", re.I)
# Means the check's own API key was not accepted, not that the gate refused.
API_KEY_REJECTED = "API key required"


def stream_refusal(response, deadline: float) -> tuple[str | None, list[str]]:
    """Read an SSE agent turn until it ends; return the refusal message, if any."""
    types = []
    for raw in response:
        line = raw.decode(errors="replace").strip()
        if line.startswith("data:"):
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            kind = str(event.get("type", ""))
            types.append(kind)
            message = str(event.get("message") or event.get("error") or event.get("detail") or "")
            if ("error" in kind or kind == "done") and SETUP_MESSAGE.search(message):
                return message, types
            if kind == "done":
                break
        if time.monotonic() > deadline:
            break
    return None, types


def check_first_session(api: Api) -> tuple[list[str], str]:
    """What a buyer's first minutes do through the API: report lines and the
    owner password it created."""
    report = []

    status, health = api.call("GET", "/health")
    assert status == 200 and health.get("status") == "healthy", f"/health answered {status}: {health}"
    assert health.get("claude_authenticated") is False, f"/health claims a CLI login: {health}"
    report.append(f"/health healthy, version {health.get('version')}")

    # S1: no owner password yet, and the host is not loopback to the app.
    status, owner = api.call("GET", "/api/v1/owner/status")
    assert status == 200, f"owner status answered {status}: {owner}"
    assert owner["password_set"] is False, f"a fresh install already has an owner password: {owner}"
    assert owner["required"] is True and owner["trusted_lan"] is False, f"owner password not required: {owner}"
    assert owner["setup_allowed"] is True, f"a local-network browser may not create the password: {owner}"
    for path in ("/api/v1/license", "/api/v1/auth/claude/cli", "/api/v1/api-users"):
        status, body = api.call("GET", path)
        assert status in (401, 403), (
            f"GET {path} without an owner password answered {status}, not 401/403: {detail(body)}. "
            "If this runs where Docker keeps the loopback peer (host networking), the S1 check does not apply.")
    status, auth = api.call("GET", "/api/v1/auth/status")
    assert status == 200 and not auth.get("authenticated") and not auth.get("local_access"), \
        f"a signed-out request is treated as the owner: {auth}"
    report.append("signed-out requests refused while no owner password exists")

    password = "buyer-check-" + secrets.token_urlsafe(18)
    status, body = api.call("POST", "/api/v1/owner/password", {"password": "too-short"})
    assert status == 400, f"a short owner password answered {status}: {detail(body)}"
    status, body = api.call("POST", "/api/v1/owner/password", {"password": password})
    assert status == 200 and body.get("status") == "ok", f"setting the owner password answered {status}: {detail(body)}"
    status, owner = api.call("GET", "/api/v1/owner/status")
    assert owner["password_set"] is True and owner["setup_allowed"] is False, f"owner status after setup: {owner}"
    status, body = api.call("POST", "/api/v1/owner/password", {"password": "someone-else-" + secrets.token_urlsafe(9)})
    assert status == 403, f"a second password creation without signing in answered {status}: {detail(body)}"
    status, body = api.call("POST", "/api/v1/owner/login", {"password": password + "x", "bearer": True})
    assert status == 401, f"a wrong owner password answered {status}: {detail(body)}"
    token = owner_login(api, password)
    report.append("owner password created through the API; wrong password refused; sign-in works")

    status, license_status = api.call("GET", "/api/v1/license", bearer=token)
    assert status == 200, f"license status answered {status}: {detail(license_status)}"
    assert license_status.get("mode") == "beta" and license_status.get("state") == "beta" \
        and license_status.get("allowed") is True, f"license is not the beta lease: {license_status}"
    report.append("license status: beta, agent runs allowed")

    status, cli = api.call("GET", "/api/v1/auth/claude/cli?refresh=true", bearer=token)
    assert status == 200, f"CLI status answered {status}: {detail(cli)}"
    assert cli.get("signed_in") is not True, f"a fresh container reports a CLI login: {cli}"
    report.append(f"CLI status: installed={cli.get('installed')}, version {cli.get('cli_version')}, "
                  f"signed_in={cli.get('signed_in')}")

    report.append(check_turn_refused(api, token))
    return report, password


def owner_login(api: Api, password: str) -> str:
    status, body = api.call("POST", "/api/v1/owner/login", {"password": password, "bearer": True})
    assert status == 200 and body.get("token"), f"owner sign-in answered {status}: {detail(body)}"
    return body["token"]


def check_turn_refused(api: Api, token: str) -> str:
    """An agent turn with no Claude login, API key or other provider must not run."""
    status, profiles = api.call("GET", "/api/v1/agents", bearer=token)
    profile = profiles[0]["id"] if status == 200 and isinstance(profiles, list) and profiles else "default"
    status, user = api.call("POST", "/api/v1/api-users", {"name": "buyer-install-check"}, bearer=token)
    assert status == 201 and user.get("api_key"), f"creating an API user answered {status}: {detail(user)}"
    turn = {"prompt": "Reply with the single word ready.", "profile": profile}
    deadline = time.monotonic() + 60
    with api.open("POST", "/api/v1/conversation/stream", turn, bearer=user["api_key"], timeout=60) as response:
        if response.status >= 400:
            text = response.read().decode(errors="replace")
            try:
                message = detail(json.loads(text))
            except ValueError:
                message = text[:300]
            assert API_KEY_REJECTED not in message, f"the check's API key was refused: {message}"
            assert SETUP_MESSAGE.search(message), (
                f"agent turn refused with {response.status}, but not with a setup message: {message}")
            return f"agent turn without a credential refused ({response.status}): {message}"
        message, types = stream_refusal(response, deadline)
        assert message, f"an agent turn started without any credential; events: {types[:20]}"
        return f"agent turn without a credential refused in the stream: {message}"


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
        api = Api(port)
        with api.open("GET", "/health/ready", timeout=10) as response:
            assert response.status == 200, f"/health/ready answered {response.status}"
        report, password = check_first_session(api)
        # A replaced container keeps its data: recreate, come back healthy, and
        # the owner password still signs in.
        run(*compose, "up", "-d", "--pull", "never", "--force-recreate", cwd=folder)
        wait_healthy(folder, timeout)
        owner_login(api, password)
        print(f"buyer compose install: healthy on 127.0.0.1:{port}, 1g shm, 90 s stop, "
              f"{len(VOLUMES)} named volumes and a workspace folder, survives recreation")
        for line in report + ["owner password still signs in after recreation"]:
            print(f"  - {line}")
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
