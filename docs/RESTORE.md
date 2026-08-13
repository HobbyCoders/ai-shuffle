# Restore Guide

How to restore an AI Shuffle backup produced by `scripts/backup.sh`. Companion
to `docs/SECURITY_NOTES.md`.

A backup set is two files, both timestamped identically:

| File | Contents | Tool |
| --- | --- | --- |
| `ai-shuffle-db-<ts>.dump` | Postgres database, custom format (`pg_dump -Fc`) | `pg_restore` |
| `ai-shuffle-media-<ts>.tar.gz` | Generated media dirs (`generated-images/`, `generated-videos/`, `generated-audio/`) | `tar` |

By default backups land in `/data/backups` inside the data volume
(`ai-shuffle-data`), i.e. accessible from any container that mounts it.

## Ordering (important)

1. **Stop the app** so nothing writes to the DB or media during the restore.
   Leave Postgres running (you restore *into* it).
   ```bash
   docker compose stop ai-shuffle
   ```
2. Restore the **database** first.
3. Restore the **media** tar.
4. Run **post-restore checks**, then start the app.

## 1. Restore the database

`pg_dump` was run with `--format=custom --no-owner --no-privileges`, so restore
with `pg_restore`. Restore into the existing `ai_shuffle` database, cleaning
existing objects first:

```bash
# From the Docker host, run pg_restore inside a throwaway postgres-client
# container on the app network (adjust the dump path / timestamp):
docker run --rm -i --network <project>_claude-network \
  -e PGPASSWORD="$POSTGRES_PASSWORD" \
  -v ai-shuffle-data:/data \
  postgres:16-alpine \
  pg_restore \
    --host=postgres --port=5432 --username=ai_shuffle --dbname=ai_shuffle \
    --clean --if-exists --no-owner --no-privileges \
    /data/backups/ai-shuffle-db-<ts>.dump
```

Flag notes:
- `--clean --if-exists` drops existing objects before recreating them, so the
  restore overwrites current data rather than colliding with it. Omit `--clean`
  only if restoring into a freshly created empty DB.
- `--no-owner --no-privileges` matches the dump options (roles/grants are not
  reproduced — the app uses a single role).
- For a **full wipe-and-restore** instead, drop and recreate the database first:
  ```bash
  dropdb -h postgres -U ai_shuffle ai_shuffle
  createdb -h postgres -U ai_shuffle ai_shuffle
  pg_restore --no-owner --no-privileges -h postgres -U ai_shuffle -d ai_shuffle <dump>
  ```

If you use the backup sidecar image, you can run these from it:
```bash
docker compose -f docker-compose.yml -f docker-compose.backup.yml \
  run --rm backup pg_restore --host=postgres --username=ai_shuffle \
  --dbname=ai_shuffle --clean --if-exists --no-owner --no-privileges \
  /data/backups/ai-shuffle-db-<ts>.dump
```
(The sidecar sets `PGPASSWORD` from `POSTGRES_PASSWORD`; pass `-e PGPASSWORD` if
`pg_restore` prompts.)

## 2. Restore the media

The tar was created with paths relative to the workspace root (`tar -C
$WORKSPACE_DIR`), so extract it back into the workspace:

```bash
docker run --rm \
  -v ai-shuffle-data:/data \
  -v <project>_workspace-or-host-path:/workspace \
  postgres:16-alpine \
  tar -xzf /data/backups/ai-shuffle-media-<ts>.tar.gz -C /workspace
```

This recreates `generated-images/`, `generated-videos/`, `generated-audio/`
under the workspace. Existing files with the same names are overwritten;
extra files already present are left in place (tar does not delete). For an
exact mirror, clear those three dirs before extracting.

## 3. Post-restore checks

1. **Start the app:**
   ```bash
   docker compose start ai-shuffle
   ```
2. **Readiness probe** — wait for it to go green (DB reachable + pool headroom):
   ```bash
   curl -fsS http://localhost:8000/health/ready && echo OK
   ```
   A 503 with `"database unreachable"` means the DB restore/connection failed.
3. **Row-count sanity** — confirm core tables have rows:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.backup.yml \
     run --rm backup psql -c \
     "SELECT (SELECT count(*) FROM sessions) AS sessions,
             (SELECT count(*) FROM system_settings) AS settings,
             (SELECT count(*) FROM admin) AS admins;"
   ```
4. **Encryption sanity** — DB-stored API keys are encrypted with a key derived
   from the admin password (see `docs/SECURITY_NOTES.md`). The restored DB
   carries the same `encryption_salt`, so the **same admin password** that was
   in use when the backup was taken still decrypts the secrets. If you restored
   an older DB and have since changed the admin password, log in with the
   password that matches the restored data, or run `scripts/rekey.py`.
5. **Log in** and spot-check that sessions, generated media, and configured API
   keys are present.

## Verifying a backup without a live restore

- `bash -n scripts/backup.sh` — syntax check.
- `scripts/backup.sh --dry-run` — prints every command it would run (pg_dump,
  tar, prune) without touching the DB or filesystem. Use this to confirm paths
  and connection strings before a real backup.
