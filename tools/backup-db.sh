#!/usr/bin/env bash
# Back up the selected database through the running app container's lifecycle CLI.
# Default: embedded PostgreSQL; existing external configuration stays authoritative.
# Usage: ./tools/backup-db.sh [output-file.dump]
# For the legacy topology, set COMPOSE_FILE=docker-compose.yml:docker-compose.external.yml.
# Produces a custom-format logical dump, NOT SQL for tools/restore-db.sh.
# See docs/RESTORE.md for an offline restore into an empty embedded database.
set -euo pipefail
umask 077

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [output-file.dump]" >&2
  exit 2
fi
case "${1:-}" in
  -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
esac

OUTPUT="${1:-backup-$(date -u +%Y%m%dT%H%M%SZ).dump}"
# A failed docker exec/dump must not truncate an existing backup or leave a
# partial archive with a successful filename. The temp file is private too.
TEMP_OUTPUT=$(mktemp -- "${OUTPUT}.partial.XXXXXX")
trap 'rm -f -- "$TEMP_OUTPUT"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'Backing up database to %s...\n' "$OUTPUT"
# '-' is archive-only stdout; diagnostics stay on stderr. No URL or password
# is fetched into the host shell, printed, or passed in command-line arguments.
docker compose exec -T ai-shuffle python /app/docker/container_runtime.py dump --file - > "$TEMP_OUTPUT"
if [ ! -s "$TEMP_OUTPUT" ]; then
  echo "Backup failed: the lifecycle CLI produced an empty archive" >&2
  exit 1
fi
mv -f -- "$TEMP_OUTPUT" "$OUTPUT"
printf 'Done. Custom-format backup saved to %s\n' "$OUTPUT"
