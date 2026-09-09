#!/usr/bin/env bash
set -euo pipefail
echo "The database backup sidecar is retired. Run the managed container supervisor dump command; no external database configuration is supported." >&2
exit 1
