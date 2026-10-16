"""Where the emulator's SDK, devices and logs live on this worker.

Everything sits under one root (``<data dir>/android-sdk`` unless
``ANDROID_SDK_DIR`` says otherwise) in sdkmanager's own layout, so a build
pointed at it with ``ANDROID_HOME`` finds the same packages.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RECORD_NAME = "ai-shuffle-packages.json"


def sdk_root() -> Path:
    from app.core.config import settings

    configured = getattr(settings, "android_sdk_dir", None)
    if configured:
        return Path(configured)
    return Path(settings.data_dir) / "android-sdk"


class SdkLayout:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else sdk_root()

    # Packages, in sdkmanager's layout.
    def package_dir(self, path: str) -> Path:
        return self.root.joinpath(*path.split(";"))

    @property
    def emulator_bin(self) -> Path:
        return self.root / "emulator" / "emulator"

    @property
    def adb(self) -> Path:
        return self.root / "platform-tools" / "adb"

    # State the app owns.
    @property
    def avd_home(self) -> Path:
        return self.root / "avd"

    @property
    def user_home(self) -> Path:
        """ANDROID_USER_HOME: adb keys and the console token, never ~/.android."""
        return self.root / "user"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def downloads(self) -> Path:
        return self.root / ".downloads"

    @property
    def staging(self) -> Path:
        return self.root / ".staging"

    @property
    def licenses(self) -> Path:
        return self.root / "licenses"

    @property
    def record_path(self) -> Path:
        return self.root / RECORD_NAME

    def read_record(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.record_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("Android SDK install record is unreadable; treating the SDK as missing")
            return {}
        return data if isinstance(data, dict) else {}

    def write_record(self, record: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.record_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.record_path)

    def installed(self, system_image: str) -> bool:
        """The emulator, platform-tools and ``system_image`` are all in place."""
        packages = self.read_record().get("packages") or {}
        if not all(name in packages for name in ("emulator", "platform-tools", system_image)):
            return False
        image = self.package_dir(system_image)
        return (self.emulator_bin.is_file() and self.adb.is_file()
                and (image / "system.img").is_file())

    @property
    def console_token_path(self) -> Path:
        return self.user_home / ".emulator_console_auth_token"

    def console_token(self) -> str:
        """The emulator console and gRPC token, created (0600) on first use."""
        import secrets

        path = self.console_token_path
        try:
            token = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = ""
        if not token:
            path.parent.mkdir(parents=True, exist_ok=True)
            token = secrets.token_urlsafe(18)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
        return token

    @property
    def runtime_dir(self) -> Path:
        """XDG_RUNTIME_DIR for the emulator: its discovery files land here."""
        return self.user_home / "run"

    def discovery_dirs(self) -> List[Path]:
        """Where a running emulator may write ``pid_<pid>.ini`` (by version)."""
        return [self.runtime_dir / "avd" / "running", self.user_home / "avd" / "running",
                self.user_home / ".android" / "avd" / "running"]

    def discovery(self, pid: int) -> Dict[str, str]:
        """A running emulator's discovery file (``grpc.port``, ``grpc.token``...), or {}."""
        for folder in self.discovery_dirs():
            try:
                text = (folder / f"pid_{pid}.ini").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            values: Dict[str, str] = {}
            for line in text.splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip()
            return values
        return {}

    def env(self) -> Dict[str, str]:
        """The environment every emulator and adb process runs with.

        Only what they need: the app's environment carries provider keys,
        which an emulator has no use for. The server-run marker stays so a
        crashed run's emulator is stopped at the next boot (process_ledger).
        """
        from app.core.process_ledger import RUN_MARKER

        keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR",
                "XDG_RUNTIME_DIR", RUN_MARKER)
        env = {key: os.environ[key] for key in keep if key in os.environ}
        env.update({
            # The emulator keeps its console/gRPC token in ~ and adb reads it
            # from there for `adb emu`: both look in the app's own folder.
            "HOME": str(self.user_home),
            "ANDROID_HOME": str(self.root),
            "ANDROID_SDK_ROOT": str(self.root),
            "ANDROID_AVD_HOME": str(self.avd_home),
            "ANDROID_USER_HOME": str(self.user_home),
            "ANDROID_EMULATOR_HOME": str(self.user_home),
            # The emulator's discovery file (with its gRPC token) goes here,
            # not into the app user's own runtime folder.
            "XDG_RUNTIME_DIR": str(self.runtime_dir),
        })
        return env
