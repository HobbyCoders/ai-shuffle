# Use official Python runtime as base image
FROM python:3.11-slim

# Set working directory in container
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_VERSION=20.x

# ── Layer 1: System deps + Playwright runtime libs (rarely changes) ──
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    git \
    gosu \
    # Playwright Chromium runtime dependencies
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 2: Node.js (changes only when NODE_VERSION changes) ──
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 3: GitHub CLI (changes rarely) ──
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       | tee /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Verify installations
RUN node --version && npm --version && gh --version

# Note: Claude Code CLI is bundled with claude-agent-sdk Python package
# No need to install separately via npm

# ── Layer 4: Python dependencies (changes when requirements.txt changes) ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Layer 5: Playwright browser (changes when playwright version changes) ──
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN playwright install chromium && \
    chmod -R 755 $PLAYWRIGHT_BROWSERS_PATH

# ── Layer 6: Frontend deps (changes when package-lock.json changes) ──
COPY frontend/package.json frontend/package-lock.json ./frontend/
WORKDIR /app/frontend
RUN npm ci

# ── Layer 7: Frontend build (changes when frontend source changes) ──
COPY frontend/ ./
RUN npm run build

WORKDIR /app

# ── Layer 8: Backend code + static assets (changes most frequently) ──
COPY app/ ./app/
RUN mkdir -p /app/app/static && cp -r /app/frontend/build/* /app/app/static/

COPY .env.example .

# Copy AI tools to /opt/ai-tools (accessible by Claude agent)
COPY tools/ /opt/ai-tools/

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create required directories
RUN mkdir -p /data /workspace

# Create a non-root user for security (default UID/GID, will be modified at runtime)
RUN groupadd -g 1000 appuser && \
    useradd -m -u 1000 -g 1000 appuser && \
    mkdir -p /home/appuser/.config/claude /home/appuser/.claude && \
    chown -R appuser:appuser /app /home/appuser /data /workspace /opt/ai-tools

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=120s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Use entrypoint script to handle runtime UID/GID changes
ENTRYPOINT ["/entrypoint.sh"]
