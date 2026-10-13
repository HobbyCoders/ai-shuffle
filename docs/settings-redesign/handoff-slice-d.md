# Slice D handoff (Integrations, Devices, Logs)

Branch `claude/settings-slice-d` (finished on `settings-slice-d-finish`), based on `032f177a8` (integration branch `claude/settings-ui-redesign-2106ec`).

## Done

- `components/settings/integrations/`: `IntegrationsCollection`, `GitHubDetail` (+ `github-auth.svelte.ts`, a port of GitHubConnection's logic), `ApiUserDetail` with Overview / Run / Access / Sessions tabs, `api-users.svelte.ts` (one draft at a time, create payload, null clearing, one-time key banner, rotate and delete through InlineConfirm, sessions list).
- `components/settings/devices/`: `DevicesCollection` plus details `AccountDetail`, `RequestDetail` (Approve > "IDs match?"; Deny or Revoke in the danger zone), `PairingDetail` (kind, address, QR, countdown, expiry), `ThisWorkerDetail` (Overview rename; Network tab with join, manual address, leave-and-join confirm, keep separate settings, account-network situations), `DeviceDetail` (workers, joining, phones and browsers, account-only, legacy shared database). Logic lives in `devices-model.svelte.ts` (singleton `devicesModel`, ref-counted polling), `device-requests.svelte.ts`, `pairing-codes.svelte.ts`, `account-flow.svelte.ts`, `join-flow.svelte.ts`, `device-status.ts`. A worker revoke raises a `notify.banner` toast with "Open providers".
- AccountPanel and JoinNetworkPanel (setup wizard) now consume `AccountFlow` and `JoinFlow`. Their tests and `routes/setup` tests pass unchanged. AccountPanel lost its unused `bind:profile` and `bind:inventoryReady` props.
- `components/settings/logs/`: `LogsCollection` and `LogDetail` on `logs-model.svelte.ts` (singleton `logsModel`). It has the level select, 300ms debounced search, refresh, export, a live count, "Same logger", and 10s polling only while Settings and the tab are visible. Detail facts are Time / Level / Logger.
- Entries: `entries/integrations.ts` (`integrations.apiUsers` plus a searchSource for GitHub and each API user), `entries/devices.ts` (no entries; searchSource for every device, plus attention for pending requests), `entries/logs.ts` (`logs.level`, `logs.export`; attention for errors).
- Deleted: ApiTab, ApiUserManager (+ test, + a11y test), DevicesSection, AddDevicePanel, WorkerDeviceConfirmations, LogsTab (+ their tests). The tests were ported to `api-users.test.ts`, `IntegrationsCollection.test.ts`, `devices-model.test.ts`, `device-requests.test.ts`, `pairing-codes.test.ts`, `DevicesCollection.test.ts`, `LogsCollection.test.ts`.
- `ConnectorManager.svelte` copy is now an `openSettings({ category: 'integrations' })` button.

## Test status

Finished on `settings-slice-d-finish` (from `claude/settings-slice-d`):

- `npm run check`: 0 errors, 0 warnings.
- `npm test` (full): 370 files, 4362 tests, all pass.
- `npm run build`: passes (`build:mobile` not run, per F0).

## Visual check (done)

`npx vite dev --port 5177` driven by Playwright (Chromium) at 1440x900 in dark and light. An init script stubs `fetch` for `/api/v1/*` (unknown endpoints get 404) and `WebSocket`, sets localStorage `ai-shuffle-layout` = `{"activeView":{"type":"settings"}}` and sessionStorage `settings.nav` = `{category, selected:{[category]: item}, tabs:{"<category>:<item>": tab}}`. Service workers are blocked so the "new version" banner stays out of the shots. Covered: GitHub, API user (Overview, Run, Access, Sessions, draft), Devices (account, pending request, pairing code, this worker Overview and Network, phone, legacy shared database approved and not approved), Logs list and entry. No console errors and no page-level horizontal overflow.

Fixed during the pass:

- Logs listed oldest first: `GET /logs` already sends newest first and the model reversed it. The test fixture had used oldest-first data, which hid the bug; it now uses the server's order.
- A pending request read "Wants to be approved" twice in its header. The status now says "Awaiting your approval", as in the mockup.
- The worker address field on the Network tab cut off its placeholder (160px to 184px).
- The pairing code Address fact showed a cut-off full URL; it now shows host and port, as in the mockup.

Prettier: all slice D files are formatted with the repo's style (tabs, single quotes, width 120, no trailing commas; the repo has no Prettier config or svelte plugin, so the run used a scratch install of `prettier-plugin-svelte` with those options, which leaves F0's `DetailPane.svelte` unchanged). The "Check the ID" indentation in `RequestDetail.svelte` is fixed.

## Remaining

1. Optional: GitHubConnection (setup) still has its own copy of the gh sign-in logic. It could consume `github-auth.svelte.ts`. It was left alone because it is not in slice D's owned list.
2. Small notes from the visual pass, left as they are: the Integrations count does not include an unsaved draft API user (the mockup counts it), and the pairing "Address the device uses" select truncates long labels at this width (the scheme is kept there because it tells http from https).

## Gotchas and requests for F0

- `src/lib/settings/coverage.test.ts` (F0) renders every collection with no data and no selection, and does not call `settingsNav.go(where)`. Any registry entry with an `item` therefore fails. Workaround here: data-bound detail rows have no registry ids, and search reaches them through `searchSource()` targets (item + tab, no anchor). Request: the generic test should call `settingsNav.go({category, ...where})` and accept per-category skips, or leave collections to their slice tests. Slices B and C will hit the same failure.
- InlineConfirm has no `primary` trigger variant. The Approve and Join buttons use the `trigger` snippet with an `st-btn primary` button. Request: `variant="primary"`.
- DetailPane renders an empty "Danger zone" heading whenever `dangerRows` is passed, even if the snippet renders nothing. Pass `dangerRows={cond ? snippet : undefined}`.
- `entries/devices.ts` and `entries/logs.ts` import the singleton models, so the registry pulls in app-data, notifications and the network APIs. Tests that import the registry still pass.
- `account-devices.ts` stays in `deck/cards/settings/` and is reused by `devices-model`.
