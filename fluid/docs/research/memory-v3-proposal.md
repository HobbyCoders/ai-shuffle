# Memory v3: a proposal for Fluid's memory system

Written 2026-11-25 against `main` at `69bb6c0a7`. The evidence comes from the owner's fact store on the owner's main worker, read-only. Section 9 adds the phase 0 replay results. Examples quoted below are illustrative, not text from that store.

## 1. Summary

Fluid's memory is careful about not losing what the owner said, and that part should stay. Everything around it has grown into a write-heavy, read-light system. It spends a lot of effort learning and storing facts, and very little making sure the right fact reaches the agent at the moment it matters.

- **Learning costs a lot and keeps little.** In 7 days the learner made 522 model calls (9.5M input tokens, 20k output tokens). 70% of those calls changed nothing. 187 learned or harvested facts sit in the archive with **zero uses** at a median age of 3 days, and 286 facts have been permanently dropped at the archive limit.
- **Delivery is a pointer the agent has to follow.** Each session gets about 9,000 characters of one-line "when" entries, and the agent has to call `Recall(ids=…)` to read any fact. In the retained journal, Recall was 2 of 392 tool calls. Most saved knowledge is announced but never read.
- **Different kinds of knowledge share one lifecycle.** Owner rules, machine facts, repo conventions, behaviour corrections and in-progress task state all sit in one pool with tier caps. One example: a "when continuing the fix in PR #N" fact is still live six days after that PR merged.
- **Complexity has grown with every fix.** About 16k lines in `fluid/codemode` plus about 2.7k in `app/` cover three tiers, an archive, revision fences, a Bloom-filter deletion barrier, causal clocks, per-field metadata stamps, a two-call model conflict review, a task-slice novelty allowance and a when-line backfill learner. One cell of the architecture doc's limits table runs to about 600 words. The issue history (#103, #688, #956, #958, #1167, #1508 …) shows each failure being fixed with another mechanism.

**The proposal has four parts:**
1. Deliver memory **just in time**: the harness matches facts to the current turn and tool calls and puts the full text in front of the agent, instead of listing pointers.
2. Learn **once per finished session**, from the user's words, as evidence-backed proposals, instead of a continuous learner reading every tool call.
3. Give each kind of knowledge **its own home and lifecycle**.
4. Move storage to **SQLite rows** and delete the mechanisms that only exist to work around the single JSON document.

## 2. What exists today (short)

