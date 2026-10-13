# Paid CI audit — October 2026

Window: **2026-09-08 00:00 UTC through 2026-10-08 00:00 UTC**, end exclusive.
Refs #1948. PR #1956 added the live-smoke runner and release gate; its diff
changed no CI workflow or job selection.

## Method and limits

Started with `gh issue view 1948 --comments`, `gh pr view 1956` and
`gh workflow list --all`. For each workflow, fetched
`gh run list --workflow <id> --limit 200 --created
"2026-09-08T00:00:00Z..2026-10-07T23:59:59Z" --json
conclusion,createdAt,event,headBranch,name,databaseId`; paginated the Actions
workflow-runs API past 200, then fetched every run's jobs for **every attempt**.
Cross-checked timings with `gh run view 35005402929 --json jobs`.

The table counts only jobs with a Blacksmith runner actually assigned: 4,152
attempts, **17,393.4 elapsed runner-minutes**. Skipped and never-assigned jobs
are excluded; executed cancellations are included. Runs are distinct run IDs
per logical job; retries count as attempts. Historical names and CPU sizes
remain in the table, including jobs since consolidated or made opt-in.
Minutes are summed seconds / 60, not invoices: no per-job rounding, CPU/OS
price multiplier or dollar estimate is applied. Standard GitHub runners are
excluded; in this private repository they consume the included allowance and
can incur overages, so they are not guaranteed free.

Reviewed **76 of 399 failed paid attempts**, using
`gh run view <run> --job <job> --attempt <attempt> --log-failed`, same-SHA reruns,
the next successful run on the same branch, and follow-up commit diffs.
**Real catches and flakes/infra are confirmed lower bounds, not estimates of
all failures.** The remaining 375 failures include unsampled and inconclusive
cases; a zero in the real-catch column does not mean a job never caught a
bug when its unclassified column is nonzero. No such job is trimmed.
A test-only fix, timeout or later green commit by itself is not proof of a
product defect or flake. Two iOS failures for the same bug count as two caught
attempts, not two distinct bugs. No secrets or raw logs are included here.

## Paid jobs

