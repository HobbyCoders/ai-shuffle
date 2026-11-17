"""Private two-worker devloop fixture. Not imported by the installed app."""
import asyncio
import json
from pathlib import Path
import runpy
import sys

from scripts.devloop.child import bounded_shutdown


class PeerFixture:
    def __init__(self, app, directory):
        self.app = app
        self.directory = directory

    async def __call__(self, scope, receive, send):
        if scope.get("shuffle.ingress") != "remote" or scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path == "/api/v1/network/peer/auth/challenge":
            hold = self.directory / "hold-auth"
            if hold.exists():
                (self.directory / "auth-held").touch()
                for _ in range(300):  # also bounds a forgotten switch
                    if not hold.exists():
                        break
                    await asyncio.sleep(0.1)
        prefix = "/api/v1/network/peer/control/"
        if not path.startswith(prefix):
            return await self.app(scope, receive, send)
        try:
            config = json.loads((self.directory / "reply.json").read_text())
        except (OSError, ValueError):
            config = None
        if (not isinstance(config, dict) or config.get("action") != path[len(prefix):]
                or not isinstance(config.get("status"), int) or not 200 <= config["status"] <= 599
                or not isinstance(config.get("body"), str)):
            return await self.app(scope, receive, send)
        # Only corrupt a real successful reply. The real auth, ownership and
        # control handler run first; refusals and exceptions pass unchanged.
        replaced = False

        async def reply(message):
            nonlocal replaced
            if message["type"] == "http.response.start" and 200 <= message["status"] < 300:
                replaced = True
                await send({"type": "http.response.start", "status": config["status"],
                            "headers": [(b"content-type", b"text/plain")]})
            elif replaced and message["type"] == "http.response.body":
                if not message.get("more_body"):
                    (self.directory / "reply-sent").touch()
                    await send({"type": "http.response.body", "body": config["body"].encode()})
            else:
                await send(message)
        await self.app(scope, receive, reply)


def main():
    from app import serve
    from app.preview_run import disposable_preview_data
    if not disposable_preview_data():
        raise RuntimeError("peer fixture requires a disposable preview")
    directory = Path(sys.argv.pop(1))
    original = serve.run
    serve.run = lambda app, settings: original(PeerFixture(app, directory), settings)
    try:
        with bounded_shutdown():
            runpy.run_module("app.main", run_name="__main__", alter_sys=True)
    finally:
        serve.run = original


if __name__ == "__main__":
    main()
