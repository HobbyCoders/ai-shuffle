"""
Application configuration and settings

Supports both Docker and Local deployment modes:
- Docker: Uses /data, /workspace, /home/appuser paths
- Local: Uses platform-specific paths (AppData, ~/.local/share, etc.)

Set LOCAL_MODE=true environment variable to force local mode.
"""

from pathlib import Path
from typing import Optional
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings

# Import platform detection (lazy to avoid circular imports)
def _get_default_data_dir() -> Path:
    """Get default data directory based on deployment mode"""
    from app.core.platform import get_app_data_dir
    return get_app_data_dir()

def _get_default_workspace_dir() -> Path:
    """Get default workspace directory based on deployment mode"""
    from app.core.platform import get_default_workspace_dir
    return get_default_workspace_dir()


_cached_persisted_instance_id: Optional[str] = None


def _get_or_create_persisted_instance_id(data_dir: Path, service_name: str) -> str:
    """Return a stable instance ID persisted to data_dir/.instance_id.

    On first startup a new ID is generated from service_name + a short UUID
    suffix (e.g. "ai-shuffle-a1b2c3d4"). The file lives inside the Docker
    volume so it survives container rebuilds while remaining unique per device.
    """
    global _cached_persisted_instance_id
    if _cached_persisted_instance_id is not None:
        return _cached_persisted_instance_id

    id_file = data_dir / ".instance_id"
    try:
        if id_file.exists():
            stored = id_file.read_text().strip()
            if stored:
                _cached_persisted_instance_id = stored
                return stored
    except OSError:
        pass

    # Generate a new stable ID
    import uuid
    new_id = f"{service_name}-{uuid.uuid4().hex[:8]}"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        id_file.write_text(new_id + "\n")
    except OSError:
        pass  # Non-fatal: worst case we regenerate next time

    _cached_persisted_instance_id = new_id
    return new_id


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Persistent on-disk logging (app.core.file_logging). Survives an app crash
    # and container recreation, unlike the in-memory ring buffer behind
    # /api/v1/logs (lost on crash) and docker stdout (lost on container
    # recreation). Files land under <data_dir>/logs, which is the durable data
    # volume in Docker (ai-shuffle-data:/data) and %USERPROFILE%\.ai-shuffle on
    # a native Windows install.
    #   - file_logging_enabled: master switch. FILE_LOGGING_ENABLED=false to
    #     disable the rotating file handler + faulthandler entirely.
    #   - file_log_level: minimum level for the on-disk file (independent of the
    #     console/ring-buffer level so you can capture more or less on disk).
    #   - file_log_max_bytes / file_log_backup_count: RotatingFileHandler sizing.
    file_logging_enabled: bool = True
    file_log_level: str = "INFO"
    file_log_max_bytes: int = 10 * 1024 * 1024  # 10MB per file
    file_log_backup_count: int = 5

    # Service info
    service_name: str = "ai-shuffle"
    version: str = "1.2.1"

    # Deployment mode (auto-detected if not set)
    local_mode: Optional[bool] = None  # None = auto-detect, True = local, False = docker

    # Paths - these will be set dynamically if not provided
    data_dir: Optional[Path] = None
    workspace_dir: Optional[Path] = None
    claude_projects_dir: Optional[Path] = None  # Legacy Claude CLI transcript root (defaults to ~/.claude/projects)
    tools_dir: Optional[Path] = None  # Path to AI tools directory (defaults to /workspace/ai-shuffle/tools in Docker)

    # Database (PostgreSQL). Set the individual connection pieces — the app
    # assembles the connection URL itself, so nobody has to hand-build a
    # postgresql:// DSN (or get percent-encoding of a password right).
    #   POSTGRES_HOST     — hostname or IP. Defaults to "postgres" (the compose
    #                       service) in Docker and "localhost" in local mode.
    #   POSTGRES_PORT     — default 5432.
    #   POSTGRES_USER     — default "ai_shuffle".
    #   POSTGRES_PASSWORD — REQUIRED, no default. Collected during setup
    #                       (run_local.py --setup writes it to .env; Docker
    #                       compose fails fast if unset).
    #   POSTGRES_DB       — default "ai_shuffle".
    #   DATABASE_URL      — LEGACY full connection string from versions before
    #                       the split. Still honored — an existing install that
    #                       upgraded has no way to re-run setup, so its old
    #                       .env must keep working as-is. Takes precedence when
    #                       set (it captures the user's complete previous
    #                       config, including a custom user/db/host the
    #                       POSTGRES_* defaults would get wrong). New installs
    #                       use the pieces above; setup comments the legacy URL
    #                       out when it writes new values.
    postgres_host: Optional[str] = None
    postgres_port: int = 5432
    postgres_user: str = "ai_shuffle"
    postgres_password: Optional[str] = None
    postgres_db: str = "ai_shuffle"
    database_url: Optional[str] = None
    db_pool_min: int = 2  # Min connections in PostgreSQL pool
    db_pool_max: int = 32  # Max connections in PostgreSQL pool
    # Headroom note: the rate-limit middleware does a synchronous DB auth
    # lookup on every HTTP request, multiple devices poll /live, and several
    # thread pools draw on this one pool concurrently (asyncio's default
    # executor is capped at 32 threads, anyio's sync-dependency pool at 40,
    # the git pool at 4), so a burst of concurrent requests can need many
    # slots at once. The old max of 20 was below that concurrency and a
    # transient overlap produced hard "connection pool exhausted" 500s. 32 is
    # still well within a default Postgres max_connections=100 even with a
    # second install sharing the same database (2×32 = 64 leaves headroom).

    # Instance identity — isolates projects, sessions, and workspace path
    # when multiple deployments share the same database.
    # Falls back to service_name if not set (stable across Docker rebuilds).
    instance_id: Optional[str] = None

    # Cross-install cluster (shared-database fleet). When multiple installs
    # share one database they form a cluster: each self-registers in the
    # ``installs`` table and can relay live session control to the install that
    # owns a running session.
    #   - cluster_secret: shared bearer secret peers present to each other. If
    #     unset, one is auto-generated and persisted in system_settings so all
    #     installs on the shared DB converge on the same value.
    #   - cluster_advertise_url: this install's externally peer-reachable base
    #     URL (e.g. https://box-a.example.com). If unset we fall back to a
    #     best-effort guess; installs with no reachable URL stay viewable but
    #     cannot be controlled remotely.
    cluster_secret: Optional[str] = None
    cluster_advertise_url: Optional[str] = None
    # Cross-install artifact GET is proxied through the authed origin by default
    # (the browser cookie cannot follow a 307 to the holder). Kill switch:
    # CLUSTER_ARTIFACT_PROXY=0 restores the 307 redirect.
    cluster_artifact_proxy: bool = True

    # Session
    session_secret: Optional[str] = None
    session_expire_days: int = 30

    # Claude
    command_timeout: int = 300

    # Security - Rate Limiting
    max_login_attempts: int = 5  # Max failed attempts before lockout
    login_attempt_window_minutes: int = 15  # Time window for counting attempts
    lockout_duration_minutes: int = 30  # Duration of lockout after max attempts

    # Security - API Key Session
    api_key_session_expire_hours: int = 24  # API key web session duration

    # Security - Trusted Proxies (for getting real IP behind reverse proxy)
    trusted_proxy_headers: str = "X-Forwarded-For,X-Real-IP"  # Comma-separated

    # Security - CORS (comma-separated origins, or "*" for development only)
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    # Security - Cookie settings
    cookie_secure: bool = True  # Set to False only for local HTTP development

    # Security - Request limits
    max_request_body_mb: int = 50  # Maximum request body size in MB

    # Security - WebSocket rate limiting (per connection)
    ws_rate_limit_messages: int = 30  # Max messages per sliding window
    ws_rate_limit_window_seconds: float = 10.0  # Sliding window duration in seconds

    # Admin password - used for encryption key derivation on startup
    # Set via ADMIN_PASSWORD env var or .env file to survive server restarts
    admin_password: Optional[str] = None

    # Notes disk-sync worker. Notes are materialized to per-install local .md
    # files; this periodic worker reconciles those files with the shared database
    # in both directions so an edit made anywhere (an agent, the user over
    # SSH/SMB, or another install) converges everywhere. See app.core.notes_sync.
    # Interval is a poll cadence (watchdog-style fs events are unreliable on the
    # network/fuse filesystems these installs commonly run on). 0 disables it.
    notes_sync_interval_seconds: int = 15

    # Codex / ChatGPT-subscription OAuth token-refresh worker. Polls every
    # enabled Codex provider on this cadence and refreshes any whose access token
    # is due (proactive 8-day interval, or JWT exp within 5 min — see
    # app.core.codex_token_refresh). The Codex refresh token ROTATES, so refreshes
    # are serialized per provider by a lock; this is just the wake-up cadence. 0
    # disables the periodic worker (the refresh-on-401 hook still works on demand).
    codex_token_refresh_interval_seconds: int = 3600

    # SuperGrok / xAI OAuth token-refresh worker. Same cadence semantics as
    # ``codex_token_refresh_interval_seconds`` (0 disables the periodic worker;
    # the refresh-on-401 hook still works on demand). Defaults to the Codex
    # interval so both sweeps wake together.
    xai_token_refresh_interval_seconds: int = 3600

    # Timeouts (milliseconds)
    hook_timeout_ms: int = 30_000
    # Size limits
    max_tool_output_chars: int = 500_000  # ~500KB, well under 1MB WS buffer
    max_display_output_chars: int = 2_000  # What we show in the UI
    sdk_buffer_size: int = 50 * 1024 * 1024  # 50MB for base64 images/PDFs

    # SDK Session Pool — max concurrent subprocess sessions kept alive between queries.
    # Each idle subprocess uses ~100MB RAM, so 8 sessions ≈ 800MB worst case
    # (idle eviction below reclaims them long before steady state hits that).
    # At 3, one long-lived orchestrator + a couple of automations evicted the
    # user's chat session on every message, forcing a full subprocess respawn
    # (and its Chrome-extension reconnect) per message.
    sdk_session_pool_size: int = 8

    # How long a pooled subprocess may sit idle before the resource monitor
    # evicts it. Every eviction turns the next message into a cold respawn
    # (slower first token, Chrome native-host handshake), so keep this
    # comfortably above a normal read-and-reply gap. Override via
    # SDK_SESSION_IDLE_TIMEOUT_SECONDS.
    sdk_session_idle_timeout_seconds: int = 1800  # 30 minutes

    # AskUserQuestion backstop (seconds). A parked question keeps its SDK
    # subprocess alive AND pinned in the pool — while awaiting an answer the
    # session still counts as "streaming", so idle/LRU eviction skips it. The
    # backstop has to be long enough that a question survives you stepping away
    # across many sessions, but short enough that a truly-abandoned session
    # reaps its hot subprocess instead of holding ~100MB for hours. 1h is the
    # default; raise it toward Claude-Desktop-style persistence if you have the
    # headroom, or lower it on a tight box. Override via
    # USER_QUESTION_TIMEOUT_SECONDS.
    user_question_timeout_seconds: int = 3600  # 1 hour

    # Between-turns idle reader — CLI-owned wake grace window (seconds). When the
    # LAST background task completes while the parent is parked, the scheduler
    # SUPPRESSES the app's own unpark wake because the CLI delivers the wake
    # itself — but that CLI wake turn arrives 7-60s later, after the idle reader
    # would otherwise exit on its next 2s poll (has_active_background_work now
    # False). The reader then never reads the wake, so its frames stay buffered
    # and the UI never sees it. This grace keeps the reader draining for up to
    # this long after a CLI-owned suppression, so it captures the wake when it
    # lands. Refreshed on every frame while waiting; cleared when the wake turn
    # completes. Override via CLI_WAKE_GRACE_SECONDS.
    cli_wake_grace_seconds: float = 300.0

    # Admission backpressure for the subagent fan-out. A single orchestrator
    # session can spawn dozens of subagent node subprocesses; rather than letting
    # a destructive watchdog reap in-flight work, we refuse to admit NEW top-level
    # sessions once the box is at a process/RAM ceiling.
    #   - sdk_max_child_processes: refuse new sessions when the server already has
    #     this many descendant processes (0 = disabled). Tune to the box.
    #   - sdk_admission_rss_mb: refuse new sessions when tree RSS exceeds this many
    #     MB (0 = disabled).
    # These only gate ADMISSION of new sessions; they never terminate live work.
    sdk_max_child_processes: int = 0
    sdk_admission_rss_mb: int = 0

    # Host memory floor for admission. The real crash signature on a shared host
    # is the GLOBAL kernel OOM-killer (not a cgroup limit) reaping our node
    # subprocesses when the WHOLE host runs out of memory — catastrophic with
    # little/no swap. When host MemAvailable drops below this floor we refuse to
    # admit a NEW top-level session (existing sessions and their subagent fan-out
    # keep running), so we stop piling on at the cliff instead of letting the
    # kernel SIGKILL a live session. Measures the whole host, every container —
    # the only signal that reflects the actual OOM trigger. Fails open on any
    # read error. This does NOT cap how many sessions you can run; it only holds
    # back new admissions in the final moments before a host OOM. 0 = disabled.
    # Raise it on zero-swap hosts, where there is no cushion for a spike.
    sdk_admission_min_available_mb: int = 1536

    # OOM victim ordering. A positive oom_score_adj written to each SDK node
    # subprocess makes it (and the subagents it forks, which inherit the value)
    # the kernel's PREFERRED OOM-kill target — so a host memory spike reaps one
    # session's disposable, auto-recoverable subprocess instead of the Python
    # orchestrator (which runs every session) or the database. Raising the score
    # needs no privilege (only lowering does), so it works in an unprivileged
    # container. Range 1..1000; 0 = disabled.
    sdk_subprocess_oom_score_adj: int = 300

    # Boot-time stray-CLI sweep (app.core.startup_reconcile). The session pool
    # tracks its CLI subprocesses in memory only, so a server crash/restart
    # leaks any still-running Claude CLI processes from the previous run. At
    # boot — before this run has spawned anything — the sweep terminates
    # leftover CLI processes it can POSITIVELY attribute to this install: the
    # process must carry AI_SHUFFLE_MANAGED_BY=<this instance id> in its
    # environment (injected into every spawn by _build_env_with_ai_tools) AND
    # look like the Claude CLI, AND not be an ancestor/descendant of this
    # server. Unattributable processes are never touched, so this is safe to
    # leave on; disable via REAP_STRAY_CLI_ON_BOOT=false if you deliberately
    # keep marked processes alive across server restarts.
    reap_stray_cli_on_boot: bool = True

    # Session transcripts live in Postgres (the SessionStore adapter) — Postgres
    # is the single source of truth and the only store that streams the
    # transcript durably in near-real-time. The old SESSION_STORE_MODE flag
    # ("disabled"/"mirror"/"postgres_only") was removed after the cutover:
    # postgres_only is now the one and only behavior.

    # Cross-install agent-memory sync. When enabled, each session mirrors the
    # Claude Code CLI's per-project memory directory (MEMORY.md + per-fact .md
    # files) to/from the shared database: it reconciles from the DB before the
    # CLI starts and pushes local changes back after each turn. Keyed by a
    # project's link_group_id (or project_id when unlinked), so installs that
    # share a git remote share one logical memory. Safe to disable
    # (AGENT_MEMORY_SYNC=false) — it no-ops for project-less sessions and never
    # breaks a turn on error.
    agent_memory_sync: bool = True

    # Cross-provider ProviderAgent workers (multi-provider phase 3, chunk P3-A).
    # When ON, every non-worker session gets an in-process SDK MCP server
    # exposing ONE tool, ``ProviderAgent``, which dispatches a task to a SEPARATE
    # session bound to a DIFFERENT model provider and returns that worker's
    # report. The Claude main stays fully native (no subscription token ever
    # reaches the translator gateway — ruling P1/F1); the two halves are
    # different processes, so the one-base-URL constraint does not apply.
    # ON by default: the safety work this capability needed (turn-lifetime
    # teardown, per-provider circuit breaker, permission/approval routing,
    # rate-limit fail-fast, worker AskUserQuestion exclusion) has all landed and
    # was E2E-verified 2026-07-25 — see the phase-3 plan §9.6 rulings 1–6.
    # Disable with PROVIDER_AGENT_ENABLED=false (BaseSettings reads env vars by
    # field name).
    provider_agent_enabled: bool = True

    # Task tool (agents-program P2c, rulings A8/A9): nesting depth for
    # agent-invokes-agent chains and the live-worker budget per root session
    # tree. Set via TASK_TOOL_MAX_DEPTH / TASK_TOOL_MAX_WORKERS.
    task_tool_max_depth: int = 5
    task_tool_max_workers: int = 8

    # Staged flip for the compiled system prompt on the delegated (claude CLI)
    # leg (agents-program ruling A3). False (default): the CLI keeps its own
    # Claude Code preset and receives the agent's configured prompt + app
    # blocks via --append-system-prompt (byte-identical to pre-compiler
    # behavior). True: the fluid compiler's full output (base + instruction
    # files + skills + env + blocks) replaces the CLI preset via
    # --system-prompt. Native legs always run the compiled prompt; this flag
    # only affects the delegated leg. Set via PROMPT_FULL_REPLACE.
    prompt_full_replace: bool = False

    # Auto-continue an autonomous turn cut at the autocompact boundary. The
    # bundled CLI ends the agentic loop when the NEXT turn would cross the
    # autocompact threshold and DEFERS compaction to the start of the next
    # query (internal ``willRetriggerNextTurn``, never surfaced to the SDK), so
    # from the app's side the turn just ends mid-task with
    # ``stop_reason == "tool_use"`` and the session sits silent until a human
    # sends a message. When ON, the server detects that boundary cut at turn end
    # and spawns ONE continuation query so the CLI compacts and the model
    # resumes. ON by default: this restores broken expected behavior. Set via
    # AUTO_CONTINUE_ON_COMPACT. See app.core.compact_continue.
    auto_continue_on_compact: bool = True

    # Idle-at-edge watchdog (#598). Belt-and-braces backstop for the edge-of-
    # context stall: a periodic sweep finds sessions that sit IDLE (no in-flight
    # turn) with context usage above ``idle_watchdog_context_fraction`` of the
    # window and, via the durable wakeup machinery, fires ONE synthetic nudge turn
    # — a real new turn is what makes the CLI run its deferred lazy compaction.
    # ON by default. Kill switch: IDLE_WATCHDOG_ENABLED=false.
    idle_watchdog_enabled: bool = True
    # Sweep cadence. Also the effective idle grace: a session must still be idle
    # (and over the fraction) at a scan tick to be nudged.
    idle_watchdog_interval_seconds: int = 300
    # Context-window fraction (totalTokens / maxTokens) at/above which an idle
    # session is treated as "at the edge". ~0.85 per the issue.
    idle_watchdog_context_fraction: float = 0.85
    # Per-session cooldown after a watchdog nudge, so a session whose compaction
    # does not drop it back under the fraction is not re-nudged every tick.
    idle_watchdog_cooldown_seconds: int = 1800
    # How recently a session must have been touched (``sessions.updated_at``)
    # to still count as a watchdog candidate. ``status='active'`` is sticky, so
    # without this gate a session abandoned months ago near the context edge
    # stayed eligible forever and drew a fresh nudge turn after every restart.
    # Default 24h. Set to 0 to disable the gate. Env: IDLE_WATCHDOG_MAX_IDLE_SECONDS.
    idle_watchdog_max_idle_seconds: int = 86400

    # Durable background-task state (Phase 1, WRITE-ONLY). When on,
    # task_registry mirrors every task transition into the background_tasks
    # table (v55) via app.core.task_persistence — a coalescing, fire-and-forget
    # async writer that can never slow or break the registry hot path. No
    # reader depends on the rows yet (Phase-1 discipline: derivation before
    # enforcement); boot reconcile marks stale non-terminal rows killed. Kill
    # switch: AI_SHUFFLE_DURABLE_TASKS=false disables all writes instantly.
    durable_task_state: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "AI_SHUFFLE_DURABLE_TASKS", "DURABLE_TASK_STATE", "durable_task_state"
        ),
    )

    # Lazy self-seed of a MISSING base checkout (#607 PR-2). ON by default: when a
    # session is pinned to a runner that has never materialized the project, the
    # checkout_manager clones the project's remote on demand (recovered from legacy
    # data) instead of failing "not on this device". Bounded by the clone-timeout,
    # the interactive-prompt disable, and the local:/manual: hard exclusion. Kill
    # switch: AI_SHUFFLE_SELF_SEED=false disables all on-demand cloning instantly.
    self_seed_checkouts: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "AI_SHUFFLE_SELF_SEED", "SELF_SEED_CHECKOUTS", "self_seed_checkouts"
        ),
    )

    # Per-provider rate-limit circuit breaker. The first upstream HTTP 429 for a
    # provider trips the breaker for that provider INSTALL-WIDE for a time-based
    # cooldown. Motivation: the 2026-07-22 rate-limit storm (3,172 upstream POSTs
    # in ~11 minutes, 1,750 of them 429s, all after the parent turn had ended)
    # came from retry loops INSIDE the CLI processes. 429s are observed by the
    # fluid harness's usage-capture response hook (they were the translator
    # gateway's job until it was retired). The breaker only ever REFUSES — it
    # never reroutes or fails over (ruling P10). ON by default: this is a safety
    # feature. Set PROVIDER_CIRCUIT_BREAKER_ENABLED=false to restore the previous
    # behavior exactly. See app.core.rate_limit_breaker.
    provider_circuit_breaker_enabled: bool = True
    # Cooldown applied when the upstream 429 carries no usable Retry-After.
    provider_circuit_breaker_cooldown_seconds: int = 60
    # Hard ceiling on any cooldown, including one derived from Retry-After, so a
    # hostile or malformed header cannot lock a provider out indefinitely.
    provider_circuit_breaker_max_cooldown_seconds: int = 300

    # V2 projects cutover (Phase 3) is COMPLETE. Project/checkout resolution keys
    # unconditionally off the origin-keyed ``projects_v2`` + per-runner
    # ``checkouts`` tables (seeded by the v60 collapse migration) for any session
    # bound to a ``project_v2_id``; sessions without one (created by an un-upgraded
    # install sharing the DB) still take the legacy resolver ladder. The
    # ``AI_SHUFFLE_V2_PROJECTS`` kill switch was removed in PR-B — rollback is now
    # git-revert, not a flag flip.

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Allow env vars not defined as fields

    def model_post_init(self, __context) -> None:
        """Initialize paths based on deployment mode after model creation"""
        # Set data_dir if not provided
        if self.data_dir is None:
            object.__setattr__(self, 'data_dir', _get_default_data_dir())

        # Set workspace_dir if not provided
        if self.workspace_dir is None:
            object.__setattr__(self, 'workspace_dir', _get_default_workspace_dir())

    @property
    def effective_instance_id(self) -> str:
        """Get the instance ID for multi-instance isolation.

        Priority:
        1. Explicit INSTANCE_ID env var (user override)
        2. Persisted ID file in data_dir (auto-generated, stable across rebuilds)
        3. service_name as final fallback

        We deliberately avoid socket.gethostname() because in Docker the
        hostname is the container ID, which changes on every rebuild —
        orphaning all projects and sessions created by the previous container.
        """
        if self.instance_id and self.instance_id.strip():
            return self.instance_id.strip()
        return _get_or_create_persisted_instance_id(self.effective_data_dir, self.service_name)

    @property
    def effective_data_dir(self) -> Path:
        """Get the effective data directory (always returns a valid Path)"""
        if self.data_dir is not None:
            return self.data_dir
        return _get_default_data_dir()

    @property
    def effective_workspace_dir(self) -> Path:
        """Get the effective workspace directory (always returns a valid Path).
        Checks runtime overrides first, then settings, then defaults."""
        if "workspace_dir" in _runtime_overrides:
            return _runtime_overrides["workspace_dir"]
        if self.workspace_dir is not None:
            return self.workspace_dir
        return _get_default_workspace_dir()

    @property
    def db_path(self) -> Path:
        """Get the old SQLite database path (used for auto-migration detection)."""
        return self.effective_data_dir / "db.sqlite"

    @property
    def get_claude_projects_dir(self) -> Path:
        """Get the Claude SDK projects directory"""
        if self.claude_projects_dir:
            return self.claude_projects_dir
        return Path.home() / ".claude" / "projects"

    @property
    def effective_tools_dir(self) -> Path:
        """Get the effective AI tools directory"""
        if self.tools_dir is not None:
            return self.tools_dir
        # Default to /opt/ai-tools in Docker mode (bundled in image with node_modules)
        if not self.is_local_mode():
            return Path("/opt/ai-tools")
        # In local mode, tools would be relative to install location
        return Path(__file__).parent.parent.parent / "tools"

    @property
    def effective_postgres_host(self) -> str:
        """Postgres host: explicit POSTGRES_HOST, else a per-mode default."""
        if self.postgres_host and self.postgres_host.strip():
            return self.postgres_host.strip()
        # Docker compose ships a "postgres" service; local installs talk to a
        # database the user runs themselves.
        return "localhost" if self.is_local_mode() else "postgres"

    def get_database_url(self) -> str:
        """Assemble the PostgreSQL URL from the POSTGRES_* connection pieces.

        A legacy DATABASE_URL (pre-split installs) takes precedence when set —
        it captures the user's complete previous configuration verbatim, so an
        upgraded install keeps connecting exactly where it always did.

        Credentials are percent-encoded so passwords containing '@', '/', or
        '%' survive the round-trip through app.db.base.parse_database_url.
        """
        if self.database_url and self.database_url.strip():
            return self.database_url.strip()
        if not self.postgres_password:
            raise ValueError(
                "Database connection is not configured. Set POSTGRES_HOST, "
                "POSTGRES_USER, and POSTGRES_PASSWORD in .env (see "
                ".env.example), or re-run setup: python run_local.py --setup. "
                "(A legacy DATABASE_URL connection string is also still "
                "honored.)"
            )
        from urllib.parse import quote
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password, safe="")
        return (
            f"postgresql://{user}:{password}@{self.effective_postgres_host}:"
            f"{self.postgres_port}/{self.postgres_db}"
        )

    def is_local_mode(self) -> bool:
        """Check if running in local mode"""
        if self.local_mode is not None:
            return self.local_mode
        from app.core.platform import is_local_mode as platform_is_local
        return platform_is_local()

    def get_deployment_info(self) -> dict:
        """Get deployment configuration for diagnostics"""
        from app.core.platform import get_platform_info
        return {
            **get_platform_info(),
            "data_dir": str(self.effective_data_dir),
            "workspace_dir": str(self.effective_workspace_dir),
            "db_path": str(self.db_path),
        }


