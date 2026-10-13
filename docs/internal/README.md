# Internal documentation

HobbyCoders team only. Nothing in this folder ships to buyers: the Docker
image excludes `docs/`, and the update source archive carries only
`docs/content` (`scripts/source_archive.py`).

## Guides

| Page | What it is |
| --- | --- |
| [DEVELOPMENT.md](DEVELOPMENT.md) | The developer half of the former README: the app and the Fluid harness, how sessions run, source builds, repository layout. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Branches, commits, pull requests, code style and test commands. |
| [LOCAL_SETUP.md](LOCAL_SETUP.md) | Native source setup without Docker, including browser automation on native installs. |
| [architecture.md](architecture.md) | Technical architecture (formerly an in-app page). |
| [changelog-template.md](changelog-template.md) | How to write a release's changelog entry and notes. |
| [acceptance-clean-box.md](acceptance-clean-box.md) | The phase 1 exit test: a stranger installs with Docker Compose on clean Ubuntu from the docs alone. |
| [computer-use.md](computer-use.md) | Native computer use internals. |
| [native-image-generation.md](native-image-generation.md) | Native Responses image generation. |
| [managed-worker-connections.md](managed-worker-connections.md) | Managed worker connection credentials. |
| [managed-worker-credential-repair.md](managed-worker-credential-repair.md) | Reconnecting one revoked account worker on Android. |
| [mobile-web-session-reservations.md](mobile-web-session-reservations.md) | Mobile web session reservations. |

## Audits, handoffs and investigations

- [AUDIT_REMEDIATION_2026-09-04.md](AUDIT_REMEDIATION_2026-09-04.md), [AUDIT_FIX_VERIFICATION.md](AUDIT_FIX_VERIFICATION.md), [HANDOFF_SESSION_AUDIT_ROUND3.md](HANDOFF_SESSION_AUDIT_ROUND3.md)
- [HANDOFF_COMPOSER_SPLIT_CHIPS.md](HANDOFF_COMPOSER_SPLIT_CHIPS.md)
- [PERFORMANCE_REMEDIATION_2026-08.md](PERFORMANCE_REMEDIATION_2026-08.md), [PERFORMANCE_HANDOFF_TESTING.md](PERFORMANCE_HANDOFF_TESTING.md)
- [repair-lifecycle-2026-09-07.md](repair-lifecycle-2026-09-07.md), [private-access-acceptance.md](private-access-acceptance.md), [backlog-triage-2026-09-19.md](backlog-triage-2026-09-19.md)

## Internal pages that stay at their old paths

Code, CI, scripts or tests name these paths, so they stay where they are:

| Page | Referenced by |
| --- | --- |
| [../RESTORE.md](../RESTORE.md) | `scripts/deploy.sh`, `tools/backup-db.sh`, `tools/restore-db.sh`, container tests |
| [../LOCAL_ACCESS.md](../LOCAL_ACCESS.md), [../credential-key-recovery.md](../credential-key-recovery.md) | operator guides linked from other docs |
| [../MEMORY_ARCHITECTURE.md](../MEMORY_ARCHITECTURE.md) | app and test comments |
| [../PERFORMANCE_REVIEW_2026-08.md](../PERFORMANCE_REVIEW_2026-08.md) | app, frontend and test comments |
| [../mobile-desktop-ui-split.md](../mobile-desktop-ui-split.md) | `app/mobile_ui.py`, `frontend/scripts/check-no-mobile-ui.mjs`, `frontend/src/lib/api/runtime.ts`, the Android and iOS `MobileUi` code, and UI code |
| [../provider-usage-sources.md](../provider-usage-sources.md) | `app/core/usage_sources.py` |
| `../network/`, `../ci/`, `../install/`, `../design/`, `../audits/` (historical reviews and mockups live in the private archive) | workflows, the hub, tests and code comments |

## Buyer documentation

Buyer pages live in `docs/content/getting-started/` and render in the app.
The frontend bundles its own copy in `frontend/src/lib/docs/content/`; keep the
two identical (`tests/test_buyer_docs.py` checks it). The Docker Compose page
embeds `docs/install/compose/compose.yaml` and `env.example` verbatim, so a
release that bumps the version updates the page too.
