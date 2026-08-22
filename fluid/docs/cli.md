# CLI reference

`fluid` is the standalone command-line host: an interactive REPL, a one-shot
runner, a session lister, and two inspection commands. Everything here is
wired in `fluid/cli/__main__.py`; the REPL and its slash commands live in
`fluid/cli/repl.py`.

```
fluid [--version] <command> [options]

fluid chat [common options] [--tui]
fluid run  [common options] "<prompt>"
fluid sessions [--store DIR] [--limit N]
fluid config [common options]
fluid import-settings [--source claude|codex] [--source-home DIR]
                      [--destination-home DIR] [--select ID] [--apply]
```

`--version` prints `fluid <version>` and exits.

## Subcommands

### `fluid chat`

Starts a session and reads prompts from stdin until `/exit` or EOF. Prints
`fluid chat — session <id> (/help, /exit)` on stderr so the id is easy to copy
for a later `--resume`. Add `--tui` for the Textual interface (needs the `tui`
extra); the TUI handles `/exit`, `/quit`, and the read-only slash commands
(`/help`, `/context`, `/usage`, `/tools`, `/skills`, `/model`) and submits
everything else as a turn.

### `fluid run "<prompt>"`

Runs one turn, streams it to stdout, and exits. If the turn started a
background worker (a `Task` in background mode, or `Monitor`), `run` keeps
streaming through the park and wake cycle and only exits once no worker is in
flight. See [background-tasks.md](background-tasks.md).

Permission prompts in `run` depend on where it is running:

- stdin and stdout are both a terminal: you get the same `[y/N/a=always]`
  prompt as the REPL.
- either is piped or redirected: the call is denied, and the model receives a
  message saying the run is non-interactive and that it can be unblocked with
  `--approval-policy never_ask` or a permission rule in `.fluid/config.toml`.

### `fluid sessions`

Lists persisted sessions from the JSONL store, newest activity first, as a
table: session id, created, last active, node count, `provider/model` of the
most recent turn, and the first user message. `--limit N` keeps the newest N
rows. `--store DIR` points at a different store directory.

### `fluid config`

Prints the fully resolved configuration as JSON after every layer (defaults,
system, user, project, environment, flags) has been applied. Useful for
checking which provider block and model a run will actually use. See
[configuration.md](configuration.md).

### `fluid import-settings`

Previews (and with `--apply`, copies) portable Claude Code and Codex content
into `~/.fluid`. Nothing is overwritten, credential-bearing MCP values are
redacted, and provider runtime settings are never enabled. `import-claude` and
`import-codex` are aliases that preselect one source.

| Flag | Meaning |
|---|---|
| `--source claude\|codex` | Which provider home to inspect. Repeat for both; the default is both. |
| `--source-home DIR` | Home directory holding the provider settings (default: your home). |
| `--destination-home DIR` | Home directory that receives `.fluid` content (default: your home). |
| `--select ID` | Apply only this preview item. Repeatable. |
| `--apply` | Copy the selected ready items. Without it the command only prints the preview. |

## Common options

`chat`, `run`, and `config` share these flags. Flags are the highest-precedence
config layer, so they beat `FLUID_*` environment variables and every config
file.

| Flag | Config key it sets | Meaning |
|---|---|---|
| `--cwd DIR` | `cwd` | Working directory for config and instruction discovery, and for the environment block in the prompt. |
| `--provider NAME` | `default_provider` | A provider block name from config (`anthropic`, `openai`, `openrouter`, `local`, `claude`, or your own). |
| `--model NAME` | `model_override` | Model id for this run. It beats both `default_model` and the selected provider block's own `model` line. `FLUID_MODEL` is the environment equivalent. |
| `--store DIR` | (store path, not config) | JSONL session store directory. Default `$XDG_DATA_HOME/fluid/sessions`, or `~/.local/share/fluid/sessions`. |
| `--code-mode` | `code_mode = true` | Expose tools through the sandboxed Python kernel (`run_python`) instead of one call at a time. |
| `--continual` | `continual = true` and `code_mode = true` | Journal kernel cells and synthesize reusable skills in the background. Implies code mode. |
| `--skills-dir DIR` | `skills_dir` | Root for persistent skills (default `~/.fluid/skills`). |
| `--approval-policy never_ask\|on_write\|always_ask` | `permission.approval_policy` | When the harness asks before running a tool. See Permissions below. |
| `--resume SESSION_ID` | — | Continue an existing session from the store. The engine rehydrates the deferred tool catalog, compaction state, and any recorded background tasks on the first turn. |
| `--continue` | — | Same as `--resume` with the most recently active session in the store. |

`--resume` with an unknown id, or `--continue` on an empty store, prints
`fluid: no such session: ...` on stderr and exits 2.

## REPL slash commands

Anything starting with `/` is handled by the REPL instead of being sent to the
model.

