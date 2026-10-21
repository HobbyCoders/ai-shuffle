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
Never ask for addresses, passwords, private keys, API keys, tokens, OAuth client
secrets or other connection details in chat or voice: the user enters and
approves them in the private input card. Never reveal private inputs or put
secrets in tool arguments. Only SSH machines, Linux/macOS folders and HTTPS
services are supported: check ChatResources; if no handle fits, explain why and
use RequestChatAccess, then follow the resource's card instructions.
Initial access requires card approval. Grants expire and are lost on restart.
Local Task descendants inherit handles.
Never use a machine grant to authorize new access, bypass a denial or reach
ungranted resources.
"""


def create_chat_access_server(session_id: str):
    async def invoke(operation, args):
        try:
            if operation == "request":
                kind = args.get("kind", "")
                # Plain-JSON callers may fill every optional field. Ignore
                # known filler, but let the broker refuse real saved/plugin
                # conflicts without looking up or comparing private targets.
                plugin = args.get("plugin") if args.get("plugin") is not None else ""
                service = args.get("service") if args.get("service") is not None else ""
                saved_id = args.get("saved_id") if args.get("saved_id") is not None else ""
                hint_kinds = {
                    "suggested_host": "machine", "suggested_port": "machine",
                    "suggested_username": "machine", "suggested_path": "folder",
                    "service_provider": "service", "suggested_url": "service",
                }
                hints = {}
                for name, hint_kind in hint_kinds.items():
                    value = args.get(name)
                    if value is None or (isinstance(value, str) and not value.strip()):
                        continue
                    if saved_id:
                        if name == "suggested_port" and type(value) is int and value in (0, 22):
                            continue
                        if name == "service_provider" and kind != "service" and value == "custom":
                            continue
                    elif kind != hint_kind:
                        continue
                    if plugin and name in ("service_provider", "suggested_url") and value == "custom":
                        continue
                    hints[name] = value
                result = await broker.request(session_id, kind, args.get("purpose", ""),
                                              **hints, plugin=plugin, service=service, saved_id=saved_id)
            elif operation == "resources":
                # Saved labels/IDs only; targets and pending command/write
                # contents belong exclusively to the owner's approval UI.
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

    @tool("RequestChatAccess", "Show a private card for an SSH machine, Linux/macOS folder or HTTPS service; explain why in purpose. Machine/folder requests wait up to four minutes and return an opaque handle. Service requests return awaiting_user immediately, NOT access: preparation lasts 24 hours and can be reopened without rushing. Do not poll, re-request or assume consent; after the user confirms readiness, call ChatResources. Identical retries reuse pending preparation without extending it; cancelled/expired cards need a new request with user consent. Hints are editable, grant no access and must be known, never guessed; omit unknowns for private entry. Machine hints use only host/port/username as specified below. Call ChatResources first for saved connection IDs, kinds and labels. Pass saved_id alone (without hints or plugin sign-in) to preselect an owned entry for approval, not to grant access. Blank or known placeholder hints are ignored; other conflicting hints are refused with the field named. Without saved_id, machine hints preselect only when host and any supplied port/user match. Approved details stay private, never in results. Folder paths are on this worker (Docker: container path, not an unmounted host path); hints do not create folders. For services, explain the task and required read-only resources so the user can scope a token. Cloudflare prefills its official API endpoint. For a plugin-declared sign-in pass plugin and service instead of hints: ChatHttpRequest attaches its token only to the plugin's declared address. No generic OAuth: plugin-declared sign-ins are the only OAuth. Grants expose only temporary handles, resource kinds, permissions and expiry. ChatResources lists saved IDs, kinds and owner-chosen labels, never addresses or credentials. Reuse via saved_id; unrelated sessions need their own grant. Local Task descendants reuse ancestor handles with unchanged scope, expiry and revocation, under their own confirmation mode; never across workers. Scope, expiry and revocation apply in every permission mode. After a grant, writes, commands and non-GET/HEAD API requests skip confirmation only in bypass mode; otherwise each needs a one-shot card decision. A service grant holds one API key/token, attached privately by ChatHttpRequest with redacted responses; collect no other secret because no tool can consume it privately. Machine access is broad administration as the approved account on the verified SSH target, not a sandbox or local worker shell grant; it can modify code or use that machine's network. For folder sandbox limits/refusals see ChatRunCommand; for service scope, read-only methods and redirects see ChatHttpRequest. Follow the private-resource safety rules in the prompt.", {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["machine", "folder", "service"]},
            "purpose": {"type": "string"},
            "saved_id": {"type": "string", "description": "Saved connection ID from ChatResources, of the requested kind. Preselects the private approval card; never grants access. Pass saved_id alone, without connection hints or plugin/service. Blank or known placeholder hints are ignored; other conflicting hints are refused with the field named."},
            "suggested_host": {"type": "string", "maxLength": 64, "description": "Machine only: known unicast IPv4 or IPv6 literal, editable for review. Hostnames are not supported; pass the machine IP. No URLs, credentials or interface scope; omit when unknown."},
            "suggested_port": {"type": "integer", "minimum": 1, "maximum": 65535, "description": "Machine only: known SSH port, editable for review. Omit when unknown."},
            "suggested_username": {"type": "string", "maxLength": 128, "description": "Machine only: known POSIX username, editable for review. Never passwords, keys or credentials; omit when unknown."},
            "suggested_path": {"type": "string", "description": "Folder only: known absolute path on the owning worker, prefilled for user review. Omit when unknown."},
            "service_provider": {"type": "string", "enum": ["cloudflare", "custom"], "description": "Service only: known provider preset, or custom. Omit for other kinds."},
            "suggested_url": {"type": "string", "description": "Service only: known public HTTPS API base, without credentials, query or fragment. Never private connection details; omit when unknown."},
            "plugin": {"type": "string", "description": "Service only: id (name@source) of an installed plugin that declares a sign-in for the service. Use with service; omit the hints. Blank or known placeholder hints are ignored; other conflicting hints are refused with the field named."},
            "service": {"type": "string", "description": "Service only: the service name that plugin declares. Use with plugin."},
        },
        "required": ["kind", "purpose"],
    })
    async def request(args):
        return await invoke("request", args)

    @tool("ChatResources", "List opaque resource handles, pending service preparation IDs, and this owner's saved connection IDs, kinds and labels on this worker. Never returns saved addresses or credentials. Re-request a suitable saved entry with RequestChatAccess(saved_id=...). Saved entries and pending preparation are not access.", {})
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

    @tool("ChatRunCommand", "Run one command on a granted SSH machine or, where supported, a folder sandbox. Folder grants expose only that folder as /work, read-only for read-only grants, with no network or other worker files. If sandbox execution is refused, use the file tools; never seek another local-command route. Outside bypass permission mode, wait for one-shot Chat access approval. Execution timeout 60 seconds; bounded output. SSH timeout/revocation closes the connection and requests termination, but changes are not rolled back and detached processes may survive disconnection.", {"grant_id": str, "command": str})
    async def command(args):
        return await invoke("command", args)

    @tool("ChatHttpRequest", "Send one HTTPS request to a granted API service. The worker attaches the private credential; you never see it. path starts with / and stays under the granted address (query allowed). method is GET, HEAD, POST, PUT, PATCH or DELETE; pass body and content_type as empty strings when unused. Read-only grants allow GET/HEAD only. Other methods need a writable grant and, outside bypass permission mode, wait for one-shot approval. Redirects are not followed; the body is bounded to 64 KiB with known encodings of the credential redacted.", {"grant_id": str, "method": str, "path": str, "body": str, "content_type": str})
    async def http(args):
        return await invoke("http", args)

    tools = [request, resources, listing, read, write, command, http]
    return patch_sdk_server(create_tool_server(name="chat-access", version="1.0.0", tools=tools), tools)
