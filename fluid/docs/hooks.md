# Hooks

Hooks are shell commands fluid runs at fixed points in a session: before and
after every tool call, and at the start and end of every turn. They are how you
format a file after the model edits it, refuse a command you never want run, or
get a notification when a turn finishes. The same mechanism that Claude Code
exposes through `settings.json` hooks, with the same stdin/exit-code contract,
so existing hook scripts carry over.

Embedding hosts get the same behaviour: `build_engine` registers configured
hooks onto the `HookBus` it is handed (or creates one), after any callbacks the
host attached programmatically.

## Configuration

Each hook is one `[[hooks]]` table in `config.toml`, at the user layer
(`~/.fluid/config.toml`) or the project layer (`./.fluid/config.toml` or
`./fluid.toml`):

```toml
[[hooks]]
event = "PreToolUse"
matcher = "Bash"
command = "python ~/.fluid/hooks/block-rm.py"
timeout = 10

[[hooks]]
event = "PostToolUse"
matcher = "Edit|Write"
command = "ruff format --quiet ."

[[hooks]]
event = "TurnEnd"
command = "notify-send 'fluid' 'turn finished'"
```

| Key       | Required | Meaning |
|-----------|----------|---------|
| `event`   | yes      | `PreToolUse`, `PostToolUse`, `TurnStart`, or `TurnEnd`. |
| `command` | yes      | Shell string. It runs through the same shell as the Bash tool (Git Bash on Windows when installed, otherwise `cmd.exe`; `$SHELL` or `/bin/sh` elsewhere), with the project directory as its working directory. |
| `matcher` | no       | Which tools a `PreToolUse`/`PostToolUse` hook applies to. Empty or `*` means every tool. A value containing `^`, `|` or `(` is a regular expression searched against the tool name (`Edit|Write`, `^mcp__`); anything else is a shell-style glob (`mcp__chrome-devtools__*`). Ignored for turn events. |
| `timeout` | no       | Seconds the command may run. Default 60. |

Unlike other config lists, `hooks` accumulates across layers instead of the
project layer replacing the user layer: user hooks run first, then project
hooks, in file order. A `hooks` list passed as an explicit override appends in
the same way.

### Importing Claude Code hooks

fluid also reads `~/.fluid/hooks.json` and `./.fluid/hooks.json` in the shape
Claude Code uses, so you can copy the `hooks` block out of a Claude Code
`settings.json` unchanged:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "python ~/.fluid/hooks/block-rm.py", "timeout": 10}]
      }
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "notify-send 'fluid' 'done'"}]}
    ]
  }
}
```

The outer `"hooks"` envelope is optional. `UserPromptSubmit` maps to
`TurnStart` and `Stop` to `TurnEnd`. Events fluid has no lifecycle point for
(`Notification`, `SessionStart`, `PreCompact`, ...) and hook types other than
`command` are skipped. A malformed file is ignored with no error. `hooks.json`
entries load before the `config.toml` of the same layer, so the combined order
is user JSON, user TOML, project JSON, project TOML.

## What the command receives

One JSON object on stdin. Fields common to every event:

| Field        | Value |
|--------------|-------|
| `event`      | The event name. |
| `session_id` | The session the hook fired in (null when fluid has no session yet). |
| `cwd`        | The project directory the command runs in. |

Tool events (`PreToolUse`, `PostToolUse`) add:

| Field         | Value |
|---------------|-------|
| `tool_name`   | The registered tool name, for example `Bash` or `mcp__wx__get_weather`. |
| `tool_input`  | The tool's input object. After a `PreToolUse` hook rewrote it, later hooks and the tool see the rewritten input. |
| `tool_use_id` | The provider's id for this call. |

`PostToolUse` also carries `tool_output` (the result text as the model will see
it) and `is_error` (true when the call failed, including a hook or permission
denial).

Turn events (`TurnStart`, `TurnEnd`) add `turn_id`. `TurnEnd` also carries
`text`: the assistant's final message for the turn, plain text only.

Everything is sent as JSON, so a Python hook starts with
`payload = json.load(sys.stdin)`; a shell hook can pipe through `jq`.

## How the command answers

The exit code decides:

| Exit code | Effect |
|-----------|--------|
| `0`       | Allow / continue. |
| `2`       | Deny. Stderr (or stdout if stderr is empty) becomes the reason. For `PreToolUse` the tool does not run and the model gets an error result reading `blocked by hook: <reason>`. On other events nothing can be blocked any more, so the request is reported as a warning and the session continues. |
| anything else | Treated as allow. fluid reports a warning with the exit code and the last part of stderr, and carries on. |

When the command exits `0` and prints a JSON object on stdout, two keys are
honoured:

- `{"decision": "deny", "reason": "..."}` denies exactly like exit code 2.
- `{"updated_input": {...}}` replaces the tool input. `PreToolUse` only; on
  other events it is ignored.

A hook that exceeds its `timeout` is killed (together with anything it
spawned) and counts as allow, with a warning. A command that cannot be started
at all is also allow with a warning. Hooks never abort a turn.

`PreToolUse` hooks run before the permission engine, so a hook denial never
reaches a permission prompt. The order of the full gate is: tool-surface check,
hooks, permission rules, dispatch, then `PostToolUse` hooks.

## What you see

In the REPL, a denial prints as `[hook] denied <tool>: <reason>` and a warning
as `[hook] <message>`. On the event stream both are a `HookEvent` with `level`
set to `deny` or `warning`, plus `hook_event` and `tool_name`; it is never a
turn-ending event, so hosts that treat `ErrorEvent` as terminal are unaffected.

## Examples

Refuse destructive shell commands (`PreToolUse`, matcher `Bash`):

```python
import json, sys
payload = json.load(sys.stdin)
command = payload["tool_input"].get("command", "")
if "rm -rf" in command or "git push --force" in command:
    sys.stderr.write("destructive command refused by hook")
    sys.exit(2)
```

Rewrite a tool input (`PreToolUse`):

```python
import json, sys
payload = json.load(sys.stdin)
new_input = dict(payload["tool_input"])
new_input["command"] = new_input["command"].replace("pip install", "uv pip install")
print(json.dumps({"updated_input": new_input}))
```

Format after edits (`PostToolUse`, matcher `Edit|Write`):

```toml
[[hooks]]
event = "PostToolUse"
matcher = "Edit|Write"
command = "ruff format --quiet $(jq -r .tool_input.file_path)"
```
