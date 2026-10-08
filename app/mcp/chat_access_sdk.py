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
For machine access, pass suggested_host with the known unicast IPv4 or IPv6
literal, never a hostname, URL or credentials. Hostname approval is not supported.
Pass suggested_port and suggested_username when known. These editable
hints prefill the card for review; omit unknown values and never guess. Never
put passwords, private keys or credentials in hints or conversation text. A saved
connection is preselected only when its host (and any supplied port/user) matches;
otherwise the card starts with a new connection. Hints grant no access. Approved
machine connection details remain private to the card, not tool results.
For folder access, include suggested_path with the absolute folder path you want
on the worker that runs this chat. The user can edit it before approving. Use a
known path, not a guessed one; in Docker/Unraid this is the container path, not
an unmounted host path. The suggestion grants no access and does not create a
folder. If the path is unknown, let the user choose privately in the card.
For service access, set service_provider to cloudflare or custom and, when known,
include suggested_url with the PUBLIC HTTPS API base address, never credentials,
query strings or private connection details. Cloudflare supplies its official
API endpoint automatically. Explain the task and required read-only resources
in purpose so the user can scope a token. Service requests return awaiting_user
immediately, NOT a grant: the preparation card remains available for 24 hours
and can be reopened without rushing setup. Do not poll, re-request, or assume
consent. Continue only after the user confirms readiness, then call ChatResources
for the approved handle. Identical retries reuse pending preparation without
extending it; cancelled or expired cards require a new request with user consent.
When an installed plugin declares a sign-in for the service, pass plugin (its
id) and service (the declared name) instead of the hints: the card lets the
user sign in, and ChatHttpRequest then attaches that sign-in's token to the
plugin's declared address only. There is no other OAuth; never collect OAuth
client secrets or tokens.
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
                result = await broker.request(session_id, args.get("kind", ""), args.get("purpose", ""),
                                              suggested_path=args.get("suggested_path", ""),
                                              suggested_host=args.get("suggested_host", ""),
                                              suggested_port=args.get("suggested_port"),
                                              suggested_username=args.get("suggested_username", ""),
                                              service_provider=args.get("service_provider", ""),
                                              suggested_url=args.get("suggested_url", ""),
                                              plugin=args.get("plugin", ""), service=args.get("service", ""))
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

    @tool("RequestChatAccess", "Show a private access card for an SSH machine, Linux or macOS folder, or HTTPS API service. Explain why access is needed. Machine/folder requests wait up to four minutes and return an opaque handle. Service requests return awaiting_user immediately, not access; preparation lasts 24 hours and identical retries resume it. The user explicitly submits private inputs when ready. After the user confirms readiness, call ChatResources for the approved handle; do not poll or assume consent. For machines, suggested_host is a known unicast IPv4 or IPv6 literal, never a hostname (hostname approval is not supported); suggested_port (1-65535) and suggested_username (POSIX username) are optional known hints. All are editable in the card. A matching saved connection is preselected; otherwise it starts with a new connection. Passwords and private keys belong only in the card. For folders, suggested_path is a known absolute path on the owning worker. For services, service_provider chooses cloudflare or custom; suggested_url is a known PUBLIC HTTPS API base without credentials/query/fragment. Cloudflare prefills its official endpoint. For a service an installed plugin declares a sign-in for, pass plugin and service instead of the hints; the user signs in from the card. Hints grant no access. Never put secrets in tool arguments; only known machine host/port/username may be passed as machine hints. Do not promise generic OAuth.", {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["machine", "folder", "service"]},
            "purpose": {"type": "string"},
            "suggested_host": {"type": "string", "maxLength": 64, "description": "Machine only: known unicast IPv4 or IPv6 literal, editable for review. Hostnames are not supported; pass the machine IP. No URLs, credentials or interface scope; omit when unknown."},
            "suggested_port": {"type": "integer", "minimum": 1, "maximum": 65535, "description": "Machine only: known SSH port, editable for review. Omit when unknown."},
            "suggested_username": {"type": "string", "maxLength": 128, "description": "Machine only: known POSIX username, editable for review. Never passwords, keys or credentials; omit when unknown."},
            "suggested_path": {"type": "string", "description": "Folder only: known absolute path on the owning worker, prefilled for user review. Omit when unknown."},
            "service_provider": {"type": "string", "enum": ["cloudflare", "custom"], "description": "Service only: known provider preset, or custom. Omit for other kinds."},
            "suggested_url": {"type": "string", "description": "Service only: known public HTTPS API base, without credentials, query or fragment. Never private connection details; omit when unknown."},
            "plugin": {"type": "string", "description": "Service only: id (name@source) of an installed plugin that declares a sign-in for the service. Use with service; omit the hints."},
            "service": {"type": "string", "description": "Service only: the service name that plugin declares. Use with plugin."},
        },
        "required": ["kind", "purpose"],
    })
    async def request(args):
        return await invoke("request", args)

    @tool("ChatResources", "List opaque resource handles and pending service preparation IDs for this session, without private connection details. Pending preparation is not access.", {})
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
