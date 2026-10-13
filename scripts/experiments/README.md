# Fluid v2 experiment runners

Runners for experiments E1, E3 and E10 from design-final section 6.1 (the fluid v2
review). None of them has been run against a live provider. They are meant to be
run by hand, with the user present. Every runner has an offline mode that
exercises its full code path against a fake transport; `tests/experiments/`
covers those modes.

Run everything from the repo root. `fluid` is not pip-installed, so either set
`PYTHONPATH` to the `fluid` directory or let the scripts add it themselves (they
do when started as `python scripts/experiments/<file>.py`).

```powershell
$env:PYTHONPATH = "$PWD\fluid"
python -m pytest tests/experiments -q
```

| Runner | Spends | Offline mode |
|---|---|---|
| `e1_cache_routing.py` | ChatGPT subscription quota, about 600 calls over 3 days | `run --dry-run` |
| `e3_compaction_probe.py` | Under $5 (OpenAI API key, Codex quota, Anthropic API key) | `--dry-run` |
| `e10_learner_yield.py` | Nothing: it reads logs, fact stores and Postgres | `--dry-run` (files named on the command line only; `--no-db` alone skips just Postgres) |

---

## E1: cache routing and quota

**Design (design-final 6.1).** One ChatGPT account, gpt-6-astra, a synthetic
conversation of about 40-60k tokens that grows by one tool round per call, 2-5 s
between calls. Five arms, interleaved:

- **A**: the client with no per-session routing headers. It is the default on every host except the Codex backend.
- **B**: A plus `session-id`, `thread-id` and `x-client-request-id`, all equal to `prompt_cache_key`. It is the default on the Codex (chatgpt.com) backend when `FLUID_RESPONSES_ROUTING_ARM` is unset.
- **C**: B plus `x-codex-turn-state` replay within a turn.
- **D**: B plus a `generate:false` prewarm at the start of each turn.
- **E**: the real Codex CLI on the same account at the same time (the positive control).

At least 120 calls per arm over at least 3 separate days. Usage-limit headers are
logged on every call, and quota consumed is regressed on uncached and cached tokens.
The report gives the zero-cache rate after each session's first call, with
cluster-robust confidence intervals.

**Pre-stated decision rule (copied from design-final 6.1):** ship headers if B or
C is ≤2% zero-cache with an upper 95% bound <5% on days when E is also ≤2% and A
is ≥15%. Days where E is also bad are flagged as backend drift and excluded. If
only D or E closes the gap, WebSocket moves into W1.

**Cost estimate (design-final 6.1):** about 600 calls, about $50-120 notional
quota. At the defaults each runner call sends 40-60k input tokens and gets a
one-word answer at low effort, so 40 calls are about 2M input tokens per arm per
day.

### What the runner does

`e1_cache_routing.py run` drives `fluid.providers.openai_responses.client.OpenAIResponsesClient`
directly, not the app. Arms A-D are chosen per call through the environment
variable `FLUID_RESPONSES_ROUTING_ARM` (values `A`, `B`, `C`, `D`). The client
implements the arms (fix #1); the runner only sets the variable, so the endpoint
default (B on the Codex backend, A elsewhere) never decides an arm here. Outside
the runner, `FLUID_RESPONSES_ROUTING_ARM=A` reverts a Codex-backend worker to
the old wire. The turn marker
goes in `request.extra["_fluid_turn_id"]`, as fix #1 describes.

- One block is one synthetic user turn: 5 calls for one arm. Each round runs
  every arm once in a fresh shuffled order, until each arm has `--calls-per-arm`
  calls.
- Each arm keeps its own conversation. A session starts at about 40k tokens and
  grows by about 600 tokens per call; at 60k a new session starts. The first
  instruction line carries a per-session nonce, so no call can hit a cache that
  another arm or session warmed. Every hit measured is within-session routing.
- Each call is logged as one JSONL row. A row holds the arm, prompt tokens,
  cached tokens, the zero-cache flag, latency, time to first byte, time to first
  token, and the `x-codex-*` / `x-ratelimit-*` usage-limit headers. It also
  records what went over the wire: whether identity headers were sent, whether a
  turn state was replayed, and how many prewarm requests there were.
- **Contract check.** Before any live call, the runner sends three calls per arm
  to a fake backend and checks that each arm does what its definition says. A
  live run refuses to start if an arm does not, so quota is never spent on four
  copies of arm A. `--skip-contract-check` overrides this. Only the arms the
  run will measure (`--arms`) are checked.
- Arm D: the p0/responses-wire client implements it as a logged no-op, because
  `generate:false` exists only on the Responses WebSocket. `run` therefore
  skips D, even from the default arm list, and prints one line saying so; it
  runs A, B and C. The summary asks for 120 calls only from the arms that were
  run, plus A and E, and lists the "TRANSPORT via D" verdict as unreachable.
  When a transport that can prewarm lands, set `PREWARM_IMPLEMENTED` in the
  runner.
- Arm C keeps its turn-state token in the request's `wire_state` (invariant
  I1), so the runner gives each session one `WireState` and passes it on every
  call of that session.
