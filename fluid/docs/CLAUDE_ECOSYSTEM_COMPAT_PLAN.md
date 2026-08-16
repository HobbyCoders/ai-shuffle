# Claude-Ecosystem Compatibility Plan (fluid)

Status: RULINGS FINAL 2026-08-16 (Fable). Implementation in waves below.
Goal: a fluid native-leg session picks up a user's existing Claude Code setup
(CLAUDE.md @-imports, settings.json, plugins, skills, agents) and codex-style
AGENTS.md conventions with ZERO conversion, renaming, or host-app wiring.
A Codex or Claude Code user switches to fluid seamlessly.

Context sources for this plan:
- Extension-point map of this repo (instructions.py / compiler.py / task.py /
  mcp / config / tests) — researched 2026-08-16, line refs below match HEAD
  a6957a1 + dirty working tree.
- Claude Code on-disk format spec (docs + live inspection of ~/.claude on the
  primary machine) — see "FORMAT REFERENCE" at bottom.

## FINAL RULINGS

- CE1 — Discovery lives in fluid, filesystem-native. No host-app injection
  required for any of this to work; host apps can only *disable* pieces via
  config toggles. Rationale: seamless-switch goal; ai-shuffle's native leg
  gets everything for free on vendor sync.
- CE2 — @-import expansion: `@path` tokens at line start or preceded by
  whitespace, OUTSIDE fenced code blocks and inline code spans, expand to the
  referenced file's content. Resolution: relative to the *containing file's
  directory*; `~/` and absolute paths supported. Max depth 5 hops; cycles cut
  silently; unreadable target keeps the literal line (no error). Expansion
  happens where content becomes `InstructionFile.content` (`_files_in_dir`,
  `discover_global_instruction_files`) — NOT inside `_read` (skills/commands
  reads must stay raw). Dedup in `build_instruction_blocks` compares
  expanded content — accepted.
- CE3 — `CLAUDE.local.md` joins per-directory candidates, ordered after
  CLAUDE.md variants: `_CLAUDE_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md",
  "CLAUDE.local.md")`. Also honor codex `AGENTS.override.md` (before
  AGENTS.md in its family, override = replaces AGENTS.md in same dir).
- CE4 — settings.json: new module `fluid/context/claude_settings.py`. Reads
  user `~/.claude/settings.json`, project `.claude/settings.json`, local
  `.claude/settings.local.json` (walk: git root → cwd; nearest wins for
  scalars). Merge: arrays accumulate, scalars override (local > project >
  user). v1 CONSUMES: `enabledPlugins`, `permissions.{allow,deny,ask,
  defaultMode}`, `env`, `disabledMcpServers`, `enabledMcpjsonServers`.
  v1 does NOT consume: hooks (follow-up wave), model/outputStyle/UI keys.
  Permission strings `Tool(pattern)` parse into fluid `Rule`s; synthesize in
  order allow → ask → deny so fluid's last-match-wins yields Claude's
  deny > ask > allow. Bare `Tool` (no parens) = tool-wide rule.
  `permissions.defaultMode` maps via the existing SetPermissionMode table.
  `env` is applied to Bash builtin child env + MCP stdio spawn env (never
  os.environ mutation).
- CE5 — Plugins: new module `fluid/context/plugins.py`. Enabled set comes
  from merged `enabledPlugins` (`name@marketplace`: true). Resolution order:
  `~/.claude/plugins/cache/<marketplace>/<name>/<version>/` (highest semver)
  else `~/.claude/plugins/marketplaces/<marketplace>/<name>/` (marketplace
  clone subdir; also check repo root when the marketplace IS the plugin).
  Read `.claude-plugin/plugin.json` (fallback `plugin.json`), honoring its
  component path overrides. v1 surfaces: skills (`skills/*/SKILL.md`),
  commands (`commands/*.md`), agents (`agents/**/*.md`), MCP servers
  (`.mcp.json` or `mcpServers` key, with `${CLAUDE_PLUGIN_ROOT}` +
  `${CLAUDE_PROJECT_DIR}` + `${ENVVAR}` substitution; sse transport skipped
  with a warning). Namespacing: `<plugin>:<name>` for skills/commands/agents.
  Out of scope v1: LSP servers, themes, output styles, plugin hooks, plugin
  settings.json, userConfig, dependencies (log-and-ignore).
- CE6 — Agents: `AgentDefinition` frozen dataclass (name, description,
  model, tools tuple, path, scope, body/system prompt, raw frontmatter dict).
  `discover_agents(cwd)`: project `.claude/agents/**/*.md` (nested; nearest
  dir to cwd wins name collisions), then `~/.claude/agents/**/*.md`, then
  plugin agents (namespaced, so no collision). `parse_frontmatter` gains
  minimal list support: `[a, b]` inline arrays and comma-split for `tools:`.
  Surfacing: new compiler section kind `"agents"` (index like skills_index,
  header explains Task-tool usage) AND definitions injected to
  `ctx.extra["agent_registry"]` + `NativeLoop(agents=...)` for usage rows.
  Dispatch stays host-app territory (task.py doctrine unchanged); ai-shuffle
  wires its dispatcher in W5.
- CE7 — Cache stability: all new indexes sorted by name (deterministic).
  Section order becomes base → memory → skills → agents → environment →
  extra. `SECTION_KINDS` gains "agents"; `fluid/context/usage.py` category
  mapping updated so the new kind lands in the system-prompt bucket (grep
  `_SYSTEM_KINDS`). One-time cached-prefix churn accepted.
