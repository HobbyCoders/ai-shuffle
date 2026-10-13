# Clean-box acceptance run

The phase 1 exit test (P10 in the review in the private archive,
PR10 in report 04). HobbyCoders team only.

**Exit criterion.** One person who has never seen the code installs AI Shuffle
from the docs alone, on a clean Ubuntu VM using Docker Compose, and
reaches a first streamed agent reply. The run passes when the VM reaches
step 7 with no step marked **blocked**, and every **guess** is either fixed in
the docs or accepted in writing by the owner.

The automated half of this test is `scripts/ci/verify_buyer_compose.py`. It
runs on a clean GitHub runner for every release (`release.yml`, job
`buyer-install`) and on packaging PRs (`container-postgres.yml`). It covers
what needs no human and no real account: the compose file as published, the
container health, the owner-password gate, the beta license, the CLI status
on a fresh container and the refusal of an agent turn without a credential.
This runbook covers the rest, which needs a person, a browser, a real Claude
account or API key and a phone.

## Roles and rules

- **Tester**: someone who has not read the code, the internal docs or this
  repository. They get only the public entry points listed below, the pull
  token and (if the beta is over) a license key. They must not ask the team
  for help during the run.
- **Observer**: someone from the team, taking notes and timing. The observer
  never tells the tester what to do. If the tester is stuck for 15 minutes on
  one step, the observer marks the step **blocked**, writes down the hint they
  then give, and the run continues.
- The tester reads the docs on hobbycoders.com (or the rendered `docs/content`
  pages, if the site is not live yet), in the order the "Start Here" page
  lists them. Nothing else: no README, no GitHub issues, no forum posts.

Public entry points the tester gets:

| Box | Start at |
| --- | --- |
| Ubuntu | Docs "Start Here", then **Install with Docker Compose** |

## What to record

Keep one sheet per box. For every step below, record:

| Field | What goes in it |
| --- | --- |
| Result | **pass**, **pass with guess**, **fail** or **blocked** |
| Time | Wall-clock minutes from the start of the step to its expected result |
| Guesses | Every point where the tester had to guess, reread, or try something the docs did not say. Quote the doc sentence they were on. |
| Evidence | The screenshot or command output named in the step |
| Doc fix | The page and sentence that would have removed the guess |

Also record, once per box: the date, the release version, the Docker and
Compose versions (`docker version`, `docker compose version`), CPU and memory,
the total time from "start reading" to the first streamed reply, and the total
time for the whole run.

Never record secrets: no pull token, license key, owner password, API key or
sign-in code in the sheet or in screenshots. Crop or blur them.

## The boxes

**Ubuntu VM.** A fresh Ubuntu Server LTS install (24.04 or newer), amd64,
4 vCPU, 8 GB of memory, swap on, 40 GB of disk, with only what the
[Requirements](../content/getting-started/requirements.md) page tells the
tester to install. The tester installs Docker Engine and the Compose plugin
themselves from Docker's own instructions, as the page says. The tester's
browser runs on a separate laptop on the same LAN, so step 1 includes
reaching the VM from another device.

**Accounts.** A Claude Pro or Max account for the subscription path, an
Anthropic Console account with a spend limit for the API key path, an Android
phone with the app available to the tester's Google account (internal testing
track), and a free HobbyCoders account (the tester creates it during step 8).

## Steps

Each step lists what the tester does (from the docs), the expected result, and
what the observer records on top of the common fields.

### 1. Install

**Ubuntu.** The tester follows **Install with Docker Compose**: makes a
folder, creates `compose.yaml` and `.env` from the page, pastes the pull
token, sets `BIND_HOST` to the VM's LAN address (they browse from a laptop),
runs `docker compose up -d` and waits for `docker compose ps` to show
`healthy`.

Expected:

- The image downloads with the pull token alone; no `docker login`.
- The container becomes healthy within 5 minutes on the first start (the
  Claude Code CLI installs during that time).
- The UI opens at `http://<address>:8000` from the laptop.
- `docker inspect ai-shuffle` shows the port published on the chosen
  address only, `ShmSize` of 1 GB and `StopTimeout` of 90 seconds.

Record: time from start of reading to healthy; the `docker compose ps` output;
whether the tester chose `0.0.0.0` or a bare port at any point (that is a doc
failure, even if they corrected it).

### 2. Owner password

The tester opens the UI in the laptop browser. The docs (**First run**,
section 2) say the first screen asks for an owner password.

Expected:

- Before a password exists, the first screen is the owner-password step; no
  other page of the app, and no API data, is shown.
- After creating a password (12 characters or more), the browser is signed
  in and the wizard continues.
