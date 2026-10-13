# Backlog triage — 2026-09-19

This records the triage of 42 issues and three existing pull requests, initially checked against `main` (`724d5a5a3`), and the subsequent fixes and GitHub closeout. Findings below distinguish source review, automated tests, and previously recorded runtime acceptance. No live model calls or production-data repair were performed in this pass.

## Current status and closure candidates

- **Merged #1416** — final merge `f70b50a85709389a925e12df1d87b1205b5a1343`. Review found no merge-blocking code defect. Its live-acceptance follow-ups remain open where the issue acceptance requires them.
- **Merged #1411** — final merge `26cfb028580f9b16d4b82cd47576bd615d4ec1f4`. README links and technical positioning were source-consistent; no product test was required.
- **Closed #1354, #1383, #1406** — final CI evidence is tied to `86468b7e2`.
- **#1389 and #1415** — fixes are present and the focused 35-test run passes, including real Windows cwd-lock coverage for #1415. Record the run and close.
- **#1412–#1414** — fixes are prepared and all 35 focused tests pass, including real code-mode approval callbacks, hook rewrites, denial, invalid inputs, and ordinary tool execution during verifier outage. Close upon merge of this batch.
- **Merged #1419; closed #1417** — devalue 5.9.2 landed as `c9ce53119`. JavaScript checks and full browser run `35468962148` passed, including merge readiness.

**Acceptance pending rather than closure:** #1407 has a sound durable claim design in #1416, but the issue explicitly requires real native producer normal/interrupt/kill/reconnect orderings. #1374 remains open because keyboard plus local/remote selection evidence is not recorded. #1341 has a canonical-ID implementation and focused tests but remains open for cross-worker adoption and the zero-message runner-menu behavior documented in review. The `GetUsage` implementation is in place and integration tests are underway; usage issues remain open until those checks finish.

## PR review

### #1416 — Address audit accounting, UI, and network recovery findings (merged as `f70b50a85709389a925e12df1d87b1205b5a1343`)

The diff is coherent with the stated scope. The usage path captures an execution-attempt key before work, claims `usage_log` and session counters in one transaction, and lets kill and normal finalization race on the same unique key. The PostgreSQL concurrency and rollback tests are useful. The historical dedupe command is intentionally read-only and correctly reports rows without identity as **not assessable**; it cannot repair the four historical duplicate cases from equal totals.

The merge had no code-review blocker. The remaining work is acceptance, not a reason to reopen the merge: the tests use synthetic events or database settlement calls, while #1407 requires the actual native producer and cancellation/reconnect finalizers in both orders and concurrently. The PR body records this handoff. Do not close #1407 or describe historical rows as reconciled from this merge.

The network change binds recovery to signed network/device identity and puts create, leave, and enrollment under the same reentrant transition lock. The added race test exercises a switch after initial proof validation. The final CI evidence tied to `86468b7e2` was sufficient to close #1383; it does not prove every external peer/reconnect schedule.

The UI fixes are narrow and reviewable. The long-history navigator keeps all options in a native select and adds first/last/previous/next controls, while the browser evidence is only 31 real targets; the component test covers 1,000 targets. Keep #1378 open until the requested hundreds/thousands real UI pointer and keyboard acceptance is recorded. Keep #1374 open too: keyboard traversal and local/remote selection evidence are not recorded.

`git diff --check origin/main..86468b7e2` is clean. No full test or live-model run was performed during this triage.

### #1411 — README positioning and Fluid compaction (merged as `26cfb028580f9b16d4b82cd47576bd615d4ec1f4`)

The README-only change is close to ready. Every repository-relative link checked from the PR tree exists, including the Fluid and network documentation. The app/harness/provider split, native versus delegated paths, recoverable compaction ladder, bounded history expansion, and provider-dependent billing language match the source and Fluid documentation at the reviewed snapshot.

Keep two claims qualified as written: “code mode and continual learning default on” applies only to eligible native AI Shuffle sessions, and the README does say that; “skills are imported in a verification kernel” describes the intended control, while #1414 showed an exception path that was subsequently implemented with strengthened tests underway. The README appropriately says code mode is not an OS sandbox and avoids performance-superiority claims. The merged documentation is ready; no product test was needed.

