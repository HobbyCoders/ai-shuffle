# Slice B (Providers) handoff

Branch `claude/settings-slice-b`, based on `032f177a8` (integration branch
`claude/settings-ui-redesign-2106ec`), finished on `settings-slice-b-finish`. The backend slice
(Claude `off`, fal enabled flag) is merged into the integration branch; this branch codes against its
contract but does not contain it.

Status: the slice is complete. `npm test`, `npm run check` and `npm run build` pass in `frontend/`.

## Done (committed)

- API and store: `ClaudeAuthMode` gains `'off'` (`api/claude-auth.ts`, `CLAUDE_AUTH_MODES`);
  `isClaudeAuthMode` accepts it and `credentialsFromSummary` treats off as no credential
  (`api/agent-credentials.ts`). `stores/providers.svelte.ts` gains `setProviderEnabled` (PATCH
  `{enabled}`, optimistic with rollback, `toggleProvider` removed), `createGateway`,
  `falEnabled`/`falKeyMasked`/`setFalEnabled`/`setFalKey`/`clearFalKey`, `claude` summary with
  `loadClaude`/`setClaudeMode`, `usage` with `loadUsage`. Store and agent-credentials tests updated.
- Extractions in `components/settings/providers/`: `claude-cli-login.svelte.ts` (ClaudeCliPanel now
  consumes it; its tests and setup tests pass), `device-enrollment.svelte.ts` (draft/reconnect rules,
  plus `draftRowId` to resume a draft's own row), `banked-resets.svelte.ts` (idempotency key reused on
  retry, `retryPending`), `loop-guard.svelte.ts`.
- Pure model: `providers-model.ts` (classification, grouping, on-count, connector line, "+" kinds,
  not-connected, Claude modes incl. off, attention), `curation.ts` (keeps context_window, pricing,
  reasoning metadata), `drafts.svelte.ts` (one draft row per add; device draft rows deleted on
  `discardAll`).
- Components: `ProvidersCollection`, `ProviderList`, `ProviderGroupHeader` (Claude
  Subscription | API key | Off, or "N on" + `AddKindMenu`), `CredentialRow` (connector line, switch),
  `NotConnectedRow`, `ProviderDetail`, `NotConnectedDetail`, `CredentialDetail` dispatching to
  `ClaudeSubDetail` (Overview/Models/Usage/Policy), `AnthropicKeyDetail`, `DeviceDetail`
  (Reconnect, Usage with banked resets), `KeyDetail` (connection form auto-save, token Replace,
  dialect, context window, max output; Usage for OpenRouter credits / OpenAI none), `FalDetail`
  (switch, key replace, balance from `/usage/limits` provider `fal-ai`), `DraftDetail` with
  `setup/{KeySetup,DeviceCodeSetup,ClaudeCliSetup,ClaudeCliTerminal,GatewaySetup,FalKeySetup}`,
  `ModelCuration` (manage + setup), `tabs/*`, `providers.css`.
- Registry: `settings/entries/providers.ts` (claudeMode, loop guard rows, CLI sign-out, Anthropic key,
  fal key and balance; `searchSource()` for providers, credentials and their tabs; `attention()`).
- Copy: `AutomationForm.svelte` notice says "Settings > Providers > Claude" and has an
  "Open Claude settings" action; `CredentialStep.svelte:189` reworded.
- Tests passing: `providers-model.test.ts` (17), `device-enrollment.test.ts` (23, ports both device
  flow suites), `banked-resets.test.ts` (15, ports BankedResetsPanel), `claude-cli-login.test.ts` (5),
  `ModelCuration.test.ts` (16, ports ProviderPanel.reasoning-metadata and ProviderAddFlows key flows),
  `stores/providers.svelte.test.ts`, `api/agent-credentials.test.ts`, `ClaudeCliPanel.test.ts`,
  `setup/CredentialStep.test.ts`, `routes/setup/setup.test.ts`.

## Finished on `settings-slice-b-finish`

1. Deleted the replaced components and their tests: `deck/cards/settings/providers/{ProviderList,
   ProviderRow,ProviderPanel,AddProviderModal,ProvidersSection,ClaudeApiKeyRow,OpenAICredits,
   OpenRouterCredits,ManageMediaModelsDrawer,MediaToolsSection,BankedResetsPanel}.svelte`, `flows/*`,
   `components/settings/{ClaudeAuthModePanel,LoopGuardPanel}.svelte` and their tests.
   `SettingsImportDialog.svelte` and its test stay (slice A). No imports of the deleted files remain;
   `AIToolsSettings` is now used only by its own test (slice C deletes both).
   - `frontend/tests/browser-workspace.ts` (the `?provider-login` fixture) mounted `OpenAICredits`; it
     now mounts `tabs/KeyUsage.svelte` for an OpenAI key, and `tests/verify-provider-login.mjs` reads
     the guidance from the Balance help tip (same passive checks: no inputs, no API calls, no polling).
     `.github/workflows/ci-browser-workspace.yml` watches `KeyUsage.svelte` instead. The verifier passes
     at 1440 and 390 against the fixture server.
