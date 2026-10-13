<!--
title: Hooks
description: Run your own command or webhook at key moments in a chat
order: 2
-->

# Hooks

Hooks run your own code at key moments in a chat. Use them to check or block tool calls, log what agents do, or tell another system when an agent finishes.

A hook is either a **shell command** or a **webhook** (a POST to a URL you choose). Manage them in **Settings → Hooks**. Pick a project at the top to see that project's hooks. Each hook can be turned on and off by itself, and **Test** runs it with sample data.

A hook belongs either to one project or to the whole app.

## Events

| Event | When | What a hook can do |
| --- | --- | --- |
| Before a tool (`PreToolUse`) | Just before the agent uses a tool | A shell hook can allow or block the tool |
| Stop (`Stop`) | The agent finishes its turn | Keep the agent working, or just get told |
| `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`, `SubagentStop`, `PreCompact`, `Notification` | After a tool, when you send a message, when a chat starts or ends, and so on | Get told. These run in the background and cannot change anything |

- Before-a-tool and Stop hooks run only in chats that belong to a project.
- `Notification` fires when the chat stops to wait for you, for example on a permission prompt.
- Webhook hooks are not used for the before-a-tool event.

## Pick which tools

**Tool matcher** limits a tool hook to certain tools. It is a regular expression that must match the whole tool name. Leave it empty for every tool.

| Pattern | Matches |
| --- | --- |
| `Bash` | Only shell commands |
| `Bash\|Edit` | Shell commands or edits |
| `mcp__.*` | Every tool from an MCP server |

## Shell hooks

A shell hook runs a command. Set its **timeout** in milliseconds (1 to 300 seconds, 30 seconds by default).

**Before a tool,** the hook gets the tool call as JSON on its standard input and runs in the chat's folder:

```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": { "command": "ls -la" },
  "session_id": "...",
  "cwd": "/workspace/my-project"
}
```

**On other events,** the hook gets the event as environment variables instead:

| Variable | Contains |
| --- | --- |
| `HOOK_EVENT` | The event, for example `PostToolUse` |
| `HOOK_TOOL_NAME` | The tool, on tool events |
| `HOOK_DATA` | The whole event as JSON |
| `HOOK_TIMESTAMP` | When it happened |

For Stop hooks, `HOOK_DATA` includes the agent's final reply as `text`, plus `session_id`, `cwd` and `project_id`.

Very large values are shortened, and then `HOOK_DATA` contains `"truncated": true`. A guard that reads `HOOK_DATA` should block when it sees that. The standard input of a before-a-tool hook is never shortened.

Hook commands do not get AI Shuffle's own secrets. Your AI provider keys and GitHub token are still available to them.

## Blocking a tool call

A before-a-tool shell hook decides what happens:

- **Exit code 2** blocks the call. Whatever the hook wrote to standard error is shown as the reason.
- **JSON on standard output**, such as `{"permissionDecision": "deny", "permissionDecisionReason": "Not on main"}`, blocks it too. `allow` and `ask` also work.
- **Exit code 0 with no output** means no opinion. The usual permission rules apply.

A hook that crashes or times out has no opinion either, and the next hook runs. If no hook could answer at all, for example because the shell is missing, the call is blocked to be safe. When several hooks match, the first one with a decision wins.

## Keeping the agent working

A Stop hook can send the agent back to work, with a reason it reads:

- a shell hook that exits with code 0 and prints `{"decision": "block", "reason": "Tests are still failing"}`;
- a webhook whose server answers with a success status and that same JSON.

## Webhook hooks

A webhook hook sends a POST with a JSON body: `{"event": ..., "timestamp": ..., "data": {...}}`. Passwords in URLs inside the body are hidden.

Every request has `X-Hook-Event` and `X-Hook-Timestamp` headers. If you set a **secret**, it also has `X-Hook-Signature: sha256=<hex>`, an HMAC-SHA256 of the body made with your secret, so your server can check the request came from you.

## Plugin-contributed hooks

Plugins can bring their own hooks. They show on the **Plugin** tab, and you cannot edit or delete them there: they come and go with the plugin. They follow the standard plugin `hooks.json` format, with timeouts in seconds. A plugin's Stop hooks run before your own.

## Examples

**Log every tool call** (prints nothing, so it never blocks):

```bash
cat >> /var/log/tool-usage.jsonl; echo >> /var/log/tool-usage.jsonl
```

**Block `rm -rf /`** (event: before a tool, tool matcher `Bash`):

```bash
grep -q 'rm -rf /' && { echo 'rm -rf / is not allowed' >&2; exit 2; } || exit 0
```

**Tell your server when the agent finishes:** a webhook hook on the Stop event, with your URL and a secret.

To manage hooks from your own code, see the [Hooks API](/docs/api-reference/hooks).
