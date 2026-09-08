# Backup and recovery

## One-container backups (default)

The app image manages PostgreSQL 16 privately inside the container. Do not expose
its port or copy a running `/data/postgres/data` directory as a database backup.
From the deployment directory, an online, database-only backup to the host is:

```sh
./tools/backup-db.sh ./ai-shuffle-db.dump
```

The helper runs `docker compose exec -T ai-shuffle python
/app/docker/container_runtime.py dump --file -`. Only a custom-format archive is
streamed to stdout; diagnostics use stderr. It keeps the host output private
(mode 0600), preserves a failed command's status, and replaces the requested file
only after a successful nonempty dump. It neither prints nor retrieves a database
URL/password into the host shell. The selected external DB also works through
this command; it is never silently substituted with an embedded DB.

For the DB **and** app files, run the existing backup script inside the app:

```sh
docker compose exec -T ai-shuffle bash /app/scripts/backup.sh --dry-run
docker compose exec -T ai-shuffle bash /app/scripts/backup.sh
docker compose cp ai-shuffle:/data/backups ./backups
```

In the app image this script uses the same lifecycle dump command and mapped
`appuser` identity, including for a root `docker exec`. Its active settings default
to `/home/appuser/.fluid`; custom `FLUID_HOME`, `DATA_DIR`, `WORKSPACE_DIR` and
`BACKUP_DIR` must match the deployment. Dumping is online; the filesystem archives
are not a transactionally consistent snapshot of changing files. Quiesce app
writers and use an offline dump plus filesystem backup when a coordinated
recovery point is required. Retention runs only after all requested steps succeed.
Copy backups to protected storage off this host/volume and test recovery there.

For an **offline retained embedded database**, stop the app first, then use the
maintenance entrypoint on its existing volumes, without starting dependencies:

```sh
docker compose stop --timeout 90 ai-shuffle
docker compose run --rm --no-deps ai-shuffle dump --file /data/offline-backup.dump
```

Choose a new filename; the maintenance CLI refuses to overwrite one. This command
starts only the retained database long enough to dump it, not the app, Chromium,
connectors or schedulers. It cannot dump a fresh/uninitialized database. Keep the
matching app files and private keys as well, and resume the app only when ready.

## Legacy external PostgreSQL and optional sidecar

**Before upgrading an existing two-container Compose install**, keep the original
Compose project name (including `-p` / `COMPOSE_PROJECT_NAME`) and select both files:

```sh
docker compose -f docker-compose.yml -f docker-compose.external.yml up -d --build
```

Continue using both files for later operations, or set
`COMPOSE_FILE=docker-compose.yml:docker-compose.external.yml` on Linux. The overlay
preserves the `postgres` service, `ai-shuffle-postgres` container and `postgres-data`
volume. Its existing `POSTGRES_PASSWORD` is still required. Keep a configured
`DATABASE_URL` unchanged: it remains authoritative over the components. Never use
`down -v`, `--remove-orphans`, a new project name or renamed volumes to migrate.
There is no automatic import from `postgres-data` into the embedded directory.
A separately hosted PostgreSQL server can use the default app-only Compose file
with external settings in `.env`; it does not need this legacy-service overlay.

The **external-only** sidecar targets the legacy `postgres` service:

```sh
docker compose -f docker-compose.yml -f docker-compose.external.yml -f docker-compose.backup.yml up -d --build
```

For one backup, replace `up -d --build` with `run --rm backup bash /scripts/backup.sh`.
Add `--dry-run` after the script to inspect its work without writing files or
starting the scheduler. Explicit commands bypass the cron entrypoint. Do **not**
attach this sidecar to the default embedded deployment: it cannot reach its private
socket. If the app's URL/components select a different external server/database,
use the in-app backup above or configure a separate job with matching `PG*` values;
the unmodified sidecar would back up the legacy service, not that other database.

## What must be retained

A successful full backup produces four timestamped files in `/data/backups`:

- `ai-shuffle-db-*.dump`: PostgreSQL custom-format dump, including transcripts and wrapped credential-key metadata.
- `ai-shuffle-media-*.tar.gz`: generated images, videos, audio and 3D models recursively beneath the workspace, retaining project paths.
- `ai-shuffle-data-*.tar.gz`: private app keys, instance identity, Fluid skills/journal/memory and PostgreSQL lifecycle files. Excludes logs, the backup destination and **`postgres/data`**, not the lifecycle parent.
- `ai-shuffle-settings-*.tar.gz`: the active Fluid settings volume.

Directories that do not exist are skipped. Symlinks are not followed. Archives
are private (umask 077); lifecycle files retain their original modes. Keep the
whole live `ai-shuffle-data:/data` volume for ordinary container recreation. The
logical backup plus app-data tar is a recovery set, **not** a physical PostgreSQL
backup. Lifecycle records/passwords in `postgres/` and `database-mode.json` are
sensitive retained state, not a way to re-create a missing physical cluster.

