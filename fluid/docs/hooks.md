# Hooks

Hooks are shell commands fluid runs at fixed points in a session: before and
after every tool call, at the start and end of every turn, when a session
opens and closes, before a compaction batch, when a subagent finishes, and
when the engine is waiting on you. They are how you format a file after the
model edits it, refuse a command you never want run, or get a notification
when a turn finishes. fluid speaks Claude Code's hook contract: the same stdin
JSON, exit codes and stdout fields, so a script written for `settings.json`
hooks carries over. The [compatibility section](#claude-code-compatibility)
lists exactly what is honoured.

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
| `event`   | yes      | `PreToolUse`, `PostToolUse`, `TurnStart`, `TurnEnd`, `SessionStart`, `SessionEnd`, `PreCompact`, `SubagentStop` or `Notification`. Claude Code's `UserPromptSubmit` and `Stop` are accepted as spellings of `TurnStart` and `TurnEnd`. |
| `command` | yes      | Shell string. It runs through the same shell as the Bash tool (Git Bash on Windows when installed, otherwise `cmd.exe`; `$SHELL` or `/bin/sh` elsewhere), with the project directory as its working directory. |
| `matcher` | no       | Which tools a `PreToolUse`/`PostToolUse` hook applies to. Empty or `*` means every tool. Otherwise a regular expression or a glob; see [matchers](#matchers). Ignored for the other events. |
| `timeout` | no       | Seconds the command may run. Default 60. |

### Matchers

A matcher is tried the way Claude Code tries it, as a regular expression:
first against the whole tool name (`re.fullmatch`, so `Edit|Write` selects
exactly those two and `Notebook.*` every Notebook tool), then searched inside
it (Claude Code's `RegExp.test`, so `^mcp__` selects every MCP tool). A
matcher with no regex metacharacter other than `*` is also tried as a
shell-style glob, so `mcp__chrome-devtools__*` and `mcp__*` mean what they
look like rather than "`mcp_` followed by underscores". A plain name such as
`Bash` matches only itself, never `BashOutput`. MCP tools are named
`mcp__<server>__<tool>` and match literally. An invalid regex matches
nothing.

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

The outer `"hooks"` envelope is optional. Event names map as in the table
below. Events fluid has no lifecycle point for (`PermissionRequest`,
`SubagentStart`, ...) and hook types other than `command` are dropped with a
logged warning naming them. A malformed file is logged and ignored.
`hooks.json` entries load before the `config.toml` of the same layer, so the
combined order is user JSON, user TOML, project JSON, project TOML.

## Claude Code compatibility

### Events

| Claude Code        | fluid          | When it fires |
|--------------------|----------------|---------------|
| `PreToolUse`       | `PreToolUse`   | Before a tool call, ahead of the permission engine. |
| `PostToolUse`      | `PostToolUse`  | After a tool call, before its result reaches the model. |
| `UserPromptSubmit` | `TurnStart`    | When a prompt is submitted, before it is stored or sent to the model. |
| `Stop`             | `TurnEnd`      | When the model has finished its reply. Also fires, with a `stop_reason` of `error`, `interrupted`, `stopped` or `blocked`, when a turn ends any other way. |
| `SessionStart`     | `SessionStart` | Once per engine, when the session is opened. `source` is `startup` for a new session and `resume` for an existing one. |
| `SessionEnd`       | `SessionEnd`   | When the engine closes (the CLI exiting). `reason` is always `other`; fluid does not distinguish how the session ended. Best effort: warnings go to the log, not the event stream. |
| `PreCompact`       | `PreCompact`   | Before a compaction batch is applied to the request. `trigger` is always `auto`; fluid has no manual compact command. The batch is already decided, so the hook observes and cannot veto. |
| `SubagentStop`     | `SubagentStop` | When a `Task` worker finishes, from the CLI host. Carries `task_id`, `worker_session_id`, `status` and `summary`. A block is logged and ignored; the worker is already done. Embedding hosts that run their own workers do not emit it. |
| `Notification`     | `Notification` | When the engine is waiting on you: `notification_type` is `permission_prompt` for a permission request and `elicitation_dialog` for a tool asking a question. |

Either name works in `config.toml` and `hooks.json`; the stdin payload
carries the Claude Code name in `hook_event_name` and fluid's in `event`.

### What the command receives

One JSON object on stdin. Fields common to every event:

| Field             | Value |
|-------------------|-------|
| `hook_event_name` | The Claude Code event name (`UserPromptSubmit`, `Stop`, ...). |
| `event`           | fluid's event name (`TurnStart`, `TurnEnd`, ...). |
| `session_id`      | The session the hook fired in (null when fluid has no session yet). |
| `transcript_path` | The session's transcript file when the store keeps one on disk (the JSONL store), otherwise an empty string. |
| `cwd`             | The project directory the command runs in. |

Per event:

| Event | Fields |
|-------|--------|
| `PreToolUse`   | `tool_name` (`Bash`, `mcp__wx__get_weather`, ...), `tool_input`, `tool_use_id`, `turn_id`. After a `PreToolUse` hook rewrote the input, later hooks and the tool see the rewritten version. |
| `PostToolUse`  | The `PreToolUse` fields plus `tool_response` (the result text as the model will see it; `tool_output` is the same value under fluid's older name) and `is_error` (true when the call failed, including a hook or permission denial). |
| `TurnStart`    | `turn_id`, `prompt` (the user's text, without the reminders fluid attaches). |
| `TurnEnd`      | `turn_id`, `text` (the assistant's final message, plain text; empty when the turn ended without one), `stop_reason` (`end_turn`, `max_tokens`, `max_calls`, `error`, `interrupted`, `stopped`, `blocked`), `stop_hook_active` (true when this turn already continued because a `TurnEnd` hook blocked). |
| `SessionStart` | `source`: `startup` or `resume`. |
| `SessionEnd`   | `reason`: always `other`. |
| `PreCompact`   | `trigger`: always `auto`; `custom_instructions`: always empty; `tiers`: the compaction tiers in the batch. |
| `SubagentStop` | `task_id`, `worker_session_id`, `status` (`finished`, `failed`, `cancelled`), `summary`, `stop_hook_active` (always false). |
| `Notification` | `message`, `notification_type`, `tool_name`, `tool_use_id`. |

Everything is sent as JSON, so a Python hook starts with
`payload = json.load(sys.stdin)`; a shell hook can pipe through `jq`.

### Exit codes

| Exit code | Effect |
|-----------|--------|
| `0`       | Allow / continue. stdout may carry a JSON object (next section). On `TurnStart` and `SessionStart` plain stdout is added to the model's context instead. |
| `2`       | Block. Stderr (or stdout if stderr is empty) is the reason. What a block does depends on the event: see below. |
| anything else | Non-blocking error. fluid reports a warning with the exit code and the last part of stderr, and carries on as if the hook allowed. |

What a block means:

| Event | A block (exit 2, `decision: block`, `decision: deny`, `permissionDecision: deny`) |
|-------|-------|
| `PreToolUse`   | The tool does not run. The model gets an error result reading `blocked by hook: <reason>`; no permission prompt is shown. |
| `PostToolUse`  | The call already ran, so the result stands and `is_error` is unchanged; the reason is appended to the result text in a system-reminder block, so the model sees it. |
| `TurnStart`    | The turn is aborted before any model call. The prompt is not stored, so it is not part of the session. An `ErrorEvent` reading `prompt blocked by hook: <reason>` ends the turn, and the `TurnEnd` hooks run with `stop_reason: blocked`. |
| `TurnEnd`      | The model is told to keep going: the reason goes to it as a user message inside a system-reminder block and another model call runs. The next `TurnEnd` payload has `stop_hook_active: true` so a hook can avoid looping. At most 3 continuations per turn; past that the block is reported as a warning and the turn ends. |
| `SessionStart`, `SessionEnd`, `PreCompact`, `SubagentStop`, `Notification` | Nothing is left to block. The request is reported as a warning. |

A hook that exceeds its `timeout` is killed (together with anything it
spawned) and counts as allow, with a warning. A command that cannot be started
at all is also allow with a warning. A programmatic hook callback that raises
is logged and reported as a warning; it never ends the turn.

### stdout JSON

When the command exits `0` and its stdout ends with a JSON object, the object
is read. Log lines printed before it are fine: fluid takes the last line that
starts with `{` and decodes as an object. Fields:

| Field | Effect |
|-------|--------|
| `continue: false` | The turn ends once this hook has run. `stopReason` (or `reason`) is shown to the user. On `PreToolUse` the call is also denied; on `PostToolUse` the result is kept and no further model call is made; on `TurnEnd` the turn just ends, with no continuation. |
| `stopReason` | The text surfaced with `continue: false`. |
| `suppressOutput` | Accepted and ignored; fluid does not echo hook stdout. |
| `systemMessage` | Shown to the user as a hook warning (`[hook] <message>` in the REPL). |
| `decision` | `block` or `deny`: a block, with `reason` as the reason. `approve` (`PreToolUse` only): the call runs without a permission prompt. |
| `reason` | The reason for a block, and for `approve`. |
| `hookSpecificOutput.permissionDecision` | `PreToolUse` only. `allow` skips the permission prompt; `deny` blocks; `ask` forces a permission prompt with `permissionDecisionReason` as the reason. |
| `hookSpecificOutput.permissionDecisionReason` | The reason for the permission decision. |
| `hookSpecificOutput.updatedInput` | `PreToolUse` only: replaces the tool input. |
| `hookSpecificOutput.additionalContext` | `TurnStart` and `SessionStart`: appended to the user message in a system-reminder block (SessionStart context rides the first prompt of the engine). `PostToolUse`: appended to the tool result the same way. |
| `updated_input` | fluid's older spelling of `updatedInput`; still honoured. |

`hookSpecificOutput.hookEventName` is accepted and not checked. Text a hook
injects is defanged first, so it cannot close a system-reminder block early or
forge a steering marker.

`PreToolUse` hooks run before the permission engine, so a hook denial never
reaches a permission prompt and a hook `allow` skips it. The order of the full
gate is: tool-surface check, hooks, permission rules, dispatch, then
`PostToolUse` hooks. The same contract holds on the other two dispatch paths:
a tool called from inside a code-mode cell, and a Fluid tool served to the
`claude` CLI over the bundled bridge. On the bridge a `continue: false` ends
the turn once the CLI's result lands.

## What you see

In the REPL, a denial prints as `[hook] denied <tool>: <reason>` and anything
else as `[hook] <message>`. On the event stream every hook message is a
`HookEvent` with `level` set to `deny` (a block), `warning` (a timeout, an
unexpected exit code, a `systemMessage`, a `continue: false`) or `info` (a
`PreToolUse` approve, a `TurnEnd` continuation, a `PostToolUse` block), plus
`hook_event` and `tool_name`; it is never a turn-ending event, so hosts that
treat `ErrorEvent` as terminal are unaffected. A `TurnStart` block and a
`continue: false` end the turn with an `ErrorEvent` whose message carries the
reason.

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
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "updatedInput": new_input,
}}))
```

Skip the permission prompt for a command you trust (`PreToolUse`, matcher
`Bash`):

```python
import json, sys
payload = json.load(sys.stdin)
if payload["tool_input"].get("command", "").startswith("git status"):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": "read-only git command",
    }}))
```

Make the model run the tests before it stops (`Stop`), without looping:

```python
import json, sys
payload = json.load(sys.stdin)
if not payload["stop_hook_active"] and "pytest" not in payload["text"]:
    print(json.dumps({"decision": "block", "reason": "Run the test suite and report the result."}))
```

Format after edits (`PostToolUse`, matcher `Edit|Write`):

```toml
[[hooks]]
event = "PostToolUse"
matcher = "Edit|Write"
command = "ruff format --quiet $(jq -r .tool_input.file_path)"
```
