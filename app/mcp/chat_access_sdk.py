"""Scoped Chat tools. All authorization lives in the broker, not tool risk flags."""
import json
import logging

from claude_agent_sdk import create_sdk_mcp_server, tool
from app.core.chat_access import AccessDenied, broker
from app.mcp.sdk_compat import patch_sdk_server

logger = logging.getLogger(__name__)
NAMES = ("RequestChatAccess", "ChatResources", "ChatListFiles", "ChatReadFile", "ChatWriteFile", "ChatRunCommand")
TOOL_NAMES = [f"mcp__chat-access__{name}" for name in NAMES]
CHAT_GUIDANCE = """You are a general-purpose assistant, not a repository development agent. Do not
create worktrees, develop project code, commit changes, or open pull requests.
For repository development, ask the user to switch to Code mode.
"""
GUIDANCE = """# Private resource access
Use ChatResources to inspect existing opaque access handles. With no suitable
handle, call RequestChatAccess with machine or folder and a short explanation.
It displays a private input card in the conversation. Never ask the user to post
addresses, passwords, private keys, or other connection details as chat text.
The card sends those inputs directly to the worker; you receive only a temporary
handle, resource kind, permissions and expiry. Never try to reveal private inputs.
Grants expire and are lost on restart. This tool currently supports SSH machines
and Linux folders, not arbitrary API keys; do not collect a secret without a tool
which can consume it privately.
Folder grants provide bounded text read/list/write operations, not Bash. Machine
grants allow commands on that verified SSH target as the approved account; they
do not grant local worker shell access. Machine administration is broad and can
modify code or use the target's network; do not describe it as a sandbox there.
Every file write and remote command waits for a separate, one-shot user decision
in the private access card, even in bypass permission mode. In voice conversations,
ask the user to enter details and approve access in the card, never aloud. Saved
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
                snap = await broker.snapshot(session_id)
                # Pending command/write contents belong to the approval UI.
                result = {"grants": [{k: g[k] for k in ("id", "kind", "expires_at", "writable")}
                                     for g in snap["grants"]], "folder_available": snap["folder_available"]}
            else:
                result = await broker.run(session_id, args.get("grant_id", ""), operation, args)
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except AccessDenied as exc:
            message = str(exc)
        except Exception as exc:
            logger.warning("Chat resource operation failed: %s", type(exc).__name__)
            message = "Operation failed or timed out. Inspect the target before retrying; a remote change may already have occurred."
        return {"isError": True, "content": [{"type": "text", "text": message}]}

    @tool("RequestChatAccess", "Show a private input card in this conversation for an SSH machine or Linux folder. Explain why access is needed. The user enters connection details privately; returns only an opaque temporary handle, never credentials or the address. Waits up to four minutes. Do not request arbitrary secrets without a supported private consumer.", {"kind": str, "purpose": str})
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

    @tool("ChatWriteFile", "Write or replace a UTF-8 file up to 64 KiB in a writable folder. Waits up to four minutes for one-shot approval in Chat access. No parent directory creation.", {"grant_id": str, "path": str, "content": str})
    async def write(args):
        return await invoke("write", args)

    @tool("ChatRunCommand", "Run a command on a granted SSH machine, never on the local worker. Waits for one-shot approval in Chat access. Execution timeout 60 seconds; bounded output. Timeout or revocation does not undo remote changes or guarantee detached processes stop.", {"grant_id": str, "command": str})
    async def command(args):
        return await invoke("command", args)

    tools = [request, resources, listing, read, write, command]
    return patch_sdk_server(create_sdk_mcp_server(name="chat-access", version="1.0.0", tools=tools), tools)
