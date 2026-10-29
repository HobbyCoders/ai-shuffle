<!--
title: Updates
description: Updating AI Shuffle, in-app updates and what the update check sends
order: 10
-->

# Updates

AI Shuffle never changes under you. Your install runs the version you chose until you update it.

## Release channels

**Stable** is the default: tested releases. **Nightly** follows yesterday's `main`: it may break. A signed nightly is published at 06:00 UTC only when `main` changed and that exact revision has a local full-suite pass. If the pass is missing, the previous nightly stays available. Both channels verify signatures before installing.

**Switching back to stable works only while this worker is not ahead of the current stable release.** Database migrations do not run backwards, so a worker running newer nightly code stays on nightly until a stable release catches up with it; the switch then works from Settings without a reinstall. A worker that runs the stable release itself, or an older version, can switch back at once. Keep a backup before switching; pointing an older stable version at a nightly database is not a downgrade path.

For Docker, set `UPDATE_CHANNEL=nightly` (or `stable`). An explicit environment value overrides the saved local choice and makes it read-only in the channel API. Without that override, the owner API saves `application-updates/update-channel.json` in the data volume; restart the container to apply a change. Desktop stores `{"channel":"nightly"}` in `updates/update-channel.json` under its user-data directory. The choice applies to this install, not every worker on the account.

The Windows installer asks which channel a new install follows: **Stable** (the default) or **Nightly**. Choosing Nightly writes that file. An install that already has a choice keeps it: the installer skips the question and never overwrites the file, and choosing Stable writes nothing. For a silent install, add `/CHANNEL=nightly` (for example `/S /CHANNEL=nightly`); without it a silent install follows stable unless the install already chose nightly. The choice is saved for the Windows account that runs the installer. If you install for all users and a different administrator account approves the Windows prompt, Nightly is saved for that account instead of yours, so your install follows Stable until you choose Nightly in **Settings → System → Updates**.

For one release, existing `edge` settings are accepted as an alias of `nightly` and log a deprecation line. Publishing a nightly does not switch channels, install it or restart a worker. Read the release notes next to the update button before updating.

## In-app updates

Most releases install from inside AI Shuffle:

1. When a new release is out, the update button appears in the sidebar with its release notes.
2. Choose **Download update**, then **Update and restart**. Agents stop, the update installs, and AI Shuffle starts again.

If a chat is still working, or a service it started (such as a preview server) is still running, the restart waits. Click the update button to see what is running: each service shows its port, when it started and when its chat's turn ended. Choose **Restart when work finishes**, or **Stop running work and restart**. **Stop running work and restart** stops every chat on that worker, including the subagents its chats started; anything still running a minute later is force-stopped and the restart goes ahead, naming what it had to force. A scheduled restart shows when it gives up waiting and can be cancelled with **Cancel schedule**; restarting the worker drops it. A worker that refuses the restart is named with its reason, and an update that finished on only some workers says which ones were not restarted.

On a Docker worker, a downloaded update that is waiting for **Update and restart** does not hide a newer release. Checking for updates still reads the feed: when a newer release is out, the button offers it, and **Download update** replaces the waiting one and removes its files. If that download fails, the earlier update stays ready to install. Nothing is replaced while an update is installing or starting. If a successful signed check instead offers the running version or an older release, a verified download that was never selected to install is no longer selected; its files are retained, but it is not offered for restart. Failed, starting and recovery-protected builds are kept.

**Settings → System → Updates → Allow updates** turns in-app updates on or off for every worker on your account.

Docker source updates reuse third-party Python dependencies only when the signed lockfiles, optional-dependency installer, installation recipe and selected Python runtime (including pip/ensurepip versions) match. Each revision still installs its own current Fluid code separately. Node dependencies are reused only when the Node/npm versions, platform/ABI, package manifest and lockfile match; source builds still run. Incomplete installs are never reused.

A completion record is checked against every installed file's path, size, mode, modification time, inode and symlink target, plus content hashes of entry points, package metadata and native code. Python also receives a bounded, isolated core-import probe. This catches missing files and ordinary damage, including same-size edits to critical code; it is not a defense against deliberately changing noncritical contents while preserving all metadata. New Python bytecode stays with each revision rather than changing the shared installation.

Damaged entries are replaced at a new permanent path, never repaired under a running or retained revision. Dependency environments stay at their original paths, and cleanup keeps environments referenced by retained revisions plus at most two unused complete environments per package. Set `AI_SHUFFLE_PERF_DOCKER_DEPENDENCY_REUSE=0` on the supervisor to return to installing dependencies for each revision; this does not disable signature, source-tree or runtime checks.

### Runtime overlays