This does not archive project source repositories, custom stores outside these
roots, legacy Claude credentials, GitHub/browser login volumes, or `.env`. Back up
those separately as needed. The database must stay paired with its matching
`.credential-keys`, `.managed-connection`, instance identity and other app data.
An adopted database needs its private key, not an admin password. When moving
hosts or Windows users, use [credential key recovery](credential-key-recovery.md)
for encrypted portable recovery exports. An older, unadopted backup still needs
its original encryption password once. Protect all archives as sensitive data.

## Restore embedded data offline into an empty disposable target

1. Stop app writers with at least 90 seconds' grace. Preserve the original volumes,
   configuration and backups for rollback; **never restore over the live install**.
   Use the same PostgreSQL-16-capable image as the source, a **new empty** data
   volume and only a read-only mount of the selected recovery files. Do not pass
   source `.env`/external DB settings or mount original writable data into the
   recovery container. Set `RECOVERY_FILES` to an absolute backup directory and
   `IMAGE` to the tested image tag/ID. The mounted directory and dump must be
   readable by the mapped runtime UID: retain private 0700/0600 permissions and
   matching ownership, rather than making backups world-readable. For example
   (adjust PUID/PGID to the original deployment's mapping):

   ```sh
   docker volume create ai-shuffle-recovery-data
   docker run --rm --network none --stop-timeout 90 \
     -e DATABASE_MODE=embedded -e PUID=99 -e PGID=100 \
     -v ai-shuffle-recovery-data:/data -v "$RECOVERY_FILES:/restore:ro" \
     "$IMAGE" restore --file /restore/ai-shuffle-db-TIMESTAMP.dump
   ```

2. The maintenance CLI verifies an **empty embedded destination**, performs
   `pg_restore --exit-on-error --single-transaction --no-owner --no-privileges`, and
   shuts PostgreSQL down without starting application services. It rejects a live
   supervisor, external mode, conflicting DB settings, nonempty targets and unsafe
   retained state. A failed restore leaves an incomplete-state fence: do not delete
   it to force startup. Inspect the error and retry recovery on a separate fresh
   disposable volume, keeping failed/source data intact.
3. Restore the **logical DB first**, before app-data files make the target look
   like an existing installation. Inspect trusted archives with `tar -tzf ARCHIVE`.
   Extract app data into the stopped target, but **exclude all old PostgreSQL
   lifecycle state and physical storage** so the newly initialized cluster keeps
   its own credentials and binding. This example bypasses the entrypoint only to
   extract files while no app/database process is running:

   ```sh
   docker run --rm --network none --entrypoint tar \
     -v ai-shuffle-recovery-data:/data -v "$RECOVERY_FILES:/restore:ro" \
     "$IMAGE" -xzf /restore/ai-shuffle-data-TIMESTAMP.tar.gz -C /data \
     --exclude=./postgres --exclude=postgres \
     --exclude=./database-mode.json --exclude=database-mode.json
   ```

   Extract media into the recovery workspace and settings into its active Fluid
   settings volume. Preserve the original mapped ownership, directory modes and
   private key modes. Restore the matching app keys/identity before any normal app
   startup; do not overwrite the new `postgres/` or `database-mode.json` afterward.
4. Attach only these recovery volumes to an **isolated** disposable app. Review
   connector/automation/network configuration before letting it start; normal boot
   runs migrations and application services, unlike maintenance restore. Do not
   enroll or run a clone on the live worker network with duplicated device/instance
   identity. Verify readiness, representative transcripts, media and saved memory
   without calling models. Switch the real installation only after validating
   recovery and planning identity/network handling; see [network migration](network/migration.md).

## External recovery and version compatibility

For an external destination, provision and verify a separate empty DB yourself;
the embedded maintenance restore intentionally refuses external targets. With
matching PostgreSQL 16 clients, use
`pg_restore --exit-on-error --single-transaction --no-owner --no-privileges --dbname=YOUR_EMPTY_DB ai-shuffle-db-TIMESTAMP.dump`.
Supply connection details via protected standard `PG*` environment variables, not
command-line passwords, then restore matching app keys/files as above. Do not
copy a source embedded lifecycle record into an external installation.

`tools/backup-db.sh` now emits **custom format**, just like the full backup script.
Never feed these dumps to the legacy `tools/restore-db.sh`, which accepts only
historical plain SQL and has destructive external-DB restore semantics. Keep old
SQL backups unchanged and follow their explicit external restore procedure.

The runtime requires PostgreSQL **16** in its fixed `postgres/data` path. A missing,
malformed or different `PG_VERSION`, missing credentials or partial cluster is an
operator error, not permission to initialize a sibling database or reset a
password. Do not edit version markers, rename the cluster or delete safety fences.
A major upgrade needs a planned, verified migration. PostgreSQL 17-to-16 is not a
physical downgrade; do not assume version-16 clients can dump or restore a newer
server's data. Database-schema rollback is separate from image rollback.
