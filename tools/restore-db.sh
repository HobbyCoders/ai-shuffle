#!/usr/bin/env bash
# Historical destructive SQL restore is no longer supported.
# Keep old SQL backups unchanged. See docs/RESTORE.md for offline recovery into
# a separate empty managed target with a PostgreSQL custom-format dump.
set -euo pipefail

case "${1:-}" in
  -h|--help)
    printf '%s\n' 'Use the managed offline restore procedure in docs/RESTORE.md.'
    exit 0
    ;;
esac
printf '%s\n' \
  'Refusing in-place SQL restore: no database or backup file was changed.' \
  'Use the managed runtime restore --file command on a separate empty recovery volume; see docs/RESTORE.md.' >&2
exit 2
