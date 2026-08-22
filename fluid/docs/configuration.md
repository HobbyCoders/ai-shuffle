# Configuration reference

fluid reads TOML config files, two optional JSON files, `FLUID_*` environment
variables, and command-line flags, merges them in a fixed order, and builds one
resolved configuration from the result. `fluid config` prints that result as
JSON. The loader is `fluid/config/loader.py`; the typed model it produces is
`fluid/config/model.py`.

## Layers and precedence

Lowest to highest. Each layer deep-merges over the previous one: tables merge
key by key, scalars and lists replace, with one exception noted below.

1. Built-in defaults (`BUILTIN_DEFAULTS` in the loader; listed in full below).
2. System: `/etc/fluid/config.toml`, if present.
3. User: `~/.fluid/mcp.json`, then `~/.fluid/hooks.json`, then
   `~/.fluid/config.toml`.
4. Project: `./.fluid/mcp.json`, then `./.fluid/hooks.json`, then
   `./.fluid/config.toml` or, if that is absent, `./fluid.toml`.
5. Environment: the `FLUID_*` variables below.
6. Command-line flags (`--provider`, `--model`, `--code-mode`, `--continual`,
   `--skills-dir`, `--approval-policy`, `--cwd`).

Within a scope the TOML file loads after the JSON files, so a native `[mcp.x]`
table beats a same-named server from `mcp.json`.

The exception: `hooks` is additive. Lists from every layer are concatenated,
user hooks first, then project hooks, each in file order. Every other list
(`tools.allowed`, `permission.rules`, ...) is replaced by the higher layer.

"Project" means the directory fluid was started in, or `--cwd`. There is no
upward search for a project file.

## Top-level keys

| Key | Default | Meaning |
|---|---|---|
| `default_provider` | `"anthropic"` | Name of the provider block to use when `--provider` is not given. |
| `default_model` | `"claude-sonnet-5"` | Model id used when the selected provider block has no `model` of its own. `--model` and `FLUID_DEFAULT_MODEL` set this key; a provider's `model` line still takes precedence over it. |
| `code_mode` | `false` | Expose tools through the sandboxed Python kernel (`run_python`) instead of as individual tool calls. Adds the code-mode addendum to the CLI prompt. |
| `continual` | `false` | Code mode only. Journal kernel cells and synthesize reusable skills in the background. `--continual` turns this and `code_mode` on together. |
| `continual_model` | none | Pin skill synthesis to one model id regardless of the session's model. |
| `skills_dir` | none (`~/.fluid/skills`) | Root directory for persistent code-mode skills. |
| `cwd` | `"."` | Working directory for config discovery, instruction files, tools, and the prompt's environment block. |
| `max_tokens` | `8192` | Maximum output tokens per model call. |
| `context_window` | none | The model's context window in tokens. fluid does not guess this; set it so `/context` can show a percentage and free space. |
| `system` | none | Your own base system prompt. When set it replaces the shipped default (`fluid/cli/prompt.py`) entirely; the instruction stack, skills index, and environment block are still appended after it. |
| `base_prompt` | `true` | `false` runs the CLI with no base prompt at all (only instruction files, skills, agents, and the environment block). Ignored when `system` is set. |

### `[permission]`