- A second browser (a private window, or the phone's browser on the LAN)
  gets the sign-in page, not the app, and signs in with the password.

Record: screenshot of the first screen; whether the tester understood why a
password is needed; whether the second browser was asked for it.

### 3. License

The wizard's license step (**First run**, section 3) and **Settings →
License**.

Expected: during the beta the step reads **Beta: no license needed** and
continues; **Settings → License** shows the same beta state. After the beta,
the key from the license email activates and the card shows the plan, seats
in use and the lease date.

Record: screenshot of the license step and the Settings card; the lease date
if a key was used.

### 4. Credential: CLI login

Exercise both the in-app terminal and the host terminal across acceptance runs.

- **In the app** (**First run**, section 5a): choose **Sign in**, open the
  printed link in any browser, sign in on Anthropic's page, paste the code if
  one is shown, **Send**, then **Check again**.
- **From the host**: `docker exec -it -u appuser ai-shuffle claude auth
  login`. Then **Check again** in the wizard.

Expected: **Check again** shows the Claude account the CLI is signed in as
(email and plan). The container log shows no credential values. Recreating
the container (`docker compose up -d --force-recreate`) keeps the login.

Record: which path was used; the time from **Sign in** to the account showing;
whether the tester ran the host command without `-u appuser` (the docs say
why not to); whether the login survived recreation.

### 5. Credential: API key alternative

On one box (the other one from step 4's in-app path), also exercise the
API key mode (**First run**, section 5b, and **Modes and costs**): create a
key in the Anthropic Console with a spend limit, paste it, **Save**, and
switch the mode to API key in Settings.

Expected: the key is accepted and shown as saved (never displayed back in
full); the active mode is visible in Settings; the first streamed reply in
step 7 on this box is billed to the key (the Console's usage page shows the
request within a few minutes).

Record: whether the tester knew which mode was active at each moment; the
Console usage entry (time and approximate cost, no key).

### 6. Workspace

**First run**, section 6.

Expected: the wizard shows `/workspace` and names the host folder behind it
(`WORKSPACE_DIR`, `./workspace` next to `compose.yaml` by default). A file the
agent creates in step 7 appears in that host folder, owned by `PUID:PGID`
(the tester's user).

Record: `ls -l` of the host folder after step 7.

### 7. First streamed agent reply

The wizard's first-run step (**First run**, section 7), or, if the wizard
ends without it, **Quick start**, "Start a Chat".

Expected: the reply streams in token by token within 30 seconds of sending,
names the account or key that pays for it, and tool use (reading the sample
project) is visible while it runs. With no credential set up, the same action
shows what is missing and how to set it up instead of starting (check this
once, before step 4, on one box).

Record: **the total time from "start reading" to the first streamed token**;
a screenshot of the streamed reply; the refusal text seen without a
credential.

This step is the exit criterion. The rest of the run checks that the
install is livable.

### 8. Phone pairing

**Remote access**, "Remote access through your HobbyCoders account".

The tester connects the worker to a HobbyCoders account in **Settings →
Providers → Devices → Sign in**, installs the Android app, signs in with the
same account, adds the worker by QR code or pairing code and approves it.
Then, on mobile data (Wi-Fi off), opens a session on the phone and sends one
message.

Expected: the phone lists the worker, the message streams a reply over mobile
data, and **Settings → Providers → Devices** on the worker lists the phone with a revoke
button. Revoking it cuts the phone off within a minute.

Record: time from opening **Settings → Providers → Devices** to the first reply on the
phone; whether the tester understood that this traffic passes through
Cloudflare (the page says so); whether revoke worked.

### 9. Update check

**Updates**. The tester opens the update button in the sidebar or **Check for
updates** in Settings.

Expected: the check answers either "up to date" or offers the newest stable
release with its notes. It never offers an unreleased (edge) build. With
**Settings → General → Allow updates** off, no check runs (the sidebar shows
nothing new after a manual check attempt, and the worker log shows no request
to the manifest URL).

Then the image update path: the observer names a newer release (or re-uses
the same version if none exists); the tester changes `AI_SHUFFLE_VERSION`
in `.env` and runs `docker compose up -d`.

Expected: the container comes back healthy with every setting, the CLI login,
the owner password, sessions and workspace files intact; the stop takes up to
90 seconds and the log ends with PostgreSQL's clean shutdown, not a kill.

Record: what the check showed; the time the stop took; anything lost.

Until the first stable release is signed, the check gets a 404 from the hub;
record that as expected, not as a failure.

### 10. Uninstall

**Install with Docker Compose**, "Remove".

- Ubuntu: `docker compose down`, confirm the volumes and `WORKSPACE_DIR`
  remain (`docker volume ls | grep ai-shuffle_`); then `docker compose down -v`
  and delete the folder and the image (`docker image rm`) to leave the VM
  clean.

Expected: the first command keeps every piece of data; nothing of AI Shuffle
is left after the second. If the worker was paired in step 8, disconnect it
in **Settings → Providers → Devices** before removing it, and confirm the phone no longer
lists it.

Record: `docker volume ls` and `docker ps -a` after each command; whether the
docs told the tester to disconnect the worker from their account first.

## What offline CI does (and does not) prove

`tests/integration/container_ui_smoke.mjs` and
`desktop/scripts/verify-windows-preview.mjs` drive the current Welcome →
Password (container browsers only) → License → Mode → Sign in sequence
against the actual worker. The container creates a random disposable owner
password through the real form, verifies it was saved without enabling
trusted-LAN access, and uses the resulting browser session. Desktop retains
its native owner handoff. In a disposable beta profile both checks choose
subscription and API-key modes through the UI,
read the real credential gate, check that **Continue stays disabled** with
neither credential, and repeat after reload/restart. No owner credentials,
license lease, model response, onboarding flag or first-party API response is
faked. The smoke checks must not click a removed “Continue without an account”
button or assume the wizard can finish without a supported credential.

Because that safe offline state cannot reach Workspace/First run/Extras or
the post-setup Settings UI, workspace/network persistence fixtures are written
through the real owner APIs independently of onboarding. Container update
policy checks likewise exercise the real settings API, not an inaccessible
Settings screen. The Windows acceptance keeps its real installer, occupied-port
refusal, PostgreSQL schema/ACL/credential persistence, native status menu,
shutdown-listener and stable-profile-sentinel checks. A green offline smoke
run is **not** proof of completed onboarding, account linking, post-setup UI
or a first streamed reply. Those remain the authorized real-box checks above.

## First stable release: owner-only evidence still required

The 0.8.19 source-preparation checks do not complete this matrix. Every row
starts **not run** until a real run records its date, platform/version,
release commit/image digest and sanitized evidence. Use disposable test
licenses/accounts only with owner authorization; never run revocation or
shutdown experiments against someone else's running work.

| Check | Required evidence before sale |
| --- | --- |
| Licensed registry on current Docker, classic and containerd image stores | Actual pull and Compose startup; record blob GET redirect behavior. A `registry_upstream_failed ... instead of a redirect` 503 is a failure, not a registry pass. |
| Clean Docker Compose install | Bound address, persistent paths, shm/stop settings, first-run owner login and setup. |
| Docker host reboot and image update without docker login | Pull results before and after reboot; Compose recreation retains data. Record revoked-token and expired-license behavior on the pinned version. |
| Container stop | Shutdown log ending in clean PostgreSQL shutdown within the configured grace; no forced kill. |
| Pull-token revoke and rotate | Revoked token refuses pulls within about a minute; running container remains unaffected; rotated token works and `pull_count`/`last_pull_at` update. Redact tokens. |
| Signed desktop on Windows and macOS | Real signer/notarization evidence, first-run license-key path and update to the signed stable release; unit tests are insufficient. |
| Play internal install/update | Verified signing continuity, authorized tester account and compatible worker, not a claim of public Play availability. |
| Hub route cut-over | Real route/custom-domain behavior, including exact `/connect` route with query parameters; browser/phone handoff works. |
| Daily hub cron | Production measurements under Cloudflare's per-run limits (budgeted around 800 external calls / 850 queries); no load experiment inferred from fixtures. |
| First streamed reply | Steps 4–7 above on the clean VM, with fresh owner authorization for the specific live model/test. No model run is part of code-only preflight. |

The initial hub 404 in step 9 is expected **only before** stable publication.
After publication it is a failed release check until a signed stable manifest
is served and a real install accepts it. Record unavailable hardware or
unconfigured signing/deployment as **blocked**, never as passed or waived.
The buyer install instructions live in `docs/install/compose/README.md`.

## After the run

1. File one issue per **guess**, **fail** or **blocked** step, titled with the
   doc page and step, with the quoted sentence and the proposed fix.
2. Fix the docs in both copies (`docs/content` and
   `frontend/src/lib/docs/content`; `tests/test_buyer_docs.py` checks they
   match), re-render the site docs, and re-run only the affected steps with a
   new tester if a step was **blocked**.
3. Add the sheet (secrets removed) in the private archive next to the release
   it tested, and note the result in the phase 1 row of the master plan.
