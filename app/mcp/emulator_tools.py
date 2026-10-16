"""Agent tools for this worker's Android emulator: ``Emulator`` and ``EmulatorScreenshot``.

One action tool and one image tool keep the surface (and its token cost)
small. Coordinates the agent sees and sends are the screenshot's pixels: the
screenshot, ``ui`` bounds and tap/swipe points all share that space, and the
handler converts to device pixels. The runtime is ``app.core.android_emulator``.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ACTIONS = ("status", "setup", "start", "stop", "install", "launch", "tap", "swipe",
           "text", "key", "ui", "logcat")
#: Actions that only read; the rest change the device or the worker.
READ_ACTIONS = frozenset({"status", "ui", "logcat"})

EMULATOR_DESCRIPTION = (
    "Run and test Android apps in this worker's emulator (device \"pixel\", shared by all chats). "
    "status first: why it is unavailable, and android_home/adb for builds. setup installs the SDK "
    "(~2 GB) in the background: only with accept_license=true after the user accepted the "
    "Android SDK License. start boots and waits until ready; stop. install an APK path; launch a "
    "package (activity optional). tap/swipe use EmulatorScreenshot pixels; text; key; ui lists "
    "on-screen elements with tap points (slow, ~3 s: use EmulatorScreenshot for a quick look); "
    "logcat (filter: Tag:Level or a package)."
)
SCREENSHOT_DESCRIPTION = (
    "Screenshot of the Android emulator's screen. Emulator tap/swipe take this image's pixels."
)

EMULATOR_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "device": {"type": "string"},
        "accept_license": {"type": "boolean"},
        "path": {"type": "string", "description": "APK on this worker; relative to the chat's folder"},
        "package": {"type": "string"},
        "activity": {"type": "string"},
        "x": {"type": "integer"}, "y": {"type": "integer"},
        "x2": {"type": "integer"}, "y2": {"type": "integer"},
        "duration_ms": {"type": "integer"},
        "text": {"type": "string"},
        "key": {"type": "string", "enum": ["HOME", "BACK", "APP_SWITCH", "ENTER", "DEL", "TAB",
                                           "ESCAPE", "MENU", "POWER", "VOLUME_UP", "VOLUME_DOWN"]},
        "filter": {"type": "string"},
        "lines": {"type": "integer"},
    },
    "required": ["action"],
    "additionalProperties": False,
}
SCREENSHOT_SCHEMA: Dict[str, Any] = {
    "type": "object", "properties": {"device": {"type": "string"}}, "additionalProperties": False,
}


def classify_risk(args: Dict[str, Any]) -> str:
    """Per-call risk for the permission engine: looking is safe."""
    return "safe" if (args or {}).get("action") in READ_ACTIONS else "write"


def _text(payload: Dict[str, Any], error: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if error:
        result["isError"] = True
    return result


def _resolve_path(raw: str, cwd: Optional[str]) -> Path:
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    return path


def _require_int(args: Dict[str, Any], *keys: str) -> List[int]:
    values = []
    for key in keys:
        value = args.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} is required (an integer)")
        values.append(int(value))
    return values


async def run_emulator(args: Dict[str, Any], *, cwd: Optional[str] = None) -> Dict[str, Any]:
    from app.core.android_emulator.manager import EmulatorError, get_manager

    manager = get_manager()
    action = args.get("action")
    device = str(args.get("device") or "pixel")
    try:
        if action == "status":
            status = await manager.status()
            status.pop("license_url", None)
            status["license"] = "https://developer.android.com/studio/terms"
            if status["devices"]:
                status["android_home"] = str(manager.layout.root)
                status["adb"] = str(manager.layout.adb)
            for dev in status["devices"]:
                live = manager.device(dev["id"])
                if live.serial and dev["state"] == "running":
                    dev["serial"] = live.serial
                    scale = live.agent_scale()
                    dev["screenshot_size"] = [round(live.width * scale), round(live.height * scale)]
                    if live.grpc_error:
                        dev["grpc_error"] = live.grpc_error
            return _text(status)
        if action == "setup":
            status = await manager.setup(args.get("accept_license") is True)
            return _text({"started": True, "reason": status["reason"], "install": status["install"],
                          "note": "The install runs in the background; check status later."})
        if action == "start":
            dev = await manager.start(device)
            await manager.wait_running(dev)
            return _text({"device": dev.to_dict(), "serial": dev.serial})
        if action == "stop":
            return _text({"device": (await manager.stop(device)).to_dict()})
        if action == "install":
            raw = args.get("path")
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("path is required: the APK file on this worker")
            path = _resolve_path(raw.strip(), cwd)
            if not path.is_file():
                raise ValueError(f"{path} is not a file")
            return _text({"package": await manager.install(device, path)})
        if action == "launch":
            package = args.get("package")
            if not isinstance(package, str) or not package:
                raise ValueError("package is required")
            component = await manager.launch(device, package, str(args.get("activity") or ""))
            return _text({"launched": component})
        if action in ("tap", "swipe"):
            dev = manager.running(device)
            scale = dev.agent_scale()

            def device_px(value: int) -> int:
                return int(round(value / scale))

            if action == "tap":
                x, y = _require_int(args, "x", "y")
                await manager.tap(device, device_px(x), device_px(y))
            else:
                x, y, x2, y2 = _require_int(args, "x", "y", "x2", "y2")
                await manager.swipe(device, device_px(x), device_px(y), device_px(x2), device_px(y2),
                                    int(args.get("duration_ms") or 300))
            return _text({"done": action})
        if action == "text":
            text = args.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError("text is required")
            await manager.type_text(device, text)
            return _text({"done": "text"})
        if action == "key":
            await manager.key(device, str(args.get("key") or ""))
            return _text({"done": "key"})
        if action == "ui":
            dev = manager.running(device)
            return {"content": [{"type": "text", "text": await manager.ui(device, dev.agent_scale())}]}
        if action == "logcat":
            output = await manager.logcat(device, str(args.get("filter") or ""), int(args.get("lines") or 200))
            return {"content": [{"type": "text", "text": output or "(no log lines)"}]}
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    except EmulatorError as exc:
        return _text({"success": False, "error": exc.message, "code": exc.code}, error=True)
    except ValueError as exc:
        return _text({"success": False, "error": str(exc)}, error=True)


async def run_screenshot(args: Dict[str, Any]) -> Dict[str, Any]:
    from app.core.android_emulator.manager import EmulatorError, get_manager

    try:
        data, width, height, dev = await get_manager().screenshot_jpeg(str(args.get("device") or "pixel"))
    except EmulatorError as exc:
        return _text({"success": False, "error": exc.message, "code": exc.code}, error=True)
    note = {"width": width, "height": height, "device_pixels": [dev.width, dev.height]}
    return {"content": [
        {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/jpeg"},
        {"type": "text", "text": json.dumps(note)},
    ]}


def emulator_tools_supported() -> bool:
    """Only workers whose platform has an emulator backend list the tools."""
    from app.core.android_emulator.host import check_host

    return check_host().reason != "unsupported_arch"


def build_emulator_tools(tool: Callable, *, cwd: Optional[str] = None) -> list:
    if not emulator_tools_supported():
        return []

    @tool("Emulator", EMULATOR_DESCRIPTION, EMULATOR_SCHEMA)
    async def emulator(args: dict) -> dict:
        return await run_emulator(args, cwd=cwd)

    @tool("EmulatorScreenshot", SCREENSHOT_DESCRIPTION, SCREENSHOT_SCHEMA)
    async def emulator_screenshot(args: dict) -> dict:
        return await run_screenshot(args)

    return [emulator, emulator_screenshot]