- CE8 — Provider-agnostic: every discovery soft-fails when ~/.claude or
  ~/.codex is absent; AGENTS.md family support retained and extended per CE3.
  Nothing here may assume the Claude provider — this layer is provider-blind.
- CE9 — Config: extend `InstructionFilesConfig` pattern with toggles
  `claude_settings`, `plugins`, `agents` (all default ON), threaded through
  BUILTIN_DEFAULTS → model.py dataclass → HarnessConfig → PromptInputs, plus
  FLUID_* env overrides, mirroring the existing three-touch pattern.
- CE10 — The dirty working tree (context-usage parity: usage.py, base.py,
  native.py, test_context_usage.py) is committed FIRST as its own commit
  before any wave lands. Vendor snapshot already contains it.
- CE11 — Never crash session assembly: malformed JSON/frontmatter/manifest →
  skip that unit, `logging` warning. No exceptions escape discovery.

## WAVES

- W0 (housekeeping): commit dirty context-usage changes verbatim.
- W1: @-imports (CE2) + CLAUDE.local.md / AGENTS.override.md (CE3).
  Files: fluid/context/instructions.py, tests/unit/test_instructions.py.
- W2: claude_settings.py (CE4) + permission-string parser + config toggles
  (CE9). Files: fluid/context/claude_settings.py (new),
  fluid/permissions/rules.py (parser helper only if needed),
  fluid/config/{loader,model}.py, fluid/tools/builtins/bash.py (env thread),
  fluid/tools/mcp/client.py (env merge), tests.
- W3: plugins.py (CE5): discovery + merge into discover_skills /
  discover_commands (scope="plugin", namespaced) + plugin MCP merge into
  NativeLoop lazy-connect path + Skill tool resolution (works via
  discover_skills for free) + settings gating via W2. Tests incl. fixture
  plugin tree.
- W4: agents (CE6, CE7): AgentDefinition + discover_agents + frontmatter
  list support + compiler "agents" section + usage.py mapping +
  ctx.extra["agent_registry"] + NativeLoop(agents=...) auto-population.
- W5 (ai-shuffle repo): rsync vendor sync, app-side: task_dispatch accepts
  file/plugin agent definitions from the registry seam; dedupe app-injected
  MCP servers vs fluid-discovered plugin servers (name-keyed, app wins);
  drop harness_adapter's silent-discard docstring lines that no longer hold;
  PR + real-app E2E (verification bar).

## ACCEPTANCE (per wave)

- Full `pytest` green in omni-harness after each wave.
- W1: imports expand (relative/`~`/absolute), depth-5 cut, cycle-safe, code
  fences and inline spans inert, unreadable literal kept, CLAUDE.local.md +
  AGENTS.override.md ordering tests.
- W2: three-scope merge test, `Tool(pattern)` → Rule mapping test proving
  deny > ask > allow, env threading test, toggles-off tests.
- W3: fixture plugin (skills+commands+agents+.mcp.json) discovered only when
  enabled in settings fixture; namespacing; ${CLAUDE_PLUGIN_ROOT} substitution;
  sse skip warning; disabled plugin invisible.
- W4: agents index in compiled prompt (sorted, after skills, before env);
  nearest-wins collision test; tools comma-split; registry seam populated.
- W5: E2E in the real app — native-leg session shows plugin skills in its
  skills index, Skill tool loads a plugin skill body, @-import content from
  ~/.claude/communication-style.md present in system prompt, agent from
  .claude/agents spawnable via Task.

## FORMAT REFERENCE (compatibility spec digest)

- @-imports: `@path` outside code fences/spans; relative to containing file;
  `~/` ok; recursion-capped; cycles graceful. CLAUDE.local.md loads beside
  CLAUDE.md, gitignored.
- settings scopes low→high: user ~/.claude/settings.json → project
  .claude/settings.json → local .claude/settings.local.json. Arrays merge,
  scalars override. Permission rule strings: `Bash(npm run *)`,
  `Read(./src/**)`, bare `mcp__pencil`.
- enabledPlugins: `{"<plugin>@<marketplace>": true|false}` in any scope.
- Installed plugin layout: `~/.claude/plugins/cache/<mp>/<plugin>/<ver>/`
  (has `.claude-plugin/plugin.json`) and `~/.claude/plugins/marketplaces/<mp>/`
  (git clones). Components: `skills/<n>/SKILL.md`, `commands/<n>.md`,
  `agents/<n>.md`, `.mcp.json`. plugin.json may override component paths
  (skills ADDS, commands/agents REPLACE; paths start `./`).
- SKILL.md frontmatter: name, description (+optional tags, argument-hint,
  allowed-tools, disable-model-invocation, user-invocable).
- Agent .md frontmatter: name, description (+optional model, tools,
  disallowedTools, permissionMode, maxTurns, skills, memory, background,
  effort, isolation, color). Body = system prompt.
- .mcp.json: `{"mcpServers": {name: {command,args,env} | {type:"http"|"sse",
  url, headers}}}`; substitutions ${CLAUDE_PLUGIN_ROOT}, ${CLAUDE_PROJECT_DIR},
  ${VAR}.
- Codex: ~/.codex/AGENTS.md (+AGENTS.override.md), AGENTS.md walk-up;
  no skills/plugins/agents equivalents — no extra work needed beyond CE3.
