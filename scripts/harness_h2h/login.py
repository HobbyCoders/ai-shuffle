"""The bench's own ChatGPT-subscription login, and a one-call model probe.

    python -m scripts.harness_h2h.login device   # prints a code, waits for approval
    python -m scripts.harness_h2h.login probe --model gpt-6.1-sol --effort high

The token lands in ``~/.config/fluid/codex_auth_h2h.json`` (0600), apart from
the token the app keeps in its database. The refresh token rotates on every
refresh, so two holders of one bundle would kill each other's credential;
a separate login keeps the bench from ever touching the app's.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from fluid.messages import Message, Text
from fluid.providers.base import ProviderRequest
from fluid.providers.openai_responses.codex_oauth import (
    CodexAuthError,
    CodexAuthPending,
    StoredBundle,
    load_bundle,
    poll_device_code,
    save_bundle,
    start_device_code,
)

from scripts.harness_h2h.metered import default_token_path, make_codex_client


async def device_login(path: Path) -> int:
    start = await start_device_code()
    print(f"Open {start.verification_uri} and enter the code: {start.user_code}", flush=True)
    print(f"(the code expires in {start.expires_in // 60} minutes)", flush=True)
    deadline = time.monotonic() + start.expires_in
    interval = max(start.interval, 3)
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        try:
            bundle = await poll_device_code(start.device_code, start.code_verifier, start.user_code)
        except CodexAuthPending as pending:
            if pending.slow_down:
                interval += 5
            continue
        except CodexAuthError as exc:
            print(f"Login failed: {exc}. Run `login device` again for a new code.", flush=True)
            return 1
        saved = save_bundle(StoredBundle.from_bundle(bundle), path)
        print(f"Signed in. Token saved to {saved}", flush=True)
        return 0
    print("The code expired before it was approved.", flush=True)
    return 1


async def probe(path: Path, model: str, effort: str) -> int:
    if load_bundle(path) is None:
        print(f"No bench login at {path}; run `login device` first.")
        return 1
    client = make_codex_client(path)
    request = ProviderRequest(
        model=model,
        messages=[Message("user", [Text("Reply with the single word: ready")])],
        system="You are a terse assistant.",
        effort=effort,
        extra={"prompt_cache_key": "h2h-probe"},
    )
    t0 = time.time()
    first = None
    text = []
    usage = {}
    async for chunk in client.stream(request):
        if first is None and chunk.kind in ("text_delta", "thinking_delta"):
            first = time.time()
        if chunk.kind == "text_delta":
            text.append(chunk.payload.get("text", ""))
        elif chunk.kind == "usage":
            usage = chunk.payload.get("usage") or {}
        elif chunk.kind == "error":
            print("error:", chunk.payload)
            return 1
    ttft = f"{first - t0:.2f}s" if first else "n/a"
    print(f"model={model} effort={effort} reply={''.join(text).strip()!r} "
          f"ttft={ttft} total={time.time() - t0:.2f}s usage={usage}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness_h2h.login")
    parser.add_argument("--token", type=Path, default=None, help="token file (default: bench login)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("device", help="device-code login")
    p = sub.add_parser("probe", help="one tiny model call")
    p.add_argument("--model", default="gpt-6.1-sol")
    p.add_argument("--effort", default="high")
    args = parser.parse_args(argv)
    path = args.token or default_token_path()
    if args.cmd == "device":
        return asyncio.run(device_login(path))
    return asyncio.run(probe(path, args.model, args.effort))


if __name__ == "__main__":
    sys.exit(main())
