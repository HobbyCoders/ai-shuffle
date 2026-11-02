# Provider safety checks: served model, reroutes and blocked calls

What the backend sends when a provider answers with a different model than
the one asked for, or its safety check declines a request, and how a client
answers. This doc is for the frontend and other clients. The engine side is
in `fluid/fluid/engine/guardrail.py`, the app side in
`app/core/guardrail_prompt.py`.

It covers Claude chats through the Claude Code CLI (delegated), native Claude
chats, OpenAI and Codex native chats (Responses), and provider-row chats over
chat completions (OpenRouter, xAI, custom OpenAI-compatible servers).

All new fields are optional. A client that ignores them behaves as before.

## 1. `model_info`: the served model

The event feed's `{"type": "system", "subtype": "model_info"}` note (main
conversation only; subagents never send one) still carries `model`,
`requested_model` and `provider`. `model` is now the model **the provider says
answered**, not the requested one, as soon as the provider reports it:

| Field | Type | Meaning |
|---|---|---|
| `model` | string | The model that answered (served), else the requested one |
| `requested_model` | string \| null | What the chat asked for (unchanged) |
| `provider` | string \| null | The chat's provider row id, null for Claude (unchanged) |
| `served_model` | string, optional | Present when the provider reported the answering model; equals `model` |
| `rerouted` | bool, optional | Present with `served_model`. `true` when the backend judged it a different model from `requested_model` |
| `reroute_reason` | string, optional | Why, when known: `"refusal_fallback"` (the provider switched models after a safety check) or `"availability"` (the CLI fell back because the model was unavailable) |

```json
{"type": "system", "subtype": "model_info",
 "data": {"model": "gpt-5.2", "requested_model": "gpt-5.5", "provider": "prov_1",
          "served_model": "gpt-5.2", "rerouted": true}}
```

The note is deduplicated: it is sent again only when the model changes.
Clients should prefer `rerouted` over their own family comparison when it is
present. When it is absent, keep the old `isReroute` heuristic.

What counts as the same model (`fluid.engine.guardrail.same_model`): case, a
provider prefix (`openai/`), a context suffix (`[1m]`), an OpenRouter variant
(`:free`) and a dated snapshot (`-20251001`, `-2026-08-01`) are ignored. A
longer id is the same model (`gemini-3-pro` → `gemini-3-pro-preview`) unless
the extra words name a smaller tier (`-mini`, `-nano`, `-lite`, `-flash`, ...)
or a newer version number. A family alias (`opus`) matches any model of that
family. When either side is unknown, it counts as the same.

Where the served model comes from:
- Anthropic: `message_start.message.model`.
- Responses (OpenAI, Codex): `response.model` on the terminal event.
- Chat completions: the chunks' `model`.
- Claude CLI: the assistant frame's `message.model` (not `<synthetic>`), plus the CLI's
  `system/model_refusal_fallback` and `system/model_fallback` frames.

A turn answered by exactly the requested id sends nothing new.

## 2. `model_rerouted`: a silent reroute (persisted)

The first time in a turn that a served model is judged a reroute, the event
feed also gets one persisted system note. It replays with the history, through
the generic system card's `data.content`:

```json
{"type": "system", "subtype": "model_rerouted",
 "data": {"content": "OpenAI answered with gpt-5.2 instead of gpt-5.5.",
          "provider": "openai_responses", "provider_label": "OpenAI",
          "requested_model": "gpt-5.5", "served_model": "gpt-5.2",
          "reason": null, "category": null}}
```

- `content` adds ` Safety check: <category>.` when the provider named one.
- `provider` is the engine's client name (`anthropic`, `openai_responses`, `openrouter`, `claude` for the CLI).
- `provider_label` is the name to show. For a provider-row chat it comes from the row type (OpenAI, OpenRouter, xAI), or from the row's own name for a `custom` server.

The turn is never blocked.

## 3. A blocked call: the `ModelGuardrail` prompt

When the provider's safety check declines a call (see §5 for the triggers), the turn pauses. It
asks through the ordinary permission machinery, so it is a durable interaction
row: it survives a reload, shows on the phone and on other workers, goes to
connector platforms, and wakes the turn the same way as any other permission
answer.

Socket frame (`permission_request`, also in `get_pending_permissions`):

