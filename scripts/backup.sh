#!/usr/bin/env bash
#
# AI Shuffle backup — pg_dump (custom format) + tar of generated media, with
# timestamped output and retention pruning. Run inside the managed app container
# through docker compose exec. The lifecycle CLI reads its private descriptor;
# this script never accepts database credentials or connects to another server.
#
# What it produces (in $BACKUP_DIR):
#   ai-shuffle-db-<timestamp>.dump      pg_dump -Fc (custom format, restore with pg_restore)
#   ai-shuffle-media-<timestamp>.tar.gz    tar of generated media dirs (see MEDIA_DIRS)
#   ai-shuffle-settings-<timestamp>.tar.gz tar of active Fluid settings (never legacy Claude data)
#   ai-shuffle-data-<timestamp>.tar.gz  private app state + lifecycle credentials,
#                                      NEVER the live postgres/data cluster
#
# Retention: files older than BACKUP_RETENTION_DAYS (default 7) matching the
# backup name patterns are pruned after a successful run.
#
# Configuration (env vars):
#   BACKUP_DIR             where backups are written (default: /data/backups)
#   BACKUP_RETENTION_DAYS  days to keep (default: 7)
#   WORKSPACE_DIR          generated media root (default: /workspace)
#   DATA_DIR              app data root, archived separately (default: /data)
#   FLUID_HOME            active settings (default: /home/appuser/.fluid)
#
# Flags:
#   --dry-run   print the commands that WOULD run; touch nothing.
#
# Verified offline with:  bash -n scripts/backup.sh  and  --dry-run
#
set -euo pipefail
umask 077

# docker exec defaults to root, but the lifecycle CLI runs as appuser. Run the
# entire backup as that same mapped identity so its 0700 destination is writable
# by the dump process and the private files retain a consistent owner.
if [ -f /app/docker/container_runtime.py ] && [ "$(id -u)" -eq 0 ]; then
  exec setpriv --reuid=appuser --regid="$(id -g appuser)" --init-groups bash "$0" "$@"
fi

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

# No standalone libpq fallback: only the managed runtime may choose the DB.
# Keep --dry-run useful outside the image, but never touch data there.
if [ ! -f /app/docker/container_runtime.py ] && [ "$DRY_RUN" -ne 1 ]; then
  echo "Run this backup inside the managed app: docker compose exec -T ai-shuffle bash /app/scripts/backup.sh" >&2
  exit 2
fi

BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"
DATA_DIR="${DATA_DIR:-/data}"
FLUID_HOME="${FLUID_HOME:-/home/appuser/.fluid}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

DB_DUMP="${BACKUP_DIR}/ai-shuffle-db-${TIMESTAMP}.dump"
MEDIA_TAR="${BACKUP_DIR}/ai-shuffle-media-${TIMESTAMP}.tar.gz"
SETTINGS_TAR="${BACKUP_DIR}/ai-shuffle-settings-${TIMESTAMP}.tar.gz"
DATA_TAR="${BACKUP_DIR}/ai-shuffle-data-${TIMESTAMP}.tar.gz"

# Generated-media directories to include in the tar, relative to WORKSPACE_DIR.
# These are where the app writes generated assets (see app/api/generated_*.py).

# run: echo the command in dry-run, else execute it.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

log "AI Shuffle backup starting (dry-run=${DRY_RUN})"
log "BACKUP_DIR=${BACKUP_DIR} RETENTION_DAYS=${BACKUP_RETENTION_DAYS}"

run mkdir -p "$BACKUP_DIR"
case "$BACKUP_RETENTION_DAYS" in
  ''|*[!0-9]*) echo "BACKUP_RETENTION_DAYS must be a non-negative integer" >&2; exit 2 ;;
esac

