# The Windows launcher protocol

Issue #1821, design item D5. An installed Windows app has two parts:

- **The launcher.** `AI Shuffle.exe` and `resources/app.asar`, from the
  installer. `app.asar` holds the import closure of
  `src/main/managed-entry.ts` (`scripts/ci/update_reach.py` lists it).
- **The updated app.** A source revision downloaded, verified and built under
  `%APPDATA%\<name>\updates\versions\<revision>`. It is started by the
  launcher with its own managed `electron.exe`.

A source update replaces only the updated app. This file is the contract
between the two. Every launcher a release can meet must keep working with it,
and the release may replace an older launcher as described below.

## Versions

`desktop/launcher.json` holds `{"version": n, "minimum": m, "start": s}`.

| Field | Meaning |
|---|---|
| `version` | The launcher this source packs into `app.asar`. Raise it whenever a file in the launcher closure changes. `scripts/ci/update_reach.py` fails a range that changes the closure without raising it (`launcher-version`), unless the owner labels it `update-reach:no-launcher-change`. Once raised, it stays raised until a release ships it: a later change while no release tag carries that version yet ships in the same launcher and needs no second bump. |
| `minimum` | The oldest launcher a release built from this source needs. It is signed into every published manifest as `desktop.launcher`. A running launcher below it is replaced, but only after the release's first healthy start (below), so it cannot protect that first start. |
| `start` | The oldest launcher that can start a release built from this source at all, before any replacement. It is signed as `desktop.start`. Optional, 0 to `version`; a manifest without it can be started by any launcher. |

**The start floor (#97).** Before a release is offered or downloaded, the
updater compares `desktop.start` with the launcher it runs under. The launcher
version is `SHUFFLE_LAUNCHER_VERSION` on Windows (0 when the launcher passes
none). On Linux, the installed app runs the update in its own process without
that variable, so the version comes from the installed `app.asar`'s
`launcher.json`, then its `desktop-build.json` stamp. Profile and development
builds have no installed launcher and are not compared. A launcher below the
floor gets "AI Shuffle <version> needs a newer desktop launcher than this
install has … Install the latest AI Shuffle installer", and nothing is
downloaded or staged. Linux never replaces its launcher, so there the start
floor is the only launcher check.

Set `start` to the oldest launcher that can still start the release, not to
`version`. A release that needs new launcher behaviour *before* its first
healthy start must either do that work itself, as the late restore point does
for launcher 3, or raise `start` and accept that older installs need an
installer. Current value: 3. Launcher 3 (0.9.6 and 1.0.0; 1.0.1 ships launcher 4) starts these builds
through the late restore point; older ones cannot verify them (the
signing-key bridge).

- `launcher.json` is packed into `app.asar`.
- `scripts/stamp-desktop-build.mjs` stamps `launcher: <version>` into the
  installer's `resources/worker/desktop-build.json`.
- A launcher reports its version as the `launcher.json` in its own archive,
  falling back to the `desktop-build.json` stamp and then to 0. Installers
  built before #1821 have neither and are version 0.
- A manifest without `desktop.launcher` needs launcher 0.

Raising `minimum` is what makes installed apps replace their launcher. The
signing-key transition sets both version and minimum to **3**: an old-key-signed
bridge must replace the installed old-key-only verifier before new-key updates.
The owner accepted this bump for the sole existing Windows install; the release
workflow verifies the transitional installer on a clean Windows runner. This is
not evidence of an existing-install migration: verify that swap before switching
signers. Future minimum bumps should follow a Windows acceptance run.

Version **4** introduced the launcher contract for making the database restore
point before an update's first start (`prepareRollback`, and `recoverRollback`
after a failed first start). The launcher closure gained them in 1.0.0 without
a version bump, so the 0.9.6 launcher (no restore points) and the 1.0.x
launchers (restore points) both report 3. Minimum 4 makes every version-3
launcher swap to this one after the update's first healthy start; from the
next update on, the launcher makes the restore point again. Run the acceptance
run below against a 0.9.6 installer and a 1.0.x installer before release.