2. Tests written (all under `components/settings/providers/`):
   - `fake-worker.ts`: test-only fake of the worker's `/api/v1` routes, stubbed at `fetch`, so tests run
     the real store and assert exact requests.
   - `ProvidersCollection.test.ts` (13): grouping and Not connected, a switch on every credential but
     Claude's, PATCH `{enabled}` (never `/toggle`) with rollback, the fal flag (`PUT
     /settings/integrations/fal-ai/enabled`), Claude Off from the header and no save on re-pick, the "+"
     menu and Add provider drafts, read-only, and `assertCategoryCoverage('providers', ...)` with
     subscription mode, CLI signed in, key set, fal key set and loop guard on. Each coverage place
     calls `settingsNav.reset()` first so an item's remembered tab does not carry over.
   - `setup/GatewaySetup.test.ts` (6): name and endpoint required, the typed name on the draft
     provider, `provider_type: 'custom'` with `api_dialect` and token then the models step, the default
     OpenAI dialect with no empty token, a refused create, Cancel.
   - `tabs/ClaudePolicy.test.ts` (9, ports LoopGuardPanel), `ProviderDetail.claude.test.ts` (5, ports
     ClaudeAuthModePanel), `KeyDetail.usage.test.ts` (4, ports OpenRouterCredits and OpenAICredits).
   - `src/lib/settings/coverage.test.ts` skips `providers` in its data-less "every category page"
     loop (`COVERED_WITH_DATA`), since those entries live on credentials that only exist with worker
     data; `ProvidersCollection.test.ts` asserts the same coverage with data.
3. Visual pass at 1440x900, dark and light, with `fetch` and `WebSocket` stubbed (Claude subscription
   and API key, ChatGPT and an OpenAI key, SuperGrok needing reconnect, fal.ai, a gateway, OpenRouter
   not connected): every credential and tab, "+", Add provider, switches and Claude Off. No console
   errors from Settings. Fixed: the ChatGPT plan fact read "pro" (now `planLabel()`, "Pro"), and a new
   gateway's draft header said "New provider" after a name was typed.
4. Prettier: the repo has no Prettier config, so the files use the style they are written in (tabs,
   single quotes, no trailing commas, print width 120) with `prettier-plugin-svelte`:
   `prettier --plugin prettier-plugin-svelte --use-tabs --single-quote --trailing-comma none
   --print-width 120 --write src/lib/components/settings/providers`. Bare `npx prettier` uses
   Prettier's defaults (double quotes, spaces) and reformats everything; do not use it here.
5. The WIP commit is reworded to "Build the Providers collection, details and setups".

## Gotchas

- Credential item ids: `provider:<id>` for provider headers (`provider:claude`, `provider:gw:<rowId>`),
  `mp:<rowId>` for model_providers rows, `claude:subscription`, `claude:api-key`, `fal:key`,
  `draft:<n>` for drafts. Registry entries only use the stable ones; dynamic rows are reached through
  `searchSource()` targets with `item` and `tab`.
- Claude windows from `/usage/limits` report quota remaining (meter shows `100 - utilization`);
  provider windows (ChatGPT, SuperGrok) report quota used.
- `DraftDetail.finish/cancel` select the next item before removing the draft, so the detail never
  renders with a missing provider.
- `KeySetup` creates key rows named after the draft ("OpenAI API key", "OpenAI API key 2"); older rows
  named just "OpenAI" display as "OpenAI API key".
- Worktree note for whoever continues: the shared `DraftHarness.test.svelte` is a test-only component
  (not matched by the vitest include pattern).

## Primitive requests for F0 (worked around locally)

- `TextField` has no `number` type or `inputmode`; context window and max output use a text field with
  validation in `KeyDetail`.
- `RowGroup`'s card class `.st-list` is scoped, so bare groups cannot reuse it; `providers.css` adds
  `.pv-card` with the same look.
- `AgentGateStatus.mode` in `api/client.ts` is typed `'subscription' | 'api_key' | null`; the worker
  can now report `'off'`.
- `ModeStep.svelte` (setup) still says "Settings > Providers > Claude Code mode", asserted in
  `routes/setup/setup.test.ts:196`; left alone (not slice B's file).
