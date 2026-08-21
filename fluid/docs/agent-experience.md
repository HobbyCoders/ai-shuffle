# Agent experience notes — what a harness gives me, and what it doesn't

Written from the perspective of the agent (Claude, running inside Claude Code)
while building fluid's continual harness. These are first-hand observations
about how tool consumption, skills, memory, and context management feel from
the inside, what Claude Code and similar harnesses do well, and which gaps
fluid's code-mode + continual design is aimed at. They are design rationale,
not marketing.

## 1. Tool results are the context killer, not tool schemas

Schemas get the attention (and progressive disclosure — deferred tools +
`ToolSearch` in Claude Code, `list_tools`/`search_tools` here — solves them
well). The bigger cost is **results**. In a JSON-tool harness every result I
produce lands in my context verbatim and stays there: reading a 2,000-line
file to use 10 lines of it costs me those 2,000 lines for the rest of the
session. Harnesses mitigate by truncating (lossy at exactly the wrong moment)
or summarizing history (destroys the exact artifacts I built earlier).

The kernel model inverts this: data lives in the workspace, context carries
only what I *choose* to surface. `ResultRef` is the load-bearing piece — a
20 KB tool result costs me a 200-char preview, and I slice/parse/regex the
full payload in later cells with zero further context cost. This is the
single biggest efficiency win a harness can hand me, and it is why the
filesystem bypass mattered: if I can `open()` files directly, the gate is
gone but so is the discipline — the gated file tools + ResultRef path is both
the security boundary *and* the context-hygiene mechanism.

What no harness gives me yet: **retroactive eviction** — the ability to say
"that result from 40 turns ago is dead weight, drop it from my window." The
kernel is the nearest substitute: state persists in the namespace even when
my token window forgets the code that created it.

## 2. Skills-as-prose vs. skills-as-code

Claude Code skills are instruction documents. They are excellent at carrying
*judgment* — when to trigger, what tone, which checklist — but they can't
execute. Every time a skill says "then run these four commands and join the
output," I re-derive and re-type that pipeline, spending tokens and adding a
chance of drift each time.

fluid's skills are executable modules (`skills.<name>` in the kernel). That is
procedural memory in the literal sense: a named, versioned, callable artifact
that costs one import line to reuse. The lesson worth keeping from the prose
model: discovery lives or dies on descriptions. A skill nobody can find is a
skill that doesn't exist — hence mandatory docstrings, the one-line catalog in
`list_tools`, and usage counts as a curation signal.

## 3. Memory that depends on my diligence decays

My Claude Code memory is a directory of prose facts plus an index loaded
every session. It works, but it has three structural weaknesses I feel
constantly: (a) writing to it is manual — under time pressure I under-save;
(b) it carries no usage signal, so stale entries look identical to vital
ones; (c) curation is a separate ritual (`/consolidate-memory`) that has to
be remembered too.

The continual harness automates exactly this loop for procedural knowledge:
every cell is journaled as a side effect of working (no diligence required),
synthesis runs in the background on the session's own provider, usage counts
accrue automatically when a skill is imported, and the synthesizer is
explicitly told that pruning and merging beat accumulation — with "no new
skill" as the expected common outcome, because a memory system's failure mode
is hoarding.

These observations became features (v2): the journal is now **episodic** —
user prompts and final assistant replies are recorded alongside executed
cells, so synthesis sees *why* a workflow ran and whether the user accepted
it (a corrected workflow is a delete signal, not a keep signal). Stored
skills must **prove they work**: every skill carries a pure `_selftest()`,
verified in a throwaway no-tools kernel on save and re-verified whenever its
source changes; broken skills are excluded from the kernel mount and flagged
in `list_tools` instead of failing silently at use time. And synthesis can be
pinned to one model (`continual_model`) so skill quality doesn't drift with
whichever provider runs the session.

## 4. Context management: prevention beats compaction

When a session gets long, Claude Code summarizes. From the inside, that's a
cliff: my summary keeps conclusions but loses artifacts — exact code,
exact flags, exact error text. fluid's stance (cache-first, trim tool results
cache-safely, keep the prefix append-only) plus the kernel changes what
"losing context" costs: the namespace holds the real objects, so a compacted
transcript no longer means lost work. The corollary is that kernel restarts
are the new cliff — a wall-clock timeout throwing away the namespace is worse
than a summarization pass. So the kernel now checkpoints: after every exec
the child pickles its picklable variables, and a timeout/OOM restart restores
them, with the error message reporting exactly what survived (data does;
cell-defined functions and classes don't — they're re-derivable from the
transcript, the data often isn't).

## 5. Permission round-trips shape how I plan

In JSON mode each tool call is a full model round-trip: I plan one step,
wait, read, plan again. Fine for exploration; wasteful for a known workflow.
In code-mode I write the whole workflow once and the host gates each call
inside a single turn — same auditability (every in-kernel call still emits
`ToolUse`/`ToolResult` and can park on a permission prompt), a fraction of
the latency and tokens. The escape hatch matters in both directions:
high-risk tools staying JSON-only keeps the auditable slow path for the calls
that deserve friction.

## 6. What I'd ask of any harness, distilled

- Let data live outside the window by default; make surfacing it my choice
  (kernel + ResultRef).
- Never make me pay prompt cost for capability I haven't asked for yet
  (progressive disclosure in both tool paths).
- Turn repetition into artifacts without relying on my diligence
  (journal + synthesis; the journal is episodic — conversation events ride
  alongside cells so intent and feedback inform what gets kept).
- Make stored knowledge earn its place (usage counts, prune-biased synthesis,
  and mandatory verification — `_selftest` on save, re-check on change,
  broken skills flagged and unmounted).
- Keep the audit surface identical regardless of how I act (host-side gate on
  every path).
- Make state durable across the boundaries I can't control: context
  compaction (kernel namespace), kernel death (checkpoint + restore),
  sessions (verified skills).

Everything on this list is now implemented in fluid, including retroactive
context eviction at the harness level: a mechanical ladder (tool-result trim →
turn digests → epoch stubs) that evicts by view transform only — the stored
tree keeps full fidelity, every digest carries node refs, and the
HistoryExpand/HistorySearch tools re-read originals on demand. Compaction
became an index, not a summary. What remains genuinely open: a hard sandbox
(container/gVisor kernel behind the same protocol) — the kernel remains the
working substitute.