Current version is **8**, with **minimum 4** and **start 3** unchanged. It starts
the managed PostgreSQL server with `statement_timeout=30s`, `lock_timeout=5s` and
`idle_in_transaction_session_timeout=30s` (#591), and its Windows process-identity
probe no longer compiles a helper with `Add-Type` (#597). It keeps launcher 7's
best-effort optional voice dependency installation, the voice lock in the Python
environment fingerprint, the start-floor/staged replacement changes, and the
bounded 10-second wait for a killed self-test before rejecting the candidate.
Launcher 8 still runs the full `ColdClusterCopy.validate()` content read-back
on a ready journal: every snapshot file is hashed against the manifest before
selection. There is no redundant copy or second read in this path to remove;
a structure-only or manifest-trust check would weaken the rollback guarantee.

Launcher 6 audit: its functional launcher changes are confined to `source-auth.ts`
and `source-updates.ts`: the `check()` withdrawal handling and the lease/key
transport removal from `e02a8a6ff`. `prepareRollback`, `ColdClusterCopy` and
`installed-entry.ts` are untouched. The `RESTORE_POINT_VALIDATING_LAUNCHERS`
allowlist was re-audited for launcher 6: ready-point preparation still performs
that full content read-back. `installed-entry factory fully hashes a real ready journal before selection`
and `ready-journal launcher preparation reads every snapshot byte once without recopying`
in [`test/source-updates.test.mjs`](test/source-updates.test.mjs) prove it, including
rejection of same-size content corruption. Launchers 4, 5 and 6 are explicitly
trusted; any future version still requires a fresh audit.

Launcher 7 audit: the optional voice changes are confined to dependency building
in `source-build.ts`; `installed-entry.ts`, `prepareRollback` and `ColdClusterCopy`
retain launcher 6's full ready-point validation. The same installed-factory test
now runs against launcher 7 and rejects same-size snapshot corruption. Launcher 7
is explicitly added to `RESTORE_POINT_VALIDATING_LAUNCHERS`; unknown versions,
non-Windows handoffs and migration-risk recovery still require full validation.
This test audit is not a Windows installed-app acceptance run.

Launcher 8 audit: the launcher closure changes in two files. `postgres.ts` adds
the three session timeout settings to the options passed on every `pg_ctl start`,
matching the container's `WAL_SETTINGS`, so existing clusters get them without a
`postgresql.conf` edit. `process-identity.ts` defines the same four kernel32 imports
(`OpenProcess`, `GetProcessTimes`, `QueryFullProcessImageNameW`, `CloseHandle`) in
memory with Reflection.Emit instead of `Add-Type`. It opens the process with the
same access right, keeps the handle open while the signal command runs, keeps
the start-time and executable checks and the 30-second probe timeout. The
lockfile refresh for #584 changes only development packages (`electron`
44.2.0 to 44.9.3, `fast-uri`, `http-cache-semantics`, `undici`). `app.asar`
packs none of them, and the Electron change is the `linux-electron` group.
`installed-entry.ts`, `prepareRollback` and `ColdClusterCopy` are untouched. The
installed-factory test runs against launcher 8 and still rejects same-size
snapshot corruption, so launcher 8 is added to `RESTORE_POINT_VALIDATING_LAUNCHERS`.

The acceptance run is `desktop-installer.yml` with `launcher_acceptance`
checked and `release_tag` set to a published installer with an older launcher.
It runs from any branch and publishes nothing
(`scripts/verify-launcher-swap.mjs`):

1. Install that installer.
2. Stage the branch as a revision whose signed `desktop.launcher` is the
   branch's `launcher.json` version. The key is ephemeral and is written into
   the installed archive and the staged source only.
3. Start the app, and check that `app.asar` was swapped and `app.asar.old`
   kept.
4. Restart twice. Check that the swap was confirmed, that the profile is
   unchanged, and that `desktop-build.json` is untouched.

## What a launcher does at each start (frozen)

1. Take the single-instance lock, or quit.
2. Read `resources/worker/desktop-build.json`
   (`{revision, sequence, launcher?}`) and its own `launcher.json`.
3. Read `updates/state.json`. These fields are 40-hex revisions:
   `active`, `previous`, `pending`, `staged`, `booting`, `waiting`, and the
   array `failed`; `blocked` is a boolean.
   - New fields may be added.
   - The meaning of these fields does not change.
4. Select `pending ?? active` and verify it:
   - `versions/<rev>/release.json` is the signed manifest. It must verify
     against `build/application-update-key.pem` in the launcher's own archive.
   - `versions/<rev>/build.json` is `{revision, runtimeKey, pythonKey,
     electron, layout?}`.
   - When `pending` is selected, write `booting = <rev>` and clear `waiting`.
5. Write `updates/installed-host.json`:
   `{executable: process.execPath, userData, name}`.
6. Release the lock and spawn the selected build's `electron.exe <desktopDir>`.
   The environment is an allowlist (`buildEnvironment`) plus these variables:
   - `SHUFFLE_SOURCE_ROOT`
   - `SHUFFLE_SOURCE_REVISION`
   - `SHUFFLE_SOURCE_NODE`
   - `SHUFFLE_LAUNCHER_VERSION`
   - `SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS`, only when it is set to `1`
7. Quit.

The updated app attaches at start: it checks `SHUFFLE_SOURCE_REVISION`
against `active` or `booting` and writes `waiting`. It then owns `state.json`
until it exits.

**Compatibility rule.** A release must keep steps 1 to 7 working for every
launcher at or above its `desktop.launcher`. A missing
`SHUFFLE_LAUNCHER_VERSION` means 0.

A release must also start under a launcher that lacks anything added after
it, because the swap below only happens once the release is healthy. The next
section is the one case so far.

## Launchers without restore points

Launchers before 1.0.0 (0.9.6 and older, version 3 or lower) select a pending
build without making a database restore point. So does the source bootstrap
(`scripts/bootstrap.mjs`, starter installs). The updated app checks for the
point (`requireRestorePoint`) before it starts PostgreSQL for the first time.
When there is none, it makes one itself (`lateRestorePoint`), the way the
launcher would have:

- Only in the booting build's own process, before it started the database
  (`attach` wrote `waiting`), and only when the database floor is the previous
  build's own. The launcher's `select` marked that floor uncertain; `waiting`
  proves nothing has started the database since. Any other state (another
  fence, an unexplained block, an unreadable floor, recovery required) is
  refused as before.
- The point is marked `late` in `restore-point.json`. The app copies the
  database, then fences itself (`databaseStarting`), then marks the point
  ready. A ready late point therefore always comes with the fence, so an old
  launcher never runs the previous build next to it; that would make the copy
  stale. A late copy left without the fence is made again; one left fenced but
  not ready is checked (or made again) and finished.
- The status page says "Preparing the update's database restore point…" while
  the copy runs.
- If the copy fails, the update does not start and the database is not
  touched. The status page says "The update could not start yet" and gives the
  reason with advice for its cause: free disk space (`ENOSPC`), close programs
  holding a file (`EBUSY`, `EPERM`), check the data folder's permissions
  (`EACCES`), or make a new copy (failed validation). **Try again** repeats the copy in the same process on Windows
  and Linux; it does not relaunch, because a relaunch goes through the old
  launcher.
- Closing the app instead: before the fence, the old launcher runs the
  previous version and the update stays ready to install. A late copy left
  behind by such a start (the app was killed during the copy) is removed by
  the previous version once it is healthy, when it has this change, or else
  replaced when the update starts again. After the fence (the
  copy was made but could not be marked ready), the old launcher says "The
  last update did not finish starting" and offers **Try Again**, which runs the
  update again and finishes the copy, or **Quit**.
- When the previous version is the installer's bundled build (no source update
  was ever active), no point is made. Only a launcher that bundles that build
  could restore it, and that launcher never restores; the point would only
  block a newer installer's recovery. The update starts under its migration
  fence, as every update did under these launchers. The same state checks
  apply first: `attach` fences the bundled build's own floor (the managed
  process has no baseline), and any other fence or block is refused.

