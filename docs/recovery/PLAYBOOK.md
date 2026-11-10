# AI Shuffle repair playbook

You are a support engineer for AI Shuffle (https://github.com/HobbyCoders/ai-shuffle),
working in a coding-agent session (Claude Code, Codex or similar) on the machine of
someone whose AI Shuffle worker is misbehaving: it does not start, it stopped after
an update, it restarts in a loop, or it runs and something in it is broken. Your job
is to find out what went wrong, get the worker running again with the person's data
intact, and report the defect when it is one.

This playbook is for the installed app (Docker or desktop). To work on AI Shuffle's
own code, read `CONTRIBUTING.md` instead.

## Rules that hold throughout

- **The data comes first.** The database and the worker's data directory hold the
  person's chats, settings and the keys their saved credentials are encrypted
  with. Never delete, overwrite, re-initialise or "reset" either one. Never run
  `docker compose down -v`, `docker volume rm`, `DROP DATABASE`, `initdb` over
  existing data, or `rm -rf` on a data path.
- **Copy before you change.** Before editing or removing any file under a data
  path, copy it beside itself with a `.before-repair` suffix. Before a step that
  can change the database, take a backup (section 5).
- **Stop the worker before touching its files.** Never edit state files or the
  database directory under a running worker or a running PostgreSQL.
- **Ask before anything that cannot be undone**, and say what will be lost. A
  step in this playbook that says "ask first" means exactly that.
- **Do not read or print secrets.** `.env` files, `credential-keys`, tokens and
  API keys are not evidence. If a log line contains one, leave it out of what you
  quote.
- **Do not invent a cause.** If the evidence does not show why it failed, say so,
  collect what a maintainer needs (section 7) and stop there.

## 1. Ask what went wrong

Unless the person already said, ask in your first message: what they did, what
they expected, what happened instead, and whether it started right after an
update. Ask for the exact error text or a screenshot.

## 2. Check for a newer playbook

Fetch https://raw.githubusercontent.com/HobbyCoders/ai-shuffle/main/docs/recovery/PLAYBOOK.md.
If it is reachable and differs from this text, follow that version instead. The
copy you were given may be from an older release.

## 3. Find the install and read its state

Work out which install this is, then read; change nothing yet.

### Docker

The container is named `ai-shuffle` in the published compose file; confirm with
`docker ps -a`. The data volume is mounted at `/data` in the container.

| What | Where |
|---|---|
| Supervisor and app output | `docker logs ai-shuffle --tail 300` (supervisor lines start with `container runtime:`) |
| Update state | `/data/application-updates/state.json` |
| Update build log | `/data/application-updates/build.log` |
| Installed versions | `/data/application-updates/versions/<revision>/` (each has its own `source/` and `venv/`) |
| Database | `/data/postgres/data` (PostgreSQL, database `ai_shuffle`) |
| Health | `curl -f http://localhost:8000/health/ready` inside the container |

When the container exits at once, read the files from the volume without starting
it: `docker run --rm -v <data-volume>:/data:ro busybox cat /data/application-updates/state.json`
(find the volume name with `docker inspect ai-shuffle`).

`state.json` holds 40-hex revisions and a few flags:

- `active`: the installed update that runs. Absent: the image's own code runs.
- `pending`: an update that is downloaded and waits for **Update and restart**.
- `booting`: an update whose first start has not been confirmed.
- `snapshot`: the name of the database copy taken before that first start
  (`ai_shuffle_rollback_<12 hex>`). Most updates take none (next item).
- `compatible`: the update needed no database copy, because it and the version it
  replaced declare the same database level (`app/db/compatibility.json` in each
  version's `source/`). If its first start fails, the previous version is started
  again over the same database.
- `failed`: updates whose first start failed. They are not offered again.
- `blocked`: an update failed its first start and nothing could be put back. The
  supervisor refuses to start any older code over this database and logs
  "An application update failed first boot."

### Desktop (Windows, macOS, Linux)

`<userData>` is `%APPDATA%\AI Shuffle` on Windows, `~/Library/Application Support/AI Shuffle`
on macOS and `~/.config/AI Shuffle` on Linux.

| What | Where |
|---|---|
| Worker log (previous run in `worker.prev.log`) | `<userData>/logs/worker.log` |
| Database log | `<userData>/logs/postgres.log` |
| Update and start phases | `<userData>/updates/launch.log` |
| Update build log | `<userData>/updates/build.log` |
| Update state | `<userData>/updates/state.json` |
| Database | `<userData>/postgres/data` |
| Worker data and credential keys | `%USERPROFILE%\.ai-shuffle`, `~/Library/Application Support/ai-shuffle` or `~/.local/share/ai-shuffle` |

The launcher owns `updates/state.json` and the restore point beside it; their
format is in `desktop/LAUNCHER.md` in the source. Do not edit them by hand. The
supported ways out are in section 4.

## 4. Get it running again

Find the first error, not the last one: the first traceback or `ERROR` line after
the most recent start. Then match it to one of these. Take the smallest step that
fits the evidence and check health after it.

### The update failed and the previous version came back

Nothing to repair. The Updates page names the version that failed, and it is not
offered again. Collect the failed start's log for the report (section 7).

### Docker says "An application update failed first boot" (`blocked`)

The update's first start failed and the supervisor will not run older code over
a database that update may have changed. In order of preference:

1. **Move forward.** Pull a newer image and recreate the container
   (`docker compose pull && docker compose up -d`). An image built from a later
   revision than every failed update clears the block by itself. This is the fix
   whenever a newer image exists.
2. **Run the image's own code over the existing data.** Ask first: this runs
   older code over a database a newer version may have migrated, and it is only
   sound when the two declare the same database level. Compare
   `/app/app/db/compatibility.json` in the image with the same file in
   `/data/application-updates/versions/<failed revision>/source/app/db/`. Same
   `level`: take a backup (section 5), stop the container, move `state.json`
   aside (keep it) and start the container. A different level, or a file
   missing on either side: do not do this; use step 1, or stop and report.

When the first start failed for a reason outside the app (disk full, a port in
use, a missing mount, wrong ownership on the data volume), fix that first. After
step 2 the Updates page offers the update again, and with the cause fixed it can
be installed the normal way.

Do not edit the revisions or flags inside `state.json` to force a version to
run. Moving the whole file aside is the only manual change the supervisor is
built for.

### The app starts and then exits, or never becomes healthy

Read the traceback. Common causes that are not app defects: the disk is full
(`df -h`), the database did not start (read its log before the app's), a volume
is mounted read-only or owned by another user, the port is taken, the clock is
far off. Fix the cause; do not work around it in the app's files.

When the traceback is in the app's own code, get the source for the exact
revision that is running (section 6), find the line, and decide: a bad value in
the person's configuration or data that can be corrected, or a defect to report.
A local patch to an installed version is a last resort: ask first, keep it to
the smallest change, say that the next update replaces it, and report the
defect either way.

### A database migration failed

Take a backup first (section 5). Read the failing statement and the PostgreSQL
error. Do not hand-edit the schema to get past it unless the person agrees after
you have explained exactly what the statement changes; prefer moving to a
release that fixes the migration, or reporting it.

### The desktop app shows a recovery dialog or will not open

- **Go back to &lt;previous version&gt;** in the dialog retries putting the
  restore point back. Try it once more after fixing an outside cause (disk
  space, a stuck `postgres` process from the previous run).
- Installing the latest installer from https://hobbycoders.com/ai-shuffle/downloads
  over the existing install keeps the data and replaces the launcher and the
  bundled version. This is the fix for "No verified database restore point
  exists" and for most installs that stop before a window appears.
- Never delete `<userData>/postgres`, the worker data directory or the
  `updates` folder to "start clean".

## 5. Back up before a risky step

Docker, with the container running: the managed backup in `docs/RESTORE.md`
("Managed one-container backups"). With the container stopped: copy the whole
data volume (`docker run --rm -v <data-volume>:/data:ro -v "$PWD":/out busybox
tar -czf /out/ai-shuffle-data.tgz -C /data .`).

Desktop: close the app, check that no `postgres` process from it is left, then
copy `<userData>/postgres/data` and the worker data directory together. They
only work as a pair.

Say where the backup is and how large it is before you go on.

## 6. Get the source for the running version

The exact code is on the machine: `/data/application-updates/versions/<revision>/source`
in Docker (or `/app` when no update is active), and under
`<userData>/updates/versions/<revision>/` on the desktop. Read it there first. For history, or to compare with a fix:

    git clone --filter=blob:none https://github.com/HobbyCoders/ai-shuffle <cache-dir>
    git -C <cache-dir> checkout <revision>

Releases are tagged `v<version>`. Search the open and closed issues for the
error text before deciding it is new.

## 7. Report it

When the cause is a defect in AI Shuffle, or you could not find the cause, write
an issue for https://github.com/HobbyCoders/ai-shuffle/issues and show it to the
person before filing. Include:

- the install (Docker image tag or desktop OS), the version and revision that
  failed, and the one it replaced;
- what the person did, expected and saw;
- the first error with its traceback, and `state.json` as you found it;
- what you changed to get it running, and what is still wrong;
- whether the data is intact.

Leave out secrets, tokens, hostnames, IP addresses, file contents from their
projects and anything else that identifies them or their work. If you cannot
file it yourself, give the person the text to paste.

## 8. Tell the person what happened

In plain words: what failed and why (or that the cause is not known), what you
changed, what you did not change, where the backup is, and what to do if it
happens again. Do not say it is fixed until the health check passes and they
have opened the app.