## Issue-by-issue disposition

| Issue | Recommendation | Evidence and bounded next step |
|---|---|---|
| #1237 | Keep open — acceptance umbrella | Live round-9 checks, cache behavior, watchdogs, preview access, memory/skills, and pre-existing flakes remain explicitly unrun or unresolved. Run only the listed provider-approved acceptance matrix and close individual follow-ups with evidence. |
| #1254 | Keep open — umbrella/security scope | #1272 covers only a scoped SSH/Linux-folder slice; the issue still requires fail-closed grants, isolated execution, revocation, delegated/native parity, and real approval coverage. Track #1267/#1266/#1310 separately. |
| #1267 | Keep open — product/security enhancement | Windows/macOS confinement, typed secret brokers, owner-device routing, HTTPS deployment, and isolated shells are explicitly outstanding. Define one platform broker and its confinement tests before implementation. |
| #1281 | Keep open — confirmed performance defect | Source and 403 polling logs prove a two-second empty/denied loop; no fix or browser verification exists. Add event-driven refresh/backoff, then count empty, denied, pending, reconnect, and hidden-tab requests. |
| #1282 | Keep open — measurement first | Multiple source costs are credible but no frame-time, heap, DOM, or hidden-traffic benchmark was run. Measure clean/dirty hidden cards and long histories before changing stores or transcript rendering. |
| #1284 | Keep open — performance investigation | Per-event PostgreSQL commits and unbounded resume replay are source-confirmed, but production cost is unmeasured. Benchmark native/delegated producers, WAL, loop lag, replay ordering, and bounded eviction on isolated PostgreSQL. |
| #1285 | Keep open — dependent measurement | PR #1240 must be resolved first; the audit explicitly says not to duplicate it. After that merge, measure exchange snapshots, SQL plans, cursor checks, and blocking peer probes on representative data. |
| #1286 | Keep open — telemetry/enhancement | Whole-stack attribution and repeated journal-quota warnings are observed, but the current process walk is not an idle baseline. Design opt-in per-role metrics and bounded warning/retention telemetry; do not change the quota from this audit alone. |
| #1298 | Keep open — needs reproduction | API 36 username and consent failures are timing-sensitive and production files were unchanged. Capture matcher state and monotonic timing, rerun API 36, and preserve fail-closed consent behavior. |
| #1311 | Keep open — needs reproduction | CHECKPOINT timed out before recovery assertions while a source build was active; cause is unproven. Re-run on an idle worker with disk/checkpoint timings and retain nonzero failure status. |
| #1323 | Keep open — needs repro/fix | User report says remote selected-worker Pull is missing, with no independent reproduction or root cause. Reproduce with a remote worker/branch and trace the header request's worker/ownership routing. |
| #1329 | Keep open — incident unresolved | Fresh browser and isolated preview render; the original desktop renderer was not inspected, and `LazyPanel` recovery is not proof of the incident fix. Capture original console/DOM if blank state recurs. |
| #1341 | Keep open — partial fix, acceptance pending | Main now resolves canonical IDs with `get_project_v2` and `require_origin_access`; focused tests cover local, remote-only, access denial, and missing IDs. Review evidence still keeps the issue open for cross-worker adoption and the zero-message runner-menu behavior. Verify those paths before closing. |
| #1345 | Keep open — needs Windows repro | Development-only Vite startup stayed blank despite 200 module/CSS responses; production and targeted tests pass. Reproduce with a clean Windows dev server and capture browser module/runtime errors. |
| #1354 | Closed | Svelte check warning was source-reproduced; the PR made both bound element refs reactive, and final CI for `86468b7e2` was recorded. |
| #1365 | Keep open — portability fixture | The Windows failure is in symlink fixture setup, not proof of an upload escape. Replace `/etc/passwd` with a platform-native outside target and run the opt-in suite without elevation. |
| #1373 | Keep open — host/tool contract defect | A stopped-worker SendMessage was rejected despite documentation promising resume; no successful/interrupted state matrix exists. Test normal completion, provider interruption, exactly-once delivery, and unsupported-state errors. |
| #1374 | Keep open — acceptance evidence missing | PR adds `ResizeObserver` viewport clamping and reports 390×844/1280×800 checks, but keyboard traversal and local/remote worker selection are not recorded. Run that narrow matrix before closing. |
| #1378 | Keep open — partial fix only | PR adds bounded controls and a 1,000-item component test, but real browser evidence used 31 targets and the body explicitly leaves hundreds/thousands acceptance open. Run real desktop/compact pointer and keyboard checks. |
| #1383 | Closed | The diff binds proof network/device identity, locks transitions, and tests switching after validation; final CI for `86468b7e2` was recorded. Retain barrier coverage for both leave/create and leave/join schedules as future regression coverage. |
| #1384 | Keep open — owner/product decision | Play deletion submission is blocked on a public actionable URL, in-app path, identity-provider scope, and an owner decision on manual versus self-service fulfillment. Do not claim deletion until the live path is verified. |
| #1386 | Keep open — confirmed policy/performance gap | Forced refresh bypasses the five-minute freshness window and live logs show repeated provider requests/403s. Define cooldown, negative-capability caching, and stale-but-responsive UI, then test refresh/failure/concurrent-worker paths. |
| #1387 | Keep open — implementation/tests underway | `GetUsage` is implemented, but integration tests are still underway. Usage sources still need explicit source/scope/credential/cadence semantics and negative-capability behavior before closure. |
| #1389 | Closure candidate — focused fix passes | The fix validates `profile_id` before insert and leaves no `s-invalid` row; the focused 35-test run passes. Record the exact run/head and close. |
| #1391 | Keep open — storage/security blocker | D1 descriptor deletion is not proof that private R2 bytes are gone; key namespace and producer are not established. Identify the deployed namespace, implement tenant-scoped retryable purge/read-back, then remove the guard only after isolated-account tests. |
| #1392 | Keep open — owner decision after #1400 | The measured CI-cost implementation from #1400 is merged, but timings and artifact/cache snapshots still do not establish invoice spend or settle Sticky Disk/artifact-retention decisions. Set a monthly ceiling and billing baseline before further workflow changes; preserve required checks and avoid deleting release assets. |
| #1394 | Keep open — needs live verification | API 36 screenshot evidence shows a contrast issue, but instrumentation/theme state may be stale. Reproduce in the native app across theme/activity transitions and preserve dark-theme behavior. |
| #1398 | Keep open — needs credential-scope repro | Both fal consumers fail to see a reportedly configured key, but credential validity/worker scope is unknown. Trace fresh and reused Node workers after settings update without exposing the key; add producing-path coverage. |
| #1399 | Keep open — product enhancement/live capability | Native Responses image generation is a requested capability, not a confirmed provider feature. Validate model/account support, request serialization, output persistence, reopen/download, cancellation, and unsupported handling with fal unavailable. |
| #1401 | Keep open — owner/configuration decision | Main has no active protection, but path-filtered checks require an aggregate gate design. Choose required checks/bypass policy and exercise ordinary, unrelated, labelled, failed, and updated PRs before enabling rules. |
| #1402 | Keep open — confirmed UI regression | Source and identical deployed bundle show the separate Use/Dismiss row; desktop in-memory state and an actual generated suggestion were not checked. Restore inline-only behavior while preserving touch/keyboard acceptance and test both clients. |
| #1403 | Keep open — bounded workflow fix | Cause is clear: untagged Play builds inherit `0.8.9`. Choose monotonic display-version semantics, add workflow/build regression coverage, and verify signing/internal gates without rebuilding solely for the cosmetic issue. |
| #1405 | Keep open — partial routing fix | #1408 covers chat file downloads, but generated media/gallery URLs still use the viewer worker. Route image/video/audio players and downloads through session ownership and verify remote web/mobile range/auth behavior. |
| #1406 | Closed | Reproduction was exact in isolated normalization/report functions; the PR uses one exclusive-counter convention, bounds fractions, and repairs historical reports. Final CI for `86468b7e2` covered the provider adapter paths. |
| #1407 | Keep open — implementation/acceptance pending | PR #1416 adds a durable unique attempt claim and safe historical “not assessable” reporting, but its tests do not run the real native producer through normal/interrupt/kill/reconnect orderings. Run that bounded matrix before closing; do not auto-reconcile old rows from matching totals. |
| #1409 | Keep open — confirmed Windows lifecycle defect | Two detached Git fsmonitor daemons held a disposable profile after preview shutdown. Identify only preview-owned daemon PIDs and add a cleanup regression that removes the profile without touching installed workers. |
| #1412 | Acceptance pending — fix prepared | The bridge fix passes the tool context directory so relative paths use the same policy base; 32 focused tests pass across the three fixes, with strengthened parity tests underway. Close after merge, those tests, and final checks. |
| #1413 | Acceptance pending — fix prepared | The approval bridge now carries revised arguments through execution; 32 focused tests pass across the three fixes, with strengthened allow/deny/rewrite/hook tests underway. Close after merge, those tests, and final checks. |
| #1414 | Acceptance pending — fix prepared | The verifier-exception path now withholds all skills rather than mounting unverified content; 32 focused tests pass across the three fixes, with strengthened degraded-mode tests underway. Close after merge, those tests, and final checks. |
| #1415 | Closure candidate — focused fix passes | The fixture now provides `.items()` and the focused 35-test run includes the real Windows owned-process cwd-lock cases. Record the exact run/head and close; keep #1237's symlink-privilege case separate. |
| #1417 | Closed via merged #1419 | Lockfile updated to devalue 5.9.2; JavaScript and full browser/readiness checks passed on 326ce1259. |
| #1418 | Keep open — needs bounded repro | Live Luna evidence proves a no-first-frame watchdog caused a replacement child, but does not prove duplicate accounting or provider billing; both children have distinct interrupted settlements. Reproduce stream stall/buffering, verify retry cancellation and parent wake, and correlate with #1407 without conflating them. |