An overlay-aware Docker supervisor can install a release's required Python, Node, Chromium and relocatable tools/libraries on the persistent data volume. Nothing changes for an ordinary release: the image's runtimes remain the default. Overlays activate only when a signed release requires a capability the image lacks and includes an archive for this architecture with a pinned SHA-256 and size limits. Downloads and installed contents are verified before use; there are no privileged package-manager operations.

A release without a Python overlay always builds its virtual environment with the immutable image interpreter, not the supervisor’s interpreter. Launch and cold handover check the environment’s base interpreter against the signed runtime selection; cleanup also protects inherited interpreter dependencies of retained environments.

Archive limits apply before tar parsing: decompressed bytes may exceed the signed expanded bound by at most 64 KiB for headers/padding, individual PAX/GNU metadata records are limited to 16 KiB (64 KiB total), and at most 50,000 headers are accepted. Large archive descriptors must include structural overhead in their expanded bound. The raw-stream guard accepts only PAX path/link, timestamp, owner/group, comment and character-set metadata. PAX size or offset overrides, sparse extensions, unknown keys and nonzero PAX padding are rejected; they cannot redirect tarfile into payload bytes as hidden headers. Sparse runtime archives are unsupported.

The application revision selects its runtimes. If its first start fails and the database snapshot is restored, the previous revision and its runtime selection return together. Active, staged, previous, retained failed-build and currently running supervisor runtimes are retained; unreferenced overlays are cleaned up. Downloads are limited to 512 MiB each, expanded payloads to 2 GiB each, and the overlay store to 8 GiB, with 256 MiB of free disk space reserved. Insufficient space leaves the current version running.

This is delivery support, not a claim that every installed image can already update forever. Releases do not request overlays by default. Older images whose baked startup code rejects missing capabilities still need an overlay-aware bootstrap image before they can cold-start such a release. Runtime artifacts need acceptance on both supported architectures before publication. PostgreSQL stays on the image's existing major version; PostgreSQL major upgrades and incompatible operating-system/ABI changes are not attempted through overlays. Before building on a runtime overlay, the updater starts it once; one that does not start on the image (built for another processor or for newer system libraries) is refused by name before anything is built or activated, and the current version keeps working.

## Updating the Docker image

When no compatible runtime overlay can satisfy a release, the in-app update says **Needs a newer Docker image. This version keeps working.** It names the missing capabilities and does not stop the running version. Update the image instead:

