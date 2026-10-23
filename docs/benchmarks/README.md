# Tuning benchmarks

A tuning default is a value AI Shuffle chooses for someone who has not chosen:
the effort a never-picked model starts at, a sampling value sent without an
explicit opt-in, and similar. Issue #95 sets the rule: **a tuning default ships
only with a benchmark showing a positive outcome against current behaviour.**
Changes without a measured win are not shipped.

## What enforces it

- [`tuning-ledger.json`](tuning-ledger.json) lists every shipped default with
  its committed evidence, and every explicit opt-in (such as the Qwen3 local
  presets) with its public source.
- [`scripts/tuning_gate.py`](../../scripts/tuning_gate.py) recomputes a verdict
  from the raw benchmark rows. It is offline and deterministic (fixed bootstrap
  seed). `win` needs all three:
  1. the candidate passes at least as many trials as the baseline;
  2. at least one of wall seconds, output tokens or input tokens has a paired
     task-cluster 95% interval wholly below zero;
  3. none of them has an interval wholly above zero.
- `tests/test_tuning_gate.py` runs in CI. It fails when the app applies a
  default that is not in the ledger, when the ledger lists one the app no
  longer applies, or when a listed default stops winning on its evidence.

Today one default ships: GPT-6.1 Sol on a ChatGPT subscription starts new chats
at High effort ([report](effort-gpt-6.1-sol-2026-10-16.md)). On the same
evidence Low, Medium and XHigh have no measured win (fewer passes), so none of
them ships.

```sh
python -m scripts.tuning_gate --check-ledger
python -m scripts.tuning_gate docs/benchmarks/effort-gpt-6.1-sol-2026-10-16.json --baseline default --candidate high
```

## Proposing a new default

1. Measure it with the live runner on the app's own worker, under a background
   task. It drives real sessions through the local app and never reads
   credentials. Start with a one-task preflight:

   ```sh
   python -m scripts.effort_benchmark --output /tmp/preflight.json \
     --model gpt-6.1-sol --provider openai --task <id> --repetitions 1 --configs default
   python -m scripts.effort_benchmark --output /tmp/results.json \
     --model gpt-6.1-sol --provider openai --configs default high --repetitions 3
   ```

   `--provider` is a provider row id, provider type or sign-in mode
   (`chatgpt`, `openai`, `openrouter`, ...), or `claude` for a built-in Claude
   alias. `default` sends no override, which is current behaviour.
2. Run `scripts/tuning_gate.py` on the results. Only `win` may ship.
3. Commit the results JSON and a short report next to it, change the code, and
   add a ledger entry. `--check-ledger` and the CI test must pass.

Request-parameter changes (what reaches each provider) are a separate check:
`scripts/effort_wire_snapshot.py` diffs the outgoing request fields before and
after a change. It proves the setting arrives, not that it helps.

## Not measured yet

- Local sampling and thinking (`local_settings`) have no outcome benchmark: it
  needs a real llama.cpp or vLLM server. They stay explicit opt-ins with no
  default, so nothing ships unmeasured.
- Lower effort for background calls (learner, summaries) and lookup subagents
  has no arm; they keep inheriting the session's effort.
- Effort defaults for models other than GPT-6.1 Sol on ChatGPT. The runner can
  measure them (`--model`, `--provider`); none has been run.
