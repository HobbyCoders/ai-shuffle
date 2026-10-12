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

`desktop/launcher.json` holds `{"version": n, "minimum": m}`.

| Field | Meaning |
|---|---|
| `version` | The launcher this source packs into `app.asar`. Raise it whenever a file in the launcher closure changes. |
| `minimum` | The oldest launcher a release built from this source needs. It is signed into every published manifest as `desktop.launcher`. |

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
