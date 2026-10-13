#!/usr/bin/env bash
#
# Docker-host deploy for AI Shuffle. Build (or pull) the image, bring the
# stack up, then poll /health/ready until the app is ready.
#
# docker-compose.yml builds from source and names no registry image, so the
# default is --build. --pull only helps after you add an image: line there.
#
# The native-Windows equivalent is scripts/deploy.ps1.
#
# Usage:
#   scripts/deploy.sh                 # build locally + up -d + wait for ready
#   scripts/deploy.sh --pull          # pull the compose file's image instead of building
#   scripts/deploy.sh --port 8000     # port to poll for readiness (default 8000)
#   scripts/deploy.sh --timeout 120   # readiness timeout seconds (default 90)
#   scripts/deploy.sh --dry-run       # print the commands, run nothing
#
# The managed container owns its database; no database environment setup.
# Schedule backups through docker compose exec; see docs/RESTORE.md.
#
set -euo pipefail

MODE="build"         # build | pull
PORT=8000
TIMEOUT=90
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --build)        MODE="build" ;;
    --pull)         MODE="pull" ;;
    --port)         PORT="$2"; shift ;;
    --timeout)      TIMEOUT="$2"; shift ;;
    --dry-run)      DRY_RUN=1 ;;
    -h|--help)      sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# Pick the docker compose invocation (v2 plugin preferred, v1 fallback).
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "docker compose not found" >&2
  exit 3
fi

# Managed-only topology. Backups run inside this same container.
FILES=(-f docker-compose.yml)

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

log() { printf '==> %s\n' "$*"; }

# Move to repo root (scripts/ is one level down) so compose finds the files.
cd "$(dirname "$0")/.."

if [ "$MODE" = "pull" ]; then
  log "Pulling latest image"
  run "${COMPOSE[@]}" "${FILES[@]}" pull
else
  log "Building image locally"
  run "${COMPOSE[@]}" "${FILES[@]}" build
fi

log "Bringing the stack up (detached)"
run "${COMPOSE[@]}" "${FILES[@]}" up -d

# ---------------------------------------------------------------------------
# Readiness loop against /health/ready (DB SELECT 1 + session-pool headroom).
# ---------------------------------------------------------------------------
log "Waiting for http://localhost:${PORT}/health/ready (timeout ${TIMEOUT}s)"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '[dry-run] poll http://localhost:%s/health/ready until 200 (or %ss)\n' "$PORT" "$TIMEOUT"
  log "Deploy complete (dry-run)"
  exit 0
fi

deadline=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if curl -fsS "http://localhost:${PORT}/health/ready" >/dev/null 2>&1; then
    log "Backend READY on http://localhost:${PORT}/health/ready"
    exit 0
  fi
  sleep 3
done

echo "ERROR: backend not ready after ${TIMEOUT}s" >&2
log "Recent app logs:"
"${COMPOSE[@]}" "${FILES[@]}" logs --tail=30 ai-shuffle || true
exit 1