| Workflow / job | Runner | Runs | Attempts | Minutes | Real catches | Flakes / infra | Unclassified failures | Action |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Android CI / Account hub Worker and D1 tests | 2vcpu Linux | 284 | 293 | 256.4 | 2 | 0 | 7 | Keep |
| Android CI / Android emulator verification | 4vcpu Linux | 62 | 64 | 230.5 | 2 | 1 | 5 | Keep |
| Android CI / Android emulator verification (API 34) | 4vcpu Linux | 71 | 75 | 305.2 | 0 | 0 | 16 | Keep |
| Android CI / Android emulator verification (API 36) | 4vcpu Linux | 71 | 75 | 398.2 | 0 | 0 | 20 | Keep |
| Android CI / Build and test Android app | 2vcpu Linux, 4vcpu Linux | 177 | 180 | 488.4 | 0 | 1 | 4 | Keep |
| Android CI / Build signed release APK | 2vcpu Linux | 95 | 100 | 377.9 | 0 | 1 | 6 | Keep |
| Android CI / Publish verified Android download | 2vcpu Linux | 1 | 1 | 0.3 | 0 | 0 | 0 | Keep |
| Build Branch Image (Lab) / build | 4vcpu Linux, 8vcpu Linux | 17 | 17 | 40.6 | 0 | 0 | 7 | Protected; unchanged |
| Build Linux desktop artifact (manual opt-in) / Linux x64 AppImage and deb with checksums | 2vcpu Linux | 1 | 1 | 6.6 | 0 | 0 | 0 | Protected; unchanged |
| Build and Push Docker Image / build | 4vcpu Linux | 39 | 39 | 79.8 | 0 | 0 | 3 | Protected; unchanged |
| Build macOS desktop artifact (manual opt-in) / macOS arm64 artifact with source provenance | 6vcpu macOS | 1 | 1 | 2.5 | 0 | 0 | 0 | Protected; unchanged |
| CI Backend / Backend tests | 2vcpu Linux, 4vcpu Linux | 957 | 976 | 9173.1 | 0 | 0 | 133 | Keep |
| CI Browser Workspace / Browser editor smoke checks | 2vcpu Linux | 94 | 95 | 22.8 | 0 | 0 | 0 | Standard Ubuntu |
| CI Browser Workspace / Real browser workspace | 2vcpu Linux | 105 | 107 | 469.6 | 0 | 1 | 13 | Keep |
| CI JavaScript / JavaScript package checks | 2vcpu Linux, 4vcpu Linux | 766 | 767 | 2978.5 | 2 | 0 | 90 | Keep |
| CI JavaScript / Mobile UI checks | 4vcpu Linux | 402 | 403 | 535.5 | 0 | 0 | 7 | Already consolidated |
| CI fluid / fluid unit tests | 2vcpu Linux, 4vcpu Linux | 252 | 252 | 640.5 | 1 | 0 | 10 | Keep |
| Container PostgreSQL / Single-container PostgreSQL and browser | 4vcpu Linux, 8vcpu Linux | 84 | 84 | 209.9 | 0 | 0 | 13 | Keep |
| Deploy account hub / deploy | 4vcpu Linux | 20 | 21 | 11.4 | 0 | 1 | 2 | Protected; unchanged |
| Deploy image registry / deploy | 4vcpu Linux | 0 | 0 | 0.0 | 0 | 0 | 0 | Protected; unchanged |
| Deploy license server / deploy | 4vcpu Linux | 0 | 0 | 0.0 | 0 | 0 | 0 | Protected; unchanged |
| Deploy website / deploy | 2vcpu Linux | 4 | 4 | 2.0 | 0 | 0 | 3 | Protected; unchanged |
| Desktop CI / Assemble worker runtime (linux-x64) | 2vcpu Linux | 10 | 10 | 10.0 | 0 | 0 | 0 | Already opt-in / historical |
| Desktop CI / Package Windows shell | 2vcpu Windows | 85 | 90 | 63.3 | 0 | 0 | 0 | Already opt-in / historical |
| Desktop CI / Package shell (blacksmith-2vcpu-ubuntu-2404) | 2vcpu Linux | 10 | 10 | 4.8 | 0 | 0 | 0 | Already opt-in / historical |
| Desktop CI / Package shell (blacksmith-2vcpu-windows-2025) | 2vcpu Windows | 10 | 10 | 5.9 | 0 | 0 | 0 | Already opt-in / historical |
| Desktop CI / Package shell (blacksmith-6vcpu-macos-latest) | 6vcpu macOS | 10 | 10 | 3.1 | 0 | 0 | 0 | Already opt-in / historical |
| Desktop CI / Typecheck and unit tests | 2vcpu Linux | 142 | 147 | 101.2 | 0 | 0 | 1 | Keep |
| Desktop CI / Windows preview installer and fresh database | 2vcpu Windows | 94 | 98 | 591.8 | 0 | 1 | 18 | Keep |
| Folder confinement (macOS) / macOS folder confinement | 6vcpu macOS | 5 | 5 | 0.8 | 2 | 0 | 0 | Keep |
| Instance identity contracts / Instance identity (blacksmith-2vcpu-windows-2025) | 2vcpu Windows | 31 | 31 | 14.4 | 1 | 0 | 0 | Already opt-in / historical |
| Instance identity contracts / Instance identity (blacksmith-6vcpu-macos-latest) | 6vcpu macOS | 33 | 33 | 7.2 | 0 | 0 | 0 | Already opt-in / historical |
| License server CI / License server Worker and D1 tests | 2vcpu Linux | 9 | 9 | 2.8 | 0 | 0 | 0 | Standard Ubuntu |
| Publish Windows installer / build | 2vcpu Windows | 10 | 10 | 38.7 | 0 | 0 | 2 | Protected; unchanged |
| Publish Windows installer / launcher-acceptance | 2vcpu Windows | 1 | 1 | 7.3 | 0 | 0 | 0 | Protected; unchanged |
| Publish Windows installer / verify | 2vcpu Windows | 10 | 10 | 67.4 | 0 | 0 | 8 | Protected; unchanged |
| Registry CI / Registry Worker tests | 2vcpu Linux | 9 | 9 | 2.5 | 0 | 0 | 0 | Standard Ubuntu |
| Release Docker and Android / Android APK / Account hub Worker and D1 tests | 2vcpu Linux | 8 | 9 | 4.2 | 0 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / Android APK / Android emulator verification | 4vcpu Linux | 8 | 9 | 30.0 | 0 | 1 | 0 | Protected; unchanged |
| Release Docker and Android / Android APK / Build signed release APK | 2vcpu Linux | 8 | 9 | 32.4 | 0 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / Android APK / Publish verified Android download | 2vcpu Linux | 5 | 5 | 1.3 | 0 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / Desktop (linux-x64) | 4vcpu Linux | 2 | 2 | 7.6 | 0 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / Desktop (mac-arm64) | 6vcpu macOS | 2 | 2 | 3.3 | 1 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / Desktop (win-x64) | 4vcpu Windows | 5 | 5 | 19.4 | 0 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / Docker image / build | 4vcpu Linux | 11 | 15 | 42.1 | 0 | 2 | 0 | Protected; unchanged |
| Release Docker and Android / Windows installer (opt-in) / build | 2vcpu Windows | 2 | 4 | 19.0 | 0 | 2 | 0 | Protected; unchanged |
| Release Docker and Android / Windows installer (opt-in) / verify | 2vcpu Windows | 2 | 2 | 14.8 | 0 | 0 | 1 | Protected; unchanged |
| Release Docker and Android / Windows installer / build | 2vcpu Windows | 1 | 2 | 9.3 | 0 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / Windows installer / verify | 2vcpu Windows | 1 | 1 | 5.9 | 0 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / build-frontend | 4vcpu Linux | 4 | 4 | 2.1 | 0 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / create-release | 4vcpu Linux | 17 | 21 | 3.5 | 0 | 0 | 0 | Protected; unchanged |
| Release Docker and Android / create-release-packages | 4vcpu Linux | 4 | 4 | 0.8 | 0 | 0 | 1 | Protected; unchanged |
| iOS CI / Build and test iOS app (Simulator) | 6vcpu macOS | 17 | 17 | 45.2 | 2 | 0 | 3 | Keep |
| tmp chat shell probe / shell | 2vcpu Linux | 3 | 3 | 1.4 | 0 | 0 | 2 | Historical probe; no current file |
| **Total (job-runs, not workflow runs)** | | **4072** | **4152** | **17393.4** | **13** | **11** | **375** | |

