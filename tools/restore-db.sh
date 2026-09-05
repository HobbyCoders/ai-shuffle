#!/bin/bash
# Restore the PostgreSQL database from a SQL backup file.
# Usage: ./tools/restore-db.sh <backup-file>
#
# WARNING: This will overwrite all current data in the database.
#
# Example:
#   ./tools/restore-db.sh backup-2026-04-10.sql

set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: ./tools/restore-db.sh <backup-file>"
    echo "Example: ./tools/restore-db.sh backup-2026-04-10.sql"
    exit 1
fi

BACKUP="$1"

if [ ! -f "${BACKUP}" ]; then
    echo "Error: File not found: ${BACKUP}"
    exit 1
fi

echo "WARNING: This will overwrite all current data in the database."
read -p "Continue? (y/N) " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "Aborted."
    exit 0
fi

echo "Restoring database from ${BACKUP}..."
# Keep schema replacement and restore in one transaction. A SQL error aborts
# rather than printing success after a partially restored database.
{
    printf 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;\n'
    cat "${BACKUP}"
} | docker compose exec -T postgres psql -U ai_shuffle --set=ON_ERROR_STOP=1 --single-transaction --file=- ai_shuffle
echo "Done. Database restored from ${BACKUP}."
