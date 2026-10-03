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
2. `permissions.defaultMode` from the settings.json stack (see below), as an
   approval policy. It sits under every file so a config file, environment
   variable, or flag that names a policy still wins.
3. System: `/etc/fluid/config.toml`, or `%ProgramData%\fluid\config.toml` on
   Windows, if present.
4. User: `~/.fluid/mcp.json`, then `~/.fluid/hooks.json`, then
   `~/.fluid/config.toml`.
5. Project: for each project directory, outer first: `<dir>/.fluid/mcp.json`,
   then `<dir>/.fluid/hooks.json`, then `<dir>/.fluid/config.toml` or, if that
   is absent, `<dir>/fluid.toml`.
6. Environment: the `FLUID_*` variables below.
7. Command-line flags (`--provider`, `--model`, `--code-mode`, `--continual`,
   `--skills-dir`, `--approval-policy`, `--cwd`).

Within a scope the TOML file loads after the JSON files, and each `mcp.<name>`
entry is replaced whole rather than merged key by key, so a native `[mcp.x]`
table beats a same-named server from `mcp.json` outright (none of the JSON
entry's `env` or `args` survive).

The exception: `hooks` is additive. Lists from every layer are concatenated,
user hooks first, then project hooks, each in file order. Every other list
(`tools.allowed`, `permission.rules`, ...) is replaced by the higher layer.

The project directories are the ones the settings stack reads: inside a git
checkout, every directory from the repository root down to the directory
fluid was started in (or `--cwd`), so a session started in a subdirectory sees
the same project config, MCP servers, and hooks as one started at the root,
and a nearer directory's values win. Outside a git checkout only the start
directory counts. The home directory is never a project directory, even when
the start directory sits under it.

## Top-level keys

| Key | Default | Meaning |
|---|---|---|
| `default_provider` | `"anthropic"` | Name of the provider block to use when `--provider` is not given. |
| `default_model` | `"claude-sonnet-5"` | Model id used when the selected provider block has no `model` of its own. `FLUID_DEFAULT_MODEL` sets this key; a provider's `model` line takes precedence over it. |
| `model_override` | `null` | Highest-precedence model choice: beats a provider's `model` line. Set by `--model` or `FLUID_MODEL`; not normally written in a file. |
| `code_mode` | `false` | Expose tools through the sandboxed Python kernel (`run_python`) instead of as individual tool calls. Adds the code-mode addendum to the CLI prompt. |
| `continual` | `false` | Journal the session and synthesize reusable skills in the background. Under `code_mode` the journal records executed cells; without it, a per-tool-call journal is kept, so plain JSON sessions learn too. `--continual` turns this and `code_mode` on together. |
| `continual_model` | none | Pin skill synthesis to one model id regardless of the session's model. |
| `continual_provider` | none | A native provider name whose client runs the synthesis passes. Unset, the session's provider does. The `claude` provider has no native client, so without this it keeps the journal and serves Recall and Remember but never synthesizes. |
| `anchor_budget` | `1` | Retention budget for the harness-evolution commit gate (`fluid evolve propose`): anchor regressions a proposed change may cause and still commit. `0` = strict stability. |
| `skills_dir` | none (`~/.fluid/skills`) | Root directory for persistent code-mode skills. |
| `cwd` | `"."` | Working directory for config discovery, instruction files, tools, and the prompt's environment block. |
| `max_tokens` | none | Maximum output tokens per model call. Unset, each request gets the model's own output maximum. |
| `max_model_calls` | `200` | Maximum model calls in one turn before the turn stops with `max_calls`. Set it to `0` for no ceiling at all, leaving the provider the only authority on when a turn ends. |
| `context_window` | none | The model's context window in tokens. Unset, fluid looks the model id up in a built-in table (`fluid/providers/windows.py`): Claude models from the 4.6 generation on 1M, earlier Claude models 200K, any Claude id carrying `[1m]` 1M; `gpt-5*` 400K, `gpt-4.1*` 1,047,576, `gpt-4o*` 128K, `o1`/`o3`/`o4` 200K. An OpenRouter vendor prefix (`anthropic/…`) is stripped before the lookup. A local or unknown model stays unknown: `/context` then shows no percentage or free space and the compaction trigger falls back to the flat `trigger_tokens`. A value here, or `FLUID_CONTEXT_WINDOW`, always wins over the table. |
| `cache_key` | none | Prompt-cache affinity key sent as OpenAI Responses `prompt_cache_key` / OpenRouter `session_id`. Unset, fluid sends the session id, which is right until a conversation is resumed onto a **new** fluid session — that repartitions the provider cache exactly when the prefix is longest and most expensive to re-send. A host with its own durable conversation id sets it here; it must be stable for the life of the conversation and distinct per conversation. |
| `system` | none | Your own base system prompt. When set it replaces the shipped default (`fluid/cli/prompt.py`) entirely; the instruction stack, skills index, and environment block are still appended after it. |
| `base_prompt` | `true` | `false` runs the CLI with no base prompt at all (only instruction files, skills, agents, and the environment block). Ignored when `system` is set. |
| `identity` | none | Inline identity text — durable voice/persona injected as the base prompt's first block. Replaces only the identity block; the operating contract below it always applies (use `system` to replace everything). |
| `identity_file` | `true` | When `identity` is unset, `~/.fluid/IDENTITY.md` fills the identity slot (screened and capped). `false` ignores the file. `FLUID_IDENTITY_FILE` overrides. |
| `model_guidance` | `"auto"` | Model-family guidance blocks (act-don't-narrate; execution discipline). `auto` matches the shipped family tables (Claude-family models get none); `true` always injects both; `false` never; a list of model-id substrings replaces the tables. `FLUID_MODEL_GUIDANCE` overrides (comma-separated list allowed). |
| `setting_sources` | `["user", "project", "local"]` | Which filesystem scopes feed context and settings discovery — the Agent SDK's vocabulary. `user` is the `~/.fluid` tree (global `AGENTS.md`, skills, agents, commands, plugins, `settings.json`, `mcp.json`, `hooks.json`); `project` is the git-root→cwd walk (`AGENTS.md` files, `.fluid`/`.agents` trees, project `mcp.json`/`hooks.json`); `local` is `.fluid/settings.local.json`. An empty list runs filesystem-isolated. `config.toml` layers are never gated — they are harness configuration, and where this key itself can live. `FLUID_SETTING_SOURCES` overrides (comma list; empty = none). |

### `[permission]`

| Key | Default | Meaning |
|---|---|---|
| `approval_policy` | `"on_write"` | `never_ask` runs everything; `on_write` asks before `write` and `destructive` tool calls; `always_ask` asks before every call. Read-only `Bash` commands count as safe under `on_write` (see [cli.md](cli.md#permission-prompts)). |
| `rules` | `[]` | Explicit per-tool decisions, evaluated in order with the last match winning. A matching rule overrides the policy and any session-scoped "allow always". |
| `protect_sensitive_files` | `true` | Default ask-before-touching rules for credential files (`.env`, key files, `.netrc`, …), prepended before your rules so yours override. Skipped under `never_ask`. See [security.md](security.md). |

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
| `project` | Root `AGENTS.md` / `AGENTS.local.md` / `AGENT.md` / `AGENTS.override.md`, plus `.fluid/` and `.agents/`. See [Instruction files](#instruction-files). |
| `skills` | `SKILL.md` discovery under `~/.fluid/skills` and each project dir's `.fluid/skills` / `.agents/skills`. |
| `commands` | User slash commands under `~/.fluid/commands` and each project dir's `.fluid/commands` / `.agents/commands`. |
| `nested` | `AGENTS.md` files in subdirectories, surfaced when a tool touches that directory. |
| `plugins` | Enabled plugins from `~/.fluid/plugins` (see below) contribute skills, commands, agents, and MCP servers, namespaced `<plugin>:<name>`. |

The long spellings `discover_global`, `discover_project`, `discover_skills`,
`discover_commands`, `discover_nested`, `discover_plugins` are accepted too.
On top of these toggles, `setting_sources` decides which scopes are read at
all: dropping `"user"` removes the global tree and plugins, dropping
`"project"` removes the project walk.

### Instruction files

Each project directory (the walk from the git root down to the cwd, else the
filesystem root) contributes, in this order: `AGENTS.md`, `AGENTS.local.md`,
`AGENT.md`, `.fluid/AGENTS.md`, `.fluid/AGENTS.local.md`, `.agents/AGENTS.md`.
`AGENTS.local.md` is the gitignored personal file that layers after the shared
one, the role `CLAUDE.local.md` has in Claude Code; the settings import maps
`CLAUDE.local.md` to it. `AGENTS.override.md` is the Codex convention and does
the opposite: when present it takes `AGENTS.md`'s place in that directory
(`.fluid/AGENTS.override.md` does the same for `.fluid/AGENTS.md`); the local
file still layers after it.

Each instruction file is capped at 24,000 characters (8,000 for a nested
file); past that, the head and tail are kept and a marker names the file so
the rest can be read on demand. Codex applies a 32 KiB budget to the whole
chain instead, so a chain that fits fluid can exceed Codex's limit.

Skills (`<dir>/.fluid/skills/<name>/SKILL.md`, `<dir>/.agents/skills/...`,
`~/.fluid/skills/...`) and user commands (`<dir>/.fluid/commands/*.md`,
`<dir>/.agents/commands/*.md`, `~/.fluid/commands/*.md`) are one entry per
name: a project entry beats a global one, the directory nearest the cwd beats
an outer one, and within a directory `.fluid` beats `.agents`. Plugin entries
are namespaced `<plugin>:<name>` and never shadow a native one.

### settings.json and plugins

fluid also reads a Claude-Code-schema `settings.json` stack from its own paths
(`fluid/context/settings_stack.py`): `~/.fluid/settings.json` (user), each
project dir's `.fluid/settings.json` (project), and `.fluid/settings.local.json`
(local), merged lowest-to-highest. Provider homes (`~/.claude`, `~/.codex`) are
never read at runtime — the explicit settings import copies from them,
non-destructively. Consumed keys:

| Key | Effect |
|---|---|
| `permissions.allow` / `ask` / `deny` | `Tool(pattern)` entries become permission rules, evaluated before the `[[permission.rules]]` from `config.toml`, so under last-match-wins a TOML rule outranks a settings rule of the same shape and `deny` outranks `ask` outranks `allow` within the settings. |
| `permissions.defaultMode` | Sets `permission.approval_policy` unless a config file, `FLUID_APPROVAL_POLICY`, or `--approval-policy` does: `default` → `on_write`, `acceptEdits` → `on_write` plus allow rules for `Edit`, `Write`, `MultiEdit`, `NotebookEdit` (a settings `deny` still wins), `bypassPermissions` → `never_ask`. Any other value is ignored with a warning. |
| `env` | Layered over the process environment for the `Bash` builtin's child processes and for each MCP stdio server's spawn (under the server's own `env`). `os.environ` itself is never changed. The same map feeds `${VAR}` expansion in `mcp.json`. |
| `enabledPlugins` | Which plugins under `~/.fluid/plugins` load (below). |
| `enabledMcpjsonServers` | When non-empty, only the named servers load from a project `.fluid/mcp.json`; user `mcp.json` and TOML servers are unaffected. |
| `disabledMcpServers` | Removed from the final server map, whatever their source (config, `mcp.json`, plugin). |

`hooks` is not read from `settings.json`; put the same block in `hooks.json`
(below). `model`, `outputStyle`, and UI keys are ignored.

Plugins use the Claude Code package layout under `~/.fluid/plugins`
(`cache/<marketplace>/<plugin>/<version>` or `marketplaces/...`), enabled via
the settings stack's `enabledPlugins` map. Their skills and agents enter the
system prompt deferred — name + description only; bodies load on demand via
the `Skill` tool or at Task dispatch. The settings import can copy an enabled
Claude plugin (and its enablement entry) straight into these paths.

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
| `trim_only` | `false` | `true` runs tier 1 alone: old tool results become recoverable placeholders, but no turn is ever folded into a digest and no epoch stub is ever written. Ignored when `enabled = false`. |
| `trigger_tokens` | `120000` | Floor for the prompt size at which trimming and compaction start; also the whole trigger when the model's context window is unknown. |
| `trigger_window_ratio` | `0.6` | Fraction of a **known** context window (`context_window`) the prompt may reach before the ladder arms. The effective trigger is `min(max(trigger_tokens, window × trigger_window_ratio), window − headroom_tokens − output reserve)`, so a 200k model keeps the historical 120k trigger while a 1M model gets ~600k instead of compacting at 12% usage. Set `0` to pin the fixed trigger. |
| `headroom_tokens` | `16000` | Reserve kept free below the trigger. |
| `target_ratio` | `0.7` | Compaction aims to bring the prompt down to this fraction of the trigger. |
| `keep_recent_turns` | `3` | Turns never folded into a digest. |
| `image_trigger_ratio` | `1.0` | Fraction of the arming threshold at which delivered images are consumed (swapped for a placeholder, sibling text kept). `1.0` is the same trigger as the other rungs; lower reclaims image bytes earlier; `0` holds them until overflow recovery. Image consumption only ever happens on the first model call of a turn — a rewrite between two calls of one turn breaks a prefix the provider cached seconds earlier. |
| `max_resident_span_digests` | `20` | Span digests kept in the prompt before they fold into an epoch stub. |
| `epoch_fold_spans` | `10` | How many span digests fold into one epoch stub. |
| `intake_stub_chars` | `25000` | User text blocks longer than this render as a stub (preview + `HistoryExpand` pointer) from the very first request. |
| `tool_result_stub_chars` | `50000` | Non-error tool results longer than this render as a stub immediately — a single monster result can never overflow the window before the batch trigger fires. Errors are never stubbed. |
| `keep_recent_tool_results` | `5` | Most recent tool results that are never trimmed. |
| `min_result_chars` | `2000` | Results shorter than this are left alone by the trimmer. |
| `elide_old_args` | `false` | `true` renders the input of an old tool call whose result is already trimmed as a small JSON summary instead of the full arguments. |
| `user_turn_text_cap` | `2000` | Characters of a real user's turn-start request a digest keeps (render version 2 and later). Wake and harness notices keep the shorter built-in cap. |
| `below_threshold_skips_retry` | `true` | A decision below the trigger, or a batch that reclaimed nothing, does not arm the retry watermark, so the first call that really crosses the trigger compacts at once. `false` restores the old arming. |
| `image_rung_in_batch` | `true` | Image consumption runs inside the size batch, so a due batch is not split into two rebuilds when images alone pull the prompt under the trigger. `false` restores the old early return. |
| `deep_target_window_ratio` | `0.35` | With a known window, a full-ladder batch reclaims down to this fraction of the window, or to the `target_ratio` target when that is lower. `0` keeps the `target_ratio` target. |
| `hard_window_ratio` | `0.75` | With `hard_margin_tokens`, sets the hard ceiling: the lower of this fraction of the window and the window-protection limit minus the margin. Above it a batch must land at or under the trigger. `0` turns the hard zone off. |
| `hard_margin_tokens` | `32000` | Distance kept between the hard ceiling and the window-protection limit, sized for the largest single-call jump seen in production. |
| `shortfall_watermark` | `true` | A batch that still leaves the prompt above its target makes later mid-turn calls wait until that much more is reclaimable, so one shallow batch is not followed by another a few calls later. The hard zone and window protection ignore it. |
| `recency_tail_tokens` | `40000` | With a known window, the newest history up to this many tokens (or `recency_tail_window_ratio` of the window, whichever is smaller) stays verbatim. `0` restores the turn and result counts. |
| `recency_tail_window_ratio` | `0.15` | Window fraction that caps the verbatim recency tail. |
| `recency_tail_calls` | `2` | The newest model calls that always stay verbatim, whatever their size. |
| `turn_budget_window_ratio` | `0.2` | When the turn in flight holds more than this fraction of the window and the prompt is past `turn_budget_floor_ratio`, the guarded rung trims that turn's results older than the recency tail. `0` turns the rung off. |
| `turn_budget_floor_ratio` | `0.45` | Window fraction the prompt must reach before the turn-budget rung may run. |
| `window_floor_waiver_ratio` | `0.9` | At or above this fraction of the window, a batch commits whenever it frees `window_floor_waiver_tokens` or more, instead of needing the usual 5% reclaim. `0` keeps the 5% floor everywhere. |
| `window_floor_waiver_tokens` | `2000` | Smallest reclaim that commits near the window. |
| `wake_aware_turns` | `true` | Turns started by an unpark, a wakeup or a peer message do not count toward `keep_recent_turns`, so a run of wakes cannot push a real user turn out of the protected set. |
| `cache_schedule` | `true` | When the engine reports the cache state, rewrites are priced against it: below the opportunistic floor nothing is built, under the trigger a batch commits only on a cold cache, and over the trigger a warm rewrite waits until deferring costs as much as rebuilding. The hard zone and window protection always commit. |
| `opportunistic_floor_window_ratio` | `0.45` | Window fraction below which the cache-priced schedule never builds a batch. |
| `sweep_mode` | `"shadow"` | The turn-boundary sweep of old tool results and already-seen images. `"off"` plans nothing, `"shadow"` logs what it would reclaim without changing the prompt, `"on"` commits it. |
| `sweep_keep_tokens` | `20000` | The newest results of earlier turns, up to this many tokens, that the sweep leaves alone. |
| `sweep_pin_tokens` | `16000` | Results the model fetched with `HistoryExpand` in the turn that just ended stay pinned for the next turn, newest first, up to this many tokens. |
| `sweep_min_reclaim_tokens` | `2000` | Smallest reclaim for which a sweep commits on its own on a cold cache. |
| `render_version` | current (`2`) | Renderer version new compaction decisions use. Stored decisions keep the version they were made under, so changing this never re-renders an old session. `1` is the original renderer. |
| `steer_receipt_chars` | `1000` | From render version 2, a harness notice at least this long renders, once compacted, as a short receipt with a `HistoryExpand` ref. User and peer messages stay verbatim. |
| `steer_receipt_preview_chars` | `300` | Characters of the notice that the receipt quotes. |
| `intent_pin_tokens` | `4000` | From render version 2, epoch stubs keep the folded turns' user requests verbatim up to this many tokens. |

A trimmed result is replaced by a placeholder that states how many characters
were removed, quotes the first 160 characters of the original, and carries a
`node_...` ref that `HistoryExpand` or `HistorySearch` can recover. When those
two builtins are off the session's surface (a `tools.allowed` list without
them, or an embedding host that never wired recall), the placeholders, digests
and stubs say instead that the content is not recoverable in this session and
name the way back that exists: re-running the tool, or asking the user.

An **image** result gets a different placeholder, because recall is a text path
and cannot put an image back: it says so outright and names the re-Read path
from the result's own `[image: <path>, …]` caption. Images are consumed once —
a later assistant node is the evidence one model call already saw them — and
that trim now reports itself as a `ContextCompaction` event with
`reason="image_consumed"`, so an image leaving the context is visible in the
transcript instead of silent. `HistoryExpand` on a node that carried an image
says the same thing rather than returning the caption alone.

`trim_only = true` is the embedding contract for host apps that want the
context saving without the model ever reading a machine summary of its own
history. In library terms it is
`CompactionConfig(enabled=False, trim=TrimConfig(enabled=True))`, and it means
exactly: tier-1 trim runs on its own trigger, oversize payloads still render as
stubs (a stateless size policy, not a tier), tiers 2 and 3 never run — not on
the trigger, not on an overflow escalation, and not from promotion decisions
rehydrated from an earlier session. The stored tree keeps full fidelity either
way.

### Memory

There is no `[memory]` table and no model-curated memory file. Declarative
memory belongs to the continual learner (`continual = true`): its synthesizer
distills remembered facts from session journals into
`<skills_dir>/memories.json` alongside the skills, and each session's prompt
carries a frozen `# Memory` snapshot of that list. Every entry is screened for
prompt-injection patterns and credentials. Episodic recall is the session
store: `SessionSearch` lists and searches past sessions, `SessionView` opens
one.

The store holds three tiers, each with its own live cap. A save is never
refused for lack of room: when a tier is full, its least useful entry moves to
the searchable archive. Usefulness is the entry's last activity (an update or
a `Recall` hit) plus seven days of credit for each recall, up to ten.

| Tier | Written by | Lifetime | Live cap |
|---|---|---|---|
| user | `Remember` with `user_requested: true` (the user asked, or corrected a fact) | Permanent. Only the user can change it. Consolidation can't retire it. | 256 (archive keeps up to 256 more) |
| agent | `Remember` without `user_requested` | 120 days from the last recall or re-save. Consolidation can merge or retire it. | 120 |
| learned | Synthesizer, from session journals | 90 days from the last recall | 40 |

A non-user fact that names a branch or worktree, such as `fix/foo` or
`.fluid/worktrees/foo`, lives 14 days. The non-user archive keeps up to 200
entries and trims learned facts first. Consolidation runs every two days. Each
pass reviews one section of the store, and the next pass starts where it left
off. Synthesis can't spend the last two daily inference calls, so
consolidation keeps a share of the budget.

Stores written before the tiers existed are migrated in place. An old
protected entry stays a user fact if its key or text shows that it records the
user (for example `profile.*` or `The user prefers…`). Any other old protected
entry becomes an agent fact. It gets at least 30 days of grace, or 7 days if it
names a branch or worktree.

### `[[hooks]]`

Shell commands run on lifecycle events: around tool calls, at turn and session
boundaries, before compaction, when a subagent stops, and on notifications. Full
contract (stdin payload, exit codes, `updated_input`) in [hooks.md](hooks.md).

| Key | Required | Default | Meaning |
|---|---|---|---|
| `event` | yes | | `PreToolUse`, `PostToolUse`, `TurnStart`, `TurnEnd`, `SessionStart`, `SessionEnd`, `PreCompact`, `SubagentStop`, or `Notification`. Claude Code's `UserPromptSubmit` and `Stop` are accepted as `TurnStart` and `TurnEnd`. |
| `command` | yes | | Shell string, run through the same shell as the `Bash` tool with `cwd` as its working directory. |
| `matcher` | no | every tool | Tool-name filter for tool events: a glob, or a regex when it contains `^`, `|`, or `(`. |
| `timeout` | no | `60` | Seconds before the hook is killed and treated as allow. |

```toml
[[hooks]]
event = "PostToolUse"
matcher = "Edit|Write"
command = "ruff format --quiet ."
```

`~/.fluid/hooks.json` and each project directory's `.fluid/hooks.json` accept
the Claude Code `settings.json` hooks shape; `UserPromptSubmit` maps to
`TurnStart` and `Stop` to `TurnEnd`. The settings import copies the `hooks`
block of `~/.claude/settings.json` and a project's `.claude/settings.json` /
`.claude/settings.local.json` into these files (merging per event when the
file exists).

### `[providers.<name>]`

Every provider block has the same keys; `kind` decides which client is built.

| Key | Default | Meaning |
|---|---|---|
| `kind` | `"anthropic"` | `anthropic`, `openai_responses`, `openrouter`, `local`, or `delegated_claude`. |
| `model` | `""` | Model id for this provider. Empty falls back to `default_model`. |
| `base_url` | none | Override the endpoint. Required for `local`. |
| `api_key_env` | none | Environment variable holding the API key. The Anthropic, OpenAI, and OpenRouter clients also fall back to `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `OPENROUTER_API_KEY` respectively. |
| `tool_style` | `"native"` | `text` switches to the `<tool_call>` text protocol for models without reliable native tool calling. See [local-models.md](local-models.md). |
| `stream_deadline` | `900` (seconds) | Wall clock for one whole model call. The HTTP timeout is per read, so a provider that keeps emitting bytes — a degenerate generation, an upstream streaming padding — resets it forever; this bounds the call itself and surfaces as a retriable error. `0` disables it. |
| `first_byte_timeout` | `60` (seconds; off for `local`) | Every native kind. How long a call may wait for its first byte (the response headers, then the first SSE event) before it is abandoned. Headers that never arrive are retried like a read timeout (on the transport-retry budget, which is 0 for `openrouter` by default); a `200` followed by silence is not re-sent by the client (`anthropic` reports it as a transport failure; `openrouter`, `local` and `openai_responses` as a truncated stream, which the engine retries as an empty response). The chat/completions kinds also keep a fixed 30 s wait for the first data event after the headers. Raise it for a gateway that sends nothing until the upstream model's first token; `0` disables it. |
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

Because the child CLI resolves its own tool surface — from its settings files,
its MCP config, and the tool flags Fluid passes it — the delegated engine does
not know what that child can call until the CLI says so. It says so in the
`init` frame it writes at the start of every turn. Fluid reads the announced
`tools` and `mcp_servers` from that frame, keeps them on the loop as
`cli_tools` and `cli_mcp_servers` (both `None` until an `init` frame carries a
tool list), and publishes them on the event stream as a `DelegatedSurface`
event with the same two tuples. The event also records what the CLI said about
itself (`cli_version`, `permission_mode`, `model`, `api_key_source`), its own
session id (`cli_session_id`), and the environment Fluid forced on it
(`child_env_overrides`). It fires once per turn and is purely informational:
nothing in the turn depends on it, and it never reaches the model. Subscribers
use it to show, log, or check the tools the child actually has and which CLI
build served the session.

What the child CLI is given, and what it is not:

- The system prompt is Fluid's, compiled for that leg: the native-only
  builtins (`HistoryExpand`, `HistorySearch`, `ToolSearch`) are left out, since the bundled bridge cannot serve them,
  and the prompt tells the model that Fluid's tools appear under the
  `mcp__fluid_bundled__` prefix. `code_mode` is ignored on this leg, with a
  note on stderr: the CLI runs its own tool loop and has no `run_python`.
- MCP servers come only from the config Fluid writes: the bundled bridge plus
  every `[mcp.<name>]` entry (rewritten into the CLI's `.mcp.json` shape).
  The CLI is started with `--strict-mcp-config`, so servers named in its own
  settings files are not connected.
- The `--resume` handle is kept per session. A handle the CLI rejects (its
  own store was pruned, or the id came from another machine) is dropped, the
  CLI restarted without it, and the turn replayed once into a fresh CLI
  conversation; the new handle is what gets stored.
- A result the CLI marks as failed ends the turn with an error, not a
  completion. The one exception is `error_max_turns`, a stop reason.
- `continual = true` journals the turn and every tool result the CLI reports,
  and serves `Recall` and `Remember` over the bridge. Synthesis passes need a
  native provider, named with `continual_provider`.
- An `allow_always` answer to a permission prompt is remembered for the
  session (scoped the way the native engine scopes it), and the CLI's own
  `permission_suggestions` are echoed back so it stops asking too.
- The system prompt text rides a file (`--append-system-prompt-file`, or
  `--system-prompt-file` for a replacing prompt) in the session's temp
  directory, not the command line, which Windows caps at 32,767 characters.
  Only a CLI known to be older than 2.1.0 gets the text inline.
- Every spawn passes `--allow-dangerously-skip-permissions`, so a session
  started in another mode can switch to `bypassPermissions` later. It does not
  turn Bypass on. The flag is left off when the CLI would run as root outside
  a sandbox (POSIX uid 0 with neither `IS_SANDBOX=1` nor
  `CLAUDE_CODE_BUBBLEWRAP` in the child env), because the CLI exits on it
  there; such a session cannot switch to Bypass later.
- The child always runs with `CLAUDE_CODE_THRIFTY_SONIC=0` (turns off the
  CLI's "do your work through Bash" steer in bypass and auto modes) and
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, in its process env and again in
  the `env` block of `--settings`, so neither the host env nor a user, project
  or local settings file can change them. A managed policy settings file is
  applied after `--settings` and still can. After each CLI process's first turn, Fluid checks the CLI's
  transcript for a bash-first steer it attached anyway; a hit is logged as a
  warning and stored as a `DelegatedDiagnostic` event.
- On Claude Code 2.1.287 and newer the child also loads AI Shuffle's Claude
  Code mod (`fluid/engine/claude_mod`), a plugin of function hooks, which is
  Claude Code's documented "mods" format, passed with `--plugin-dir`. It leaves
  the bash-first steer (the `auto_mode` reminder) out of what the model reads,
  and when the CLI has trimmed the Bash tool description it adds a line
  pointing file reads, searches and edits back to Read, Grep, Glob, Edit and
  Write. The CLI writes type files into any folder it loads a mod from, so
  each session loads a copy in its temp directory, removed when the session
  closes. An older CLI, or one whose version is unknown, gets no flag, and
  `FLUID_CLAUDE_MOD=false` turns the mod off. The override above stays on
  either way: with it, 2.1.287 sends the same request with or without the mod,
  and the mod takes over if a later CLI ignores the variable. A CLI that
  cannot load the mod still runs the turn; the init frame's plugin list says
  whether it loaded, and a steer the transcript check finds says whether the
  mod kept it from the model. `--setting-sources` does not affect the mod.
- A `tool_policy` that raises (or returns an awaitable that raises) denies the
  call.

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

`--model` (or `FLUID_MODEL`) overrides the block's `model` for one run without
editing the file.

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
| `headers` | http: a table of request headers sent with every call (an `Authorization` bearer token, say). |
| `connect_timeout` | Seconds allowed for the transport to open and the handshake to finish (default 30). A server that misses it is reported as a non-fatal error and the session runs without it. |
| `call_timeout` | Seconds allowed for one tool call, and for any other request to the server (default 60). A call that misses it returns an error result to the model; the connection stays up. The engine's registry adds its own 75 s ceiling around every MCP and in-process call so a wedged transport can never hold a turn. |
| `trust_annotations` | `true` lets a tool's own `readOnlyHint` count: the tool runs without a prompt under `on_write`. Off by default, since the hint is the server's word about itself; a `destructiveHint` always counts. |
| `shutdown_timeout` | Seconds to wait for the server to stop at session end before it is abandoned (default 5). |

```toml
[mcp.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
call_timeout = 30

[mcp.remote]
transport = "http"
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ..." }
```

`~/.fluid/mcp.json` and each project directory's `.fluid/mcp.json` accept the
same servers in Claude Code's JSON shape, with or without the
`{"mcpServers": {...}}` envelope: `type: "http"` becomes the streamable HTTP
transport, `type: "sse"` is skipped with a warning (fluid has no SSE
transport), and an entry with `disabled: true` or `enabled: false` is left
out. `${VAR}` tokens anywhere in an entry expand from the process environment
overlaid with the settings stack's `env` (an unset variable expands to the
empty string), and `${CLAUDE_PROJECT_DIR}` expands to the cwd — so the
`${SERVER_KEY}` placeholders the settings import writes in place of
credentials resolve once the variable is set. MCP support needs the `mcp`
extra (`uv sync --extra mcp`), which installs `mcp>=2.0`.

## Environment variables

Config overrides, applied as layer 6 (`env_overrides` in the loader):

| Variable | Sets |
|---|---|
| `FLUID_DEFAULT_PROVIDER` | `default_provider` |
| `FLUID_DEFAULT_MODEL` | `default_model` |
| `FLUID_MODEL` | `model_override` (beats a provider's `model` line) |
| `FLUID_CWD` | `cwd` |
| `FLUID_CODE_MODE` | `code_mode` (`1`, `true`, `yes`, `on` are true) |
| `FLUID_BASE_PROMPT` | `base_prompt` (same boolean spellings) |
| `FLUID_IDENTITY_FILE` | `identity_file` (same boolean spellings) |
| `FLUID_MODEL_GUIDANCE` | `model_guidance` (`auto`/`true`/`false`, or a comma-separated substring list) |
| `FLUID_SKILLS_DIR` | `skills_dir` |
| `FLUID_SETTING_SOURCES` | `setting_sources` (comma list of `user`/`project`/`local`; empty = none) |
| `FLUID_CONTINUAL` | `continual` |
| `FLUID_CONTINUAL_MODEL` | `continual_model` |
| `FLUID_CONTINUAL_PROVIDER` | `continual_provider` |
| `FLUID_ANCHOR_BUDGET` | `anchor_budget` (ignored if not an integer) |
| `FLUID_MAX_TOKENS` | `max_tokens` (ignored if not an integer) |
| `FLUID_MAX_CALLS` | `max_model_calls` (ignored if not an integer) |
| `FLUID_CONTEXT_WINDOW` | `context_window` (ignored if not an integer) |
| `FLUID_APPROVAL_POLICY` | `permission.approval_policy` |
| `FLUID_CONTEXT_ENABLED` | `context.enabled` |
| `FLUID_CONTEXT_TRIGGER_TOKENS` | `context.trigger_tokens` |
| `FLUID_CONTEXT_TRIGGER_WINDOW_RATIO` | `context.trigger_window_ratio` (ignored if not a float) |
| `FLUID_CONTEXT_HEADROOM_TOKENS` | `context.headroom_tokens` |

Read outside the config loader:

| Variable | Effect |
|---|---|
| `FLUID_SHELL` | Windows only: path to the shell for `Bash`, `Monitor`, and hook commands. Otherwise fluid finds Git Bash itself and falls back to `cmd.exe`. |
| `FLUID_TRACE_REQUESTS` | Directory that receives a JSON dump of every outbound model request. Unset means no tracing. |
| `FLUID_CLAUDE_MOD` | `delegated_claude` only: whether the child CLI loads AI Shuffle's Claude Code mod (see the provider section). Unset means on; otherwise `1`, `true`, `yes`, `on` are on and anything else is off. |
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

Three things keep a cell's output honest:

- **Result handles.** A tool result over 8 KB (`KernelConfig.ref_threshold`)
  comes back as a `ResultRef` — a `str` subclass whose `repr` is a preview, so
  the payload stays in the kernel and the model slices it there. Every ref is
  filed in the namespace's `_refs` registry under a short id its `repr` prints
  (`<ResultRef #7: 55123 bytes; …>`), so a handle the cell forgot to assign is
  recovered with `_refs[7]` instead of re-running the tool. The registry keeps
  the 50 most recent refs; the previous cell's displayed value stays bound to
  `_`, Python-REPL style. Both survive across cells. A kernel restart rebuilds
  the registry from the refs the checkpoint restored — a ref that was only ever
  reachable through `_refs` does not come back.
- **Fair-share stdout.** A cell's stdout is capped at 16384 chars
  (`KernelConfig.stdout_cap`). The budget is divided across the cell's prints
  rather than kept from the front, so the summary a model prints *last* still
  arrives; the closing notice names the totals, the budget, the heaviest drops,
  and the `_refs` handles still holding this cell's full tool results, so the
  recovery is a slice rather than a re-run
  (`…[stdout truncated: kept 16384 of 139201 chars across 13 prints — one
  cell's stdout budget is 16,384 chars; heaviest drops: print 11 (-48200),
  print 12 (-31005). This cell's full tool output is held in the kernel:
  _refs[4] (55,123 bytes) — slice it instead of re-running the tools or
  re-printing.]`). The cap is stated in `run_python`'s own description.
- **Committed-call manifest.** A tool error aborts the whole cell, so a cell
  that edited three files before failing would otherwise show only a traceback.
  The result appends the calls that already ran, in order, with their targets
  and outcomes, and says plainly that completed Edits and Writes are already on
  disk and must not be re-issued.
