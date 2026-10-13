# Handoff — finish the 2026-09-01 audit fixes

For a fresh session. A full code review of fluid at `eb84ce7` produced 146
verified findings; eleven fix batches have landed as commits on branch
`claude/fluid-codebase-review-7f7cd2`, worked in the git worktree
`.claude/worktrees/readme-rewrite-review-cef785`. This document says what
was done, what was left out, and how the work has been run, so a later
session can pick up the remaining items without the conversation.

The full report, with every finding, its evidence and its fix, is the
artifact **Fluid Harness Audit**:
https://claude.ai/code/artifact/143fefe8-c896-4d4a-9e5f-6923510657ec
Finding ids below (CFG-, PRM-, CL-, CTX-, ENG-, STO-) refer to it. Line
numbers in the report are from `eb84ce7`; grep for the symbols, the files
have moved.

## State of the branch

Base `eb84ce7`, then eight commits, each green on its own:

| Commit | Batch | Suite |
|---|---|---|
| `a2bc2f3` | 1 security: Bash classifier, rule matching, skill-name traversal, bridge token and gate, wake defang, kernel audit hook | 1606 |
| `918409b` | 2 data safety: store locking, atomic appends, tolerant parse, atomic memory files, event schema version, seq seeding | 1651 |
| `a69bc17` | 3 infinite sessions: overflow ladder guard-off rungs, estimator, 413, window table, accounting, pricing | 1719 |
| `0499b07` | 4 daily driver: Ctrl-C, stop-reason dispatch, wake retry, MCP timeouts and http transport, tool-name sanitizing, YAML block scalars | 1749 |
| `3749765` | 5 port fidelity: Claude Code hook contract and nine events, lossless importer, settings.json wiring, config walk-up, precedence | 1805 |
| `58412fc` | 6 Claude-CLI leg: delegated prompt surface and prefix note, code-mode addendum gate, 16 MiB stream and bridge line limit, failed results as errors, dead `--resume` handle dropped and the turn replayed, strict MCP config with `[mcp.*]` forwarded, error guard over the whole turn, interrupt-aware permission wait with sticky `allow_always`, stdin-close then tree kill on close, continual journal plus Recall and Remember on the leg (`continual_provider` for synthesis), hook stop, permission bypass and PostToolUse feedback on both bridges | 1824 |
| `8887f24` | 7 continual learning: session and agent stamps on journal entries with per-session prompt separators, one synthesizer per store (children share the parent's) and a cross-process synthesis lock, anchor requirement on the commit gate with `--allow-no-anchors` waiver, `.bak` plus diff on guidance apply, memory provenance with tool-only facts held out of the prompt and surfaced on `ContinualUpdate`, denial inputs journaled, live-skill cap and a per-pass skill source budget, oversized skill files flagged, synthesis usage recorded, mount verification budget, audit guard covering skill frames, README and design-doc wording | 1843 |
| `3d5506a` | 8 prompts: plan-mode texts name the real removed set, recovery-free placeholders and digests when the recall builtins are off the surface, outage told apart from denial, code-mode-contradicting bullets gated, `from tools import` spelling, capability-backed prose gated on what the host wires, Monitor hedge and index and environment wording, addendum folded into the base ahead of the environment block, Skill miss list honouring `setting_sources`, golden snapshots of the base prompt and the addendum | 1856 |
| `b73aee9` | 9 providers: `response.incomplete` handled with the cut-off block closed and `max_tokens` reported, `max_output_tokens` sent, refusal deltas as text, content filter before output as an error; retry policy with separate status and transport budgets, jitter, Retry-After dates, OpenRouter retrying 429 and 5xx, pre-content in-stream errors and Anthropic `overloaded_error` retried with `message_start` held back; chat-completions tool calls keyed by id when `index` is absent; text-tool markers escaped in results and arguments; Anthropic empty text and unsigned thinking dropped, same-role neighbours folded, redacted thinking and server tool blocks kept as opaque envelopes; Fable 5.1 cache-read rate; Codex token store atomic and owner-only with a cross-process refresh lock, lock helper moved to `fluid/_filelock.py` | 1887 |
| `0eb98d7` | 10 engine mediums: Bash stdin closed, output capped while streaming, timeout clamped; allow-always labels for wrappers and subcommand programs, unscopable calls one-shot; subagent surface inside the parent's; serving monitors excluded from `busy` and `wait_idle`; MCP `readOnlyHint` honoured only under `trust_annotations`; catalog descriptions defanged; kernel restart on an interrupted cell, `SystemExit` kept in the cell, stderr drained with a death diagnostic, checkpoint budget in the child, host paths scrubbed from tracebacks, POSIX process group; stdout budget ellipsis paid from its share | 1910 |
| `59c68ed` | 11 lows: registration item as diagnostic, plugin path dedup, `CLAUDE_PLUGIN_ROOT` exported, instruction cap documented, synthesis cursor over corrupt lines, memory order after eviction, cache-probe rotation on empty retries, store session cache bounded, `/context` after resume, "chars truncated", `.env.example` allowed, deltas without `block_start`, MCP resource/audio/structured results, `tools/list_changed`, question timeout, bus close generation | 1920 |

Working tree is clean. Every batch in the fix order has landed; what was
left out is listed under "Left out" below. Run the suite with:

```bash
uv run --extra dev pytest tests/unit -q -p no:cacheprovider
```

Expect 1920 passed, 4 skipped, about 80 s on Windows.

## How the work has been run

These are the owner's standing rules for this campaign; keep them.

- **The main agent does the work itself.** Batches 1 to 5 were delegated to
  pairs of code subagents; from batch 6 on, do not. Read the files, make the
  edits, write the tests, run the suite, all in the main session. A `haiku`
  explore agent is fine for a broad read-only search when the answer is a
  location rather than a judgment, but no subagent edits code.
- **Work one batch at a time, in the order below,** and within a batch one
  finding at a time: read the code around it, make the smallest correct
  change, add a regression test in the existing module for that area, run
  that module plus the neighbours it touches, then move on. Run the full
  suite once per batch before reporting.
- **Re-verify before reporting.** Re-run the audit's original reproduction
  for each finding against the fixed code (the report gives the exact input
  and the wrong output); say plainly what could not be done and why.
- **Commit only when the owner says so**, one commit per batch, plain commit
  messages, no attribution lines. Doing the work in one session removes the
  need to split shared files by hunk, so finish a batch and commit it before
  starting the next; do not let two batches accumulate in the tree.
- **CRLF.** `core.autocrlf=true`; the checkout is CRLF. A whole-file rewrite
  can come out LF; harmless for the index, but prefer targeted edits.
- **Prose.** The owner's global CLAUDE.md loads the `unnslop` skill: plain,
  specific wording in comments, docstrings, docs, commit text.
- **Pyright noise.** The IDE reports stale "unknown import symbol" errors
  for symbols added in the same session and pre-existing `Mixin` attribute
  complaints. Trust pytest, not those.

## What was done, in order

Every batch below has landed. The sections record the choices each one
made; "Left out" at the end lists the items that were not done and why.

### Batch 6 — the Claude-CLI leg (`provider = claude`) — done, `58412fc`

Everything below landed in the working tree (see the table). Choices made:
PRM-1 was fixed by intersecting the prompt surface (a `NATIVE_ONLY_TOOL_NAMES`
literal, checked against the bridge's real listing by a test) plus one bullet
about the `mcp__fluid_bundled__` prefix, not by renaming every mention; the
CLI's own Read/Bash keep their plain names, so a wholesale rename would have
been wrong. `error_max_turns` still completes the turn; every other
`is_error` result is an ErrorEvent. Continual synthesis on this leg needs a
native client, so a `continual_provider` config key was added; without it
the journal, Recall and Remember work and synthesis does not. The bridge's
`ask` verdict is still the non-interactive denial (nobody on the socket to
ask); routing it through the loop's PermissionRequest is not done. One
pre-existing ruff E731 in `prompt.py` (`has = lambda`) was left alone.

Files: `fluid/engine/delegated.py`, `fluid/engine/delegated_proto.py`,
`fluid/cli/prompt.py` (`profile_for`), `fluid/cli/config.py`,
`fluid/config/loader.py` (the `delegated_claude` branch of `build_engine`),
`fluid/tools/mcp/bundled_server.py`, `tests/unit/test_delegated.py`,
`tests/unit/test_prompt_regression.py`.

- **PRM-1 (Critical).** `DelegatedLoop` always takes the `--system-prompt`
  branch because nothing in production sets `append_system_prompt`, so
  fluid's full base prompt replaces Claude Code's own and names about
  sixteen tools that leg cannot reach: HistoryExpand, HistorySearch,
  ToolSearch, Plan, StopPlanning, Remember, Recall do not exist there, and
  the rest exist only as `mcp__fluid_bundled__<Name>`. Either make
  `profile_for` provider-aware (intersect with the reachable set and rename
  to the prefixed form) or switch the leg to `--append-system-prompt` with
  base plus extras. Add a regression asserting the mentioned tools are a
  subset of the delegated surface.
- **PRM-2 (Critical).** `fluid/cli/__main__.py` forces `code_mode` on with
  `--continual`, and `cli/config.py` adds the code-mode addendum without a
  provider check, so the delegated engine (which has `code_mode=False`) is
  told tools are reached through `run_python`. Gate the addendum on the
  resolved engine and warn or error on that combination.
- **ENG-6 (Critical).** Bare `readline()` on the CLI's stdout with the
  default 64 KiB limit; Claude Code echoes full tool results in user frames,
  so real turns die and the stream desyncs. Pass `limit=16*1024*1024` to
  `create_subprocess_exec` or read in chunks.
- **ENG-15 (High).** A dead `--resume` handle is re-emitted forever and
  `error_during_execution` maps to `TurnComplete`; treat `result.is_error`
  as an error, clear `cli_session_id`, retry once without `--resume`.
- **Mediums.** `--strict-mcp-config` is never passed and `config.mcp` never
  forwarded, so the child inherits the user's unrelated servers and drops
  fluid's; a block in `_run_turn` sits outside the error guard, so a failure
  there emits no terminal event and `events()` hangs; permission requests
  are answered inline under the stdout lock with no timeout, and
  `allow_always` is downgraded to `allow`; `send_signal(SIGINT)` raises on
  Windows and `aclose` uses `terminate()`, orphaning the CLI's children;
  seq seeding was added in batch 2, but the leg still gets no continual
  learning because `build_engine` returns before that wiring. The bundled
  bridge (`bundled_server.py`) reads only `deny` and `modify` from hooks and
  does not honor the batch-5 `stop`, permission bypass or PostToolUse
  feedback; `fluid/codemode/bridge.py` has the same gap.

### Batch 7 — continual learning — done, `8887f24`

Everything below landed (see the table). Choices made: the synthesizer is
shared by handing the parent's instance to `build_engine(synthesizer=...)`
from `CliHost._build_child`, not by a process-wide registry; across
processes a `synthesis.lock` file next to the journal makes a pass
single-flight, and a pass that finds it held does nothing (the slice is
retried by whoever holds it). Memory provenance is derived mechanically
(`memory_provenance`: half the fact's content words in user or Remember
text means `user`, otherwise in tool or cell output means `tool`, otherwise
`session`); only `tool` entries are held back, and a Remember or user
statement that restates the fact promotes it through the normal rewrite.
There is no accept or reject command; the held entries ride
`ContinualUpdate.held` and the REPL prints them. ENG-23's reproduction
showed the verification kernel already refused an escaping skill body,
because verification runs as a cell and the guard walks to that frame; the
guard now also recognises `skills/<name>.py` frames on their own.

Files: `fluid/codemode/continual.py`, `skills.py`, `evolution.py`,
`verify.py`, `journal.py`, `session.py`, `fluid/cli/evolve.py`,
`fluid/cli/host.py` (child engine construction), `fluid/config/loader.py`
(synthesizer wiring), `fluid/engine/native.py` (`_finish_tool` call sites),
README "A harness that learns", `docs/agent-experience.md`.

- **CL-3 (High).** Every subagent builds its own `Synthesizer` over the same
  store, so parallel workers pay N× for the same journal slice and write
  duplicate memories, and the journal braids every agent's entries with no
  session tag. Stamp `session_id` and agent on journal entries and group by
  it in the prompt; make the synthesizer single-flight per store root or do
  not wire one into child engines; take the journal lock (added in batch 2)
  or a cursor compare-and-swap around `run()`.
- **CL-4 (High).** The retention gate passes trivially with zero anchors,
  which is the default install, and `apply_guidance` writes with no diff and
  no backup. Refuse synthesizer proposals when no anchor has a passing
  baseline unless a human passes a waiver flag; print the count and a diff;
  keep a `.bak`.
- **CL-5 (High).** Memories distilled from tool output render under a
  header that says to trust them. Record provenance per memory and do not
  render ones whose only evidence was tool output; surface new memories for
  accept or reject through the existing `ContinualUpdate` event.
- **CL-6.** The five early returns in `_dispatch_tool` call `_finish_tool`
  without `input=`, so denials journal with empty arguments and PostToolUse
  hooks get `input: None`.
- **CL-7, CL-8, CL-11.** No cap on live skills and every pass re-sends every
  skill's source; synthesis usage is discarded so spend is invisible;
  verification is serially unbounded on the first `run_python` (cap wall
  clock per mount, verify the rest lazily).
- **ENG-23 (High, belongs here).** Saved skills execute model-authored
  module bodies at verification time and are reported verified. The batch-1
  audit guard only fires for cell frames, so a skill module's own filename
  is not covered. Extend the guard's "cell code" predicate to skill module
  files under the store root, or verify in the sandbox with the guard armed
  for that path.
- **CL-12.** README says archived skills are never deleted and every skill
  carries a self-test; neither is true. Fix the wording.

### Batch 8 — prompts — done, `3d5506a`

Everything below landed (see the table). Choices made: the static code-mode
addendum is folded into the `base` section by `resolved_prompt` rather than
reordering the compiler's `extra_blocks` (their append-last contract and
its tests stand; per-run `extra_system` still rides last). PRM-9 is served
by `wired_capabilities(config, store, ...)` in `cli/config.py`, computed by
`make_engine` and threaded to `profile_for(wired=...)`; a literal
`TOOL_CAPABILITIES` map in `prompt.py` mirrors the registry's
`requires_extra` and a test keeps them equal. PRM-5 lives in a
`recall_tools` flag on `TrimConfig` and `CompactionConfig`; `NativeLoop`
clears it when HistoryExpand and HistorySearch are not both on its surface,
so an embedder with a small registry gets the recovery-free wording (one
hardening test now builds its comparison compactor from the loop's config).
Golden files: `tests/golden/base_prompt.txt` and
`tests/golden/code_mode_addendum.txt`, regenerated with
`FLUID_UPDATE_GOLDEN=1`. Pre-existing lint noise left alone: E731 in
`prompt.py`, E741 in `test_hardening.py`, unused `Path` imports in
`test_anchor_gate.py` and `test_prompt_gating.py`.

Files: `fluid/cli/prompt.py`, `fluid/tools/builtins/plan.py`,
`fluid/context/trim.py`, `fluid/context/compaction.py`,
`fluid/tools/registry.py`, `fluid/codemode/_runner.py`,
`tests/unit/test_prompt_regression.py`, `tests/golden`.

- **PRM-4.** `Plan`'s description says write and shell tools leave the
  surface; `plan_mode_blocks` also removes Task, Monitor and WebFetch while
  the base prompt tells the model to use them. Name the real set or
  reclassify.
- **PRM-5.** Trim and compaction placeholders name `HistoryExpand` even when
  the recall tools are off the surface. Thread the tool filter into the
  trimmer and compactor and emit a recovery-free variant.
- **PRM-6 to PRM-10.** Registry "unavailable" text contradicts the prompt's
  no-other-route rule (distinguish outage from denial); three base bullets
  contradict the code-mode addendum (gate them on `not code_mode`); the
  addendum teaches `from tools.<name> import` (use `from tools import`);
  capability-backed bullets are not gated on the capability being wired;
  PRM-10 (indented continuation lines becoming fields) was fixed in batch 4.
- **Lows.** Hedged "when it is available", bullets pointing at index
  sections that may not exist, the environment bullet promising manifests,
  the static addendum riding after the dated environment block (move static
  extras ahead of it), `Skill`'s miss list ignoring `setting_sources`.
- Add a golden snapshot of the rendered system prompt; it would have caught
  PRM-3 immediately.

### Batch 9 — providers — done, `b73aee9`

Choices made: `RetryPolicy` grew `max_transport_retries` (None means the
status budget) and `jitter`; OpenRouter's default is three status retries
and no transport retries, so a 429 retries and a dropped socket still does
not. An error a provider sends in place of content (an OpenRouter error
chunk, an Anthropic `overloaded_error` event) retries only while nothing
has been delivered; the clients hold `message_start` back until the first
content chunk so a retry never shows the engine two message starts. Blocks
the engine has no model for (redacted thinking, server tool use and
results) ride the existing `Reasoning` envelope as `raw`, recorded from
the `block_start` payload and resent verbatim by the Anthropic serializer;
no new content-block class was added. The Codex store's cross-process lock
is the journal lock helper, moved to `fluid/_filelock.py`. The Fable 5.1
cache-read override is a prefix table in `anthropic/pricing.py`; Mythos 5.1
keeps the default because its rate was open at launch. Tests: nine new
tests in `test_openai_responses.py`, twelve in `test_openrouter.py`, three
in `test_text_tools.py`, four in `test_bus_and_serialize.py`, one in
`test_golden_stream.py`, three in `test_codex_oauth.py`, one in
`test_pricing.py`, and the new `test_anthropic_client.py`.

Files: `fluid/providers/openai_responses/serialize.py`,
`fluid/providers/_chat_completions.py`, `fluid/providers/openrouter/client.py`,
`fluid/providers/_retry.py`, `fluid/providers/anthropic/serialize.py`,
`fluid/providers/local/text_tools.py`, `fluid/engine/native.py`
(`_assemble_message`), `fluid/providers/openai_responses/codex_oauth.py`.

- **ENG-16.** `response.incomplete` is unhandled (the status check is dead
  code inside the completed branch): truncation reports a clean end with
  zero usage, and mid-tool-call truncation triggers ten empty-completion
  retries. `max_output_tokens` is never emitted.
- **ENG-17.** OpenRouter classifies retriable errors and never retries;
  `max_retries=0`, so a 429 ends the turn.
- **ENG-18.** Chat-completions tool calls collapse into one when the server
  omits `index`.
- **ENG-19.** The local text-tool serializer interpolates tool output into
  `<tool_result>` with no escaping; a file containing the closing tag forges
  a prior call. The parser is robust; escape on the way out.
- **ENG-21.** Anthropic history fidelity: empty thinking signatures resent
  and rejected forever; an assistant message whose blocks all serialize to
  `None` is dropped, breaking role alternation; empty `Text("")` from an
  MCP tool goes on the wire; `redacted_thinking` and `server_tool_use` are
  silently dropped. All poison the stored prefix.
- **Mediums.** Mid-stream `overloaded_error` treated as fatal; `Retry-After`
  parses seconds only and there is no jitter; `refusal.delta` and
  `content_filter` render a blank reply; the Codex OAuth store (unwired
  today) writes non-atomically, chmods after writing, is a no-op mode on
  Windows, and two processes re-POST the same refresh token. Fix before
  wiring it. CTX-10 left Fable's 0.025x cache-read rate unmodelled.

### Batch 10 — engine mediums — done, `0eb98d7`

Choices made: Bash reads its child's output through a head-and-tail cap
of eight times the tool's truncation limit and clamps the timeout to
between one second and ten minutes. Allow-always labels come from
`scope_labels` in `bash_risk.py`: a wrapper (`sudo`, `env`, `xargs`, and
the rest of `WRAPPER_PROGRAMS`) labels as itself plus what it ran, a
subcommand program (`npm`, `uv`, `make`, and the rest of
`SUBCOMMAND_PROGRAMS`) as itself plus its first argument, and a wrapper
with options yields no label at all, which the engine now treats as a
one-shot allow instead of an unscoped one. A subagent's allowlist is the
parent engine's list narrowed by the agent definition's `tools`, and the
parent's denylist is appended. `Worker.serves` is set from the Monitor
input and `CliHost.pending()` is what `busy()` and `wait_idle()` consult.
MCP tools take a server's `readOnlyHint` only under the new
`trust_annotations` config key; a `destructiveHint` always counts. The
kernel restarts on an interrupted cell (the checkpoint restores the
namespace), the runner catches `SystemExit` and `KeyboardInterrupt`
inside the cell, the host drains stderr into a 4 KiB tail quoted in the
death diagnostic, the child stops pickling at `checkpoint_max_bytes` and
reports the oversized names as not restorable, and tracebacks replace the
kernel directory and the home directory with placeholders. Tests: the new
`test_bash_handler.py`, four in `test_bash_risk.py`, four in
`test_cli_host.py`, two in `test_mcp_client.py`, one in
`test_tool_hardening.py`, five in `test_codemode_kernel.py`, one in
`test_audit_kernel_hygiene.py`, one in `test_hardening.py`.

- Bash inherits the parent's stdin (a command that reads stdin steals
  keystrokes; the description claims otherwise), buffers output unbounded via
  `communicate()`, and does not clamp `timeout` in either direction.
- Subagents do not inherit the parent's tool allowlist
  (`fluid/cli/host.py`); a read-only session spawns a full-surface worker.
- `CliHost` has no notion of `serves` monitors, so a dev server hangs
  `fluid run` and every synchronous Task.
- MCP servers self-assess risk: `readOnlyHint: true` becomes `safe`, skipping
  the prompt and staying callable in plan mode.
- Bash `allow_always` scopes by program name, so approving `sudo apt list`
  once covers `sudo rm -rf /`; same for `npm`, `uv`, `xargs`, `env`, `make`.
- Deferred-catalog names and descriptions render into a system-reminder
  block with no defanging (`fluid/tools/builtins/tool_search.py`).
- Code-mode kernel (ENG-22): an interrupted cell desyncs the pipe and hands
  the next cell the previous result; `SystemExit` is misreported as OOM;
  child stderr is a pipe nobody drains; the whole namespace is pickled over
  the pipe on every cell (0.003 s to 0.42 s with one 50 MB variable);
  `truncate.py` overshoots its cap; tracebacks leak host paths; POSIX spawn
  has no process group.
- Batch 1 left one known gap in the audit guard: code that runs after every
  cell frame has left the stack (an `atexit` handler compiled under another
  filename). Documented in `docs/security.md`.

### Lows — done, `59c68ed`

Every Low in the report was checked against the tree. Several had been
fixed by earlier batches (the system config path, the `max_tokens` doc,
hook matchers and hook JSON, `--continue` ranking, the delegated leg's
`Interrupted`, `send_signal` on Windows, the empty ToolSearch query, the
unknown prompt-section kind, the README claims). The rest landed in this
commit; the regression for each is in `tests/unit/test_audit_lows.py`,
and the pinned expectations in `test_plugins.py` and
`test_settings_importer.py` were updated to the new shapes. Choices made:
the store keeps eight sessions parsed (`_MAX_CACHED_SESSIONS`), never the
one it holds the write lock for; the question timeout is a module constant
`QUESTION_TIMEOUT_S` of thirty minutes that an engine instance can
override through `question_timeout`; `tools/list_changed` invalidates the
client's cached listing (the registry re-lists on the next request) and
no reconnect logic was added.

### Left out

- **Tool spec join on the delegated leg.** `--allowed-tools` and
  `--disallowed-tools` are still comma-joined (`delegated.py`). A spec
  containing a comma would need either repeated flags or the CLI's
  variadic form; which one Claude Code accepts has to be checked against a
  live CLI, so it was not changed blind.
- **Background wake summaries are not tier-1 trimmable.** The wake rides a
  user message and tier 1 trims tool results only; making the summary
  trimmable is a design change to the trim ladder, not a fix. Monitor
  summaries are already capped at 16 KB by the host.
- **Plan-mode toggles cost one cache miss.** Inherent, as the report said;
  no doc line was added.
- **POSIX process group on the kernel.** Written but exercised only on
  Windows in this campaign; the Linux path needs one run there.
- **Mythos 5.1 cache-read rate.** Left at the 0.1x default until it is
  published.
- **The delegated leg's `ask` verdict** still resolves to the
  non-interactive denial (batch 6 note); routing it through the loop's
  `PermissionRequest` is not done.

### Deferred feature — live reading of foreign homes

The report's recommendation on the "direct port" question: keep `~/.fluid`
as the home (the 2026-08-20 and 2026-08-23 commits `f45f03f` and `72cb5e7`
made that decision for safety reasons, and batch 5 made the importer
lossless), then add opt-in `setting_sources` entries such as `claude` and
`codex` that read `~/.claude` and `~/.codex` live, for users who want zero
conversion. Not started. It is a feature, not a fix; slot it after batch 7
or later. The owner has not yet approved it explicitly.

## Verification recipes

- The reviewer's long-session and oversize-turn scripts lived in the
  session scratchpad and are gone. Their assertions now live in
  `tests/unit/test_overflow_ladder.py` and
  `tests/unit/test_hardening.py::test_context_overflow_recovers_an_oversize_current_turn`
  (a `SizeLimitedProvider` that rejects prompts over N tokens).
- Store concurrency: `tests/unit/test_store_durability.py` spawns two real
  processes appending 300 records each.
- Sandbox: `tests/unit/test_codemode_kernel.py` runs real cells against the
  real `SubprocessKernel` and asserts `os`, `_socket`, `subprocess` and
  `os.environ` are refused.
- Hook contract: `tests/unit/test_hooks_protocol.py` runs real Python hook
  scripts through the engine.
- Interrupts: `tests/unit/test_cli_sigint.py` sends a real signal to a
  child REPL. It flaked one run in four until the `LineReader` stash race
  was fixed in batch 5; twenty consecutive runs pass now. If it flakes
  again, loop it with `-x` and read the failing assertion before touching
  anything.

## Related notes

- Memory file for this campaign:
  `~/.claude/projects/D--Development-omni-harness/memory/fluid-full-audit-2026-09-01.md`.
- `docs/CLAUDE_ECOSYSTEM_COMPAT_PLAN.md` is superseded (batch 5 marked it);
  `docs/configuration.md` and `docs/hooks.md` are the current references.
- Earlier handoffs for the continual-memory and ai-shuffle work:
  [continual-memory-handoff.md](continual-memory-handoff.md),
  [ai-shuffle-wiring-handoff.md](ai-shuffle-wiring-handoff.md). The
  ai-shuffle vendored copy predates all five batches; re-vendoring is a
  separate task in that repo.