## Classification evidence

| Failed run(s) | Evidence and follow-up | Classification |
| --- | --- | --- |
| [37689205017](https://github.com/HobbyCoders/ai-shuffle/actions/runs/37689205017) | JavaScript row-props regression; `72bf0c4a8` changed the SubagentMessage parent-session prop in production; next run 37690146904 passed. | Real |
| [36816286714](https://github.com/HobbyCoders/ai-shuffle/actions/runs/36816286714), [37265392677](https://github.com/HobbyCoders/ai-shuffle/actions/runs/37265392677), [36288355828](https://github.com/HobbyCoders/ai-shuffle/actions/runs/36288355828) | Stale shipped website docs; `10634d2e8`, `11b7be943` / `fbc709273`, and `2dd4be788` regenerated the artifact; subsequent runs 36816613300, 37266508086 and 36288628332 passed. | Real artifact defects |
| [35469563823](https://github.com/HobbyCoders/ai-shuffle/actions/runs/35469563823) | Fluid prompt omitted GetUsage; `10d32fd91` repaired the production vocabulary, not the assertion; run 35469710930 passed. | Real |
| [34263547825](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34263547825), [34190565560](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34190565560) | Android failures followed by production WebView range handling (`6cee341e8`) and launch-screen restoration (`48d7281ce`); runs 34265229946 and 34191851631 passed. | Real |
| [34429203166](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34429203166) | Windows identity lock raised PermissionError; `97988f5e6` removed the contended marker write; run 34429413257 passed. | Real |
| [36950812645](https://github.com/HobbyCoders/ai-shuffle/actions/runs/36950812645), [36951108197](https://github.com/HobbyCoders/ai-shuffle/actions/runs/36951108197) | Native macOS descriptor flags and firmlink checks failed; production fixes `3286cbd59` and `74f2b3585`; run 36951331746 passed. | Real |
| [36957049590](https://github.com/HobbyCoders/ai-shuffle/actions/runs/36957049590), [36957578126](https://github.com/HobbyCoders/ai-shuffle/actions/runs/36957578126) | WebKit window.open never offered links; `6a5f1f1a0` changed production popup content rules; run 36957905315 passed. | Real |
| [34310403039](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34310403039) | macOS cloudflared checksum mismatch; `5f5291ecd` corrected production archive pins; run 34312585467 passed. | Real |
| [34806108693](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34806108693) | Two installer build attempts hit artifact quota; two Docker evidence-upload attempts also failed at upload, then the same SHA passed. | Four infra failures |
| [34787456630](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34787456630), [34786894032](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34786894032) | Browser screenshots and instrumentation APK upload hit artifact quota. | Two infra failures |
| [34912936743](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34912936743) | SDK setup requested missing `tools`; `164c56dd3` changed SDK setup; run 34913294928 passed. | One CI setup failure |
| [34564385662](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34564385662), [34427572300](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34427572300), [34401342797](https://github.com/HobbyCoders/ai-shuffle/actions/runs/34401342797) | Emulator / Windows acceptance failed, then succeeded on a later attempt of the same SHA. Counted as observed rerun-only failures, not proof that no nondeterministic product bug exists. | Three flakes / rerun-only |
| [36640062531](https://github.com/HobbyCoders/ai-shuffle/actions/runs/36640062531) | Cloudflare route API failed, then the same SHA passed on rerun. | One infra failure |

## Trims chosen

Move only **Browser editor smoke checks**, **License server Worker and D1
tests**, and **Registry Worker tests** from Blacksmith 2-vCPU Linux to standard
GitHub `ubuntu-24.04`. All three had zero failed attempts across the window;
smoke had 94 executed runs / 95 attempts, and each service had nine.
Keep their existing PR paths, manual dispatch, commands, timeouts, check names,
concurrency and fail-closed behavior. The paid real-browser job still depends
on smoke and still requires `ci:full` or manual dispatch.

At the observed volume, avoid **28.1 Blacksmith elapsed minutes per 30 days**:
22.8 browser smoke + 2.8 license + 2.5 registry. This is a runner-cost transfer,
not a reduction in total test execution; GitHub's rounding and allowance can
change the net bill. No monthly cap or dollar saving is asserted.

No current paid workflow runs unconditionally on every PR: they already have
path filters. The historically broad jobs were narrowed before this review,
not by #1956: #1400 measured runner sizes, #1508 removed schedules and put
platform checks behind `ci:full`, #1511 excluded unrelated labels/web-only APK
builds, and #1893 consolidated mobile checks and limited local test load.
Do not count those earlier savings again. Desktop shell packaging, Linux
runtime assembly and native identity checks already have opt-in/manual gates.

Keep Backend, JavaScript, Fluid, Android, real-browser, container, desktop,
macOS confinement and iOS coverage: several have proven catches; others have
unresolved failures and cannot honestly be called never-catching. In
particular the Windows identity failure was a real locking bug, not an infra
permission error. Leave **all release, publish, deploy and signing workflows**
unchanged, including Android CI's mixed publishing workflow and Publish desktop
source update. No Docker/runtime file is modified. No coverage is deleted,
no new schedule is added, and no production workflow is dispatched.