# Exclude the destination from every source tree, including custom destinations
# beneath a project's generated-media directory or the Fluid settings directory.
exclude_backup_destination() {
  BACKUP_EXCLUDES=()
  if [ "$DRY_RUN" -eq 1 ]; then return 0; fi
  local source_root backup_root
  source_root=$(cd "$1" && pwd -P)
  backup_root=$(cd "$BACKUP_DIR" && pwd -P)
  if [ "$source_root" = "$backup_root" ]; then
    echo "BACKUP_DIR must not equal an archive source directory" >&2; exit 2
  fi
  case "$backup_root" in
    "$source_root"/*) BACKUP_EXCLUDES+=("--exclude=./${backup_root#"$source_root"/}") ;;
  esac
}

# ---------------------------------------------------------------------------
# 1) Database dump (pg_dump custom format)
# ---------------------------------------------------------------------------
# Let the supervisor resolve the private descriptor used by normal startup.
# Never retrieve connection values into this shell or log them.
log "Dumping managed database via container lifecycle CLI"
run python /app/docker/container_runtime.py dump --file "$DB_DUMP"
log "DB dump -> ${DB_DUMP}"

# ---------------------------------------------------------------------------
# 2) Media tar (generated assets)
# ---------------------------------------------------------------------------
# Collect the media dirs that actually exist under WORKSPACE_DIR. tar with -C so
# paths in the archive are relative to the workspace root (clean restore).
if [ -d "$WORKSPACE_DIR" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] Archive generated-images/videos/audio/models recursively under $WORKSPACE_DIR"
  else
    exclude_backup_destination "$WORKSPACE_DIR"
    # Find project-local media too. NUL delimiters preserve whitespace/newlines;
    # -prune prevents repeated inclusion and find does not follow symlinks.
    (cd "$WORKSPACE_DIR" && find . -type d \( -name generated-images -o -name generated-videos -o -name generated-audio -o -name generated-models \) -prune -print0) \
      | tar -czf "$MEDIA_TAR" -C "$WORKSPACE_DIR" "${BACKUP_EXCLUDES[@]}" --null --files-from=-
  fi
  log "Media tar -> ${MEDIA_TAR}"
else
  log "No generated-media dirs found under ${WORKSPACE_DIR}; skipping media tar"
fi

# DATA_DIR contains app keys, memory, identity and private PostgreSQL lifecycle
# files. Keep the lifecycle parent (postgres/) but NEVER tar the live physical
# cluster (postgres/data): only the logical dump above is a database backup.
# umask 077 makes the archive private; tar preserves the lifecycle file modes.
# Exclude the backup destination itself to avoid recursive archives.
if [ -d "$DATA_DIR" ]; then
  exclude_backup_destination "$DATA_DIR"
  run tar -czf "$DATA_TAR" -C "$DATA_DIR" --exclude=./logs --exclude=./postgres/data "${BACKUP_EXCLUDES[@]}" .
fi

# ---------------------------------------------------------------------------
# 3) Active Fluid settings
# ---------------------------------------------------------------------------
# Archive only the active destination, never the legacy Claude auth/import volume.
if [ -d "$FLUID_HOME" ]; then
  log "Archiving active Fluid settings"
  exclude_backup_destination "$FLUID_HOME"
  run tar -czf "$SETTINGS_TAR" -C "$FLUID_HOME" "${BACKUP_EXCLUDES[@]}" .
  log "Settings tar -> ${SETTINGS_TAR}"
else
  log "No Fluid settings directory found at ${FLUID_HOME}; skipping settings tar"
fi

# ---------------------------------------------------------------------------
# 4) Retention prune
# ---------------------------------------------------------------------------
# Delete backup artifacts older than the retention window. Scoped strictly to
# our own name patterns so nothing else in BACKUP_DIR is ever touched.
log "Pruning backups older than ${BACKUP_RETENTION_DAYS} day(s)"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] find %s -maxdepth 1 -type f \\( -name "ai-shuffle-db-*.dump" -o -name "ai-shuffle-media-*.tar.gz" -o -name "ai-shuffle-settings-*.tar.gz" -o -name "ai-shuffle-data-*.tar.gz" \\) -mtime +%s -print\n' \
    "$BACKUP_DIR" "$BACKUP_RETENTION_DAYS"
else
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'ai-shuffle-db-*.dump' -o -name 'ai-shuffle-media-*.tar.gz' -o -name 'ai-shuffle-settings-*.tar.gz' -o -name 'ai-shuffle-data-*.tar.gz' \) \
    -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete
fi

log "Backup complete"
