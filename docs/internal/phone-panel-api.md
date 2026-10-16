# Phone panel API (Android emulator)

The Phone panel shows and drives the Android emulator that AI Shuffle runs on
a worker. The backend lives in `app/core/android_emulator/` (runtime) and
`app/api/emulator.py` (routes). The agent tools are in `app/mcp/emulator_tools.py`.
The user-facing page is `docs/content/features/android-emulator.md`.

The emulator belongs to the worker, not to a session. There is one device,
`pixel`, and every chat and every panel share it (`EMULATOR_MAX_DEVICES`,
default 1). The panel talks to the worker it is connected to. Forwarding to
an emulator on another worker is not built yet.

## Auth

Only the owner can use these routes: the owner password session, a paired
device whose account owner is the install administrator, the local listener,
or the agent's preview cookie (the set that `require_auth` marks `is_admin`).
API keys and non-owner paired devices get 403 on HTTP, and the WebSocket is
closed with 4001. On the WebSocket, send the credential the same way as on
the other panel sockets (`?token=` or the `session` cookie).

## Errors

Refusals return a JSON error body, shown to the owner as is:
`{"detail": str, "code": str}`. The status codes are 400 (bad input), 404
(unknown device), 409 (not available, not running, device limit) and 500.

## `GET /api/v1/emulator/status`

```
{"available": bool,
 "reason": "ready"|"no_kvm"|"kvm_permission"|"unsupported_arch"|"sdk_missing"|"installing"|"error",
 "message": str,
 "install": {"state": "idle"|"downloading"|"extracting"|"done"|"error", "progress": 0..1, "message": str} | null,
 "license_url": str,
 "devices": [{"id": str, "name": str, "state": "stopped"|"booting"|"running"|"error",
              "api": int, "width": int, "height": int,
              "frame_source": ""|"grpc"|"adb"}]}
```

- When more than one reason applies, `reason` is the first match in this order:
  `unsupported_arch`, `installing`, `no_kvm` / `kvm_permission`, `error` (the last
  install failed), `sdk_missing`, `ready`.
- `install` is `null` until this process starts an install. After that it holds
  the last install's state until the app restarts.
- `devices` is empty until the SDK is installed. After that it always lists
  `pixel`, which is `stopped` until it is started for the first time.
- `width` and `height` are in device pixels and follow the current
  orientation (1080×2400 portrait, 2400×1080 after a rotate).
- `frame_source` is `""` unless the device is `running`. `grpc` is the normal
  fast path (10+ fps, sub-millisecond input). `adb` means the emulator's gRPC
  control did not answer: frames drop to about 2 fps and input is slower. The
  panel may show a quiet "slow mode" hint; nothing else changes.
- When a device is `error`, the reason is in the device's stream `state`
  message (see below).

## `POST /api/v1/emulator/setup`

Body: `{"accept_license": true}`. The optional `"system_image"` field takes an
SDK package path such as `system-images;android-35;google_apis;x86_64`.

The call returns **202** with the status body and starts the install in the
background. While it runs, `reason` is `installing`.

The call is refused when:

- `accept_license` is not `true` (400, `license_required`);
- the worker cannot run the emulator (409, `unsupported_arch`);
- a device is running (409, `device_running`).

If an install is already running, the call returns the current status. Calling
it again after an install has finished only downloads packages that changed.

## `POST /api/v1/emulator/devices/{id}/start` and `/stop`

Both return the device object from `devices` (200). `start` returns straight
away with `state: "booting"`. Watch the stream, or poll the status, for
`running`. A cold boot takes about 30–60 s. `stop` waits until the emulator
has exited (up to about 30 s) and is a no-op on a stopped device. Starting a
running device returns it unchanged.

## `POST /api/v1/emulator/devices/{id}/install`

Send `multipart/form-data` with exactly one file field named `apk`, of up to
512 MB. The file must be an APK: a zip that contains a binary
`AndroidManifest.xml`. The device must be `running`. The call returns
`{"package": str}` after `adb install -r -t -g` succeeds. Only one install
runs per device at a time; a second one gets 409.

## `WS /api/v1/emulator/devices/{id}/stream`

Server to client:

- `{"type":"state","state":"stopped"|"booting"|"running"|"error","message":str,"width":int,"height":int}`
  is sent first, then on every change. `message` holds the error reason or
  progress text; `width` and `height` are the current device-pixel size.
- `{"type":"frame","format":"jpeg","data":<base64>,"width":int,"height":int,"seq":int}`
  is sent only while the device is `running`.
- `{"type":"error","message":str,"code":str}` is sent when an input could not
  be applied. The socket stays open.

**Frame size.** `width` and `height` are the device's size in device pixels.
The JPEG itself fits within 1280×1280 (576×1280 for the Pixel in portrait), so
it can be smaller than `width`×`height`. Map a pointer position with
`width`/`height`, not with the image's natural size:
`x = offsetX / renderedWidth * frame.width`.

Frames arrive when the screen changes, at most 12 per second. A still screen
sends nothing new. A panel that connects while the device is running gets the
latest frame straight away.

Client to server (coordinates are device pixels):

- `{"type":"tap","x":int,"y":int}`
- `{"type":"swipe","x1":int,"y1":int,"x2":int,"y2":int,"duration_ms":int}`
  (`duration_ms` defaults to 300 and is clamped to 50–5000)
- `{"type":"text","text":str}` (at most 2000 characters)
- `{"type":"key","key":"HOME"|"BACK"|"APP_SWITCH"|"ENTER"|"DEL"|"POWER"|"VOLUME_UP"|"VOLUME_DOWN"}`
- `{"type":"rotate"}` turns the device 90°. The next frames and the `state`
  message carry the swapped `width`/`height`.
- `{"type":"pong"}` is ignored.

Inputs are applied in order. A message that is not a JSON object, or is
larger than 16 KB, closes the socket with 4400. An unknown `type` or a point
off the screen gets an `error` message instead. An open panel counts as use,
so the idle auto-stop (`EMULATOR_IDLE_TIMEOUT_SECONDS`, default 30 min) waits
until the last panel has closed.

## Input and frame transport

The emulator starts with `-grpc <port>` on loopback. Frames come from gRPC
`streamScreenshot` (RGB888, scaled by the emulator) and are encoded as JPEG
on a worker thread. Touches, keys and text go through gRPC `sendTouch` and
`sendKey`. adb (`screencap`, `input`) is the fallback when gRPC does not
answer. On the lab worker, `adb exec-out screencap` took about 400 ms per
raw frame and about 900 ms per PNG, which is too slow for 5 fps. That is why
the panel uses gRPC.