# Global settings instance
settings = Settings()

# Mutable runtime overrides (separate from frozen Pydantic settings)
# These take priority over settings fields when set.
_runtime_overrides: dict[str, Path] = {}

# Ensure directories exist
def ensure_directories():
    """Create required directories if they don't exist"""
    settings.effective_data_dir.mkdir(parents=True, exist_ok=True)
    settings.effective_workspace_dir.mkdir(parents=True, exist_ok=True)


def load_workspace_from_database():
    """
    Load workspace path from database if configured.

    This should be called after database initialization to apply any
    user-configured workspace path from local mode setup.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        from app.db import database
        # Instance-scoped workspace path, with fallback to global for backward compat
        instance_key = f"workspace_path:{settings.effective_instance_id}"
        configured_path = database.get_system_setting(instance_key) or database.get_system_setting("workspace_path")
        logger.info(f"Loading workspace from database: configured_path={configured_path}")

        if configured_path:
            path = Path(configured_path)
            if path.exists() or path.parent.exists():
                _runtime_overrides["workspace_dir"] = path
                logger.info(f"Workspace directory set to: {path}")
            else:
                logger.warning(f"Configured workspace path does not exist: {path}")
        else:
            logger.info(f"No workspace path configured in database, using default: {settings.workspace_dir}")
    except Exception as e:
        logger.error(f"Failed to load workspace from database: {e}")
        # Database might not be initialized yet
        pass
