# Backup and recovery

## Managed one-container backups

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
URL/password into the host shell. The runtime reads its validated private
`DATA_DIR/managed-postgres.json` descriptor; database connection environment
settings are not supported.

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

## Schedule managed backups on the host

There is no separate database or backup-sidecar Compose service. Schedule the
same managed in-container script with your host scheduler instead. For example,
run at 03:30 daily from the deployment directory (substitute its absolute path):

```cron
30 3 * * * cd /path/to/ai-shuffle && docker compose exec -T ai-shuffle bash /app/scripts/backup.sh
```

Set `BACKUP_DIR`, `BACKUP_RETENTION_DAYS`, `WORKSPACE_DIR`, `DATA_DIR` and
`FLUID_HOME` as needed for the app's existing paths. For a scheduled override,
pass only these non-database options with `docker compose exec -e NAME=value`.
Move copies to protected storage outside the app's volume and alert on failed
jobs. A backup scheduler must not parse, print or copy database credentials.

## Preserve an existing non-managed installation

This release does not support connecting to a separately hosted database or
starting the old two-container database service. Do not upgrade an existing
non-managed deployment in place. Keep its previous release, original Compose
configuration/project name, database volumes, private keys and protected backups
unchanged while a separate migration is planned and rehearsed.

Never use `down -v`, `--remove-orphans`, renamed volumes, edited lifecycle markers
or deleted credentials to make the new runtime start. Nothing automatically
imports an old database into the managed directory. Take any final source backup
using the old release's own maintenance procedure before retirement; this release
cannot connect to that database to back it up. For a clean test installation, use
a separate new data directory and **do not import production data or settings**.

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
backup. Lifecycle records/passwords in `postgres/`, `managed-postgres.json` and
any retained `database-mode.json` are sensitive runtime state, not a way to
re-create a missing physical cluster. Never publish the descriptor or archive it
as a standalone configuration file.

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
     -e PUID=99 -e PGID=100 \
     -v ai-shuffle-recovery-data:/data -v "$RECOVERY_FILES:/restore:ro" \
     "$IMAGE" restore --file /restore/ai-shuffle-db-TIMESTAMP.dump
   ```

2. The maintenance CLI verifies an **empty embedded destination**, performs
   `pg_restore --exit-on-error --single-transaction --no-owner --no-privileges`, and
   shuts PostgreSQL down without starting application services. It rejects a live
   supervisor, nonempty targets and unsafe retained state. A failed restore leaves an incomplete-state fence: do not delete
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
     --numeric-owner --exclude=./postgres --exclude=postgres \
     --exclude=./database-mode.json --exclude=database-mode.json \
     --exclude=./managed-postgres.json --exclude=managed-postgres.json
   ```

   Extract media into the recovery workspace and settings into its active Fluid
   settings volume. Preserve the original mapped ownership, directory modes and
   private key modes. `--numeric-owner` is required because bypassing the entrypoint
   also bypasses UID mapping: resolving the archived `appuser` name would use the
   image's UID 1000 instead of the backup's UID (99 by default). Start the recovered
   app with the original `PUID`/`PGID`. Restore matching app keys/identity before any normal app
   startup; do not overwrite the new `postgres/`, `managed-postgres.json` or
   `database-mode.json` afterward.
4. Attach only these recovery volumes to an **isolated** disposable app. Review
   connector/automation/network configuration before letting it start; normal boot
   runs migrations and application services, unlike maintenance restore. Do not
   enroll or run a clone on the live worker network with duplicated device/instance
   identity. Verify readiness, representative transcripts, media and saved memory
   without calling models. Switch the real installation only after validating
   recovery and planning identity/network handling; see [network migration](network/migration.md).

## Historical backups and version compatibility

`tools/backup-db.sh` emits **custom format**, just like the full backup script.
Use the managed offline maintenance restore above on an empty disposable target,
never an in-place restore into a running installation. `tools/restore-db.sh` now
refuses its former destructive SQL operation without changing data.

Keep historical plain SQL backups and their original recovery tooling unchanged.
They need a separately planned and tested conversion to a compatible logical
archive; do not feed SQL to the managed custom-format restore command. A restored
backup also needs its matching private app keys and identity files. An external
database is not a supported runtime destination for this release.

The runtime requires PostgreSQL **16** in its fixed `postgres/data` path. A missing,
malformed or different `PG_VERSION`, missing credentials or partial cluster is an
operator error, not permission to initialize a sibling database or reset a
password. Do not edit version markers, rename the cluster or delete safety fences.
A major upgrade needs a planned, verified migration. PostgreSQL 17-to-16 is not a
physical downgrade; do not assume version-16 clients can dump or restore a newer
server's data. Database-schema rollback is separate from image rollback.