## Recommended order under a 13% usage budget

### Local validation for the prepared batch

- Peer session controls and Windows checkout lifecycle: 35 passed, including both real owned-process cwd-lock cases.
- Fluid code-mode dispatch and continual skills: 35 passed, including actual session approval rewrites, pre-hook ordering, structured denials, invalid rewrite payloads, path-rule resolution, and tools remaining callable during verification outage.
- Usage app/MCP/native integration: 54 passed, 264 deselected; standalone Fluid usage: 4 passed; relevant usage monitor tests: 12 passed.
- Existing canonical worktree listing regressions: 10 passed, 66 deselected. These support the implemented API but do not complete the broader cross-worker acceptance issue.
- `GetUsage` has native and delegated registration, cache-only reads, explicit standalone unavailability, and provider-only freshness coverage. An independent source review found no concrete blockers. It does not complete the broader provider UI/cooldown work in #1386/#1387.

### Next work

1. Record closure evidence for **#1389 and #1415**, then finish the strengthened focused tests for **#1412–#1414** and integration tests for `GetUsage`.
2. The dependency update is complete: #1419 merged and #1417 closed after full CI.
3. Spend live/runtime budget on **#1407 and #1418** together, because cancellation settlement and Luna retry behavior can interact; keep provider usage evidence separate from app estimates.
4. Leave performance audits, product decisions, external storage/Play work, and broad resource-access umbrellas open until their stated owner or runtime evidence exists.

## Proposed replacement issue-filing prompt

This text is a proposal for a future issue-filing instruction; this triage did not modify any agent prompt:

> Before filing an issue, search existing open and closed issues and pull requests for the same root cause. File only when you have source evidence or a reproducible observation with a concrete impact. If the root cause is already tracked, update the existing issue instead of opening a duplicate. Label the scope as confirmed, suspected, or enhancement. Do not file environment-only failures without supporting evidence. Put uncertain observations in the task summary and state what would confirm them. Do not make autonomous fixes outside the files assigned to the task.
