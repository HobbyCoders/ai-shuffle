# Audit remediation — 4 September 2026

The source, configuration, dependency, documentation and browser audit started at
`1827f744`. This change addresses the 25 finding groups below. It preserves
working compatibility interfaces rather than renaming persisted identifiers.

## Changes

| Findings | Remediation |
| --- | --- |
| F1 | Strip all inline event handlers from rendered markdown, including buttons with app CSS classes. |
| B1 | Authorize file/media reads against the API user's project origins and local checkouts, rather than global served roots. |
| DEP-01 | Upgrade FastAPI and Starlette together; refresh vulnerable installation bootstrap tooling. |
| F2 | Preserve untitled Notes drafts, guard navigation, serialize saves, and reject stale selection/attachment results. |
| B2 | Prefer full unified agent configurations during legacy imports; provide a canonical agents export while retaining old formats. |
| BACKUP-01/02 | Include current memory/settings and project media, exclude recursive backup destinations, honor explicit sidecar commands and document restore. |
| SETUP-01/02/03/04 | Install required Fluid and Node tools, stage complete releases, align runtime prerequisites, and rebuild existing frontend output during setup/update. |
| B3 | Emit lifecycle events from winning turn and interaction transitions; match automation event/connector/filter on the same trigger and prevent recursive automation runs. |
| B4 | Record actual setup, logout, password and API key lifecycle actions; keep credentials out of audit details and preserve successful responses if audit recording fails. |
| B5 | Migrate legacy SQLite data in foreign-key order within one transaction; preserve destination schema version and fail visibly on unsupported columns. |
| F3 | Restore session import, tag management and project knowledge through quick navigation. Knowledge requires a selected project. |
| F5 | Validate canonical SSE envelopes, retain unknown-event forward compatibility, remove unused handwritten WebSocket types, and make generation checks independent of line endings. |
| DEP-02 | Declare Pillow for native computer use and include it in availability checks. |
| CONFIG-01 | Honor START_CHROME=0 in container startup. |
| CI-01/02 | Run relevant frontend/browser/tool tests and builds plus contract/dependency checks; remove the secret hook's nonexistent-baseline requirement. |
| TOOLS-01 | Remove tracked compiled tools output and compile cleanly from source for deployment. |
| DOCS-01 | Replace removed-provider/tool references; synchronize installation/architecture pages and parse CRLF documentation metadata. |
| F4/B6/B7 | Remove proven retired UI/adapter/subagent implementations while preserving active translation/note resolution; correct architecture terminology. |

Additional fixes retain API response error details, pause hidden log polling,
allow browser zoom, and remove the empty root lockfile. A browser check also
found old branding in the restored session-import dialog; that text is updated.
Docker Node stages/runtime now use Node 22, matching CI's supported release
line; Node 20 is listed as end-of-life in the official
[Node.js release inventory](https://nodejs.org/en/about/previous-releases).

The original F5 report overstated generated schema drift. Regeneration showed
the content was current: CRLF versus LF comparison caused the failed check.
The missing transport validation and dead handwritten types were real findings.

## Compatibility and terminology

- **Agent** is the unified configuration. `profile_id`, profiles and subagent
  import/export shapes remain compatibility interfaces, not separate new schemas.
- **Project origin** identifies the project; **checkout/worktree** is a local
  materialization. File authorization must honor both the identity and its scope.
- **Provider** supplies models; credentials authorize provider access. Removing a
  retired adapter does not imply removing the SDK used by active translation.
- **Commands** request changes; durable **events** describe accepted transitions.
  SSE carries transcript events while WebSocket controls remain active.

Lifecycle delivery is bounded and best effort, not a durable outbox. Process
failure can lose pending external notifications. Stable event IDs support
deduplication. Automation-owned sessions do not recursively trigger further
event automations; intentional chaining uses explicit action steps.
Notifications for another install's connector use the existing PostgreSQL
gateway, recheck ownership/subscription at the receiver, and deliver only to
linked chats. They never redispatch global webhooks or automations.

## Verification boundaries

- Frontend: 2,080 tests passed; type checking found zero errors and two existing
  initial-prop warnings. Production build and contract check passed. After final
  documentation synchronization, the six registry tests and production build
  passed again.
- Full offline backend run: 9,407 passed, 64 skipped, 12 failed. The failures were
  outdated assumptions about flattened FastAPI routes (eight) and inherited
  DATA_DIR in default-setting tests (four). Those were corrected; all 232 tests
  in the affected route modules and 73 configuration/packaging/supervisor tests
  passed on rerun (five platform skips).
- Final combined lifecycle, connector relay, gateway, automation and security
  regression run: 158 passed. Shared audit/auth attribution checks: 72 passed.
- Browser MCP: 172 passed, 21 skipped; clean tools and browser builds passed.
  Public Python and all three npm dependency scans reported no known
  vulnerabilities at the time of validation.

The existing local environment's incomplete aiohttp and browser-MCP installs
were also repaired. Python dependency consistency/import checks and browser-MCP
build passed, and the original main checkout restarted with HTTP 200 health.
The remediation source stays isolated on the PR branch; monitoring is paused.

Regression tests use mocked providers and disposable test data. No paid model,
generation, voice or external connector action was executed. The full backend
run blocks outbound sockets and disables real PostgreSQL initialization.

Browser verification mounts the real fixed Notes, navigation and management
components against in-memory APIs on a separate localhost port. It confirmed
draft navigation protection and remount recovery, stripped handler attributes,
all three restored dialogs, and project gating. It does not claim a paid
provider end-to-end test.

Docker image execution, a real PostgreSQL migration/restore, and physical phone
input remain untested. Backup and release selection use disposable filesystem
fixtures; migration tests use synthetic database adapters. Vendored Fluid was
reviewed at integration boundaries, not exhaustively audited upstream.

Public dependency scans do not cover the locally editable Fluid distribution.
Compatibility names and currently used dependencies remain intentionally.

## Review follow-ups

A review of this change found gaps the first pass left open. Each is fixed here:

- B1 also applies to the `/api/v1/workspace` file browser routes, which
  previously let a project-scoped key list, read, upload, rename and delete
  under any project in the shared workspace.
- B3 connector-event automations fire only for sessions linked to a connector.
  Every turn on every session now emits lifecycle events, so an unfiltered
  trigger would otherwise start a run after each wake, queue and subagent turn.
- F2 restores a saved draft of an existing note only while the server still
  holds the version the draft was taken from; a note edited elsewhere wins.
- F1 left the documentation code-copy buttons without a handler once inline
  handlers were stripped; they now use a delegated listener like chat markdown.
- B2 legacy subagent imports write only the fields the file carries, so an older
  export no longer clears skills, memory and MCP bindings on overwrite, and a
  profile never keeps a reference to an agent whose import failed.
- SETUP a first run of `run_local.bat` or `run_local.sh` installs and builds
  before starting instead of exiting with a message to rerun with `--setup`.
- CONFIG-01 `START_CHROME` is a real setting again: the app stops warning that
  it was removed and, when it is `0`, neither offers the browser tool suite to
  sessions nor starts a native Chromium supervisor.
- CI-02 npm audits fail on high severity and above so a low-severity advisory
  in a transitive dependency does not block unrelated changes.
- The file and media API docs now state which routes a scoped key can use.
