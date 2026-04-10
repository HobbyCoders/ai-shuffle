#!/bin/bash
# Backup the PostgreSQL database to a SQL file.
# Usage: ./tools/backup-db.sh [output-file]
#
# Examples:
#   ./tools/backup-db.sh                    # -> backup-2026-04-10.sql
#   ./tools/backup-db.sh my-backup.sql      # -> my-backup.sql

set -e

OUTPUT="${1:-backup-$(date +%Y-%m-%d).sql}"

echo "Backing up database to ${OUTPUT}..."
docker compose exec -T postgres pg_dump -U ai_shuffle ai_shuffle > "${OUTPUT}"
echo "Done. Backup saved to ${OUTPUT} ($(du -h "${OUTPUT}" | cut -f1))"
