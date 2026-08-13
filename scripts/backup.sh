#!/usr/bin/env bash
#
# AI Shuffle backup — pg_dump (custom format) + tar of generated media, with
# timestamped output and retention pruning. Targets the Docker deployment
# (shared Postgres + /workspace + /data volumes) but runs anywhere the env is
# set correctly.
#
# What it produces (in $BACKUP_DIR):
#   ai-shuffle-db-<timestamp>.dump      pg_dump -Fc (custom format, restore with pg_restore)
#   ai-shuffle-media-<timestamp>.tar.gz tar of generated media dirs (see MEDIA_DIRS)
#
# Retention: files older than BACKUP_RETENTION_DAYS (default 7) matching the
# backup name patterns are pruned after a successful run.
#
# Configuration (env vars):
#   PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE   standard libpq vars
#   BACKUP_DIR             where backups are written (default: /data/backups)
#   BACKUP_RETENTION_DAYS  days to keep (default: 7)
#   WORKSPACE_DIR          generated media root (default: /workspace)
#   DATA_DIR              app data root, included in media tar if present (default: /data)
#
# Flags:
#   --dry-run   print the commands that WOULD run; touch nothing.
#
# Verified offline with:  bash -n scripts/backup.sh  and  --dry-run
#
set -euo pipefail

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

BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"
DATA_DIR="${DATA_DIR:-/data}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

DB_DUMP="${BACKUP_DIR}/ai-shuffle-db-${TIMESTAMP}.dump"
MEDIA_TAR="${BACKUP_DIR}/ai-shuffle-media-${TIMESTAMP}.tar.gz"

# Generated-media directories to include in the tar, relative to WORKSPACE_DIR.
# These are where the app writes generated assets (see app/api/generated_*.py).
MEDIA_SUBDIRS=(generated-images generated-videos generated-audio)

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

# ---------------------------------------------------------------------------
# 1) Database dump (pg_dump custom format)
# ---------------------------------------------------------------------------
# pg_dump reads the standard libpq PG* vars from the environment.
log "Dumping database via PG* environment variables"
run pg_dump --format=custom --no-owner --no-privileges --file="$DB_DUMP" 
log "DB dump -> ${DB_DUMP}"

# ---------------------------------------------------------------------------
# 2) Media tar (generated assets)
# ---------------------------------------------------------------------------
# Collect the media dirs that actually exist under WORKSPACE_DIR. tar with -C so
# paths in the archive are relative to the workspace root (clean restore).
TAR_ARGS=()
for sub in "${MEDIA_SUBDIRS[@]}"; do
  if [ -d "${WORKSPACE_DIR}/${sub}" ]; then
    TAR_ARGS+=("$sub")
  fi
done

if [ "${#TAR_ARGS[@]}" -gt 0 ]; then
  log "Archiving media dirs: ${TAR_ARGS[*]}"
  run tar -czf "$MEDIA_TAR" -C "$WORKSPACE_DIR" "${TAR_ARGS[@]}"
  log "Media tar -> ${MEDIA_TAR}"
else
  log "No generated-media dirs found under ${WORKSPACE_DIR}; skipping media tar"
fi

# ---------------------------------------------------------------------------
# 3) Retention prune
# ---------------------------------------------------------------------------
# Delete backup artifacts older than the retention window. Scoped strictly to
# our own name patterns so nothing else in BACKUP_DIR is ever touched.
log "Pruning backups older than ${BACKUP_RETENTION_DAYS} day(s)"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] find %s -maxdepth 1 -type f \\( -name "ai-shuffle-db-*.dump" -o -name "ai-shuffle-media-*.tar.gz" \\) -mtime +%s -print\n' \
    "$BACKUP_DIR" "$BACKUP_RETENTION_DAYS"
else
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'ai-shuffle-db-*.dump' -o -name 'ai-shuffle-media-*.tar.gz' \) \
    -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete
fi

log "Backup complete"
