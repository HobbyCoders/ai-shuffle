"""A scripted ACP agent over stdio, for driving fluid's AcpLoop in tests.

Shaped from Antigravity CLI 1.5.6's real ``initialize`` / ``session/new``
replies (docs/internal/antigravity-acp-spike.md) and the public ACP schema.
The prompt text picks the behaviour:

* ``stream`` — a thought chunk, two message chunks, usage, end_turn.
* ``tool`` — an edit tool call that asks permission; allowed → completed with
  a diff, rejected → failed.
* ``mcp <Tool> <json>`` — calls a tool on the first stdio MCP server the
  client passed in session/new, by launching it like a real agent would.
  ``mcpask`` asks permission first, titled ``mcp__fluid_bundled__<Tool>``.
* ``agymcp <Tool> <json>`` — a bridged call as agy 1.5.9 sends it (seen live,
  issue #1751): titled ``Run mcp_fluid_bundled_<Tool>``, kind ``other``, and
  it asks permission (and says ``chose=<option>``). Allowed, it runs the
  tool. Rejected, agy ends the turn: a sibling call of its own (``Read``)
  fails with no text and the turn ends with no message at all. ``agyquiet``
  ends it at once, with no failed calls; ``agyid`` asks by the call's id
  alone, as the ACP protocol's example does; ``agyalways`` offers only
  "Always allow" and, unless chosen, ends it at once (``agycancelled``: with
  stopReason cancelled); ``agyhold``, rejected, waits for session/cancel.
* ``slow`` — streams, then waits for ``session/cancel`` (stopReason
  cancelled) or a second prompt (steering, answered in the same turn).
* ``slowonce`` / ``slowonce tool`` — ``slow`` the first time (the second after
  a finished ``Read`` call); sent again, answers "resumed" at once.
* ``ask`` — sends ``elicitation/create`` and echoes the answer.
* ``plan`` — a plan update.
* ``error`` — Antigravity's in-band error text.
* ``die`` — exits mid-turn.
* anything else — echoes the text.

Environment: FAKE_ACP_LOG (append every received message as JSON lines),
FAKE_ACP_SIGNED_IN=1 (a stored sign-in exists), GEMINI_API_KEY,
FAKE_ACP_STATE (directory remembering session ids across processes),
FAKE_ACP_MODES (comma-separated mode ids it offers; default
"default,accept-edits,plan"), FAKE_ACP_REJECT_MODES (mode ids
``session/set_mode`` refuses with an RPC error), FAKE_ACP_CATALOG (JSON
``{model: [thought levels]}`` it offers, in place of 1.5.6's three models with
low/high; a session keeps the model and level it was set to),
FAKE_ACP_CONFIG_UPDATES=1 (also announce each config change as a
``config_option_update`` before answering it).

Other entry points, like the real binary's: ``acp --login`` prints a sign-in
link and reads the code on stdin (code ``good`` signs in by writing a marker
in the agent's own HOME); ``-p /usage --output-format json`` prints
FAKE_ACP_USAGE verbatim, or a quota reply in the community-reported shape
(after FAKE_ACP_USAGE_SLEEP seconds, when set); ``models`` prints the model
list as 1.5.9 does (agy_models_1.5.9.tsv, the real binary's output, or the
file FAKE_ACP_MODELS_FILE names), or its sign-in error with exit 1 when not
signed in (after FAKE_ACP_MODELS_SLEEP seconds, when set).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

LOG = os.environ.get("FAKE_ACP_LOG")
STATE = os.environ.get("FAKE_ACP_STATE")

INIT = {
    "protocolVersion": 1,
    "agentCapabilities": {
        "loadSession": True,
        "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
        "sessionCapabilities": {"resume": {}, "close": {}, "load": {}, "list": {}, "additionalDirectories": {}},
        "auth": {"logout": {}},
        "_meta": {"antigravity": {"tier": True, "systemPrompt": {"append": True}}},
    },
    "agentInfo": {"name": "Antigravity ACP Server", "version": "1.5.6"},
    "authMethods": [
        {"id": "oauth-personal", "name": "Log in with Google", "description": "Log in with your Google account"},
        {"id": "oauth-business", "name": "Log in with Gemini Enterprise", "description": "x"},
        {"id": "gemini-api-key", "name": "Gemini API key", "description": "x"},
        {"id": "agent-platform", "name": "Gemini Enterprise Agent Platform", "description": "x"},
    ],
}
_MARKER = os.path.join(os.environ.get("HOME") or ".", ".fake_acp_signed_in")
TERMINAL_LOGIN = {"id": "terminal-login", "name": "Log in from the terminal", "description": "x",
                  "type": "terminal", "args": ["--login"]}


def _write_json(path: str, data: object) -> None:
    with open(path, "w") as fh:
        json.dump(data, fh)


def mode_ids() -> list[str]:
    raw = os.environ.get("FAKE_ACP_MODES") or "default,accept-edits,plan"
    return [m.strip() for m in raw.split(",") if m.strip()]


def catalog() -> dict[str, list[str]]:
    raw = os.environ.get("FAKE_ACP_CATALOG")
    if raw:
        return json.loads(raw)
    return {m: ["low", "high"] for m in ("gemini-4-argon", "gemini-3.8-flash", "gemini-3.1-pro")}


def default_config() -> dict:
    model = "gemini-3.1-pro" if "gemini-3.1-pro" in catalog() else next(iter(catalog()))
    return {"model": model, "thought_level": catalog()[model][0], "mode": mode_ids()[0]}


def config_options(config: dict | None = None) -> list:
    config = config or default_config()
    model, level = config["model"], config["thought_level"]
    return [
        {"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": model,
         "options": [{"value": v, "name": v} for v in catalog()]},
        {"id": "thought_level", "name": "Thinking Level", "category": "thought_level", "type": "select",
         "currentValue": level, "options": [{"value": v, "name": v.title()} for v in catalog()[model]]},
        {"id": "mode", "name": "Mode", "category": "mode", "type": "select", "currentValue": config["mode"],
         "options": [{"value": v, "name": v} for v in mode_ids()]},
    ]


def models_state(config: dict) -> dict:
    """The legacy ``models`` reply: one combined id per model and level."""
    return {"currentModelId": f"{config['model']}-{config['thought_level']}",
            "availableModels": [{"modelId": f"{m}-{lvl}", "name": f"{m}-{lvl}"}
                                for m, levels in catalog().items() for lvl in levels]}


class Agent:
    def __init__(self) -> None:
        self.out_lock = asyncio.Lock()
        self.next_id = 1000
        self.pending: dict[int, asyncio.Future] = {}
        self.authed = os.environ.get("FAKE_ACP_SIGNED_IN") == "1" or os.path.exists(_MARKER)
        self.sessions: dict[str, dict] = {}
        self.turn: dict | None = None
        self.terminal_client = False

    async def write(self, msg: dict) -> None:
        async with self.out_lock:
            sys.stdout.write(json.dumps(msg) + "\n")
            sys.stdout.flush()

    async def update(self, sid: str, update: dict) -> None:
        await self.write({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}})

    async def call(self, method: str, params: dict):
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return await fut

    def known(self, sid: str) -> bool:
        return sid in self.sessions or bool(STATE and os.path.exists(os.path.join(STATE, sid)))

    async def handle(self, msg: dict) -> None:
        if LOG:
            with open(LOG, "a") as f:
                f.write(json.dumps(msg) + "\n")
        if "method" not in msg:
            fut = self.pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        method, params, rid = msg["method"], msg.get("params") or {}, msg.get("id")
        try:
            result = await self.dispatch(method, params)
        except RpcError as exc:
            if rid is not None:
                await self.write({"jsonrpc": "2.0", "id": rid, "error": {"code": exc.code, "message": exc.message}})
            return
        except Exception as exc:  # a broken command must fail its request, not hang it
            if rid is not None:
                await self.write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": repr(exc)}})
            return
        if rid is not None:
            await self.write({"jsonrpc": "2.0", "id": rid, "result": result})

    def need_auth(self) -> None:
        if not self.authed:
            raise RpcError(-32000, "Authentication required")

    async def dispatch(self, method: str, params: dict):
        if method == "initialize":
            caps = params.get("clientCapabilities") or {}
            init = json.loads(json.dumps(INIT))
            if (caps.get("auth") or {}).get("terminal"):
                init["authMethods"].append(TERMINAL_LOGIN)
            return init
        if method == "authenticate":
            if params.get("methodId") == "gemini-api-key":
                if not os.environ.get("GEMINI_API_KEY"):
                    raise RpcError(-32602, "GEMINI_API_KEY is not set")
                self.authed = True
                return {}
            raise RpcError(-32603, "No browser is available here")
        if method == "session/new":
            self.need_auth()
            sid = str(uuid.uuid4())
            config = default_config()
            self.sessions[sid] = {"params": params, "mcp": params.get("mcpServers") or [], "config": config}
            if STATE:
                open(os.path.join(STATE, sid), "w").close()
            return {"sessionId": sid, "modes": {"currentModeId": mode_ids()[0], "availableModes": [
                {"id": m, "name": m} for m in mode_ids()]}, "models": models_state(config),
                "configOptions": config_options(config)}
        if method == "session/resume":
            self.need_auth()
            sid = params.get("sessionId")
            if not self.known(sid):
                raise RpcError(-32602, f"unknown session {sid}")
            config = default_config()
            self.sessions[sid] = {"params": params, "mcp": params.get("mcpServers") or [], "resumed": True,
                                  "config": config}
            return {"models": models_state(config), "configOptions": config_options(config)}
        if method == "session/set_config_option":
            session = self.sessions.get(params.get("sessionId")) or {}
            config = session.setdefault("config", default_config())
            option = next((o for o in config_options(config) if o["id"] == params.get("configId")), None)
            if option is None or params.get("value") not in [v["value"] for v in option["options"]]:
                raise RpcError(-32602, "bad value")
            config[option["id"]] = params["value"]
            if option["id"] == "model" and config["thought_level"] not in catalog()[config["model"]]:
                config["thought_level"] = catalog()[config["model"]][0]
            if os.environ.get("FAKE_ACP_CONFIG_UPDATES") == "1":
                await self.update(params.get("sessionId"), {"sessionUpdate": "config_option_update",
                                                            "configOptions": config_options(config)})
            return {"configOptions": config_options(config)}
        if method == "session/set_mode":
            rejected = (os.environ.get("FAKE_ACP_REJECT_MODES") or "").split(",")
            if params.get("modeId") in rejected or params.get("modeId") not in mode_ids():
                raise RpcError(-32602, f"mode {params.get('modeId')} is not available")
            return {}
        if method == "session/close":
            self.sessions.pop(params.get("sessionId"), None)
            return {}
        if method == "session/list":
            self.need_auth()
            return {"sessions": []}
        if method == "session/cancel":
            if self.turn is not None:
                self.turn["cancel"].set()
            return None
        if method == "session/prompt":
            return await self.prompt(params)
        raise RpcError(-32601, f"Method not found: {method}")

    async def prompt(self, params: dict):
        sid = params["sessionId"]
        if sid not in self.sessions:
            raise RpcError(-32602, "unknown session")
        text = "".join(b.get("text", "") for b in params.get("prompt") or [] if b.get("type") == "text")
        images = [b for b in params.get("prompt") or [] if b.get("type") == "image"]
        if self.turn is not None:  # steering: fold into the running turn
            self.turn["steers"].put_nowait(text)
            return {"stopReason": "end_turn"}
        self.turn = {"cancel": asyncio.Event(), "steers": asyncio.Queue()}
        try:
            return await self.run(sid, text, images)
        finally:
            self.turn = None

    async def say(self, sid: str, text: str, kind: str = "agent_message_chunk") -> None:
        await self.update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

    async def run(self, sid: str, text: str, images: list):
        # The command is the first line; a host may append context after it.
        text = text.split("\n", 1)[0].strip()
        word = text.split(" ", 1)[0]
        if word == "stream":
            await self.say(sid, "pondering", "agent_thought_chunk")
            await self.say(sid, "Hello ")
            await self.say(sid, "world")
            await self.update(sid, {"sessionUpdate": "usage_update", "used": 120, "size": 1000})
            return {"stopReason": "end_turn", "usage": {"inputTokens": 11, "outputTokens": 7, "totalTokens": 18}}
        if word == "tool":
            call = {"toolCallId": "call-1", "title": "Edit notes.txt", "kind": "edit", "status": "pending",
                    "locations": [{"path": "/w/notes.txt"}],
                    "content": [{"type": "diff", "path": "/w/notes.txt", "oldText": "a\n", "newText": "b\n"}]}
            await self.update(sid, {"sessionUpdate": "tool_call", **call})
            reply = await self.call("session/request_permission", {"sessionId": sid, "toolCall": call, "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]})
            outcome = (reply.get("result") or {}).get("outcome") or {}
            allowed = outcome.get("optionId") == "allow"
            await self.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "call-1",
                                    "status": "completed" if allowed else "failed",
                                    "content": [{"type": "content", "content": {"type": "text",
                                                 "text": "edited" if allowed else "denied by user"}}]})
            await self.say(sid, f"outcome={outcome.get('outcome')}:{outcome.get('optionId')}")
            return {"stopReason": "end_turn"}
        if word in ("mcp", "mcpask"):
            _, name, args = text.split(" ", 2)
            if word == "mcpask":
                # As agy does for an MCP tool: ask the host before running it.
                call = {"toolCallId": "mcp-1", "title": f"mcp__fluid_bundled__{name}", "kind": "other",
                        "status": "pending", "rawInput": json.loads(args)}
                await self.update(sid, {"sessionUpdate": "tool_call", **call})
                reply = await self.call("session/request_permission", {"sessionId": sid, "toolCall": call, "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]})
                outcome = (reply.get("result") or {}).get("outcome") or {}
                await self.say(sid, f"chose={outcome.get('optionId')} ")
                if outcome.get("optionId") not in ("allow", "always"):
                    await self.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "mcp-1",
                                            "status": "failed"})
                    await self.say(sid, "mcp denied")
                    return {"stopReason": "end_turn"}
            result = await self.call_mcp(sid, name, json.loads(args))
            await self.update(sid, {"sessionUpdate": "tool_call", "toolCallId": "mcp-1",
                                    "title": f"mcp__fluid_bundled__{name}", "kind": "other", "status": "completed",
                                    "rawInput": json.loads(args), "content": [{"type": "content",
                                    "content": {"type": "text", "text": result}}]})
            await self.say(sid, "mcp said: " + result)
            return {"stopReason": "end_turn"}
        if word in ("agymcp", "agyquiet", "agyid", "agyalways", "agycancelled", "agyhold"):
            _, name, args = text.split(" ", 2)
            call = {"toolCallId": "agy-1", "title": f"Run mcp_fluid_bundled_{name}", "kind": "other",
                    "status": "pending", "rawInput": json.loads(args)}
            await self.update(sid, {"sessionUpdate": "tool_call", **call})
            sibling = {"toolCallId": "agy-2", "title": "Read notes.txt", "kind": "read", "status": "pending",
                       "rawInput": {"Path": "notes.txt", "file_path": "notes.txt"}}
            await self.update(sid, {"sessionUpdate": "tool_call", **sibling})
            asked = {"toolCallId": "agy-1"} if word == "agyid" else call
            offered = [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                       {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
                       {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]
            if word in ("agyalways", "agycancelled"):
                offered = offered[1:2]
            reply = await self.call("session/request_permission",
                                    {"sessionId": sid, "toolCall": asked, "options": offered})
            outcome = (reply.get("result") or {}).get("outcome") or {}
            if outcome.get("optionId") not in ("allow", "always"):
                # agy reads a rejection as the person saying no: every open
                # call fails without text and the turn ends silently.
                for tool_id in ("agy-1", "agy-2") if word == "agymcp" else ():
                    await self.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tool_id,
                                            "status": "failed"})
                if word == "agyhold":
                    assert self.turn is not None
                    await asyncio.wait_for(self.turn["cancel"].wait(), timeout=10)
                return {"stopReason": "cancelled" if word in ("agycancelled", "agyhold") else "end_turn"}
            result = await self.call_mcp(sid, name, json.loads(args))
            await self.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "agy-1",
                                    "status": "completed", "content": [{"type": "content",
                                    "content": {"type": "text", "text": result}}]})
            await self.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "agy-2",
                                    "status": "completed", "content": [{"type": "content",
                                    "content": {"type": "text", "text": "notes"}}]})
            await self.say(sid, f"chose={outcome.get('optionId')} mcp said: " + result)
            return {"stopReason": "end_turn"}
        if word == "slowonce":
            # ``slow`` the first time; resent (a Resume), it answers at once,
            # also from a new process (remembered in FAKE_ACP_STATE).
            # ``slowonce tool`` runs a finished tool call before streaming.
            seen = os.path.join(STATE or ".", "slowonce_seen")
            if os.path.exists(seen):
                await self.say(sid, "resumed")
                return {"stopReason": "end_turn"}
            os.close(os.open(seen, os.O_CREAT | os.O_WRONLY))
            if text == "slowonce tool":
                await self.update(sid, {"sessionUpdate": "tool_call", "toolCallId": "read-1",
                                        "title": "Read notes.txt", "kind": "read", "status": "completed",
                                        "rawInput": {"file_path": "notes.txt"}, "content": [
                                            {"type": "content", "content": {"type": "text", "text": "notes"}}]})
            word = "slow"
        if word == "slow":
            await self.say(sid, "working")
            assert self.turn is not None
            cancel = asyncio.create_task(self.turn["cancel"].wait())
            steer = asyncio.create_task(self.turn["steers"].get())
            done, pending = await asyncio.wait({cancel, steer}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if cancel in done:
                return {"stopReason": "cancelled"}
            if steer in done:
                await self.say(sid, " steered:" + steer.result())
            return {"stopReason": "end_turn"}
        if word == "ask":
            reply = await self.call("elicitation/create", {"sessionId": sid, "mode": "form",
                                    "message": "Which colour?", "requestedSchema": {
                                        "type": "object", "properties": {"colour": {"type": "string"}}}})
            result = reply.get("result") or {}
            await self.say(sid, f"answer={json.dumps(result.get('content'))}")
            return {"stopReason": "end_turn"}
        if word == "plan":
            await self.update(sid, {"sessionUpdate": "plan", "entries": [
                {"content": "Read", "priority": "high", "status": "completed"},
                {"content": "Write", "priority": "medium", "status": "in_progress"}]})
            await self.say(sid, "planned")
            return {"stopReason": "end_turn"}
        if word == "error":
            await self.say(sid, "\n⚠ Agent execution terminated due to error.\n")
            return {"stopReason": "end_turn"}
        if word == "die":
            await self.say(sid, "bye")
            os._exit(3)
        await self.say(sid, f"echo: {text}" + (f" images={len(images)}" if images else ""))
        return {"stopReason": "end_turn"}

    async def call_mcp(self, sid: str, name: str, args: dict) -> str:
        servers = [s for s in self.sessions[sid]["mcp"] if s.get("command")]
        if not servers:
            return "no mcp server"
        server = servers[0]
        env = dict(os.environ)
        env.update({e["name"]: e["value"] for e in server.get("env") or []})
        proc = await asyncio.create_subprocess_exec(
            server["command"], *server.get("args", []), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024)  # the app's full tools/list is one long line

        async def rpc(rid, method, params):
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
            await proc.stdin.drain()
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), 20)
                if not line:
                    raise RuntimeError("mcp server exited")
                msg = json.loads(line)
                if msg.get("id") == rid:
                    return msg

        try:
            await rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "fake-acp", "version": "1"}})
            proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            listed = await rpc(2, "tools/list", {})
            tools = listed.get("result", {}).get("tools", [])
            names = [t["name"] for t in tools]
            if name == "__list__":
                # The tool list exactly as this agent was served it.
                state = os.path.join(os.environ.get("FAKE_ACP_STATE", "."), "tools_list.json")
                await asyncio.to_thread(_write_json, state, tools)
                return f"listed {len(tools)}"
            if name not in names:
                return f"tool {name} not listed: {sorted(names)}"
            called = await rpc(3, "tools/call", {"name": name, "arguments": args})
            content = (called.get("result") or {}).get("content") or []
            return "".join(c.get("text", "") for c in content) or json.dumps(called)
        finally:
            proc.kill()
            await proc.wait()


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


async def main() -> None:
    agent = Agent()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    tasks = set()
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        task = asyncio.create_task(agent.handle(msg))
        tasks.add(task)
        task.add_done_callback(tasks.discard)


def login() -> int:
    print("Please visit the following URL to authorize the application:", flush=True)
    print("https://accounts.google.com/o/oauth2/v2/auth?client_id=fake&state=x", flush=True)
    print("Enter the authorization code: ", end="", flush=True)
    code = sys.stdin.readline().strip()
    if code != "good":
        print("Authentication failed: invalid_grant", flush=True)
        return 1
    with open(_MARKER, "w", encoding="utf-8") as fh:
        fh.write("1")
    print("Authentication succeeded.", flush=True)
    return 0


def usage() -> int:
    if os.environ.get("FAKE_ACP_USAGE_SLEEP"):  # a read that does not answer
        import time
        time.sleep(float(os.environ["FAKE_ACP_USAGE_SLEEP"]))
    raw = os.environ.get("FAKE_ACP_USAGE")
    if raw is not None:
        print(raw, flush=True)
        return 0
    if not (os.environ.get("FAKE_ACP_SIGNED_IN") == "1" or os.path.exists(_MARKER)
            or os.environ.get("GEMINI_API_KEY")):
        print(json.dumps({"status": "ERROR", "error": {"message": "Authentication required"}}), flush=True)
        return 1
    print(json.dumps({"status": "OK", "quotas": [
        {"model": "gemini-3.1-pro", "remainingFraction": 0.75, "resetTime": "2026-11-12T10:00:00Z"},
        {"model": "gemini-3.8-flash", "remainingFraction": 1.0, "resetTime": "2026-11-12T10:00:00Z"},
    ]}), flush=True)
    return 0


def models() -> int:
    import time
    if os.environ.get("FAKE_ACP_MODELS_SLEEP"):  # a listing that does not answer
        time.sleep(float(os.environ["FAKE_ACP_MODELS_SLEEP"]))
    sys.stderr.write("Fetching available models...")
    if not (os.environ.get("FAKE_ACP_SIGNED_IN") == "1" or os.path.exists(_MARKER)
            or os.environ.get("GEMINI_API_KEY")):
        sys.stderr.write(" Error: Please sign in to view available models. "
                         "Launch the CLI without arguments to sign in.\n")
        return 1
    sys.stderr.write("\n")
    path = os.environ.get("FAKE_ACP_MODELS_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "agy_models_1.5.9.tsv")
    with open(path, encoding="utf-8") as listing:
        sys.stdout.write(listing.read())
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(INIT["agentInfo"]["version"], flush=True)
        sys.exit(0)
    if "--login" in sys.argv:
        sys.exit(login())
    if "-p" in sys.argv:
        sys.exit(usage())
    if sys.argv[1:2] == ["models"]:
        sys.exit(models())
    asyncio.run(main())