If the update then fails its first start, the app marks it failed and fenced
(`failed`, `mayHaveMigrated`, the database floor) and relaunches the launcher.
An old launcher ignores `restore-point.json`. It never runs older code on that
database: it offers **Try Again**, which runs the same update again, or
**Quit**. The restore point is kept. A retried start that becomes healthy
deletes it. Installing a current installer instead restores the database to
the point and goes back to the previous version, as a current launcher does.

Once the update is healthy, the launcher swap below replaces the old launcher.
Linux installs share this check but have no launcher swap: under a 0.9.6
installer, every update makes its own point until a current installer is
installed.

**Installs that already hit the refusal** (1.0.0 or a nightly before this
change, started by a 0.9.6 launcher): the refused build is left staged, and
0.9.6's updater offers a staged build again without checking the feed. Those
installs never receive this change in-app; nothing in a newer release runs
there. Installing a current installer recovers them: its launcher makes the
restore point and starts the staged update.

Acceptance against a real 0.9.6 installer runs on a GitHub-hosted Windows
runner: the `late-restore-acceptance` job of `desktop-installer.yml`
(`launcher_acceptance` with `old_installer_sha256`; the installer comes from
the `OLD_INSTALLER_URL` secret; `late_restore_steps` picks some of N1, N2 and
N3, after the install and P). `desktop/scripts/verify-late-restore-point.mjs`
takes the install through a bundled previous build, a failed copy with Try
again, a kill during the copy and a kill after PostgreSQL started, then the
launcher swap.