| Command | What it does |
|---|---|
| `/help` | Lists the commands below and reminds you how to resume the session later. |
| `/context`, `/usage` | Prompt size against the context window: total tokens and percentage, one row per category (base prompt, instructions, tools, messages, and so on, with deferred tools marked as not in the prompt), free space, the last call's cache hit rate with read/write/input counts, and compaction stats when any compaction has run. |
| `/tools` | Every registered tool, one per line, with `loaded` or `deferred` and its risk level. |
| `/skills` | Every discovered `SKILL.md`: name, scope (global or project), and description. |
| `/tasks` | Background workers known to the CLI host: task id, kind (`task` or `monitor`), status, description. |
| `/cancel <task_id>` | Kills a running worker (the whole process tree for `Monitor`). Reports `cancelled <id>` or `no running task <id>`. |
| `/model [name]` | Shows the current model, or switches to `name` for the rest of the session. |
| `/fork <node_id>` | Forks the session at a node; later turns grow from the fork. |
| `/rewind <node_id>` | Moves the session head back to a node. |
| `/exit`, `/quit` | Leaves the REPL. Ctrl-D and Ctrl-C do the same. |

Any other `/name` is looked up as a user-defined command: a
`~/.fluid/commands/<name>.md` or `.agents/commands/<name>.md` file, rendered
with `$ARGUMENTS`, `$1`, `$2`, inline `` !`command` `` output, and `@file`
contents, then submitted as a turn. An unknown name prints
`unknown command: /name (try /help)`.

While a background worker runs, the prompt comes back immediately. Worker
events (`started`, `finished`, `failed`, `cancelled`, session parked and
woken) and the wake turn that delivers results are rendered as they arrive.
A worker that finishes while the model is mid-reply has its note held until
the turn ends.

## Permission prompts

The harness classifies every tool call as `safe`, `write`, or `destructive`
and combines that with the approval policy:

| Policy | Behaviour |
|---|---|
| `never_ask` | Run everything without asking. |
| `on_write` (default) | Ask before `write` and `destructive` calls; run `safe` calls. |
| `always_ask` | Ask before every call. |

`Bash` is statically a `write` tool, but a command the classifier can prove is
read-only (`git status`, `ls`, `grep ... | head`, `git log --oneline -5`, and
similar: no redirection, no substitution, no program outside the allowlist) is
treated as `safe` and runs without a prompt under `on_write`. The allowlist is
in `fluid/tools/builtins/bash_risk.py`.

When a prompt appears it looks like

```
  [permission] Bash (risk=write) {'command': 'uv run pytest'}
  allow Bash? [y/N/a=always]
```

| Answer | Effect |
|---|---|
| `y`, `yes` | Allow this one call. |
| `a`, `always` | Allow this tool for the rest of the session. For `Bash` the allow is scoped to the programs the command runs: answering `a` to `uv run pytest` covers later `uv` commands, not `rm`. A configured `[[permission.rules]]` entry still wins over a session allow. |
| anything else, or Enter | Deny. The model is told the user denied it at the prompt and should not retry the same action through another tool. |

Three identical calls in a row force one extra prompt regardless of policy, to
break retry loops. Denials from a configured rule, from the prompt, and from a
non-interactive run each carry a different message so the model knows whether
to ask, stop, or suggest a config change.

Child agents started through `Task` inherit the parent's policy and rules; in
the REPL their prompts come to the same `[y/N/a=always]` line.

## Output format

The REPL and `run` write the same stream: assistant text inline, thinking
under a `[thinking]` marker (dimmed on a terminal), `[tool] Name {...}` and
`[tool] output` lines, `[tool!]` for a failed call, `[hook] ...` for hook
denials and warnings, `[usage] in=... out=... cache_read=... cache_write=...
hit_rate=...` per model call, and `[done] stop=<reason> calls=<n> cost=$<usd>`
when the turn ends. Console output is forced to UTF-8 on Windows.

## Exit codes

| Code | When |
|---|---|
| `0` | `chat` ended normally; `run`'s turn completed (including a user-denied permission or an interrupted turn). |
| `1` | `run`'s turn ended with an engine error (`[error] ...`). |
| `2` | Bad arguments (argparse), or `--resume`/`--continue` named a session that does not exist. |

## Environment variables the CLI reads directly

Config-level `FLUID_*` variables are listed in
[configuration.md](configuration.md). Two more are read outside the config
loader:

- `FLUID_SHELL` — path to the shell the `Bash` and `Monitor` tools run through
  on Windows. Without it, fluid looks for Git Bash in `%ProgramFiles%\Git`,
  `%ProgramFiles(x86)%\Git`, `%LOCALAPPDATA%\Programs\Git`, and on `PATH`
  (skipping the WSL launcher in `System32`), and falls back to `cmd.exe`. On
  POSIX, `$SHELL` is used when it is a Bourne-style shell, otherwise
  `/bin/sh`. The prompt's environment block names the shell that was picked.
- `FLUID_TRACE_REQUESTS=<dir>` — write every outbound model request body to
  `<dir>/<utc-timestamp>-<n>-<provider>.json` before it is sent. Diffing two
  consecutive files shows exactly where a prompt prefix changed, which is the
  usual cause of a missed cache.
- `XDG_DATA_HOME` — base for the default session store.
