# ============================================================================
# Stage 1: Frontend Builder — builds the SvelteKit static assets
# ============================================================================
FROM node:20-slim AS frontend-builder

WORKDIR /build

# Install dependencies first (cached unless package-lock changes)
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Build frontend
ARG CACHEBUST=0
COPY frontend/ ./
RUN npm run build


# ============================================================================
# Stage 2: AI Tools — install Node.js deps for AI tools
# ============================================================================
FROM node:20-slim AS tools-builder

WORKDIR /tools
COPY tools/package.json tools/package-lock.json* ./
RUN npm install --omit=dev --ignore-scripts
COPY tools/ ./


# ============================================================================
# Stage 3: Runtime — lean Python image with only what's needed
# ============================================================================
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── System deps (git, curl for health check, gosu for UID/GID, gh CLI) ──
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    git \
    gosu \
    tini \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# ── GitHub CLI ──
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       | tee /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js runtime (needed by Claude Agent SDK subprocess) ──
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN node --version && npm --version && gh --version

# ── Browser for the "Claude in Chrome" integration ──
# Gives the bundled Claude Code CLI's --chrome integration a real browser to drive.
# socat relays Chrome's localhost-only DevTools port when CHROME_DEBUG=1 (one-time
# extension sign-in/pairing).
#
# BOTH arches install Debian **Chromium** (this is python:3.11-slim / bookworm,
# package name `chromium`) and symlink it to /usr/bin/google-chrome-stable so
# CHROME_EXECUTABLE and start-chrome.sh resolve unchanged. We switched amd64 off
# full Google Chrome because full Chrome was consuming ~5GB RAM for a single tab
# in this headless single-tab setup; Chromium is far leaner and behaves
# identically for the extension + native-messaging path used here.
#
# Native-messaging caveat (#14391): Chromium reads user native-messaging host
# manifests from ~/.config/chromium/NativeMessagingHosts/, but the bundled CLI
# writes them under ~/.config/google-chrome/NativeMessagingHosts/. entrypoint.sh
# already bridges that at runtime for any chromium-resolving CHROME_EXECUTABLE,
# so it now applies on amd64 too. The managed-policy files below are written to
# BOTH /etc/opt/chrome/... and /etc/chromium/... so the force-install applies
# regardless; Chromium honours /etc/chromium/policies/managed/.
ARG TARGETARCH
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
        chromium socat; \
    ln -sf /usr/bin/chromium /usr/bin/google-chrome-stable; \
    rm -rf /var/lib/apt/lists/*

# Auto-install the "Claude in Chrome" extension via enterprise policy so it
# doesn't need to be clicked through the Web Store inside the container.
# DeveloperToolsAvailability=1 lifts the default ban on DevTools for
# force-installed extensions — the pairing/sign-in pages can only be reached
# through DevTools in this headless setup (CHROME_DEBUG=1).
# Written to both the Google Chrome (/etc/opt/chrome) and Chromium (/etc/chromium)
# managed-policy dirs so the force-install applies regardless of which dir the
# installed browser reads (we now ship Chromium on both arches → /etc/chromium).
RUN echo '{"ExtensionInstallForcelist":["fcoeoabgfenejglbffodgkkbkcdhcgfn;https://clients2.google.com/service/update2/crx"],"DeveloperToolsAvailability":1}' \
      > /tmp/claude-extension.json && \
    mkdir -p /etc/opt/chrome/policies/managed /etc/chromium/policies/managed && \
    cp /tmp/claude-extension.json /etc/opt/chrome/policies/managed/claude-extension.json && \
    cp /tmp/claude-extension.json /etc/chromium/policies/managed/claude-extension.json && \
    rm /tmp/claude-extension.json

# ── Create non-root user ──
RUN groupadd -g 1000 appuser && \
    useradd -m -u 1000 -g 1000 appuser && \
    mkdir -p /home/appuser/.config/claude /home/appuser/.claude && \
    mkdir -p /data /workspace && \
    chown -R appuser:appuser /home/appuser /data /workspace

# ── Python dependencies ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Make the bundled Claude Code CLI writable by appuser ──
# app/core/cli_interrupt_patch.py rewrites a byte in this binary at runtime
# (as appuser) to keep background subagents alive across Stop/interrupt
# (PRESERVE_BACKGROUND_ON_INTERRUPT). pip installs claude_agent_sdk as root,
# leaving the binary root:root -rwxr-xr-x, so the runtime patch fails with
# "Cannot write ... (read-only): Permission denied" and the feature is inert
# even with the env var set. The patch also writes a marker JSON file next to
# the binary to avoid re-scanning it on every boot, so the whole `_bundled/`
# directory (not just the `claude` file) must be appuser-owned or those
# marker writes fail too. We only chown here — the actual byte patch stays
# a runtime concern (it relocates its anchor per CLI build; see
# resolve_bundled_cli_path()/apply_patch() in cli_interrupt_patch.py) rather
# than being baked into the image. NOTE: entrypoint.sh remaps appuser to
# $PUID/$PGID at container start, which orphans this build-time chown — it
# must re-apply this same chown after that remap (see entrypoint.sh).
RUN find /usr/local/lib/python3*/site-packages/claude_agent_sdk \
    -type d -name _bundled -exec chown -R appuser:appuser {} +