- A live run asks you to type `yes` before it starts. `--yes` skips the prompt.

### Commands

One-time login. This writes a fluid token file (`~/.config/fluid/codex_auth.json`).
It is a separate login from the Codex CLI's. Do not copy `~/.codex/auth.json`:
the refresh token rotates, and two tools refreshing one credential kill it.

```powershell
python scripts/experiments/e1_cache_routing.py login
```

Offline check (no network, no quota):

```powershell
python scripts/experiments/e1_cache_routing.py run --dry-run --calls-per-arm 10
```

Each day, for at least 3 separate days. Start the runner in one terminal:

```powershell
mkdir e1-logs -Force
python scripts/experiments/e1_cache_routing.py run --arms A,B,C --calls-per-arm 40 --out e1-logs\e1-runner-$(Get-Date -Format yyyy-MM-dd).jsonl
```

**Arm E (manual, the same day and at the same time as the runner).** In a second
terminal, prepare the workspace once, then run the eight turns with the Codex CLI
on the same ChatGPT account (checked with codex-cli 0.153.4):

```powershell
python scripts/experiments/e1_cache_routing.py prepare-codex --dir $env:TEMP\e1-codex
cd $env:TEMP\e1-codex
codex exec -m gpt-6-astra -s read-only --skip-git-repo-check (Get-Content turn_01.txt -Raw)
foreach ($t in 2..8) { codex exec resume --last --skip-git-repo-check -m gpt-6-astra -c sandbox_mode=read-only (Get-Content ("turn_{0:D2}.txt" -f $t) -Raw) }
cd -
```

`codex exec resume` has no `-s` flag, so the sandbox is set with `-c`. It needs
`--skip-git-repo-check` again because the workspace is not a repository, and
`--last` picks the newest session started in the current directory.

That gives about 40 model calls in one Codex thread. Codex writes its usage to
rollout files under `~/.codex/sessions`. Read them back as arm-E rows:

```powershell
python scripts/experiments/e1_cache_routing.py ingest-codex --cwd $env:TEMP\e1-codex --since (Get-Date -Format yyyy-MM-dd) --out e1-logs\e1-codex-$(Get-Date -Format yyyy-MM-dd).jsonl
```

Apply the rule across every day logged so far:

```powershell
python scripts/experiments/e1_cache_routing.py summarize (Get-ChildItem e1-logs\*.jsonl) --json e1-logs\summary.json
```

The summary prints one line per day, marked either qualifying or excluded with
the reason: no control, backend drift, or gap not reproduced. It then gives
per-arm rates pooled over the qualifying days, the verdict, latency per arm, and
the quota regression. The verdict is `PROVISIONAL` until there are at least 3
qualifying days and at least 120 measured calls per arm.

**How the bounds are computed.** Misses cluster, so each (session, turn) is one
cluster. The upper bound is the larger of two bounds. The first is a
cluster-robust (CR1) bound with a t critical value on G-1 degrees of freedom. The
second is a Clopper-Pearson bound on the effective sample size (N divided by the
design effect). The second bound still means something when no misses are seen:
0 of 120 gives an upper bound of about 3.0%.

---

## E3: native compaction acceptance

**Design (design-final 6.1).** `compaction_trigger` on the public API and on the
Codex backend; Anthropic server-compaction and `clear_tool_uses` identifiers;
whether `ttl:"1h"` needs a beta header.

**Pre-stated decision (design-final 6.1):** decides whether W2's native tiers exist.

**Cost estimate (design-final 6.1):** under $5. In practice it is one OpenAI
public request, one Codex request and 1 to 6 small Anthropic requests of about
2-6k input tokens each.

What it sends:

- `openai_public`: a Responses request to `api.openai.com` whose input ends with
  `{"type":"compaction_trigger"}`. Uses `OPENAI_API_KEY`.
- `openai_codex`: the same request to the ChatGPT Codex backend, with the headers
  fluid sends today. Uses the fluid token file from `e1 ... login`.
