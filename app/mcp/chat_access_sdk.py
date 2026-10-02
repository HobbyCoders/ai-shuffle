"""Scoped Chat tools. All authorization lives in the broker, not tool risk flags."""
import json
import logging

from app.mcp.inproc import create_tool_server, tool
from app.core.chat_access import AccessDenied, broker
from app.mcp.sdk_compat import patch_sdk_server

logger = logging.getLogger(__name__)
NAMES = ("RequestChatAccess", "ChatResources", "ChatListFiles", "ChatReadFile", "ChatWriteFile", "ChatRunCommand",
         "ChatHttpRequest")
TOOL_NAMES = [f"mcp__chat-access__{name}" for name in NAMES]
CHAT_GUIDANCE = """You are a general-purpose assistant, not a repository development agent. Do not
create worktrees, develop project code, commit changes, or open pull requests.
For repository development, ask the user to switch to Code mode.
"""
GUIDANCE = """# Private resource access
Use ChatResources to inspect existing opaque access handles. With no suitable
handle, call RequestChatAccess with machine, folder or service and a short
explanation. It displays a private input card in the conversation. Never ask the
user to post addresses, passwords, private keys, API keys, tokens or other
connection details as chat text. The card sends those inputs directly to the
worker; you receive only a temporary handle, resource kind, permissions and
expiry. Never try to reveal private inputs.
Grants expire and are lost on restart. Supported resources are SSH machines,
Linux or macOS folders and HTTPS API services. A service grant holds one API key or token
which ChatHttpRequest attaches for you; you never see it, and responses are
redacted. Do not collect any other secret, because no tool can consume it
privately.
Folder grants provide bounded text read/list/write operations. On a worker
that supports it, ChatRunCommand on a folder grant runs one shell command in an
isolated sandbox: the folder is its working directory /work (read-only for a
read-only grant), it has no network and sees no other worker files, and it is
stopped after 60 seconds. Otherwise the tool refuses; then use the file tools
and never look for another way to run local commands. Machine grants allow
commands on that verified SSH target as the approved account; they do not grant
local worker shell access. Service grants allow HTTPS requests to
paths under the granted address with that credential's authority; read-only
service grants allow GET and HEAD only. Redirects are returned, never followed.
Machine administration is broad and can
modify code or use the target's network; do not describe it as a sandbox there.
Initial access always requires user approval in the private access card. Once
granted, file writes, remote commands and API requests other than GET/HEAD run
without another confirmation only
while the session is in bypass permission mode; other modes require a separate,
one-shot decision in the card. Scope, expiry and revocation apply in every mode.
In voice conversations, ask the user to enter details and approve access in the
card, never aloud. Saved
connections can be selected there without re-entering credentials; each session
still needs its own grant. Explain the action before calling the tool. Never use a granted
machine to authorize new access, bypass a denial, or reach ungranted resources.
Revocation/timeout closes the SSH connection and requests termination; remote
changes are not rolled back and detached processes can survive disconnection.
"""


def create_chat_access_server(session_id: str):
    async def invoke(operation, args):
        try:
            if operation == "request":
                result = await broker.request(session_id, args.get("kind", ""), args.get("purpose", ""))
            elif operation == "resources":
                # Grant metadata only: pending command/write contents belong to
                # the approval UI, saved connections to the access panel.
                result = await broker.resources(session_id)
            else:
                result = await broker.run(session_id, args.get("grant_id", ""), operation, args)
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except AccessDenied as exc:
            message = str(exc)
        except Exception as exc:
            logger.warning("Chat resource operation failed: %s", type(exc).__name__)
            message = "Operation failed or timed out. Inspect the target before retrying; a remote change may already have occurred."
        return {"isError": True, "content": [{"type": "text", "text": message}]}

    @tool("RequestChatAccess", "Show a private input card in this conversation for an SSH machine (kind=machine), a Linux or macOS folder (kind=folder) or an HTTPS API service credential (kind=service). Explain why access is needed. The user enters connection details privately; returns only an opaque temporary handle, never credentials or the address. Waits up to four minutes. Do not request arbitrary secrets without a supported private consumer.", {"kind": str, "purpose": str})
    async def request(args):
        return await invoke("request", args)

    @tool("ChatResources", "List opaque resource handles granted to this session, without private connection details.", {})
    async def resources(args):
        return await invoke("resources", args)

    @tool("ChatListFiles", "List up to 500 names inside a granted folder. Relative paths only; no links or traversal.", {"grant_id": str, "path": str})
    async def listing(args):
        return await invoke("list", args)

    @tool("ChatReadFile", "Read a UTF-8 file up to 64 KiB in a granted folder. Relative paths only; no links or traversal.", {"grant_id": str, "path": str})
    async def read(args):
        return await invoke("read", args)

    @tool("ChatWriteFile", "Write or replace a UTF-8 file up to 64 KiB in a writable folder. Outside bypass permission mode, waits up to four minutes for one-shot approval in Chat access. No parent directory creation.", {"grant_id": str, "path": str, "content": str})
    async def write(args):
        return await invoke("write", args)

    @tool("ChatRunCommand", "Run a command on a granted SSH machine, or for a folder grant in an isolated sandbox on this worker that sees only that folder (as /work), without network. Outside bypass permission mode, waits for one-shot approval in Chat access. Execution timeout 60 seconds; bounded output. Timeout or revocation does not undo changes already made, and on a machine does not guarantee detached processes stop.", {"grant_id": str, "command": str})
    async def command(args):
        return await invoke("command", args)

    @tool("ChatHttpRequest", "Send one HTTPS request to a granted API service. The worker attaches the private credential; you never see it. path starts with / and stays under the granted address (query allowed). method is GET, HEAD, POST, PUT, PATCH or DELETE; pass body and content_type as empty strings when unused. Methods other than GET/HEAD need a writable grant and, outside bypass permission mode, wait for one-shot approval. Redirects are not followed; the body is bounded to 64 KiB with known encodings of the credential redacted.", {"grant_id": str, "method": str, "path": str, "body": str, "content_type": str})
    async def http(args):
        return await invoke("http", args)

    tools = [request, resources, listing, read, write, command, http]
    return patch_sdk_server(create_tool_server(name="chat-access", version="1.0.0", tools=tools), tools)
