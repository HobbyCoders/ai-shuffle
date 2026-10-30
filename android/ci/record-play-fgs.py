"""Temporary recording harness: real Android app + real disposable worker.

Only the voice provider and native pairing receipt are fixtures. The phone UI,
getUserMedia, audio WebSocket, bridge and microphone FGS are production code.
The HTTPS proxy authenticates only to the dev instance it owns; never owner data.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from scripts.devloop.ci import DevInstance

PACKAGE = "com.hobbycoders.aishuffle"
REMOTE = f"/sdcard/Android/media/{PACKAGE}/play-fgs"


def adb(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", "-s", "emulator-5554", *args], cwd=ROOT,
                          check=check, text=True, capture_output=True, timeout=60)


async def record(args, instance: DevInstance) -> None:
    status = json.loads(instance.status.read_text())
    base = status["urls"]["preview_listener"].rstrip("/")
    session_id = status["seed"]["chats"]["no_project"]
    phase = "setup"
    frames = defaultdict(lambda: {"frames": 0, "bytes": 0})
    timeline = []
    requests = []
    start = time.monotonic()
    async with ClientSession(timeout=ClientTimeout(total=None)) as client:
        async with client.get(status["access_link"], allow_redirects=False) as access:
            assert access.status in (302, 303, 307), access.status
            cookie = "; ".join(value.split(";", 1)[0] for value in access.headers.getall("Set-Cookie"))
            assert cookie
        headers = {"Cookie": cookie, "Origin": base, "Referer": base + "/mobile/"}

        async def proxy(request: web.Request) -> web.StreamResponse:
            nonlocal phase
            if request.path == "/demo/phase":
                phase = request.query["name"]
                timeline.append({"phase": phase, "seconds": round(time.monotonic() - start, 3)})
                return web.json_response({"ok": True})
            if request.path == "/demo/evidence":
                return web.json_response(dict(frames))
            # The native pairing token is a fixture. As with HTTP Authorization,
            # replace only that transport credential with our own preview cookie.
            # A dummy query token would otherwise (correctly) override cookie auth.
            path = request.rel_url.with_query([(name, value) for name, value in request.query.items() if name != "token"])
            target = base + str(path)
            if request.headers.get("Upgrade", "").lower() == "websocket":
                downstream = web.WebSocketResponse()
                await downstream.prepare(request)
                async with client.ws_connect(target, headers=headers) as upstream:
                    async def upload():
                        async for message in downstream:
                            if message.type == WSMsgType.BINARY:
                                if request.path.endswith("/audio"):
                                    frames[phase]["frames"] += 1
                                    frames[phase]["bytes"] += len(message.data)
                                await upstream.send_bytes(message.data)
                            elif message.type == WSMsgType.TEXT:
                                await upstream.send_str(message.data)
                        await upstream.close()
                    async def download():
                        async for message in upstream:
                            if message.type == WSMsgType.BINARY:
                                await downstream.send_bytes(message.data)
                            elif message.type == WSMsgType.TEXT:
                                await downstream.send_str(message.data)
                        await downstream.close()
                    tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                return downstream
            forwarded = dict(headers)
            for name in ("Content-Type", "Accept", "Last-Event-ID"):
                if name in request.headers:
                    forwarded[name] = request.headers[name]
            async with client.request(request.method, target, headers=forwarded,
                                      data=await request.read(), allow_redirects=False) as upstream:
                # The proxy never changes UI, API bodies, call states or audio.
                response_headers = {name: value for name, value in upstream.headers.items()
                                    if name.lower() not in {"content-length", "content-encoding", "transfer-encoding",
                                                            "connection", "set-cookie"}}
                requests.append({"method": request.method, "path": request.path, "status": upstream.status})
                response = web.StreamResponse(status=upstream.status, headers=response_headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", proxy)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.cert, args.key)
        await web.TCPSite(runner, "127.0.0.1", 8443, ssl_context=tls).start()
        try:
            command = ["adb", "-s", "emulator-5554", "shell", "am", "instrument", "-w", "-r",
                       "-e", "timeout_msec", "180000", "-e", "fixture_session", session_id,
                       "-e", "class", "com.hobbycoders.aishuffle.PlayFgsRecordingTest",
                       PACKAGE + ".test/androidx.test.runner.AndroidJUnitRunner"]
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.STDOUT)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=240)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
            result = args.out / "instrumentation.txt"
            result.write_bytes(output)
            print(output.decode(errors="replace"), flush=True)
            assert process.returncode == 0, process.returncode
            subprocess.run([sys.executable, str(ROOT / "android/ci/check-instrumentation.py"), str(result)],
                           check=True, timeout=30)
            # These counts are real microphone PCM uploaded by the unmodified call.
            assert frames["locked"]["bytes"] > 0, "No microphone PCM arrived with the screen off"
            assert frames["unlocked"]["bytes"] > 0, "No microphone PCM arrived after unlocking"
        finally:
            (args.out / "microphone-evidence.json").write_text(json.dumps({
                "source": "Real WebView getUserMedia microphone PCM; emulator input may be silent",
                "backend": "Actual dev-loop worker with repository fake voice provider (not a real AI call)",
                "uploaded_pcm": dict(frames), "timeline": timeline, "http_requests": requests,
            }, indent=2))
            await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "ci/prepare-android-ui.py"], cwd=ROOT / "android", check=True, timeout=180)
    adb("install", "-r", str(ROOT / "android/app/build/outputs/apk/debug/app-debug.apk"))
    adb("install", "-r", str(ROOT / "android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk"))
    adb("reverse", "tcp:8443", "tcp:8443")
    adb("shell", "wm", "size", "720x1280")
    adb("shell", "wm", "density", "320")
    adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
    adb("shell", "settings", "put", "system", "user_rotation", "0")
    adb("shell", "settings", "put", "system", "show_touches", "1")
    adb("shell", "locksettings", "set-disabled", "false")
    with tempfile.TemporaryDirectory(prefix="play-fgs-worker-") as temp:
        instance = DevInstance(Path(temp), ttl=900)
        try:
            failure = instance.wait_ready(420)
            if failure:
                # Diagnose startup without copying status, cookies or connection strings.
                import re
                tail = instance.log_tail()
                tail = re.sub(r"postgres(?:ql)?://[^\s]+", "postgresql://<redacted>", tail)
                tail = re.sub(r"v1\.[A-Za-z0-9_.-]+", "<preview-token-redacted>", tail)
                print(tail, flush=True)
                raise RuntimeError(failure)
            asyncio.run(record(args, instance))
        finally:
            adb("shell", "pkill", "-2", "screenrecord", check=False)
            pulled = adb("pull", REMOTE, str(args.out), check=False)
            print(pulled.stdout, pulled.stderr, flush=True)
            log = adb("logcat", "-d", "-v", "brief", "PlayFgsDemo:I", "VoiceCallService:I", "chromium:I", "*:S", check=False)
            (args.out / "android-verification.txt").write_text(log.stdout)
            instance.down()


if __name__ == "__main__":
    main()