- `anthropic`: one Messages request that combines three things. The first is a
  system block with `cache_control: {"type":"ephemeral","ttl":"1h"}` and no TTL
  beta. The second is the edits `clear_thinking_20251015` and
  `clear_tool_uses_20250919` under beta `context-management-2025-06-27`. The
  third is `compact_20260112` under beta `compact-2026-01-12`. Uses
  `ANTHROPIC_API_KEY`. If the API refuses the combined request, the probe sends
  each identifier alone to find which one was refused. If the 1h TTL is refused
  without a beta, it retries once with `extended-cache-ttl-2025-04-11`.

Outcomes:

- `accepted`: HTTP 200 and the feature visibly took effect (a compaction item was
  returned, or a 1h cache write was reported in `usage.cache_creation`).
- `accepted_no_effect`: HTTP 200 without that evidence. For the 1h TTL the
  detail says why: a write reported at 5m means the TTL was ignored; a cache
  read with no write means an earlier entry was hit, which does not say which
  TTL wrote it, so re-run after 5 minutes.
- `rejected`: a 4xx response or a stream error, with the API's message.
- `error`: anything else that failed.
- `skipped`: no credential was available.

```powershell
python scripts/experiments/e3_compaction_probe.py --dry-run
python scripts/experiments/e3_compaction_probe.py --out e3-results.json
```

Use `--only openai_public,openai_codex,anthropic` to run a subset. The model
flags are `--public-model` and `--codex-model` (both default `gpt-6-astra`) and
`--anthropic-model` (default `claude-opus-5`). Re-run with the model the native
Anthropic lane actually uses if that differs.

---

## E10: learner yield after #1450

**Design (design-final 6.1).** Collect at least 7 days of logs on both replicas
(with `store_id`). Replay the 25 sessions through the rollout-learner stub. Judge
fact usefulness by later citation. Also count how many of the 92 Remember facts a
user message supports.

**Pre-stated decision (design-final 6.1):** decides whether the rollout learner
earns L, and decision D1 (the memory amendment in design-final §2.8, decided after
E10).

**Cost estimate (design-final 6.1):** about $5, and all of it is the replay. This
report makes no model calls and costs nothing.

The report reads the same data the app uses:

- **Fact stores.** The local `memories.json` under the app's fluid skills root
  (`app.core.fluid_memory.skills_root()`, normally `DATA_DIR/fluid/skills`). It
  also reads every worker's replicated snapshot from Postgres (rows
  `network_memory_records` with id `memory-v2:<device>:<owner>`). Postgres is
  reached through `app.db.base`, which reads `DATA_DIR/managed-postgres.json`.
  The app's managed Postgres must be running. Set `DATA_DIR` if the data
  directory is not the default.
- **Pass logs.** The local `.synthesis-log.jsonl` is read automatically. Copy
  the other replica's log out of its container and pass it with
  `--log LABEL=PATH`. A log row that carries `store_id` (fix #14) is credited to
  that replica whatever file it came from.
- **Transcripts.** `fluid_session_nodes` rows written since `--since`, plus every
  node of the Remember facts' source sessions.

For each replica and day, the report gives:

- facts written, by class (learner, Remember, environment harvest);
- learner passes run, passes that wrote, and errors;
- the longest silent run of synthesis passes and the days since the last write,
  with an alarm at 3 silent days (fix #14's threshold);
- how many learner and Remember facts written in the range were cited later.

It also counts how many Remember facts a user message in their source session
supports. Tool results, `<system-reminder>` blocks (closed or cut off) and
`<task-notification>` wakes do not count as user messages.

Citation and support are lexical checks. A citation is a later assistant message
that repeats five words in a row from the fact, one of its distinctive
identifiers, or its key. Support means the user message contains at least half
of the fact's content words. Read both as rough counts, not judgements of
usefulness.

```powershell
python scripts/experiments/e10_learner_yield.py --since 2026-09-24 --until 2026-10-01 --log peer=C:\path\to\peer-synthesis-log.jsonl --json e10.json
python scripts/experiments/e10_learner_yield.py --no-db --since 2026-09-24   # local root and files, no Postgres
python scripts/experiments/e10_learner_yield.py --dry-run --store peer=C:\path\to\memories.json --log peer=C:\path\to\peer-synthesis-log.jsonl   # offline: no Postgres, no app config
```

`--dry-run` reads only what the command line names (`--store`, `--log`,
`--skills-root`); without `--skills-root` there is no local replica in the
report.

Not covered here: the 25-session replay through the rollout-learner stub. It
needs a learner stub that does not exist yet, and model calls.