```json
{"type": "permission_request", "session_id": "…",
 "request_id": "guardrail-<tool_use_id>",
 "tool_name": "ModelGuardrail", "display_name": "Safety check",
 "deferrable": false,
 "title": "Blocked by Anthropic's safety check (cyber). Continue this turn on claude-opus-4-8?",
 "description": "<same as title>",
 "decision_reason": "<the provider's explanation, else the message>",
 "tool_input": {
   "guardrail": true,
   "provider": "anthropic",
   "provider_label": "Anthropic",
   "category": "cyber",
   "explanation": "This request looks like offensive security work.",
   "requested_model": "claude-opus-5-5",
   "suggested_model": "claude-opus-4-8",
   "suggested_by": "provider",
   "request_id": "req_…",
   "partial_output_discarded": false,
   "message": "Blocked by Anthropic's safety check (cyber). Continue this turn on claude-opus-4-8?"
 }}
```

| `tool_input` field | Meaning |
|---|---|
| `guardrail` | Always `true`; lets a client identify the card without matching the tool name |
| `provider`, `provider_label` | As in §2 |
| `category` | Anthropic: `cyber`, `bio`, `frontier_llm`, `reasoning_extraction`. OpenAI and chat completions: `content_filter`. May be null |
| `explanation` | The provider's own text, may be null |
| `requested_model` | The model that was declined |
| `suggested_model` | The model Continue retries on (never null in a prompt that reaches a client) |
| `suggested_by` | `"provider"` (the provider recommended it) or `"app"` (the app's next-best, see below) |
| `request_id` | The provider's request id, when known (for support) |
| `partial_output_discarded` | `true` when the provider had streamed part of a reply before declining; that part is dropped |
| `message` | One plain sentence to show |

`remember_scopes` is always `["none"]`: the prompt is a one-shot safety check.
Saved rules and permission modes never answer it, and that includes Bypass.
`deferrable` is `false`: a `defer` answer is refused, and the card stays open.

### Answering

Send the ordinary `permission_response`:

- `decision: "allow"` means **Continue**. The blocked call is retried on
  `suggested_model`, and the rest of this turn stays on it. The next turn goes
  back to the chat's own model; the chat's saved model never changes.
- `decision: "deny"` means **Stop**. The turn ends with `stop_reason: "refusal"`
  (the `done` metadata and the engine's `TurnComplete`).

`permission_response` carries no edited input today, so Continue always means
the `suggested_model` shown. The engine already accepts a different choice
through `updated_input.model`, if the respond API ever carries one.

Owner chats (where other approvals are asked in words, `assistant_confirm`)
still show this prompt as the Continue / Stop button card, answered only by its
request id: `owner_confirm.asks_in_words` is false for it, so it never carries
the `assistant_confirm` marker and is never in the typed approval set. A message
typed while the card waits is an ordinary message, as in any chat: it queues
behind the live turn (Interrupt / Unsend), never settles the card, and runs as
its own turn on the chat's model once the card is answered.

### How the suggested model is chosen

1. **The provider's recommendation.** This is Anthropic `stop_details.recommended_model`
   when present (the API sends it only to a request that asked for
   `fallbacks`, which AI Shuffle never does). Otherwise, for Anthropic and for
   the Claude CLI, whose `model_refusal_no_fallback` frame names no model, it
   is the per-category table Claude Code ships
   (`fluid.engine.guardrail._ANTHROPIC_FALLBACKS`). For example, `claude-opus-5-5`
   with `cyber` goes to `claude-opus-4-8`, and `bio` or `frontier_llm` goes to
   `claude-opus-5`. A target that is the same model as the request does not
   count. `suggested_by: "provider"`.
2. **Else the app's next-best on the same account** (`suggested_by: "app"`):
   - Claude: one step down in the family. `claude-fable-5` and
     `claude-opus-5-5` go to `claude-opus-5`, `claude-opus-5` goes to
     `claude-opus-4-8`, and `claude-sonnet-5-5` goes to `claude-sonnet-5`.
     Aliases resolve first, so `opus` is `claude-opus-5`.
   - A provider row: the model listed after the requested one on the same row,
     else the row's first other model.
3. **Nothing to offer** (category `reasoning_extraction`, which asks for a
   rephrase rather than another model, or no other model on the account): no
   card is shown. The turn stops with the note in §4.

The provider's own server-side fallback is never turned on. The Claude CLI
runs with `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1`, and native requests never
send `fallbacks`.

### Limits and unattended turns

- At most 2 prompts per turn. A third block in the same turn stops, with the
  reason "blocked again after a retry".
- With no one to answer, the turn stops at once with the §4 note and does not
  hang. This covers a session with no approver wired (scripts, a bare
  adapter), Away mode (denied straight away with its usual explanation), and
  a delegated turn nobody is watching (the unwatched permission timeout).
- An unanswered prompt expires on the normal permission timeout and stops the
  turn. An interrupt while the card is open ends the turn as interrupted.
- Claude CLI: Continue sends `set_model` to the CLI and waits for the CLI to
  acknowledge it (the usual setting timeout) before re-sending the turn's user
  message; the next turn's settings resync puts the chat's model back. If the
  CLI refuses the switch, does not answer in time, exits, or the person
  presses Stop meanwhile, the turn is not re-sent: it stops with the §4 note,
  and its `reason` says what happened to the switch. A Stop here ends this
  turn only.
- Usage: the declined call is billed and counted. On Continue the turn's
  totals (tokens, cost, model calls) are the declined call's plus the retry's,
  on every provider and the Claude CLI.

## 3b. `attempt_discarded`: the declined reply leaves the transcript (persisted)

Whatever the declined call streamed (text, thinking, tool calls that never ran)
is not the turn's reply, whichever way the person answers. Before the card is
shown the feed records:

```json
{"type": "system", "subtype": "attempt_discarded", "data": {"reason": "guardrail"}}
```

It has no card of its own. Clients remove every assistant text, thinking and
tool message that came after the last attempt boundary (the turn start, a user
message, a tool result or a `provider_retry` note), the same fold as a
connection-drop retry, live and on reload. The REST transcript, the session
list's open-question marker and the web reducer all apply it, so every client
and the other worker's view drop the same content. Usage is not changed.

## 4. `guardrail_blocked`: the outcome (persisted)

Every block that reached a decision leaves one persisted note, whether the
answer was Continue or Stop. It replays with the history:

```json
{"type": "system", "subtype": "guardrail_blocked",
 "data": {"content": "Blocked by Anthropic's safety check (cyber). Continued this turn on claude-opus-4-8.",
          "outcome": "continued",
          "provider": "anthropic", "provider_label": "Anthropic",
          "category": "cyber", "explanation": "…",
          "requested_model": "claude-opus-5-5",
          "suggested_model": "claude-opus-4-8", "retry_model": "claude-opus-4-8",
          "reason": null, "request_id": "req_…", "turn_id": "…"}}
```

- `outcome`: `"continued"`, with `retry_model` set, or `"stopped"`.
- For a stop, `content` ends with `The turn stopped.` and `reason` says why:
  - the deny text (`User denied permission`);
  - `no one was there to approve continuing on another model`;
  - `no other model to continue on`;
  - `Permission request timed out`;
  - the Away-mode explanation;
  - `blocked again after a retry`.

It is the main conversation's note even while a subagent is running.

## 5. What counts as a block

- **Anthropic** (native): `stop_reason: "refusal"`. `stop_details`
  (`type`, `category`, `explanation`, `recommended_model`) arrives on the
  streamed `message_delta`.
- **OpenAI Responses / Codex**: `response.incomplete` with
  `incomplete_details.reason: "content_filter"` before any output.
- **Chat completions** (OpenRouter, xAI, custom): `finish_reason: "content_filter"` before any output.
- **Claude CLI**: `system/model_refusal_no_fallback`, or an assistant frame with
  `stop_reason: "refusal"`. A `system/model_refusal_fallback` (a CLI that
  ignored the opt-out) or `system/model_fallback` (model unavailable) means the
  CLI already answered on another model. It is reported as a reroute (§2),
  not a block, unless its `scope` is `"local"`: that is a subagent's own
  fallback, not the turn's model, and it is ignored.

A chat-completions stream that ends on `[DONE]` without a usage chunk is read
the same way: a filter before any output is a block.

A content filter that cuts a reply after some text has streamed is left as
the existing truncated-reply handling. It does not raise a card.

## 6. Engine events (fluid)

For fluid hosts other than the app:

- `ModelServed(requested_model, served_model, provider, rerouted, reason, category)`
  is sent when the served model is news.
- `PermissionRequest(name="ModelGuardrail", input=<the tool_input above, without suggested_by>, risk="safe", reason=<message>)`.
  Answer it with `RespondPermission(decision="allow" | "deny", updated_input={"model": …}?)`.
- `GuardrailBlocked(provider, requested_model, outcome, category, explanation, suggested_model, retry_model, reason, request_id)`.
- `AttemptDiscarded(reason="guardrail")` before the prompt: drop what the
  declined call streamed (§3b).
- `TurnComplete(stop_reason="refusal")` after a stop.

## Trying it

The dev loop's fake AI plays both cases: `[script:guardrail-refusal]` and
`[script:guardrail-reroute]` (or `served_model=ID` on any scenario tag). See
the fake AI section of `docs/internal/dev-loop.md`.