| Part | Where | What it does |
|---|---|---|
| Fact store | `codemode/facts.py` (1.8k) | One fsynced JSON document per owner: live entries, archive, revisions, tombstones, Bloom deletion filter. Three tiers: explicit (user-saved, 512), durable (agent-saved, 120, 120-day TTL), inferred (40, 90-day TTL). Archive of 200, where the least useful facts are permanently dropped |
| Session snapshot | `skills.py:render_memory_section`, `app/core/memory_reminder.py` | ALWAYS facts in full, identity facts, then one `- [id rN; source] <when-line>` per fact (10k chars). Plus a lexical "task slice" and a change-list chain with an 8k novelty allowance |
| Tools | `tools/builtins/remember.py`, `recall.py`, `forget.py` | Save with a when-line, keyword or id lookup, forget with a revision |
| Learner | `codemode/continual.py` (2.3k) | Paced continuous passes over `journal.jsonl` (every tool call, cell, user and assistant turn). Writes facts, behaviour rules and Python helper skills. Also runs a consolidation pass |
| Harvest | `context/runtime_facts.py` | Turns tool output into per-worker environment facts (`env.pytest` …) |
| Conflict review | `codemode/conflict_review.py` (1k) | Model looks for contradicting owner rules, with a second confirming call |
| Health | `codemode/memory_health.py` (950) | Nine signals, repairs, notifications |
| Replication | `app/network/memory_records.py` | One record per fact plus a barrier record per store |
| Workflow proposals | `codemode/workflows.py` | Prose skill proposals; documented as inert in normal use (#206) |

## 3. What the store shows (the owner's main worker, 2026-11-25)

| Measure | Value | What it means |
|---|---|---|
| Learner calls, last 7 days | 522 (346 Sol, 150 Luna, 26 Astra) | About 75 a day on the subscription |
| Learner input / output tokens, 7 days | 9.5M / 20k | A 475:1 read-to-write ratio |
| Passes that changed anything | 157 of 522 (30%) | — |
| Live facts | 101 (37 user, 24 agent, 21 inferred, 19 harvested) | — |
| Archived facts | 201, of which 196 were never recalled | Learned and harvested facts are evicted from the 40-slot cap within days |
| Archive median age, inferred and harvested | 3.1 days | The learner fills slots, the cap pushes facts out, then the archive drops them |
| Permanent drops at the archive limit | 286 (79 in the last 3 days) | Health has flagged this "attention" signal for days |
| Journal entries dropped unread (lifetime) | 71,323 (cell 18.5k, tool 16.7k, user 462, assistant 457) | Most of the journal is tool noise; some user turns were dropped too |
| `env.pytest` revisions | 39 | A harvested fact rewritten over and over; the session's "Workspace facts" probe already reports the same thing |
| Python helper skills | 4 listed in meta, 11 uses lifetime, no module files present | The skill-synthesis half of the learner prompt produces almost nothing. Delegated (Claude Code) sessions have no kernel to use one |
| Recall in retained journal | 2 of 392 tool calls (10 sessions) | Small sample, but it matches what I see from inside a session: about 75 "when" lines and no reason to dereference any of them |
| Memory index per AI Shuffle session | about 9k chars | Paid every session, mostly as unread pointers |
| Task-state facts still live | 7, for example "continuing the fix in PR #N" (that PR merged 2026-11-19) | Task progress has no expiry tied to its task |

The parts that work and should stay: explicit saves are never lost (39 of 39 kept), corrections by revision, owner and project scoping, per-worker env facts, secret screening, and per-fact replication.

## 4. Root causes

1. **Retrieval waits for the agent to ask.** The when-line is a good trigger, but nothing fires it. Models rarely stop to look things up, especially mid-task. That is exactly when a rule like "before merging, …" matters.
2. **Learning reads the wrong evidence.** The facts worth keeping come from the user's words: corrections, preferences, decisions. The learner pays to read every Read, Edit and Bash call (about 70% of the journal), and then filters most of it out.
3. **Caps instead of acceptance.** Inferred facts go straight into a small live pool and compete on recency. Nothing asks whether a fact is good before it takes a slot, so the pool churns.
4. **One pool for unlike things.** Machine state belongs in a probe. Repo conventions belong in the repo. Task progress belongs in the working record. Owner preferences belong in memory. Mixing them gives each the wrong lifetime.
5. **A document store under a concurrent, replicated workload.** A Recall rewrites the 416 KB JSON document under a lock (`note_recalled`). Whole-document size caps, archive caps, the Bloom deletion barrier and the 18 GB sync-log incident (#1167) all follow from storing everything as one file.

## 5. The design

### 5.1 Five kinds, five homes

| Kind | Example | Home | Lifetime |
|---|---|---|---|
| **Owner rule or preference** | "Run the linter before every push" | Memory, owner-kept | Until the owner changes it |
| **Owner fact** (profile, business, career) | Name, GitHub username | Memory, owner-kept | Until changed |
| **Project knowledge** | "API handlers validate input with the shared schema helper" | Memory now; offered as a repo `AGENTS.md`/skill PR once it has proven useful | Until contradicted or promoted |
| **Environment fact** | Which Python has pytest, where the browser runs | The **workspace-facts probe** (already runs at session start), not memory | Re-probed each session |
| **Task state** | "Continuing PR #N" | **WorkingRecord** / session history | Ends with the task; Remember steers it there |

The changes in practice:
- The `env.*` harvest into memory stops. Each harvest key becomes a probe in the workspace-facts block, which is always current and never needs confirming.
- Remember refuses text or when-lines that read as task progress (PR or branch references together with "continuing", "in progress" and similar). It points the agent to WorkingRecord. Any fact that names a PR gets `until: pr#N closed`, and maintenance retires it when the PR closes.

### 5.2 Delivery: always-set plus just-in-time

**Always-set** (cached system prefix, small): ALWAYS facts and identity facts, as today, plus a short index of the owner's rules that have no trigger. Target: 3k characters or less, down from about 9k.

**Just-in-time delivery** (new): every fact keeps its human `when` line and gains optional machine **triggers**:

```yaml
when: "When opening a pull request or pushing review fixes"
triggers:
  words:   ["pull request", "open a pr", "push"]   # user turn / brief text
  tools:   ["Bash"]
  command: ["gh pr create", "git push"]            # substring or regex on tool input
  paths:   ["docs/guides/**"]                      # file-tool path globs
```

- **User turn:** the harness scores the turn (and the brief, for subagents) against `when` plus `words` with the existing lexical scorer, and adds the full text of the top matches to the turn's memory reminder.
- **Tool boundary:** before a tool runs (or on its result, where the leg cannot inject before), matching facts are delivered in full. The native leg controls its own loop. The delegated leg already injects `additionalContext` at tool boundaries for peer messages (`engine/delegated.py:3447`), so both legs have a delivery channel.
- **Once per session per fact**, re-sent after compaction drops it. Each delivery is capped at about 1,500 characters (about 3 facts); the rest are named for Recall.
- **Where triggers come from:** Remember accepts them. If none are given, they are derived without a model from the when-line (tool and command words, paths, content words). An agent's Remember call can also be asked to fill them in, so no backfill learner is needed.
- Recall and the index stay as the fallback. They are no longer the main path.

This replaces the task-slice, novelty-allowance and change-list machinery in `memory_reminder.py`. Delivery is per turn and per tool call; there is no running delta of a snapshot to maintain.

### 5.3 Writing: one reflection per finished session, as proposals

- **Remember stays**, with the same receipts, revisions and owner protection. At save time it shows the saving agent the 3 most similar existing facts, and the agent chooses `replaces=[…]` or keep. That catches duplicates and contradictions when they are written, with the context to decide.
- **Reflection replaces the continuous learner.** When a session settles, one model call runs, and only if the session had at least one of: a user correction or frustration signal, an explicit "remember", or friction that repeated. Its input is the user turns, the assistant's final replies, friction records, and the current facts in scope. It does not read the tool-call stream.
- **Output is proposals with evidence:** a quote from the user's words (required for rules, as `behavior.py` already requires), the session id and the proposed kind.
  - Quoted owner rules and preferences apply at once as **agent-saved** facts, visible and revertible, as behaviour rules are today.
  - Other inferred facts wait in a **proposal inbox**. A proposal becomes a fact when a second, distinct session produces it independently, or when the owner accepts it in Settings → Memory. Otherwise it expires quietly after 14 days. It never takes a live slot and never counts as "lost".
- **Estimated cost:** about 10–25 calls a day at about 6–8k input tokens each, versus about 75 a day at about 18k. That is roughly an 80–90% cut. This is an estimate to confirm in phase 0, not a measurement.
- **Removed:** Python helper-skill synthesis (near-zero yield, no kernel on the delegated leg), the separate consolidation pass, the when-line backfill learner, and the inert prose-workflow proposals (#206, until it has a real design).

### 5.4 Lifecycle: earn your place

- **Owner-kept facts:** unchanged. Never expire, never dropped, 512 cap with a refusal message.
- **Agent-saved and accepted facts:** one pool, no tier split. A fact's value is **deliveries plus Recalls in the last 60 days**. A fact with no deliveries for 90 days is retired to the archive, where Recall still finds it.
- **Archive:** unbounded rows with a 365-day retention. A row is about 0.5 KB, so 10,000 archived facts is about 5 MB in SQLite. No "least useful first" permanent drops.
- **Agent feedback:** a fact delivered just in time can be flagged `stale` by the agent (a Remember correction, or `Forget` with a reason). The flag goes to the owner inbox; it does not delete the fact.
- **Conflicts:** handled at write time (5.3) and by the owner in the inbox. The model conflict review (two calls, a 1k-line module, 6 runs in 7 days) is removed. A short "possible conflict" note is kept when two owner rules share a trigger and their texts disagree on a negation; that check is deterministic.

### 5.5 Storage and replication

- **SQLite per store** (stdlib, so standalone Fluid keeps working) in WAL mode, with tables `facts`, `revisions`, `tombstones`, `deliveries` and `proposals`. Recording a use becomes a one-row update instead of a 416 KB rewrite. Concurrent sessions stop serializing on one lock file.
- **Replication** keeps today's per-fact network records (`memory-fact:…`) and per-field last-writer-wins with hybrid timestamps, which already exists for `when` and `always`. Tombstones become rows kept for 365 days and are replicated like facts, which replaces the Bloom barrier and its "may withhold, may saturate" caveats. Deliveries and usage stay local to each worker and are not replicated.
- **Migration:** a one-way import of `memories.json` into SQLite. The JSON file is kept as a backup for one release, and an explicit downgrade tool exports it back.

### 5.6 What gets deleted (estimate)

| Module or mechanism | Lines today | Replaced by |
|---|---|---|
| `conflict_review.py` | 1,030 | Write-time similarity check plus owner inbox |
| `memory_backfill.py` | 276 | Triggers set at save time |
| `workflows.py`, `workflow_loss.py` | 388 | Removed until #206 has a design |
| `continual.py` (synthesis, consolidation, pacing, leasing, skill synthesis) | 2,290 | About 400-line `reflection.py` |
| Task-slice and novelty logic in `memory_reminder.py` and `render_memory_section` | about 500 | About 300-line just-in-time matcher |
| Bloom barrier, archive drop tiers and revision fences in `facts.py` | about 400 | SQLite rows plus tombstone table |
| Journal tiered drops and lease (`skills.py`) | about 500 | The journal keeps only user, assistant and friction records, so it is about 10× smaller |

Net: about 5k lines removed and about 1.5–2k added. Fewer moving parts means fewer health signals to explain. "Learner keeps up with the journal" and "archive drops" disappear as failure classes.

## 6. How we know it's better (acceptance per phase)

| Metric | Today | Target |
|---|---|---|
| Learner calls per day | about 75 | 25 or fewer |
| Learner input tokens per week | 9.5M | 1.5M or fewer |
| Saved facts dropped without an owner action | 286 recorded | 0 (proposals expire before they are saved) |
| Facts delivered in full at least once per 30 days | unknown (Recall is rare) | 70% or more of live non-owner facts |
| Always-set memory characters per session | about 9k | 3k or fewer |
| Trigger precision on replayed sessions | — | 80% or more of just-in-time deliveries judged relevant on a labeled sample |
| Stale task-state facts live after their PR closed | 7 | 0 |

## 7. Phases (each phase is its own PR and is useful alone)

1. **Phase 0, measure (no behaviour change):** a pure trigger module (`fluid/codemode/triggers.py`) and an offline replay (`tools/research/memory_trigger_replay.py`) that runs triggers derived from current when-lines over the last 30 days of transcripts, to measure match rate and precision. This settles whether delivery timing works before any user-visible change. Results are in section 9. Live delivery and Recall counters are left for phase 1, where deliveries first happen.
2. **Phase 1, just-in-time delivery:** add triggers to Remember, plus the matcher on user turns and tool boundaries for both legs. Run beside the current index, then shrink the index once phase 0 numbers hold.
3. **Phase 2, writing:** reflection per settled session, the proposal inbox (Settings → Memory), env harvest moved to probes, the task-state guard, and the continuous learner turned off.
4. **Phase 3, storage and simplification:** SQLite migration, tombstone rows, the single non-owner pool, and deletion of conflict review, backfill and workflows.
5. **Phase 4 (optional), promotion:** project facts with sustained deliveries are offered as an `AGENTS.md`/skill PR, so repo conventions live where every agent and human sees them.

## 8. Risks and open questions

- **Pre-tool timing on the delegated leg.** If the CLI can only take context on a tool's result, a fact may arrive one call late. Mitigation: user-turn matching covers intent ("merge it"), and phase 0 measures how often the first matching event is the action itself.
- **Trigger noise.** Over-broad words could inject irrelevant facts. Mitigation: two-word minimum for word triggers, delivery once per session, and per-delivery caps. Phase 0 precision is the gate.
- **Less background learning.** Some facts the continuous learner would have caught from tool output alone will be missed. The data says those facts mostly went unused (196 of 201 archived never recalled), so this trade looks right, but it is a trade.
- **Mixed-version networks** during the SQLite migration. Replication records do not change, so older workers keep merging. The local file format is the only break, and it gets an export tool.
- **Owner inbox fatigue.** The 2-session corroboration rule should keep it small. If it grows, auto-expiry does the cleanup.

## 9. Phase 0 results: offline trigger replay (2026-11-25)

Triggers were derived from the live when-lines with `fluid/codemode/triggers.py` (no model call) and replayed over the Claude Code CLI transcripts of the owner's main worker for the last 30 days with `tools/research/memory_trigger_replay.py`. Nothing user-visible changed. Facts are scoped like the snapshot: global facts plus the session's project, mapped from the git remote of the transcript's working directory; 269 sessions with no mappable project got global facts only.

### Replay

```
python tools/research/memory_trigger_replay.py \
    --facts <fluid home>/skills/memories.json \
    --transcripts ~/.claude/projects --days 30 \
    --json <scratch>/replay.json --sample 60 --out <scratch>/sample.jsonl
```

The store is read only. `--sample N --out FILE` writes a labelling sample (when-line, trigger terms and at most 200 characters of the triggering snippet); it quotes transcript text, so keep it outside the repository. `--sample-kinds words,tool,path` samples only those kinds, and `--word-threshold N` tries another word threshold. The script docstring has the details.

### Numbers (final rules)

| Measure | Value |
|---|---|
| Sessions replayed | 2,061 (5,835 user turns, 196,562 tool calls) |
| Candidate facts (live, not ALWAYS, identity, behaviour rule or pair answer) | 89 of 102 live; 77 in scope of at least one replayed session |
| Facts delivered at least once | 44 (49% of candidates, 57% of those in scope) |
| Coverage by trigger shape, in scope | command 14 of 14, path 1 of 1, tool 1 of 1, words only 28 of 61 |
| Deliveries per session, mean / p50 / p90 | 2.7 / 0 / 8 |
| Characters per session if each delivery carried full text (1,500 cap per event), mean / p90 | 1,200 / 3,456 |
| Events with more fresh matches than the 3-per-event cap | 205 of 5,611 deliveries |
| Deliveries by kind | command 5,153 (92%), words 321, tool 113, path 24 |
| Word deliveries by source | delegation brief 198, long user turn (usually a parent's brief opening a subagent) 113, short user turn 10 |
| Command facts matched before their first matching command | 18 of 5,279 session-fact pairs (0.3%) |

The five most-delivered facts are all commit and push rules. Several of them fire on the same `git commit` or `git push`, which is what the per-event cap is for.

### Precision

Deliveries were labelled by hand: relevant if a careful agent would want that fact at that moment.

| Sample (final rules unless noted) | Relevant |
|---|---|
| 60 drawn from all deliveries | 59 (98%) |
| 40 command deliveries | 39 (98%) |
| 60 word, tool and path deliveries | 49 (82%) |
| 60 word deliveries, same word rules, earlier command rules | 48 (80%) |
| 60 word deliveries with a word threshold of 2 (rejected) | 15 (25%) |

The first and third samples were labelled before program names were limited to command position. That change only removes `curl`, `pytest` and `psql` matches in which the program is an argument, and the 40-command sample was drawn after it. Delivery-weighted precision is therefore about 97%. Word triggers on their own sit at the 80% gate.

### What it took

| Step | Coverage | Deliveries per session |
|---|---|---|
| First cut: two distinct content words or one phrase anywhere in a turn; bare program words as commands | 86% | 13.0 |
| Weak words (pr, ci, test, merge …) never count as one of the two; a when-line naming a place (another checkout, a Windows worker) keeps only words | 80% | 7.1 |
| Negated clauses ("do not edit, commit or push") ignored; the place name is required | 80% | 6.7 |
| Command rules anchored to the action (first push needs `-u`; journeys need `node …journey.mjs`); question when-lines ("when the owner asks why …") get no command | 53% | 3.2 |
| Words must sit within 12 content words and cover more than half of one alternative of the when-line; heredoc bodies and long quoted strings are not searched; paths fire only on edits | 68% | 3.9 (word precision 25% at threshold 2) |
| Word threshold 3 (a phrase counts 2, a strong word 1) | 52% | 3.0 |
| Multi-word proper names count once; mutating-curl and nightly rules need the method or script; program names only at command position | 49% | 2.7 |

Long briefs were the main source of noise: a brief that opens a subagent mentions most project topics once, so word matching has to be local (a window) and has to cover the when-line, not just touch it.

### What phase 0 says for phase 1

- **Enable command, path and tool triggers.** They are 94% of deliveries, about 98% precise, and they reach every fact that has one.
- **Delivery has to land before the tool runs.** For 99.7% of session-fact pairs the matching command is the first signal, so delivery on the tool result arrives one call late, after the commit or push has already happened. User-turn matching does not close that gap. The delegated leg needs a pre-tool injection point; without one, command-triggered facts arrive late and phase 1 should say so rather than count them as delivered in time.
- **Enable word triggers with the final rules (window 12, threshold 3, clause coverage), on user turns and delegation briefs.** They are 6% of deliveries and 80–82% precise. Log their deliveries so the thresholds can be retuned on real data.
- **Coverage is the gap.** Words-only facts reach 46% (28 of 61), so overall coverage is 57% of in-scope facts, short of the 70% target in section 6. Most undelivered facts describe a judgement situation ("when deciding whether …") with no command and no distinctive words. Phase 1 should have Remember ask the saving agent for explicit `command` or `paths` triggers, and keep the index and Recall for facts without one.
- **Caps:** at most 3 facts and about 1,500 characters per event, once per fact per session. With these rules the p90 session would carry about 3.5k characters of delivered memory, less than today's 9k index.