# ── Backend code ──
COPY --chown=appuser:appuser app/ ./app/

# ── Maintenance / migration scripts (e.g. session-store backfill) ──
COPY --chown=appuser:appuser scripts/ ./scripts/

# ── Copy pre-built frontend assets from builder stage ──
RUN mkdir -p /app/app/static
COPY --from=frontend-builder --chown=appuser:appuser /build/build/ /app/app/static/

# ── Copy pre-built AI tools from builder stage ──
COPY --from=tools-builder --chown=appuser:appuser /tools/ /opt/ai-tools/

COPY --chown=appuser:appuser .env.example .

# Copy entrypoint + Claude in Chrome helper scripts
COPY entrypoint.sh /entrypoint.sh
COPY docker/start-chrome.sh /usr/local/bin/start-chrome
COPY docker/chrome-supervisor.sh /usr/local/bin/chrome-supervisor
COPY docker/chrome-doctor.sh /usr/local/bin/chrome-doctor
RUN chmod +x /entrypoint.sh /usr/local/bin/start-chrome /usr/local/bin/chrome-supervisor /usr/local/bin/chrome-doctor

# Ensure /app is owned by appuser
RUN chown appuser:appuser /app

# ── Bake the git SHA into the image (deploy-restart detection) ──
# scripts/unraid-lab/wake_detect.py compares this running-image SHA against a
# pre-deploy marker to tell "new container landed" (matches target) apart from
# "container restarted but old image is still running" (mismatch). Falls back
# to boot-time-only detection (landed_unverified) if this file is absent.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
LABEL org.opencontainers.image.revision=$GIT_SHA
RUN printf '%s\n' "$GIT_SHA" > /app/GIT_SHA

# ── Claude in Chrome integration defaults ──
# START_CHROME=1 launches headless Chromium at container start so the bundled
# CLI's --chrome integration has a browser to drive (set 0 to disable).
# CHROME_CDP=1 binds DevTools to a localhost-only port so the AI Shuffle backend
#   can proxy it for the in-app "Set up browser" panel (one-time sign-in/pairing
#   with no exposed port). Set 0 to disable the in-app browser setup UI.
# CHROME_DEBUG=1 additionally relays DevTools out to :9222 for manual/legacy
#   access — prefer the in-app panel; this exposes the browser to the network.
ENV START_CHROME=1 \
    CHROME_CDP=1 \
    CHROME_DEBUG=0 \
    CHROME_EXECUTABLE=/usr/bin/google-chrome-stable

EXPOSE 8000

HEALTHCHECK --interval=120s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health/ready || exit 1

# tini runs as PID 1 so orphaned/zombie processes (e.g. the many short-lived
# grandchildren of a crash-looping headless Chromium) are reaped by a real init.
# Without this, the app process (PID 1) inherits every orphan but never wait()s
# on them, so defunct chromium/chrome_crashpad corpses accumulate until the
# container's pids cgroup fills and ALL forks fail (SDK spawns, git worktree,
# even `docker exec`). `-g` forwards signals to the whole process group so a
# `docker stop` shuts the app down cleanly.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/entrypoint.sh"]