| Key | Default | Meaning |
|---|---|---|
| `approval_policy` | `"on_write"` | `never_ask` runs everything; `on_write` asks before `write` and `destructive` tool calls; `always_ask` asks before every call. Read-only `Bash` commands count as safe under `on_write` (see [cli.md](cli.md#permission-prompts)). |
| `rules` | `[]` | Explicit per-tool decisions, evaluated in order with the last match winning. A matching rule overrides the policy and any session-scoped "allow always". |

Each `[[permission.rules]]` entry:

| Key | Required | Meaning |
|---|---|---|
| `tool` | yes | Glob over the tool name: `Bash`, `mcp__*`, `*`. |
| `decision` | no (`"ask"`) | `allow`, `deny`, or `ask`. |
| `input_globs` | no | Table of input field to glob, matched against the stringified value. Every listed field must match. |

```toml
[permission]
approval_policy = "on_write"

[[permission.rules]]
tool = "Bash"
decision = "allow"
input_globs = { command = "uv run pytest*" }

[[permission.rules]]
tool = "Bash"
decision = "deny"
input_globs = { command = "*rm -rf*" }

[[permission.rules]]
tool = "mcp__chrome-devtools__*"
decision = "allow"
```

### `[instruction_files]`

Toggles for the instruction stack (`AGENTS.md` files, skills, agents, user
commands). All default to `true`.

| Key | Meaning |
|---|---|
| `enabled` | Master switch. `false` disables every discovery below. |
| `global` | `~/.fluid/AGENTS.md`. |
| `project` | Root `AGENTS.md` / `AGENT.md` / `AGENTS.override.md` and `.agents/`. |
| `skills` | `SKILL.md` discovery under `~/.fluid/skills` and `.agents/skills`. |
| `commands` | User slash commands under `~/.fluid/commands` and `.agents/commands`. |
| `nested` | `AGENTS.md` files in subdirectories, surfaced when a tool touches that directory. |

The long spellings `discover_global`, `discover_project`, `discover_skills`,
`discover_commands`, `discover_nested` are accepted too.

### `[tools]`

| Key | Default | Meaning |
|---|---|---|
| `allowed` | unset (every tool) | A list makes the tool surface exactly that set. An empty list means no tools. Exact names or fnmatch patterns. |
| `disallowed` | `[]` | Subtracted after `allowed`, so a deny wins. |

```toml
[tools]
disallowed = ["Write", "mcp__chrome-devtools__*"]
```

### `[context]`

Knobs for tool-result trimming and the compaction ladder
(`fluid/context/compaction.py`, `fluid/context/trim.py`).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` disables both trimming and compaction. |
| `trigger_tokens` | `120000` | Prompt size at which trimming and compaction start. |
| `headroom_tokens` | `16000` | Reserve kept free below the trigger. |
| `target_ratio` | `0.7` | Compaction aims to bring the prompt down to this fraction of the trigger. |
| `keep_recent_turns` | `3` | Turns never folded into a digest. |
| `max_resident_span_digests` | `20` | Span digests kept in the prompt before they fold into an epoch stub. |
| `epoch_fold_spans` | `10` | How many span digests fold into one epoch stub. |
| `intake_stub_chars` | `25000` | Tool results longer than this are stubbed on arrival. |
| `keep_recent_tool_results` | `5` | Most recent tool results that are never trimmed. |
| `min_result_chars` | `2000` | Results shorter than this are left alone by the trimmer. |

A trimmed result is replaced by a placeholder that states how many characters
were removed, quotes the first 160 characters of the original, and carries a
`node_...` ref that `HistoryExpand` or `HistorySearch` can recover.

### `[[hooks]]`

Shell commands run before and after tool calls and at turn boundaries. Full
contract (stdin payload, exit codes, `updated_input`) in [hooks.md](hooks.md).

| Key | Required | Default | Meaning |
|---|---|---|---|
| `event` | yes | | `PreToolUse`, `PostToolUse`, `TurnStart`, or `TurnEnd`. |
| `command` | yes | | Shell string, run through the same shell as the `Bash` tool with `cwd` as its working directory. |
| `matcher` | no | every tool | Tool-name filter for tool events: a glob, or a regex when it contains `^`, `|`, or `(`. |
| `timeout` | no | `60` | Seconds before the hook is killed and treated as allow. |

```toml
[[hooks]]
event = "PostToolUse"
matcher = "Edit|Write"
command = "ruff format --quiet ."
```

`~/.fluid/hooks.json` and `./.fluid/hooks.json` accept the Claude Code
`settings.json` hooks shape; `UserPromptSubmit` maps to `TurnStart` and `Stop`
to `TurnEnd`.

### `[providers.<name>]`

Every provider block has the same keys; `kind` decides which client is built.

| Key | Default | Meaning |
|---|---|---|
| `kind` | `"anthropic"` | `anthropic`, `openai_responses`, `openrouter`, `local`, or `delegated_claude`. |
| `model` | `""` | Model id for this provider. Empty falls back to `default_model`. |
| `base_url` | none | Override the endpoint. Required for `local`. |
| `api_key_env` | none | Environment variable holding the API key. The Anthropic, OpenAI, and OpenRouter clients also fall back to `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `OPENROUTER_API_KEY` respectively. |
| `tool_style` | `"native"` | `text` switches to the `<tool_call>` text protocol for models without reliable native tool calling. See [local-models.md](local-models.md). |
| `extra` | `{}` | Request-body fields merged into every request. Honoured by `local` (llama.cpp `cache_prompt`, samplers, Ollama `options`). |

The built-in blocks:

```toml
[providers.anthropic]
kind = "anthropic"
model = "claude-sonnet-5"
api_key_env = "ANTHROPIC_API_KEY"

[providers.openai]
kind = "openai_responses"
model = "gpt-5-codex"        # placeholder; Codex model ids depend on the account
api_key_env = "OPENAI_API_KEY"

[providers.openrouter]
kind = "openrouter"
model = "openai/gpt-5-mini"
api_key_env = "OPENROUTER_API_KEY"

[providers.local]
kind = "local"
base_url = "http://127.0.0.1:8080"

[providers.claude]
kind = "delegated_claude"
model = "claude-sonnet-5"
```

`delegated_claude` runs the official `claude` CLI as a subprocess and uses its
login; `api_key_env` and `base_url` do not apply. The `openai_responses` client
can also authenticate with Codex/ChatGPT subscription OAuth when an embedding
host hands it an auth object; the CLI config path uses the API key.

A working OpenRouter setup, end to end:

```toml
# ~/.fluid/config.toml
default_provider = "openrouter"

[providers.openrouter]
kind = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "OPENROUTER_API_KEY"
```

```sh
export OPENROUTER_API_KEY=sk-or-...
fluid chat
# or, without touching default_provider:
fluid chat --provider openrouter
```

Because the block sets `model`, `--model` will not change it; remove the
`model` line (or edit it) to pick the model from the command line.

Adding a second block of the same kind is just a new name:

```toml
[providers.fast]
kind = "openrouter"
model = "openai/gpt-5-mini"
api_key_env = "OPENROUTER_API_KEY"
```

Then `fluid run --provider fast "..."`.

### `[mcp.<name>]`

MCP servers connected at session start. Their tools appear as
`mcp__<name>__<tool>`.

| Key | Meaning |
|---|---|
| `transport` | `stdio` (default when `command` is set) or `http` (`streamable_http` is accepted; default when `url` is set). |
| `command`, `args`, `env`, `cwd` | stdio: the server process to spawn. |
| `url` | http: the server endpoint. |

```toml
[mcp.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]

[mcp.remote]
transport = "http"
url = "https://example.com/mcp"
```

`~/.fluid/mcp.json` and `./.fluid/mcp.json` accept the same servers in JSON,
with or without the `{"mcpServers": {...}}` envelope. MCP support needs the
`mcp` extra (`uv sync --extra mcp`), which installs `mcp>=2.0`.

## Environment variables

Config overrides, applied as layer 5 (`env_overrides` in the loader):

| Variable | Sets |
|---|---|
| `FLUID_DEFAULT_PROVIDER` | `default_provider` |
| `FLUID_DEFAULT_MODEL` | `default_model` |
| `FLUID_CWD` | `cwd` |
| `FLUID_CODE_MODE` | `code_mode` (`1`, `true`, `yes`, `on` are true) |
| `FLUID_BASE_PROMPT` | `base_prompt` (same boolean spellings) |
| `FLUID_SKILLS_DIR` | `skills_dir` |
| `FLUID_CONTINUAL` | `continual` |
| `FLUID_CONTINUAL_MODEL` | `continual_model` |
| `FLUID_MAX_TOKENS` | `max_tokens` (ignored if not an integer) |
| `FLUID_CONTEXT_WINDOW` | `context_window` (ignored if not an integer) |
| `FLUID_APPROVAL_POLICY` | `permission.approval_policy` |
| `FLUID_CONTEXT_ENABLED` | `context.enabled` |
| `FLUID_CONTEXT_TRIGGER_TOKENS` | `context.trigger_tokens` |
| `FLUID_CONTEXT_HEADROOM_TOKENS` | `context.headroom_tokens` |

Read outside the config loader:

| Variable | Effect |
|---|---|
| `FLUID_SHELL` | Windows only: path to the shell for `Bash`, `Monitor`, and hook commands. Otherwise fluid finds Git Bash itself and falls back to `cmd.exe`. |
| `FLUID_TRACE_REQUESTS` | Directory that receives a JSON dump of every outbound model request. Unset means no tracing. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` | Default key sources for the matching provider kinds, and the defaults for `api_key_env`. |
| `XDG_DATA_HOME` | Base for the default session store (`<base>/fluid/sessions`). |

## Code mode details

With `code_mode = true` the model gets a `run_python` tool and a persistent,
sandboxed kernel. Tools are importable in every spelling: `from tools import
Glob`, `import tools; tools.Glob(...)`, `import tools.Glob`, and `from
tools.Glob import Glob`. Standard-library imports are limited to a pure-helper
allowlist (`DEFAULT_ALLOWLIST` in `fluid/codemode/vfs.py`): `json`, `re`,
`math`, `statistics`, `itertools`, `functools`, `collections`, `datetime`,
`textwrap`, `pathlib`, `csv`, `io`, `base64`, `hashlib`, `random`, `string`,
`typing`, `dataclasses`, `decimal`, `fractions`, `enum`, `operator`, `heapq`,
`bisect`, `uuid`, `shlex`, `fnmatch`, `difflib`, `copy`, `pprint`,
`unicodedata`, `html`, `ipaddress`, `struct`, `zlib`, `time`, and
`__future__`. `open()` and the network are disabled; files go through the
permission-gated tools. A failing cell returns a traceback trimmed to the
cell's own frames.