## A first start that did not finish

At step 4 a launcher that finds `booting` without `waiting` marks that build
failed (`failed()`), and, when a restore point exists, puts the database back
(`recoverRollback`). It cannot tell a build that crashed from one whose process
was only slow to load: a freshly built Electron that an antivirus scans can
take minutes to reach `attach`, and opening the app again in that time starts
the launcher. The launcher's behaviour here is frozen.

The updated app reads what the launcher left (`src/main/failed-start.ts`, not
in the launcher closure). A revision in both `failed` and `supersededFailed`,
with nothing in flight (`booting`, `pending`, `waiting`), no fence or block, no
restore-point journal, and a readable, certain database floor that is not that
revision, was fully put back: retrying it is as safe as the first attempt, with
a new restore point. When the feed offers it and the source updater refuses it
("This revision failed to start"), the app takes it out of `failed` (a write in
the updater's own serialization), reloads, and offers it again with a note that
the last attempt did not finish. Each revision is offered again at most three
times (`start-retries.json`). Otherwise the refusal stands, and the app reports
whether the hub's installer is a newer release (`installer_newer`) so the worker
only suggests an installer that can help.

An update's first start also gives its worker 10 minutes to become ready
instead of 3: a worker that times out marks the update failed.

**The restore point before the restart.** A launcher (4 and later) makes the
restore point in `prepareRollback`, before `select` writes `booting`, with no
window open. `ColdClusterCopy.snapshot` reads the cluster four times (hash,
copy, hash the copy, hash the source again), so on a large database the
launcher seems to hang for a minute or more; closing it then leaves a
`preparing` journal, and `recoverRollback` marks the revision failed. Since
this change the running app makes the point itself, once the update is
pending and its worker and PostgreSQL are stopped, while the status page says
"Preparing the update's database restore point…"
(`src/main/update-restore-point.ts`). It writes the same folders and manifest
in one copying pass plus one check of the copy, and checks the source for
changes by size and modification time. The launcher's `prepareRollback` then
finds a `ready` point for the pending revision and only validates it (one
read). If the restart fails, the app discards the point
(`cancelPreparedRollback`) before the update stops being pending, so a copy of
a database the running version opens again is never kept. If that discard
fails too, the update stays pending with its `ready` point and the app asks
the owner to quit and open it again; the next launcher start then starts the
update from that exact copy. Until then nothing in the running version opens
the database: every start (a failed install, a cancelled quit, Restart
Worker) refuses while `restore-point.json` names another revision than the
running one or cannot be read (`pointHeldFor`), because the launcher would
later restore that copy over anything written since. If the copy itself fails,
`prepareRollback` has already ended the install and marked the revision
failed, as the launcher would have; the app takes it out of `failed` again
without counting a retry (it never started), removes the launcher's notice and
says that the copy failed. The app does this
only under a launcher that makes restore points (Windows, launcher version 4
or later, or the installer's own app); otherwise the launcher acts as before.
Making the launcher's own copy faster would change the launcher closure and
need a new launcher version.

## Replacing an older launcher

All of these files are in the installed `resources` folder unless shown
under `updates/`.

| File | Written by | Purpose |
|---|---|---|
| `app.new.asar` | updated app | The candidate launcher, built from the running verified source. |
| `app.asar.old` | updated app | The previous `app.asar`, kept until a later release runs. |
| `app.restore.tmp` | restore helper | Staging copy of `app.asar.old` during a restore. |
| `updates/launcher-swap.json` | both | The swap record (below). |

The candidate is named `app.new.asar`, not the `app.asar.new` the design
first named. Electron opens an archive only through a path segment that ends
in `.asar`.

**Swap** (`replaceLauncher`). It runs in the updated app once per run, after
its worker reports ready. It runs only when all of these hold:

- the platform is Windows;
- there is an installed host and no profile build;
- `SHUFFLE_LAUNCHER_VERSION` is below the release's `desktop.launcher`;
- the source's `launcher.json` version is at least that minimum and newer
  than the running launcher.

The steps:

1. Build the archive from the verified, built source:
   - the files `electron-builder.yml` packs, including the production
     `node_modules` from `package-lock.json`;
   - the installed archive's own `package.json`. electron-builder rewrote
     that file, and its `name` picks the profile folder.
2. Write the archive to `app.new.asar`.
3. Run the candidate's self-test:
   `ELECTRON_RUN_AS_NODE=1 "AI Shuffle.exe" <resources>/app.new.asar/dist/main/launcher-self-test.js --nonce=<random> --expect=<version> --root=<updates>`.
   - It passes only when the process exits 0 and prints
     `{"launcherSelfTest":1,"nonce":<same>,"ok":true,"version":<expect>}`.
   - An EXE that ignores the script and exits 0 therefore fails it.
   - The design's `--launcher-self-test=` switch would have passed on any
     launcher that ignores unknown switches, which every existing one does.
   - The self-test loads the archive's launcher modules and compiles the rest.
     It reads `installed-host.json` and `state.json`, and verifies the
     selected `release.json` with the archive's key.
   - If the test fails, the candidate is removed and the record says
     `rejected` for this revision. That revision never tries again; a later
     one may.
4. Copy `app.asar` to `app.asar.old`.
5. Write the record:
   `{format: 1, phase: "trial", revision, from, to, starts: 0, old: sha256, new: sha256}`.
6. Rename `app.new.asar` over `app.asar`. If Windows reports the file in use,
   undo the record and try at a later start.

`app.asar` is a complete launcher after every step.

**Trial** (the new launcher, `launcherTrial`). The new launcher checks the
record before it does anything else:

- A record in `trial` for its own version with `starts: 0`: increment
  `starts` just before spawning the app.
- `starts >= 1`, phase `restoring`, or a failure on a trial start before the
  spawn: restore. This means the app it started never confirmed.

**Confirm** (the updated app, `confirmLauncher`). It runs right after
`attach`, which is where `waiting` is written:

| Situation | Result |
|---|---|
| `SHUFFLE_LAUNCHER_VERSION >= to` | `confirmed` |
| A later revision starts with a `confirmed` record | Delete `app.asar.old` and the record (`retired`). |
| `app.asar` still hashes to `old` and the phase is `trial` (interrupted before the rename) | Delete the leftovers and retry later. |
| `app.asar` still hashes to `old` and the phase is `restoring` | `restored`. That revision is not retried. |
| `app.asar` hashes to neither (a reinstall) | Clear the record. |

**Restore** (`restoreLauncher`).

1. The launcher marks the record `restoring`.
2. It starts a detached copy of itself in node mode with an inline helper,
   then quits.
3. The helper waits for that process to exit, because Windows keeps
   `app.asar` open while it runs.
4. It checks that `app.asar.old` is a regular file with the recorded SHA-256.
5. It copies it to `app.restore.tmp` and renames that over `app.asar`.
6. It starts `AI Shuffle.exe` again.

The restore needs the new launcher to reach its first lines of
`installed-entry`. A launcher that cannot load at all is repaired by running
the installer again, which also replaces `app.asar`.

## Security

- The install is per-user (`nsis.perMachine: false`). Anything that can write
  `app.asar` could already replace it, so the swap grants nothing new.
- Electron's asar integrity fuse is off; no fuses are configured.
  `RunAsNode` is on, so any local program can already run code through
  `AI Shuffle.exe`. The self-test uses it and adds nothing to it.
- The archive is built only from the running revision's source. That source
  was signature-verified when it was downloaded, its `release.json` is
  verified again before the swap, and it was built by this install.
- These checks stop the swap from being redirected:
  - The resources folder must equal its real path, so it has no symlink or
    junction.
  - `app.asar` must be a regular file.
  - Source files must not be links.
  - New files are created exclusively (`wx`), after removing anything planted
    at that path without following it.
  - Nothing is written to `%TEMP%`.
- The self-test nonce comes from `crypto.randomBytes`. Another program can
  start a self-test, but it gains nothing: the test is read-only, and the swap
  trusts only its own child's output.
