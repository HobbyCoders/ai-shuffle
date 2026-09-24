"""Verified first-party connector plugins.

Only the code snapshots in catalog.json can be installed. They run with the
same OS permissions as the host, in a separate process and virtual environment.
This is process/dependency isolation, not an OS security sandbox.
Existing unowned connections retain their bundled implementation until an
administrator installs the corresponding package. IDs and configs are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.core.plugin_models import _get_fluid_dir, AvailablePlugin, PluginInfo, PluginDetails

MARKETPLACE = "shuffle-connectors"
_SOURCE_DIR = Path(__file__).parent / "connectors"
_MAX_FILE_BYTES = 2_000_000
CATALOG = json.loads((_SOURCE_DIR / "catalog.json").read_text())
_DISPLAY_NAMES = {"discord": "Discord Bot"}
_LOCK = threading.RLock()


def root() -> Path:
    return _get_fluid_dir() / "plugins" / "connector-runtimes"


def package_id(kind: str) -> str:
    return f"{kind}@{MARKETPLACE}"


def kind_for(plugin_id: str) -> str | None:
    kind, sep, marketplace = plugin_id.rpartition("@")
    if sep and marketplace == MARKETPLACE:
        if kind not in CATALOG["packages"]:
            raise ValueError("Unknown connector plugin")
        return kind
    return None


def record(kind: str) -> dict | None:
    if kind not in CATALOG["packages"]:
        raise ValueError("Unknown connector plugin")
    path = root() / kind / "installed.json"
    if not path.exists():
        return None
    # Corrupt state must not silently reactivate a disabled or missing package.
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def lifecycle_lock():
    from app.core.config import _lock_instance_id_file
    with _LOCK:
        root().mkdir(parents=True, exist_ok=True)
        with _lock_instance_id_file(root() / "lifecycle.lock"):
            yield


def _publish(kind: str, state: dict) -> None:
    target = root() / kind
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target, delete=False) as f:
        json.dump(state, f)
        scratch = Path(f.name)
    try:
        scratch.replace(target / "installed.json")
    finally:
        scratch.unlink(missing_ok=True)


def dependencies(kind: str) -> list[dict]:
    from app.db import database
    from app.core.connection_dependencies import connection_dependencies
    return [{"id": c["id"], "name": c["name"], "active": c["is_active"],
             "automations": connection_dependencies(c["id"])}
            for c in database.get_all_connectors() if c["type"] == kind]


def _require_unused(kind: str) -> None:
    # Include inactive accounts and paused automations. A package must not be
    # removed out from under a saved account, even when it is not running.
    refs = dependencies(kind)
    if refs:
        names = ", ".join(c["name"] for c in refs)
        raise ValueError(f"Plugin is used by connections: {names}. Remove those connections first; their automation references must be resolved before deletion.")


def _verify(path: Path, kind: str, spec: dict | None = None) -> None:
    for filename, digest in (spec or CATALOG["packages"][kind])["files"].items():
        if Path(filename).name != filename:
            raise ValueError("Invalid connector package filename")
        source = path / filename
        if hashlib.sha256(_read_package_file(source)).hexdigest() != digest:
            raise ValueError("Connector package integrity check failed. Stop its connections and restore or reinstall this version before restarting.")


def _read_package_file(source: Path) -> bytes:
    """Bound reads of both shipped sources and installed snapshots."""
    if source.is_symlink() or not source.is_file():
        raise ValueError("Connector package integrity check failed: missing or linked source file. Repair this AI Shuffle installation, then retry.")
    with source.open("rb") as stream:
        data = stream.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("Connector package file exceeds size limit")
    return data


def _bundled_source(filename: str) -> bytes:
    """Use the snapshot already carried by the buyer's app/update artifact.

    The catalog ships with the app, so discovering a newer connector version
    already requires an app update. Never ask buyers for private-repository
    access or fall back to a different source when the bundle is damaged.
    """
    if Path(filename).name != filename or "\\" in filename or filename in ("", ".", ".."):
        raise ValueError("Invalid connector package filename")
    return _read_package_file(_SOURCE_DIR / filename)


def install(kind: str, scope: str = "user") -> PluginInfo:
    if kind not in CATALOG["packages"] or scope != "user":
        raise ValueError("Connector plugins are installed for this host in user scope")
    root().mkdir(parents=True, exist_ok=True)
    with lifecycle_lock():
        existing = record(kind)
        if existing and existing.get("release") == CATALOG["release"]:
            _verify(root() / kind / existing["release"], kind, existing["spec"])
            return info(kind)
        if existing:
            _require_stopped(kind)
    # Stage verified bundled sources before taking the publication lock. The
    # previous runtime survives missing/corrupt sources or dependency failures.
    with tempfile.TemporaryDirectory(prefix="stage-", dir=root()) as scratch:
        staged = Path(scratch)
        for filename in CATALOG["packages"][kind]["files"]:
            data = _bundled_source(filename)
            (staged / filename).write_bytes(data)
        _verify(staged, kind)
        try:
            _prepare_environment(staged, kind)
            _validate_worker(staged, CATALOG["packages"][kind])
        except Exception as exc:
            raise ValueError("The isolated connector environment could not be installed or validated. Check this host's Python support and access to PyPI, then retry. The previous version is unchanged.") from exc
        with lifecycle_lock():
            old = record(kind)
            if old and old.get("release") != CATALOG["release"]:
                _require_stopped(kind)
            target = root() / kind / CATALOG["release"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                _verify(target, kind)
            else:
                shutil.copytree(staged, target)
            # Validate the relocated environment before publishing it.
            _validate_worker(target, CATALOG["packages"][kind])
            previous = ({k: v for k, v in old.items() if k != "previous"}
                        if old and old.get("release") != CATALOG["release"] else (old or {}).get("previous"))
            _publish(kind, {"revision": CATALOG["revision"], "release": CATALOG["release"], "sdk": 1,
                            "spec": CATALOG["packages"][kind], "previous": previous,
                            "enabled": old.get("enabled", True) if old else True,
                            "installed_at": (old or {}).get("installed_at", datetime.now(timezone.utc).isoformat())})
    return info(kind)


def set_enabled(kind: str, enabled: bool) -> bool:
    with lifecycle_lock():
        state = record(kind)
        if not state:
            raise ValueError("Connector plugin is not installed")
        if not enabled:
            _require_unused(kind)
        else:
            _verify(root() / kind / state["release"], kind, state["spec"])
        _publish(kind, {**state, "enabled": enabled})
    return True


def uninstall(kind: str) -> bool:
    with lifecycle_lock():
        _require_unused(kind)
        if not record(kind):
            raise ValueError("Connector plugin is not installed")
        # Keep verified version snapshots for rollback; remove only the active
        # record. Never recurse through an arbitrary plugin installPath/junction.
        (root() / kind / "installed.json").unlink()
    return True


def require_installed(kind: str) -> str:
    state = record(kind)
    if not state or not state.get("enabled"):
        raise ValueError("Install and enable this app in Plugins before connecting an account")
    _verify(root() / kind / state["release"], kind, state["spec"])
    return package_id(kind)


def info(kind: str) -> PluginInfo:
    state = record(kind)
    return PluginInfo(id=package_id(kind), name=_DISPLAY_NAMES.get(kind, kind), marketplace=MARKETPLACE,
                      description=f"Official {kind} connector runtime", version=(state or {}).get("release", CATALOG["release"]),
                      scope="user", install_path=str(root() / kind), enabled=bool(state and state.get("enabled")),
                      installed_at=datetime.fromisoformat(state["installed_at"]) if state else None,
                      connector_type=kind)


def installed() -> list[PluginInfo]:
    return [info(kind) for kind in CATALOG["packages"] if record(kind)]


def available() -> list[AvailablePlugin]:
    return [AvailablePlugin(id=package_id(kind), name=_DISPLAY_NAMES.get(kind, kind), marketplace=MARKETPLACE,
                            marketplace_path="", description=f"Official {kind} connector runtime",
                            installed=bool(record(kind)), enabled=info(kind).enabled, connector_type=kind)
            for kind in CATALOG["packages"]]


def details(kind: str) -> PluginDetails:
    return PluginDetails(**asdict(info(kind)), readme="First-party connector code shipped with this AI Shuffle version and SHA-256 verified against its pinned catalog. No GitHub account or repository access is required. Updating AI Shuffle provides newer connector releases; Python dependencies are installed from PyPI. Runs in a separate process and dependency environment, with the host's OS permissions. Existing account IDs and automation bindings are preserved. Remove dependent connections before disabling or uninstalling. To update or roll back, pause automations and disable connections; their IDs and settings are retained.")


def _prepare_environment(path: Path, kind: str) -> None:
    import subprocess
    import venv
    from app.core.connectors.package_proxy import interpreter, worker_environment
    venv.EnvBuilder(with_pip=True).create(path / "env")
    requirements = CATALOG["dependencies"]["default"] + CATALOG["dependencies"].get(kind, [])
    subprocess.run([str(interpreter(path)), "-I", "-m", "pip", "--isolated", "install",
                    "--disable-pip-version-check", "--no-input", "--no-cache-dir", "--only-binary=:all:",
                    "--index-url", "https://pypi.org/simple", *requirements],
                   check=True, timeout=240, capture_output=True, env=worker_environment())


def _validate_worker(path: Path, spec: dict) -> None:
    import subprocess
    from app.core.connectors.package_proxy import interpreter, worker_environment
    result = subprocess.run([str(interpreter(path)), "-I", str(Path(__file__).parent / "connectors" / "package_worker.py"),
                             str(path), spec["module"], spec["class"], "validate"],
                            check=True, capture_output=True, timeout=30, env=worker_environment())
    if json.loads(result.stdout).get("sdk") != 1:
        raise ValueError("Incompatible connector SDK")


def _require_stopped(kind: str) -> None:
    refs = dependencies(kind)
    busy = [c for c in refs if c["active"] or any(a["is_enabled"] for a in c["automations"])]
    if busy:
        raise ValueError("Pause dependent automations and disable these connections before changing runtime versions: " + ", ".join(c["name"] for c in busy))


def rollback(kind: str) -> PluginInfo:
    with lifecycle_lock():
        state = record(kind)
        previous = (state or {}).get("previous")
        if not previous:
            raise ValueError("No previous connector version is available")
        _require_stopped(kind)
        path = root() / kind / previous["release"]
        _verify(path, kind, previous["spec"])
        _validate_worker(path, previous["spec"])
        _publish(kind, {**previous, "enabled": state["enabled"], "previous": {k: v for k, v in state.items() if k != "previous"}})
    return info(kind)


def runtime_class(kind: str):
    if kind not in CATALOG["packages"]:
        return None
    state = record(kind)
    if not state:
        return None  # compatibility for existing, unowned accounts only
    if not state.get("enabled"):
        raise ValueError("Connector plugin is disabled")
    if state.get("sdk") != 1:
        raise ValueError("Connector plugin needs an update for this host SDK")
    from app.core.connectors.package_proxy import proxy_class
    path = root() / kind / state["release"]
    _verify(path, kind, state["spec"])
    return proxy_class(kind, path, state["spec"])
