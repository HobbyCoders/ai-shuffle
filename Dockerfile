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

# ── Headless browser for the Playwright MCP server ──
# Installs Chromium + its OS dependencies so the @playwright/mcp server can
# drive a real headless browser server-side (no visible window). The browser
# lives in a shared cache dir (PLAYWRIGHT_BROWSERS_PATH) that is world-readable
# so the non-root appuser can launch it at runtime.
#
# Version pinning (why it matters): the runtime launches a PINNED
# @playwright/mcp@0.0.76, which bundles playwright-core@1.61.0-alpha-1781023400000.
# Each Playwright release expects a SPECIFIC Chromium build revision. If we baked
# the browser with a different (e.g. @latest) Playwright, the MCP server would
# look for "chromium-<rev>" that doesn't exist under /ms-playwright and fail at
# runtime ("Executable doesn't exist ..."), especially offline/air-gapped.
#
# To keep the baked Chromium revision in sync with what the MCP server launches,
# we install the SAME pinned @playwright/mcp globally and drive the browser
# install THROUGH its bundled playwright-core, so the revision is guaranteed to
# match. The global install is also reused at runtime (no network fetch needed).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PLAYWRIGHT_MCP_VERSION=0.0.76
RUN npm install -g "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" \
    && PW_CORE_DIR="$(node -e "process.stdout.write(require('path').dirname(require.resolve('playwright-core/package.json',{paths:[require.resolve('@playwright/mcp',{paths:[require('child_process').execSync('npm root -g').toString().trim()]})]})))")" \
    && echo "Using playwright-core at: ${PW_CORE_DIR} ($(node -e "process.stdout.write(require(\"${PW_CORE_DIR}/package.json\").version)"))" \
    && node "${PW_CORE_DIR}/cli.js" install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

# ── Create non-root user ──
RUN groupadd -g 1000 appuser && \
    useradd -m -u 1000 -g 1000 appuser && \
    mkdir -p /home/appuser/.config/claude /home/appuser/.claude && \
    mkdir -p /data /workspace && \
    chown -R appuser:appuser /home/appuser /data /workspace

# ── Python dependencies ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Backend code ──
COPY --chown=appuser:appuser app/ ./app/

# ── Copy pre-built frontend assets from builder stage ──
RUN mkdir -p /app/app/static
COPY --from=frontend-builder --chown=appuser:appuser /build/build/ /app/app/static/

# ── Copy pre-built AI tools from builder stage ──
COPY --from=tools-builder --chown=appuser:appuser /tools/ /opt/ai-tools/

COPY --chown=appuser:appuser .env.example .

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Ensure /app is owned by appuser
RUN chown appuser:appuser /app

EXPOSE 8000

HEALTHCHECK --interval=120s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
