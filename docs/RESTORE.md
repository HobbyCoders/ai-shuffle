# Backup and recovery

Enable the optional sidecar with:

```sh
docker compose -f docker-compose.yml -f docker-compose.backup.yml up -d --build
```

For a one-off backup, replace `up -d --build` with `run --rm backup bash /scripts/backup.sh`.
Add `--dry-run` after the script to inspect its work without writing files or
starting the scheduler. Explicit commands bypass the cron entrypoint.

A successful backup produces four timestamped files in `/data/backups`:

- `ai-shuffle-db-*.dump`: PostgreSQL custom-format dump, including transcripts and wrapped credential-key metadata.
- `ai-shuffle-media-*.tar.gz`: generated images, videos, audio and 3D models recursively beneath the workspace, retaining project paths.
- `ai-shuffle-data-*.tar.gz`: app data, including private credential keys, instance identity and the Fluid skills/journal/memory store; excludes logs and the backup destination.
- `ai-shuffle-settings-*.tar.gz`: the active Fluid settings volume.

Directories that do not exist are skipped. Symlinks are not followed. This does
not archive project source repositories, custom stores outside these roots,
legacy Claude credentials, GitHub/browser login volumes, or `.env`. Back up those
separately as needed. Keep a protected copy off the same host/volume: local
backups alone do not survive losing that disk. Protect backups as sensitive data.

## Restore into a disposable installation first

1. Stop app writers. Keep the original deployment and current data available for rollback.
2. Create a fresh PostgreSQL database and use the matching PostgreSQL 16 client:
   `pg_restore --exit-on-error --single-transaction --no-owner --no-privileges --dbname=YOUR_EMPTY_DB ai-shuffle-db-TIMESTAMP.dump`.
   Supply credentials via the usual PG environment variables, not command-line passwords.
3. Inspect each archive with `tar -tzf ARCHIVE`. Extract media into the workspace,
   app data into DATA_DIR, and settings into FLUID_HOME, preserving ownership for
   the app user. Only extract archives you trust. Do not extract over live data.
4. Restore `.env` connection/configuration values and any separately protected
   provider credentials. Restore the matching private `.credential-keys` and
   `.managed-connection` directories with DATA_DIR. An adopted database needs
   its private key, not an admin password. When moving hosts or Windows users,
   use the encrypted export/import in [credential key recovery](credential-key-recovery.md).
   An older, unadopted backup still needs its original encryption password once.
5. Start the disposable app and verify health, representative transcripts, media,
   and saved memory without calling models. Only switch the real installation
   after validating recovery. A cloned installation sharing the same database
   needs its own INSTANCE_ID; do not run two instances with a duplicated identity.

The legacy `tools/backup-db.sh` / `tools/restore-db.sh` pair uses plain SQL and
backs up only the database. Do not feed the sidecar's custom-format `.dump` into
that plain-SQL restore script. Its restore is destructive and asks for confirmation.
