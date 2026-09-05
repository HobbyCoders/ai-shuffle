# ============================================================================
# Stage 1: Frontend Builder — builds the SvelteKit static assets
# ============================================================================
FROM node:22-slim AS frontend-builder

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
FROM node:22-slim AS tools-builder

WORKDIR /tools
COPY tools/package.json tools/package-lock.json* ./
RUN npm ci --ignore-scripts
COPY tools/ ./
RUN npm run build && npm prune --omit=dev --ignore-scripts


# ============================================================================
# Stage 3: Browser MCP — build the browser-automation MCP server (TypeScript)
# ============================================================================
FROM node:22-slim AS browser-mcp-builder

WORKDIR /browser-mcp
# Install deps first (cached unless the lockfile changes). --ignore-scripts skips
# dependency lifecycle scripts; compilation is explicit below — the real build runs below. playwright-core pulls NO
# bundled browsers (the server drives the supervised Chromium over CDP), which
# saves ~300MB versus the full `playwright` package.
COPY browser-mcp/package.json browser-mcp/package-lock.json* ./
RUN npm ci --ignore-scripts
COPY browser-mcp/ ./
# Compile TS -> dist/ (entry point: dist/index.js), then drop devDeps
# (typescript, vitest) from node_modules so only runtime deps ship in the image.
RUN npm run build && npm prune --omit=dev


# ============================================================================
# Stage 4: Runtime — lean Python image with only what's needed
# ============================================================================
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── System deps (git, curl for health check, gosu for UID/GID, gh CLI) ──
# ripgrep is not optional comfort: the agent's Grep tool falls back to a pure
# Python scan without it, which is slow enough to dominate a session.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    git \
    gosu \
    ripgrep \
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

# ── Node.js runtime (delegated CLI providers and bundled MCP tools) ──
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN node --version && npm --version && gh --version

# ── Browser for the built-in browser-automation MCP server ──
# The browser-mcp server (Stage 3) drives this browser over CDP; the
# chrome-supervisor owns the process (see docker/start-chrome.sh). No browser
# extension, no native-messaging host, and no --chrome CLI flag are involved.
#
# BOTH arches install Debian **Chromium** (this is python:3.12-slim / bookworm,
# package name `chromium`) and symlink it to /usr/bin/google-chrome-stable so
# CHROME_EXECUTABLE and start-chrome.sh resolve unchanged. We switched amd64 off
# full Google Chrome because full Chrome was consuming ~5GB RAM for a single tab
# in this headless single-tab setup; Chromium is far leaner and behaves
# identically for the CDP automation used here.
ARG TARGETARCH
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
        chromium; \
    ln -sf /usr/bin/chromium /usr/bin/google-chrome-stable; \
    rm -rf /var/lib/apt/lists/*

# ── Create non-root user ──
RUN groupadd -g 1000 appuser && \
    useradd -m -u 1000 -g 1000 appuser && \
    mkdir -p /home/appuser/.fluid /home/appuser/.config/claude /home/appuser/.claude && \
    mkdir -p /data /workspace && \
    chown -R appuser:appuser /home/appuser /data /workspace

# ── Python dependencies ──
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 && pip install --no-cache-dir -r requirements.txt

# ── Dev / test dependencies ──
# The runtime image is also the agent's workbench: it checks out this repo and
# runs the suite. Without pytest baked in, a session spends its first turns
# pip-installing one. Kept as its own layer so editing dev deps never
# invalidates the runtime-deps layer above.
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

# ── fluid harness (REQUIRED — the only provider backend) ──
# vendor/fluid contains the harness source as a
# pristine copy of fluid main, committed to this repo and kept current by
# .github/workflows/vendor-sync.yml, so the build needs no cross-repo
# credentials. The image build must fail if the required harness cannot install.
COPY vendor/ /opt/vendor/
RUN pip install --no-cache-dir /opt/vendor/fluid && python -c "import fluid"

# ── Backend code ──
COPY --chown=appuser:appuser app/ ./app/

# ── Maintenance / migration scripts (e.g. session-store backfill) ──
COPY --chown=appuser:appuser scripts/ ./scripts/

# ── Copy pre-built frontend assets from builder stage ──
RUN mkdir -p /app/app/static
COPY --from=frontend-builder --chown=appuser:appuser /build/build/ /app/app/static/

# ── Copy pre-built AI tools from builder stage ──
COPY --from=tools-builder --chown=appuser:appuser /tools/ /opt/ai-tools/

# ── Copy the built browser-automation MCP server from its builder stage ──
# Fluid launches `node /opt/browser-mcp/dist/index.js` when the tool is used (see
# app/core/builtin_mcp_servers.py DEFAULT_BROWSER_MCP_ENTRY). node_modules here
# is the pruned prod-only set; package.json ships so its `"type": "module"`
# resolves at runtime.
COPY --from=browser-mcp-builder --chown=appuser:appuser /browser-mcp/dist/ /opt/browser-mcp/dist/
COPY --from=browser-mcp-builder --chown=appuser:appuser /browser-mcp/node_modules/ /opt/browser-mcp/node_modules/
COPY --from=browser-mcp-builder --chown=appuser:appuser /browser-mcp/package.json /opt/browser-mcp/package.json

COPY --chown=appuser:appuser .env.example .

# Copy entrypoint + browser (Chromium supervisor) helper scripts
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

# ── PATH ──
# Anything the agent installs for itself at runtime (`pip install --user`, an
# npm prefix in its home) lands in ~/.local/bin. Off PATH those binaries are on
# disk but "not found", which reads as a failed install.
ENV PATH="/home/appuser/.local/bin:${PATH}"

# ── Browser-automation defaults ──
# START_CHROME=1 launches the supervised headless Chromium at container start so
#   the built-in browser MCP server has a browser to drive (set 0 to disable).
# CHROME_CDP=1 binds DevTools to a localhost-only port (CHROME_CDP_PORT, default
#   9221) inside the container. Nothing is published to the network; the
#   browser-mcp server attaches via connectOverCDP('http://127.0.0.1:9221').
ENV START_CHROME=1 \
    CHROME_CDP=1 \
    CHROME_CDP_PORT=9221 \
    CHROME_EXECUTABLE=/usr/bin/google-chrome-stable \
    SETTINGS_IMPORT_SOURCE_HOME=/home/appuser \
    SETTINGS_IMPORT_ALLOWED_HOMES=/imports/provider-settings

EXPOSE 8000

HEALTHCHECK --interval=120s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health/ready || exit 1

# tini runs as PID 1 so orphaned/zombie processes (e.g. the many short-lived
# grandchildren of a crash-looping headless Chromium) are reaped by a real init.
# Without this, the app process (PID 1) inherits every orphan but never wait()s
# on them, so defunct chromium/chrome_crashpad corpses accumulate until the
# container's pids cgroup fills and ALL forks fail (provider/MCP spawns, git worktree,
# even `docker exec`). `-g` forwards signals to the whole process group so a
# `docker stop` shuts the app down cleanly.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/entrypoint.sh"]
