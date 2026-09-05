#!/usr/bin/env bash
# Explicit commands (including --dry-run) never start the scheduler.
set -euo pipefail
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
printf '%s bash /scripts/backup.sh >> /data/backup.log 2>&1\n' "${BACKUP_CRON:-30 3 * * *}" > /etc/crontabs/root
echo "[backup] scheduler started"
exec crond -f -l 8