- **Docker Compose:** set `AI_SHUFFLE_VERSION` in `.env` to the new version and run `docker compose up -d`.
- **docker run:** pull the new version, remove the container and start it again with the new version. See [Install with Docker](/docs/getting-started/install-docker-compose#update).

Your data stays in its volumes. Give the container up to 120 seconds to stop cleanly.

Updating the image always works, even when an in-app update would have, and it is the way to jump several versions at once.

## The desktop app

The desktop app checks for new releases when it starts and once a day, and offers to install them.

A downloaded update that is waiting for **Update and restart** does not hide a newer release: a check still reads the feed, and **Download update** replaces the waiting update with the newer one. If that download fails, the earlier update stays ready. If the newer release needs a different launcher or Electron, a notice before the release notes explains its installer requirement while the earlier download remains ready to install. If a download's release information cannot be verified, it cannot be installed, but a newer verified download can replace it. An update that is installing, starting or held back after a failed start is never replaced. A successful signed check that offers the running version or an older release stops offering any verified download that has never been chosen to install. Its files are kept, but it is not offered for restart. If the running version's information cannot be read, the download is kept. Updates already chosen to install or held after a failed start keep their existing protections. This takes effect once AI Shuffle runs a version that contains this change.

An existing install that has never taken an in-app update still runs the updater bundled in its installer. A waiting download can therefore keep hiding newer releases until that first in-app update installs code containing this fix. The staged-replacement behaviour above takes effect after that update, on both Windows and Linux.

If a release cannot start on this install's desktop launcher, the app says so before it downloads anything: install the latest AI Shuffle installer from hobbycoders.com. Your data is kept.

On packaged Linux (AppImage/deb), a release requiring a different Electron version also needs the latest installer, to keep the installed Linux sandbox. The signed update feed names the locked Electron version; the app refuses a mismatch before offering or downloading it and shows the installer remedy. Older manifests without that field still use the launch-time compatibility check. This preflight takes effect after the install has received an update containing it.

Before a source update first starts, a current desktop launcher stops the worker and managed PostgreSQL and saves one validated database restore point. If the copy fails (for example, the disk is full), the update does not start and the previous version is kept. Leave room for a database copy and temporary restore files.

A Windows or Linux install whose launcher came from 0.9.6 or earlier does not make that copy itself. The update makes it at its first start instead, and the window shows **Preparing the update's database restore point…** while it runs. If the copy fails, the update does not start and your data is not changed: the window gives the reason and what to do (for example, free some disk space), then **Try again** makes the copy again. If you close AI Shuffle instead, the previous version opens and the update stays ready to install. If the previous version was the one your installer included, no copy is made and the update starts behind its migration fence, as updates always did under those launchers. On Windows, once the update has started successfully it replaces the old launcher, so later updates are protected as described above. On Linux, the update makes its own copy each time until you install a current installer.

If an update stopped with "No verified database restore point exists", install the latest AI Shuffle installer from the downloads page. Your data is kept and the update finishes. An install in that state cannot get the fix through the in-app update.

Before restarting into an update, the Windows installer app (or a Windows source app handed off by audited launcher versions 4–7) saves its database restore point itself and the window shows **Preparing the update's database restore point…** while it copies; a large database can take a minute or more. If the copy fails (for example, the disk is full), the update is not installed, your data is unchanged and the current version keeps running; the update button says so, and the update can be installed again once the cause is fixed. If the restart into the update fails after the copy was made and the copy cannot be removed, the update stays ready and AI Shuffle asks you to quit and open it again to finish installing it; until then it does not reopen the database, so the copy stays exact. The first start after an update can take a few minutes, for example while antivirus software checks the newly built files. On Windows source installs, a separate **Starting the update to AI Shuffle X…** window stays open while the launcher checks the database copy, then closes when the new version attaches or the launcher stops. The restart notification is also shown, in case the separate window cannot start. Follow any other AI Shuffle recovery or installer dialog that appears. If the new version never attaches, the notice closes after three minutes in the opening phase; if no app window has appeared, open AI Shuffle again to retry or recover. Closing the notice does not stop the app or its launcher. Audited launchers 4–7 hash the entire copy before selecting the update; other launcher versions keep the full validation in the app. The new version's worker gets up to 10 minutes to become ready on that first start.

If the first start crashes, times out or is interrupted (including opening AI Shuffle again before the new version appeared), the next launch restores that database copy before running the previous version. A notice names the failed update and the version restored. Because the database was put back, the update button then offers the same update again and says that its last attempt did not finish; download it to try again. An update is offered again up to three times; after that it waits for a newer update. The update button suggests the latest installer for such a failure only when that installer is a newer release than the version you run. The rollback journal is removed once the new build is healthy. Supported Windows apps retain the cold-copy cache described below; other paths delete the restore point. If cleanup is temporarily blocked, the healthy app still starts. Update checks show the reason and folder instead of offering a restart, and retry cleanup on each check and launch. Close other programs using that folder, check its permissions, then choose Retry; the update becomes installable after cleanup succeeds. Installing a newer desktop installer can still update the app while cleanup is blocked. This protects the update's first start, not later downgrades or switching from nightly back to stable.

On Windows under audited launchers 4–7, a healthy update retains one private cold copy as a disposable cache for the next update. The app copies and hashes only files that changed, then the installed launcher still hashes the entire restore point before selecting the update. The first point still copies the whole cluster, and the cache occupies about one database copy of disk space between updates. It shares no writable files with the live database. This cache is not a general backup or permission to downgrade.

If restoration fails, no older worker starts on the changed database. The recovery dialog offers **Go back to <previous version>** to retry the restoration, or **Quit**. Keep the restore point and logs. Older launchers and earlier failures without a verified restore point retain their migration fence: retry the affected build or install a verified newer release; never delete the fence or point older code at the database.

Recovery also checks the operating-system start identity and executable of any leftover worker or database process before stopping it. A matching network response alone is not enough. If an older installation has no process-identity record, or the record does not match, recovery stops with a notice: close the original application/server normally and retry. Do not kill an unrelated process or delete identity records to force recovery.

## The Claude Code CLI

If you use Claude, the Claude Code CLI keeps itself up to date, as on any computer. Updating AI Shuffle does not reinstall it.

## What the update check sends

- **What:** a plain request for the latest release information. No account, device ID or anything about your projects, chats or settings. Like any web request, it reveals your IP address.
- **When:** shortly after the app starts, then once a day.
- **What comes back:** a small signed file naming the newest release and its notes. Nothing is downloaded until you choose to.
- **Turning it off:** **Settings → System → Updates → Allow updates**.

Updates are free with or without a Cloud account. A linked worker's Cloud plan controls remote capacity, not access to updates. See [AI Shuffle Cloud](/docs/getting-started/cloud).

## If your Cloud plan ends

Your install keeps running, including agent runs, and you can still download and install new versions for free. Only paid Cloud services change when a plan ends.
