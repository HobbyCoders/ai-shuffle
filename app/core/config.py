"""
Application configuration and settings

Supports both Docker and Local deployment modes:
- Docker: Uses /data, /workspace, /home/appuser paths
- Local: Uses platform-specific paths (AppData, ~/.local/share, etc.)

Set LOCAL_MODE=true environment variable to force local mode.
"""

import logging
import os
import tempfile
import threading
import types
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar, Optional, Union, get_args, get_origin
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings

from app import __version__

logger = logging.getLogger(__name__)

# Name of the env var that points this process at a config file living
# somewhere other than its own working directory.
ENV_FILE_REF_VAR = "AI_SHUFFLE_ENV_FILE"


def _resolve_env_file() -> str:
    """Which ``.env`` this process should read.

    Normally the one in the working directory. A process started inside a git
    worktree has no ``.env`` there — ``.env`` is gitignored, so `git worktree
    add` never brings it across — and the historical workaround was to copy the
    file in, which left a plaintext database password sitting in every worktree
    on disk. Instead the spawner sets ``AI_SHUFFLE_ENV_FILE`` to the absolute
    path of the checkout that does have one, and the worktree process reads
    that single canonical file. Nothing is copied and no secret is duplicated.

    An unreadable or missing path falls back to ``.env`` rather than failing
    the process: pydantic treats a nonexistent env_file as "no file", which is
    the same position a plain worktree run was in before.
    """
    ref = os.environ.get(ENV_FILE_REF_VAR, "").strip()
    if ref and os.path.isfile(ref):
        return ref
    return ".env"

# Import platform detection (lazy to avoid circular imports)
def _get_default_data_dir() -> Path:
    """Get default data directory based on deployment mode"""
    from app.core.platform import get_app_data_dir
    return get_app_data_dir()

def _get_default_workspace_dir() -> Path:
    """Get default workspace directory based on deployment mode"""
    from app.core.platform import get_default_workspace_dir
    return get_default_workspace_dir()


def _parse_legacy_database_url(db_url: str) -> dict:
    """Decompose a PostgreSQL URL into its connection pieces.

    Pure compatibility helper for explicit maintenance/test URLs. It never
    reads the environment and is not a runtime connection selector.
    app.db.base.parse_database_url delegates here.

    urlparse splits userinfo at the LAST ``@`` and credentials are unquoted,
    so both percent-encoded passwords and literal-``@`` passwords work.
    Tolerates URLs missing the ``postgresql://`` scheme prefix.
    """
    from urllib.parse import urlparse, unquote

    if not db_url.startswith(("postgresql://", "postgres://")):
        db_url = f"postgresql://{db_url}"
    parsed = urlparse(db_url)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/"),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


_cached_persisted_instance_id: Optional[str] = None
_persisted_instance_id_lock = threading.Lock()


@contextmanager
def _lock_instance_id_file(path: Path):
    """Serialize initial publication across processes, including on Windows.

    Keep this sidecar: unlinking a lock file can let waiters lock different
    inodes. The identity itself is replaced atomically, never used as the lock.
    """
    with path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt
            # Byte-range locks can extend past EOF. A marker write before
            # locking races another process that already owns that byte.
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _get_or_create_persisted_instance_id(data_dir: Path, service_name: str) -> str:
    """Return one process-stable ID, persisted to data_dir/.instance_id.

    A sidecar file lock and atomic rename make first use safe across processes.
    Broken storage must not manufacture a new process-local identity: doing so
    loses the existing account binding and can register a duplicate on restart.
    """
    global _cached_persisted_instance_id
    with _persisted_instance_id_lock:
        if _cached_persisted_instance_id is not None:
            return _cached_persisted_instance_id

        id_file = data_dir / ".instance_id"
        try:
            stored = id_file.read_text().strip()
            if not stored:
                raise RuntimeError(f"Saved worker instance ID is empty: {id_file}. Restore the data volume before starting.")
            _cached_persisted_instance_id = stored
            return stored  # Reading an established ID needs no writable lock.
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f"Cannot read saved worker instance ID: {id_file}. Restore access to the data volume.") from exc

        import uuid
        new_id = f"{service_name}-{uuid.uuid4().hex[:8]}"
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            with _lock_instance_id_file(data_dir / ".instance_id.lock"):
                # Another process may have published while we waited. Never
                # replace an existing empty/corrupt/unreadable identity.
                try:
                    stored = id_file.read_text().strip()
                except FileNotFoundError:
                    stored = None
                if stored is not None:
                    if not stored:
                        raise RuntimeError(f"Saved worker instance ID is empty: {id_file}. Restore the data volume.")
                    new_id = stored
                else:
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="w", encoding="utf-8", dir=data_dir,
                            prefix=".instance_id-", delete=False,
                        ) as stream:
                            temporary = Path(stream.name)
                            stream.write(new_id + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, id_file)
                    finally:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"Cannot persist worker instance ID in {data_dir}. "
                "Restore access to the data volume; a temporary identity would break account registration."
            ) from exc

        _cached_persisted_instance_id = new_id
        return new_id


def _is_text(annotation) -> bool:
    """True for ``str`` and ``Optional[str]``: settings where an empty value is a value."""
    if get_origin(annotation) in (Union, types.UnionType):
        return all(arg is str for arg in get_args(annotation) if arg is not type(None))
    return annotation is str


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

    # START_CHROME=0 tells the container entrypoint not to launch the supervised
    # Chromium. The app reads the same flag so it neither advertises the browser
    # tool suite to agents nor starts a native supervisor with no browser behind it.
    start_chrome: bool = True

    # Service info
    service_name: str = "ai-shuffle"
    # The release tag is the version source. Release builds pass it as the
    # APP_VERSION build arg (docker-publish.yml); app.__version__ is only the
    # fallback for source runs.
    version: str = Field(
        default=__version__,
        validation_alias=AliasChoices("APP_VERSION", "VERSION", "version"),
    )

    # Deployment mode (auto-detected if not set)
    local_mode: Optional[bool] = None  # None = auto-detect, True = local, False = docker

    # Paths - these will be set dynamically if not provided
    data_dir: Optional[Path] = None
    workspace_dir: Optional[Path] = None
    claude_projects_dir: Optional[Path] = None  # Legacy Claude CLI transcript root (defaults to ~/.claude/projects)
    # Native-leg project-memory root. A native (fluid
    # NativeLoop) session keeps its per-project memory under
    # ``<fluid_projects_dir>/<slug>/memory/``, where <slug> is the SAME
    # path-based normalization the Claude CLI uses for ~/.claude/projects — so
    # two projects with the same NAME never collide. Defaults to ~/.fluid/projects
    # deliberately: that is fluid's own home, mirroring ~/.claude/projects for the
    # delegated leg. (Unlike the skills store, which is pinned under the app's
    # data dir precisely to stay OUT of ~/.fluid.) Env: FLUID_PROJECTS_DIR.
    fluid_projects_dir: Optional[str] = None
    tools_dir: Optional[Path] = None  # Path to AI tools directory (defaults to /workspace/ai-shuffle/tools in Docker)

    # Database endpoints come only from the private managed supervisor descriptor.
    db_pool_min: ClassVar[int] = 2  # Internal managed defaults, not environment knobs
    db_pool_max: ClassVar[int] = 32  # Healthy burst connections stay open for reuse
    # Several thread pools share this pool: the asyncio default executor,
    # AnyIO sync dependencies, and Git workers. Agents borrow connections for
    # short operations rather than holding one for their entire turn.
    # Budget db_pool_max plus one dedicated listener for EVERY app process
    # across all installs against the server's actual max_connections, minus
    # reserved slots and headroom for other clients. Agent count alone is not
    # a reason to raise this limit.

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
    session_expire_days: int = 30

    # fluid_session_events retention. The engine persists every bus event, so
    # streaming deltas (ToolArgsDelta / TextDelta / ThinkingDelta) dominate the
    # table by volume — tens of thousands of rows for a single busy session.
    # They are reconstructible from the completed ToolUse rows and the node
    # tree, so the maintenance pass drops them once a session has been idle
    # this many days. Everything else in the log is evidence and is kept, Usage
    # in particular (it backs the accounting backfill). 0 disables pruning.
    fluid_event_retention_days: int = 7

    # Security - Trusted Proxies (for getting real IP behind reverse proxy)
    trusted_proxy_headers: str = "X-Forwarded-For,X-Real-IP"  # Comma-separated
    # Reverse proxies (comma-separated IPs or CIDRs) whose proxy headers are
    # believed, in addition to loopback. Empty: only a proxy on this host is.
    trusted_proxies: str = ""

    # Security - CORS (comma-separated origins, or "*" for development only)
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    # Android apps from before /mobile/ (app/mobile_ui.py) ship their own build
    # of the mobile UI and load it from this origin (WebViewAssetLoader). They
    # talk to a worker only through the HTTP/WebSocket APIs, authenticated with
    # a Bearer paired-session token, so this origin stays allowed for CORS
    # regardless of `cors_origins` while such builds are installed.
    mobile_app_origin: str = "https://appassets.androidplatform.net"
    # iOS builds from before /mobile/ did the same from their own URL scheme
    # (a WKURLSchemeHandler), under the same rules.
    mobile_ios_app_origin: str = "aishuffle://app"

    # Security - Cookie settings
    cookie_secure: bool = True  # Set to False only for local HTTP development
    # Interactive API docs (/docs, /redoc, /openapi.json) are unauthenticated,
    # so they stay off unless a developer sets API_DOCS_ENABLED=true.
    api_docs_enabled: bool = False

    # Local transport access and optional account service share normal Settings
    # precedence: process environment overrides .env; neither is exported to os.environ.
    shuffle_local_access: bool = True
    shuffle_local_hostnames: str = ""
    account_browser_origin: str = "https://hobbycoders.com"
    mobile_account_hub_url: str = "https://hobbycoders.com"
    # A paired device's hub verdict is reused for this long before the hub is
    # asked again, and honored for the grace window when the hub is down or
    # rate limiting. Both bound hub traffic; the hub allows 120 requests a
    # minute per worker. Hub-side revocation takes effect within the TTL.
    mobile_account_hub_verify_ttl_seconds: float = 90.0
    mobile_account_hub_grace_seconds: float = 1800.0

    # Optional paid Cloud leases (app/core/license.py), never local run gates.
    # With no server URL, LICENSE_BETA=true, or no verification keys, skip
    # acquisition/renewal. Linked free accounts still have one remote worker.
    # A release build sets LICENSE_SERVER_URL=https://hobbycoders.com.
    license_server_url: str = ""
    license_beta: bool = False

    # Security - Request limits
    max_request_body_mb: int = 50  # Maximum request body size in MB

    # Security - WebSocket rate limiting (per connection)
    ws_rate_limit_messages: int = 30  # Max messages per sliding window
    ws_rate_limit_window_seconds: float = 10.0  # Sliding window duration in seconds

    # Optional one-time adoption of legacy encrypted credentials. Once private
    # server key custody is active, restarts and human access do not use this.
    admin_password: Optional[str] = None

    # Notes disk-sync worker. Notes are materialized to per-install local .md
    # files; this periodic worker reconciles those files with the shared database
    # in both directions so an edit made anywhere (an agent, the user over
    # SSH/SMB, or another install) converges everywhere. See app.core.notes_sync.
    # Interval is a poll cadence (watchdog-style fs events are unreliable on the
    # network/fuse filesystems these installs commonly run on). 0 disables it.
    #
    # A pass is METADATA-FIRST — it reads (id, title, file_path, updated_at) for
    # every note and pulls the multi-KB ``content`` column only for the rows
    # whose file mtime or DB ``updated_at`` actually moved — so the steady-state
    # cost of a tick is one small indexed read plus one ``stat`` per note.
    # Nothing in this loop is latency-critical (the UI's own GET reconcile covers
    # the note you are looking at), so 60s is the default.
    notes_sync_interval_seconds: int = 60

    # Codex / ChatGPT-subscription OAuth token-refresh worker. Polls every
    # enabled Codex provider on this cadence and refreshes any whose access token
    # is due (proactive 8-day interval, or JWT exp within 5 min — see
    # app.core.codex_token_refresh). The Codex refresh token ROTATES, so refreshes
    # are serialized per provider by a lock; this is just the wake-up cadence. 0
    # disables the periodic worker (the refresh-on-401 hook still works on demand).
    codex_token_refresh_interval_seconds: int = 3600

    # ─── Cross-install background sweep ─────────────────────────────────────
    # turn-claim, git-ops-claim and interaction-expiry are cross-INSTALL
    # correctness loops multiplexed onto one timer (see app.core.turn_claim).
    # On a deployment with no peer installs they read tables only this process
    # ever writes, so their tight cadence is pure overhead; the sweep stretches
    # every pass to this interval when the install registry shows no peers, and
    # snaps back to the tight per-pass cadences the moment a peer row appears.
    # NOTIFY (turn submit/interrupt) remains the latency path either way.
    # Set to 0 to disable the stretch and always run the tight cadences.
    single_install_sweep_interval_seconds: int = 60

    # How long a "do we have peer installs?" answer is cached before the sweep
    # re-reads the registry. Bounds the extra read to ~0.2 queries/min, and is
    # also invalidated immediately whenever a pass actually finds work.
    single_install_peer_check_seconds: int = 300

    # ─── Orphaned-session reconciler cadence ────────────────────────────────
    # The reconciler heals sessions whose ``sdk_session_id`` a mid-turn crash
    # left NULL. It is a RECOVERY sweep, not a hot path: the unbounded backfill
    # already runs once at startup, and new orphans are prevented at the source
    # by the persist-on-init write. Each pass re-aggregates the session store for
    # every never-used session, so it runs hourly rather than on the 5-minute
    # monitor tick. 0 disables the periodic pass (startup still runs one).
    orphan_reconcile_interval_seconds: int = 3600

    # SuperGrok / xAI OAuth token-refresh worker. Same cadence semantics as
    # ``codex_token_refresh_interval_seconds`` (0 disables the periodic worker;
    # the refresh-on-401 hook still works on demand). Defaults to the Codex
    # interval so both sweeps wake together.
    xai_token_refresh_interval_seconds: int = 3600

    # fluid code-mode + continual harness (native legs only — the delegated
    # claude-CLI leg has code_mode capped off and is untouched). Code-mode moves
    # the tool surface into a persistent Python kernel; the continual harness
    # journals every executed cell and distills recurring workflows into saved
    # skills in the background. Both default ON; env overrides are the usual
    # pydantic ones (FLUID_CODE_MODE=false, FLUID_CONTINUAL=false, ...).
    #   - fluid_continual_model: pins skill synthesis to one model so skill
    #     quality doesn't drift with the session's provider choice. None = the
    #     session's own model.
    #   - fluid_skills_dir: where saved skills + the episodic journal live.
    #     None = <data_dir>/fluid/skills (never fluid's ~/.fluid default).
    fluid_code_mode: bool = True
    # AI Shuffle's Claude Code mod on the delegated Claude leg (Claude Code
    # 2.1.287 and newer; fluid/fluid/engine/claude_mod). It keeps the CLI's
    # bash-first steer from reaching the model. FLUID_CLAUDE_MOD=false turns it
    # off; the CLAUDE_CODE_THRIFTY_SONIC override stays on either way.
    fluid_claude_mod: bool = True
    fluid_continual: bool = True
    fluid_memory_enabled: bool = True
    fluid_continual_model: Optional[str] = None
    fluid_skills_dir: Optional[str] = None
    # OS-level sandbox (bubblewrap) for the code-mode kernel child. ``auto``
    # uses it when bwrap is installed and its one-off self-test passes (else
    # one warning, then the plain child); ``required`` refuses to start the
    # kernel without it (every cell gets the reason as its error); ``off``
    # never uses it. See fluid/fluid/codemode/os_sandbox.py. Env:
    # CODE_MODE_OS_SANDBOX (fluid's KernelConfig reads the same variable).
    code_mode_os_sandbox: str = "auto"
    # On Linux the app marks itself non-dumpable at startup
    # (prctl(PR_SET_DUMPABLE, 0)), so the model-run children that share its
    # uid (kernel, hooks, CLI, MCP servers) cannot read /proc/<app>/environ,
    # maps or mem. True skips that, keeping core dumps and same-uid debugging.
    # Env: ALLOW_PROCESS_INTROSPECTION.
    allow_process_introspection: bool = False
    # Bounds passed to fluid when the vendored build has the seams
    # (see harness_adapter._registry_bound_kwargs / _loop_bound_kwargs).
    #   - fluid_external_call_timeout_seconds: cap on ONE MCP/inproc tool call.
    #     Above browser_batch's worst case (20 × 15s waits) and Monitor's 120s
    #     inline wait. Env: FLUID_EXTERNAL_CALL_TIMEOUT_SECONDS.
    #   - fluid_turn_watchdog_seconds: a turn silent this long is ended by the
    #     engine with an error instead of dying silently. Env:
    #     FLUID_TURN_WATCHDOG_SECONDS.
    fluid_external_call_timeout_seconds: int = 330
    fluid_turn_watchdog_seconds: int = 900
    # HTTP bounds on every native provider client the app builds (seconds;
    # see harness_adapter._provider_timeout_kwargs; defaults match fluid's).
    # Longest silence between two reads of a model response.
    provider_read_timeout: float = 600.0
    # Wall clock for one whole model call; 0 disables it.
    provider_stream_deadline: float = 900.0
    # Wait for a call's first byte (every native client: Anthropic, OpenRouter/OpenAI-format chat, Responses); 0 disables it. Retried only
    # when even the response headers never came; after a 200 the call is not re-sent.
    provider_first_byte_timeout: float = 60.0
    # The first-byte bound for a row whose base_url is a local server (loopback,
    # private or link-local address, localhost, *.local, host.docker.internal:
    # Ollama, vLLM, LM Studio). Such a server may hold its headers while it loads
    # a model, so the default 0 disables it, as fluid's LocalClient does.
    # Env: PROVIDER_FIRST_BYTE_TIMEOUT_LOCAL.
    provider_first_byte_timeout_local: float = 0.0
    # Responses lane (OpenAI/Codex): how long an accepted call may send only
    # heartbeats before any reasoning or output; past it the request is
    # re-sent on the client's retry budget and the chat shows a retry card
    # (#1992). 0 disables it. Env: PROVIDER_PRE_CONTENT_HEARTBEAT_BUDGET.
    provider_pre_content_heartbeat_budget: float = 90.0

    # Size limits
    max_tool_output_chars: int = 500_000  # ~500KB, well under 1MB WS buffer
    max_display_output_chars: int = 2_000  # What we show in the UI

    # ─── SDK Session Pool sizing + TIERED idle policy ───────────────────────
    #
    # Every pooled delegated session owns ONE long-lived Node CLI subprocess at
    # 100-300+MB RSS, so the pool's steady-state size IS the app's idle RAM
    # floor. AI Shuffle targets weak machines, so the defaults below trade a
    # little respawn latency on rarely-touched sessions for a much lower idle
    # floor. All of it is configurable; see app.core.session_pool.IdlePolicy,
    # which is the single place the policy is expressed.

    # How many IDLE subprocess sessions stay warm between queries. 4 covers the
    # realistic idle working set (the chat you are in, the one you just left,
    # and a running automation or two). It is NOT a concurrency limit: busy
    # sessions are never evicted, so any number can run at once and the pool
    # grows past this while they do. Only real resource pressure (the memory /
    # process guards below) refuses new work. Raise it on a big box via
    # SDK_SESSION_POOL_SIZE to keep more idle sessions warm.
    #
    # SUBPROCESS ONLY. It is sized against the
    # 100-300MB node CLI a DELEGATED session owns. A session on a native leg
    # (openai_responses / anthropic / openrouter) is coroutines inside this
    # process and is not counted here at all. What governs native sessions and native
    # subagent workers instead: the real resource guards
    # (sdk_admission_min_available_mb, sdk_max_child_processes,
    # sdk_admission_rss_mb), the per-parent fan-out limit
    # subagent_max_concurrent_per_session, and sdk_native_session_soft_cap.
    sdk_session_pool_size: int = 4

    # Safety valve for the NATIVE lane: the most in-process sessions the pool
    # will admit. Deliberately generous — a native session costs Python objects,
    # not a process, so this exists to stop a runaway fan-out from growing
    # without any bound at all, not to shape normal use. The RSS/host-memory
    # guards above are the ceiling that actually reflects cost; this one just
    # fails earlier and more legibly. 0 disables it.
    # Override via SDK_NATIVE_SESSION_SOFT_CAP.
    sdk_native_session_soft_cap: int = 64

    # HOT tier size: how many most-recently-used sessions keep the long idle
    # timeout below. These are the ones a user realistically comes back to
    # within the hour, and keeping them hot is what preserves the "no cold
    # respawn between messages" feel. Everything past this rank is COLD and
    # goes on the short timeout. 0 disables the hot tier entirely (everything
    # cold). Override via SDK_SESSION_HOT_POOL_SIZE.
    sdk_session_hot_pool_size: int = 2

    # HOT-tier idle timeout: how long one of the ``sdk_session_hot_pool_size``
    # most-recently-used pooled subprocesses may sit idle before eviction.
    # Every eviction turns the next message into a cold respawn (slower first
    # token), so keep this comfortably above a
    # normal read-and-reply gap. Override via
    # SDK_SESSION_IDLE_TIMEOUT_SECONDS.
    #
    # It is SHORTER than ``user_question_timeout_seconds`` (1h) below, and that
    # is only safe because ``SessionPool._is_busy`` exempts a session blocked on
    # a pending interaction from idle eviction. Deadlines a user can see must be
    # the ones that fire — this timeout is not one of them.
    sdk_session_idle_timeout_seconds: int = 1800  # 30 minutes

    # COLD-tier idle timeout: everything ranked below the hot tier. These are
    # sessions you have already moved on from; holding 200MB for half an hour
    # on the chance you scroll back is the wrong trade on a small box. 7
    # minutes is long enough to survive a detour into another chat and back.
    # Override via SDK_SESSION_COLD_IDLE_TIMEOUT_SECONDS.
    sdk_session_cold_idle_timeout_seconds: int = 420  # 7 minutes

    # DETACHED grace: once the last device (browser/app) watching a session
    # disconnects, its subprocess is evicted after this grace period REGARDLESS
    # of tier — a session nobody is watching cannot receive a message without a
    # reconnect first, and the reconnect itself is slower than the respawn we
    # would be paying for. The grace absorbs page reloads and brief network
    # drops. Set to 0 to disable disconnect-driven eviction and rely purely on
    # the idle tiers. Override via SDK_SESSION_DETACHED_GRACE_SECONDS.
    sdk_session_detached_grace_seconds: int = 180  # 3 minutes

    # How often the pool's idle watchdog re-evaluates deadlines while sessions
    # are pooled. The watchdog normally sleeps exactly until the next known
    # eviction deadline (so RAM is released ON TIME instead of on the resource
    # monitor's 5-minute tick); this cap bounds how late it can notice a state
    # change it was not told about — most importantly a device disconnect on an
    # install where the sync engine is not wired to notify the pool. It is a
    # pure in-process timer: no DB, no thread, and NO wake at all when the pool
    # is empty. Override via SDK_SESSION_IDLE_REEVALUATE_SECONDS.
    sdk_session_idle_reevaluate_seconds: int = 60

    # AskUserQuestion backstop (seconds). A parked question keeps its SDK
    # subprocess alive AND pinned in the pool: while awaiting an answer the
    # session counts as "streaming" AND ``SessionPool._is_busy`` holds it via
    # the ``interaction_wait`` waiter registry, so idle/LRU eviction skips it.
    # The backstop has to be long enough that a question survives you stepping
    # away across many sessions, but short enough that a truly-abandoned session
    # reaps its hot subprocess instead of holding ~100MB for hours. 1h is the
    # default; raise it toward Claude-Desktop-style persistence if you have the
    # headroom, or lower it on a tight box. Override via
    # USER_QUESTION_TIMEOUT_SECONDS.
    #
    # THIS is the deadline the durable ``pending_interactions`` row advertises
    # and the ONLY one that may end a question. It deliberately exceeds
    # ``sdk_session_idle_timeout_seconds`` (30 min) above; that is safe only
    # because of the pool exemption named there. Without it the pool reclaimed
    # the subprocess at 1800s under a question whose modal said 3600s, fluid
    # answered the tool with "input_required round-trip interrupted", and the
    # user's device kept showing a live question nobody was listening to.
    # Whoever changes either number must keep that exemption honest.
    user_question_timeout_seconds: int = 3600  # 1 hour

    # Per-turn progress watchdog (seconds). A turn that produces NO event at all
    # — no tool result, no usage, no text — for this long is terminalized with a
    # provider-error-shaped failure and a durable system message. It exists for
    # a host tool call that never returns, such as a browser automation call
    # whose own cell timeout cannot interrupt the blocked host call.
    #
    # Deliberately generous — a legitimately slow model call, a long compaction
    # or a big file write must never trip it, and a turn blocked on a user
    # question or permission prompt is exempt outright (the wait is bounded by
    # ``user_question_timeout_seconds`` instead). 0 disables the watchdog.
    # Override via TURN_PROGRESS_TIMEOUT_SECONDS.
    turn_progress_timeout_seconds: int = 1800  # 30 minutes of total silence

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

    # CPU priority of the shell commands agents run through Bash (fluid's
    # tool) and Monitor (this app's background shells). Without it a test
    # suite or build competes with the server's own event loop at equal
    # priority, and the UI and other sessions lag while it runs. On POSIX each
    # command's process group gets the server's nice value plus this many
    # steps (capped at 19), inherited by everything it forks; on Windows any
    # positive value means BELOW_NORMAL_PRIORITY_CLASS. The children still get
    # all idle CPU; this orders the scheduler, it does not cap usage. Range
    # 0..19; 0 = disabled; an invalid value falls back to 10. Copied to
    # FLUID_CHILD_NICE at startup (app.core.startup.propagate_child_priority),
    # which is the variable fluid's Bash reads. Env: CHILD_PROCESS_NICE.
    child_process_nice: int = 10

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
    # transcript durably in near-real-time. There is no store-mode setting.

    # Cross-install agent-memory sync. When enabled, each session mirrors the
    # Claude Code CLI's per-project memory directory (MEMORY.md + per-fact .md
    # files) to/from the shared database: it reconciles from the DB before the
    # CLI starts and pushes local changes back after each turn. Keyed by a
    # project's link_group_id (or project_id when unlinked), so installs that
    # share a git remote share one logical memory. Safe to disable
    # (AGENT_MEMORY_SYNC=false) — it no-ops for project-less sessions and never
    # breaks a turn on error.
    agent_memory_sync: bool = True

    # Worker-session restrictions for Task-dispatched workers: when ON,
    # query_options removes AskUserQuestion/AskUser from worker sessions (their
    # tool approvals are routed to the parent, but a question would stall until
    # user_question_timeout_seconds). Disable with PROVIDER_AGENT_ENABLED=false
    # (BaseSettings reads env vars by field name).
    provider_agent_enabled: bool = True

    # Task tool: nesting depth for
    # agent-invokes-agent chains and the live-worker budget per root session
    # tree. Set via TASK_TOOL_MAX_DEPTH / TASK_TOOL_MAX_WORKERS.
    task_tool_max_depth: int = 5
    task_tool_max_workers: int = 64

    # The most permissive mode a subagent another worker of this account starts
    # here may run in (default, acceptEdits, auto or bypassPermissions). It is
    # a ceiling only: the child also stays at or below the mode its parent
    # asked for and its own agent's mode. Set via REMOTE_TASK_MAX_PERMISSION_MODE.
    remote_task_max_permission_mode: str = "bypassPermissions"

    # Per-parent concurrent subagent workers, enforced in the dispatch admission
    # gate (app/core/task_dispatch.py). This is the NATIVE lane's backpressure:
    # native workers are coroutines, so the subprocess pool cap does not bound
    # them and something has to. Unlike task_tool_max_workers — a per-root-tree
    # budget checked against workers that have already REGISTERED — this one
    # also counts dispatches admitted but not yet registered, which is the race
    # a simultaneous fan-out actually loses, and it waits briefly for a worker
    # to finish before refusing with a retryable capacity error. 0 disables it.
    # Override via SUBAGENT_MAX_CONCURRENT_PER_SESSION.
    subagent_max_concurrent_per_session: int = 64

    # ── Task worker lifecycle ────────────────────────────────────────────
    # A worker has no wall-clock ceiling by design — a subagent may legitimately
    # run for hours — but a worker that has never streamed a SINGLE frame has
    # not started, and the harness knows it (``frames_forwarded == 0`` is what
    # produces the "worker failed to start" summary). These settings bound only
    # that pre-first-frame state; a working worker stays unbounded.
    #
    # How long an admitted worker may go without streaming its first frame
    # before it is stopped as "forwarded no output". 0 disables the watchdog.
    #
    # Provider progress the parent card never shows also counts: reasoning and
    # tool-argument deltas, a finished call's usage, and turn.provider_alive in
    # the worker's own log. The last is fluid's ProviderAlive: the
    # provider accepted the request and its stream is open. That covers a
    # reasoning model whose reasoning is encrypted-only, which streams nothing
    # else until the reasoning item ends. So this deadline catches a
    # request that was never accepted (queued, refused, or a connection that
    # never answered), not a slow model. It does not catch a stream that was
    # accepted and then stalled; fluid's per-call stream deadline and turn
    # watchdog bound that. A watchdog stop is never re-dispatched
    # automatically, because the provider may already have been working when
    # the interrupt landed.
    # Override via TASK_WORKER_FIRST_FRAME_SECONDS.
    task_worker_first_frame_seconds: float = 90.0
    # Hard wall-clock cap on a SYNCHRONOUS Task worker, measured across every
    # attempt. A sync dispatch blocks the parent turn — it cannot park, cannot
    # be steered and cannot be told the worker went quiet — so unlike the
    # background lane it gets a real ceiling.
    # 0 disables the cap. Override via TASK_WORKER_SYNC_MAX_SECONDS.
    task_worker_sync_max_seconds: float = 900.0
    # How many times a BACKGROUND dispatch whose worker never streamed a frame
    # is re-dispatched automatically with the same brief. Each retry is its own
    # visible task row. Sync dispatches never take this path — their caller is
    # live and gets the failure to act on. Override via
    # TASK_WORKER_START_RETRIES.
    task_worker_start_retries: int = 1
    # How many times a worker refused at connect by a provider rate limit (a
    # 429 carrying a parseable "try again in Ns" / Retry-After) is RESCHEDULED
    # rather than terminated: fluid's own 3-retry / ~7s budget cannot outlast a
    # 60s token window. Applies to both modes.
    # Override via TASK_WORKER_RATE_LIMIT_RETRIES.
    task_worker_rate_limit_retries: int = 3
    # Ceiling on one rate-limit reschedule's backoff. The provider's own
    # retry-after is the floor; it is doubled per attempt and clamped here.
    # Override via TASK_WORKER_RATE_LIMIT_BACKOFF_CAP_SECONDS.
    task_worker_rate_limit_backoff_cap_seconds: float = 60.0
    # Optional administrator limit on concurrent workers per provider row.
    # Worker count is not a provider token quota: ten small requests can fit
    # where one large request does not. Default to the native/session resource
    # guards and actual provider rate-limit feedback instead of an artificial
    # three-worker ceiling. An explicit positive limit still queues dispatches
    # until a sibling settles. 0 disables this limit. The provider-less
    # delegated Claude leg remains governed by its process/resource guards.
    # Override via TASK_WORKER_MAX_PER_PROVIDER.
    task_worker_max_per_provider: int = 0
    # How long a dispatch queues on an explicitly configured provider-row
    # limit before giving up with a retryable capacity error. Longer than the
    # pool admission wait, but bounded: the queue happens inside the model's
    # Task tool call, so this
    # is also the worst case for how long that call can take to hand back a
    # receipt. Raising it trades a slower tool call for fewer refusals.
    # Override via TASK_WORKER_PROVIDER_QUEUE_SECONDS.
    task_worker_provider_queue_seconds: float = 120.0
    # How long a worker's OWN background work (Monitor shells, nested workers)
    # is given to drain after its turn ends, before its report is written. A
    # worker has no wake seam, so without the wait an interim sentence ("tests
    # are running") would become its report and registry summary. Work still
    # outstanding when this expires fails the worker instead of booking an
    # interim note as a result. 0 disables the WAIT, not the check: a worker
    # that ends with live work then fails immediately.
    # Override via TASK_WORKER_BACKGROUND_SETTLE_SECONDS.
    task_worker_background_settle_seconds: float = 120.0

    # Compiled system prompt for a PLAIN-STRING profile prompt on the
    # delegated (claude CLI) leg. A preset profile is not affected: it always
    # keeps Claude Code's own preset prompt and gets the
    # app's text through --append-system-prompt. For a plain-string profile,
    # True compiles the prompt (base + instruction files + skills + env +
    # blocks) and False sends it uncompiled plus the agents index; either way
    # it replaces the CLI preset via --system-prompt, because the profile
    # chose its own prompt. Native legs always run the compiled prompt. Set
    # via PROMPT_FULL_REPLACE.
    prompt_full_replace: bool = True

    # Auto-continue an autonomous turn cut at the autocompact boundary. The
    # Claude Code CLI ends the agentic loop when the NEXT turn would cross the
    # autocompact threshold and DEFERS compaction to the start of the next
    # query without surfacing it, so from the app's side the turn just ends mid-task with
    # ``stop_reason == "tool_use"`` and the session sits silent until a human
    # sends a message. When ON, the server detects that boundary cut at turn end
    # and spawns ONE continuation query so the CLI compacts and the model
    # resumes. ON by default. Set via
    # AUTO_CONTINUE_ON_COMPACT. See app.core.compact_continue.
    auto_continue_on_compact: bool = True

    # Idle-at-edge watchdog. Belt-and-braces backstop for the edge-of-
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
    # session is treated as "at the edge".
    idle_watchdog_context_fraction: float = 0.85
    # Per-session cooldown after a watchdog nudge, so a session whose compaction
    # does not drop it back under the fraction is not re-nudged every tick.
    idle_watchdog_cooldown_seconds: int = 1800
    # How recently a session must have been touched (``sessions.updated_at``)
    # to still count as a watchdog candidate. ``status='active'`` is sticky, so
    # without this gate a session abandoned months ago near the context edge
    # would stay eligible forever and draw a fresh nudge turn after every restart.
    # Default 24h. Set to 0 to disable the gate. Env: IDLE_WATCHDOG_MAX_IDLE_SECONDS.
    idle_watchdog_max_idle_seconds: int = 86400

    # ── Monitor guardrails (background shells) ────────────────────────────
    #
    # Default time budget for a Monitor command. Monitor's completion fires on
    # exit and only on exit, so a command that never exits parks the session
    # until the session ends. This is the backstop: when the deadline passes the
    # process is stopped and the completion fires with a "timed out after Ns"
    # summary, so "parked forever" becomes "woken with a timeout".
    #
    # NOT applied when the model set ``serves`` (the process is meant to stay
    # up) or when the command already carries its own bound (``timeout``,
    # ``--max-time``, deadline arithmetic, an iteration cap) — the model's own
    # budget wins. Set to 0 to disable the backstop entirely.
    # Env: MONITOR_COMMAND_TIMEOUT_SECONDS.
    monitor_command_timeout_seconds: int = 2700  # 45 minutes
    # Head+tail cap on the completion summary the parent is WOKEN with. The
    # full buffer stays available via TaskOutput; this only bounds what a wake
    # turn has to read (a stack-trace tail can otherwise burn a whole turn).
    # 4,000 is large enough that the summary is a result, not a pointer that
    # sends the agent to TaskOutput. Env: MONITOR_SUMMARY_MAX_CHARS.
    monitor_summary_max_chars: int = 4000
    # Idle bound for a SERVING Monitor task (``serves``). Those are exempt from
    # the command budget by design — they are meant to stay up — so without this
    # a server outlives a session whose wake died. The bound is on the
    # SESSION, not the process: while the session that started it keeps being
    # touched, the server is never disturbed. Past this many seconds with no
    # activity on that session, the process is stopped and the completion says
    # why. 0 disables. Env: MONITOR_SERVING_IDLE_SECONDS.
    monitor_serving_idle_seconds: int = 3600  # 60 minutes

    # ── Park watchdog ─────────────────────────────────────────────────────
    #
    # A session parked on background work wakes when that work settles. If the
    # settle never arrives (a watcher that cannot exit, a lost terminal frame)
    # the park is silent and unbounded — the failure mode is indistinguishable
    # from a healthy long wait. The periodic sweep (resource_monitor's 5-minute
    # tick) reports a park with no wake for ``warn`` seconds, and past the
    # ``force`` ceiling settles the session's still-running task rows and fires
    # their terminal frames so the session wakes instead of sitting forever.
    # Kill switch: PARK_WATCHDOG_ENABLED=false.
    park_watchdog_enabled: bool = True
    # Park age at which a stalled park is REPORTED (log + durable error row).
    # Env: PARK_WATCHDOG_WARN_SECONDS.
    park_watchdog_warn_seconds: int = 1800  # 30 minutes
    # Hard ceiling: past this the sweep forces the terminal frames. Generous —
    # a legitimate overnight CI run or training job must never be reaped by a
    # timer. 0 disables forcing (report only).
    # Env: PARK_WATCHDOG_FORCE_SECONDS.
    park_watchdog_force_seconds: int = 3600  # 60 minutes

    # STOP SEMANTICS opt-out. Stop kills EVERYTHING the session is doing:
    # sub-agents and their descendants, provider-lane workers, and Monitor
    # shells including serving tasks and CI watchers. That is what the button
    # says; sparing a Monitor shell is one of the ways a stopped session comes
    # back to life. Set STOP_KEEPS_MONITOR_SHELLS=true to leave running Monitor
    # shells alive on Stop; only ghost rows are then swept.
    # Defaults OFF; the default must stay "kill everything".
    stop_keeps_monitor_shells: bool = False

    # Durable background-task state. When on, task_registry mirrors every task
    # transition into the background_tasks table via app.core.task_persistence
    # — a coalescing, fire-and-forget async writer that can never slow or break
    # the registry hot path. The rows back shared sidebar task presence, boot
    # reconcile (stale non-terminal rows are marked killed) and the stop-task
    # force-settle fallback. Kill switch: AI_SHUFFLE_DURABLE_TASKS=false
    # disables all writes instantly.
    durable_task_state: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "AI_SHUFFLE_DURABLE_TASKS", "DURABLE_TASK_STATE", "durable_task_state"
        ),
    )

    # Lazy self-seed of a MISSING base checkout. ON by default: when a
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

    # Whether WARNING records join ERROR+ in the durable ``error_log`` table
    # (app.core.error_log_handler). ON by default: ERROR-only capture hides
    # failures that only log a WARNING (e.g. a provider 403) from
    # ``GetAppErrors``. Warnings never change that tool's DEFAULT output — it asks for
    # ERROR+ unless the caller passes min_level: "WARNING" — and they are
    # trimmed to their own smaller ceiling before the table-wide prune, so they
    # cannot evict errors. Set false to restore ERROR-only capture.
    error_log_capture_warnings: bool = True
    # Ceiling on retained WARNING rows, applied before the table-wide trim.
    error_log_max_warning_rows: int = 1000

    # Whether the browser MCP server may reach loopback (127.0.0.0/8, ::1,
    # "localhost"). browser-mcp blocks loopback by default, so a hostile page
    # cannot steer the agent's browser into a service on 127.0.0.1.
    #
    # None (default) = auto, which ALLOWS loopback in every runtime (Docker,
    # native and desktop): AI Shuffle has one owner and the agent already has a
    # shell on the box, and blocking loopback means an agent cannot view the dev
    # server it just started. The deployment-mode probe is not a tenancy signal
    # and does not gate this. The worker's own ports and the DevTools port stay
    # denied either way.
    #
    # BROWSER_SSRF_ALLOW_LOOPBACK=false turns it off for an install that is
    # fronted for other people. Setting BROWSER_SSRF_ALLOW explicitly is
    # stronger still: that value is passed through verbatim and nothing is
    # appended to it — see app.core.mcp_config._browser_ssrf_allow.
    browser_ssrf_allow_loopback: Optional[bool] = Field(
        default=None,
        validation_alias=AliasChoices(
            "BROWSER_SSRF_ALLOW_LOOPBACK", "browser_ssrf_allow_loopback"
        ),
    )

    # Per-provider rate-limit circuit breaker. The first upstream HTTP 429 for a
    # provider trips the breaker for that provider INSTALL-WIDE for a time-based
    # cooldown. 429s are recorded only by harness_adapter._usage_capture_http_client,
    # which is attached only to the openai_responses leg with a bound provider
    # row (anthropic/openrouter native legs and the delegated CLI are not
    # observed). The breaker never gates requests: its only reader is
    # provider_agent._await_worker_idle, which fails a ProviderAgent worker fast
    # while its provider is tripped. It never reroutes or fails over. ON by
    # default. Set PROVIDER_CIRCUIT_BREAKER_ENABLED=false to disable it.
    # See app.core.rate_limit_breaker.
    provider_circuit_breaker_enabled: bool = True
    # Cooldown applied when the upstream 429 carries no usable Retry-After.
    provider_circuit_breaker_cooldown_seconds: int = 60
    # Hard ceiling on any cooldown, including one derived from Retry-After, so a
    # hostile or malformed header cannot lock a provider out indefinitely.
    provider_circuit_breaker_max_cooldown_seconds: int = 300

    # Project/checkout resolution keys unconditionally off the origin-keyed
    # ``projects_v2`` + per-runner ``checkouts`` tables for any session bound to
    # a ``project_v2_id``; sessions without one (created by an un-upgraded
    # install sharing the DB) take the legacy resolver ladder. There is no
    # setting for it.

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value, info):
        """A blank value for a non-text setting means "use the default".

        Unraid passes every template field to the container, filled in or not,
        so ``BROWSER_SSRF_ALLOW_LOOPBACK=`` must not stop start-up. Text
        settings keep a blank value as given; anything non-blank still parses
        strictly."""
        field = cls.model_fields[info.field_name]
        if isinstance(value, str) and not value.strip() and not _is_text(field.annotation):
            return field.get_default(call_default_factory=True)
        return value

    @field_validator("child_process_nice", mode="before")
    @classmethod
    def _child_process_nice_or_default(cls, value):
        """Out-of-range or unparsable values fall back to the default
        instead of stopping start-up."""
        default = cls.model_fields["child_process_nice"].default
        try:
            nice = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return nice if 0 <= nice <= 19 else default

    @field_validator("version", mode="before")
    @classmethod
    def _normalise_version(cls, value):
        """Accept a raw tag ("v0.8.18") and treat an empty build arg as unset."""
        text = str(value or "").strip()
        if text[:1] in ("v", "V") and text[1:2].isdigit():
            text = text[1:]
        return text or cls.model_fields["version"].default

    class Config:
        env_file = _resolve_env_file()
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
    def get_fluid_projects_dir(self) -> Path:
        """Native-leg project-memory root — ~/.fluid/projects."""
        if self.fluid_projects_dir:
            return Path(self.fluid_projects_dir)
        return Path.home() / ".fluid" / "projects"

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

    def get_database_connection(self) -> dict:
        """Read the supervisor-owned endpoint; never consult database environment variables.

        Reading is lazy so source utilities can import settings without starting
        PostgreSQL. A worker must fail before connecting if it was not supervised.
        """
        import json
        import re
        import stat

        path = self.effective_data_dir / "managed-postgres.json"
        message = (
            "Managed PostgreSQL is not ready: a valid private managed-postgres.json "
            "is required in DATA_DIR. Start AI Shuffle with the desktop launcher "
            "or container supervisor; direct unsupervised backend startup is unsupported."
        )
        try:
            for component in (path, *path.absolute().parents):
                if component.is_symlink():
                    raise ValueError()
            from app.core.private_storage import owner_uid, service_files

            with service_files():
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                owner = owner_uid() if os.name != "nt" else None
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
                    raise ValueError()
                if os.name != "nt" and (info.st_uid != owner or info.st_mode & 0o077):
                    raise ValueError()
                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError()
                        result[key] = value
                    return result
                record = json.loads(stream.read(16385), object_pairs_hook=unique_object)
            if not isinstance(record, dict) or set(record) != {"version", "host", "port", "user", "password", "database"}:
                raise ValueError()
            if type(record["version"]) is not int or record["version"] != 1 or record["host"] != "127.0.0.1":
                raise ValueError()
            if type(record["port"]) is not int or not 1 <= record["port"] <= 65535:
                raise ValueError()
            for key in ("user", "database"):
                if not isinstance(record[key], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", record[key]):
                    raise ValueError()
            password = record["password"]
            if not isinstance(password, str) or not password or len(password) > 4096 or any(ord(c) < 32 for c in password):
                raise ValueError()
            # hostaddr defeats inherited PGHOSTADDR; explicit options prevent
            # PGOPTIONS from changing the managed connection's search path.
            return {"host": "127.0.0.1", "hostaddr": "127.0.0.1", "port": record["port"],
                    "user": record["user"], "password": password, "dbname": record["database"],
                    "connect_timeout": 5, "options": "", "sslmode": "disable"}
        except (OSError, ValueError, TypeError, UnicodeError):
            raise RuntimeError(message) from None

    def managed_database_available(self) -> bool:
        """Availability probe for tools that can operate without a database."""
        try:
            self.get_database_connection()
            return True
        except RuntimeError:
            return False

    def get_database_url(self) -> str:
        """Descriptor-derived URL for maintenance consumers; never log this value."""
        from urllib.parse import quote
        connection = self.get_database_connection()
        user = quote(connection["user"], safe="")
        password = quote(connection["password"], safe="")
        database = quote(connection["dbname"], safe="")
        return f"postgresql://{user}:{password}@127.0.0.1:{connection['port']}/{database}"

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


# Absolute path of the config file THIS process read, captured at import time
# while the working directory is still the one the app was started in. Handed
# to subprocesses (see app.core.environment) as AI_SHUFFLE_ENV_FILE so a process
# running in a worktree resolves the same file instead of needing a copy.
# None when there is no file at all — a Docker/compose install configured purely
# from the process environment, which needs no reference.
def _absolute_env_file() -> Optional[str]:
    resolved = _resolve_env_file()
    try:
        path = Path(resolved).resolve()
    except OSError:  # pragma: no cover - defensive
        return None
    return str(path) if path.is_file() else None


ENV_FILE_PATH: Optional[str] = _absolute_env_file()

# Global settings instance
settings = Settings()

# Mutable runtime overrides (separate from frozen Pydantic settings)
# These take priority over settings fields when set.
_runtime_overrides: dict[str, Path] = {}

def set_workspace_override(path: Path) -> None:
    """Make ``path`` the effective workspace, as start-up does for a saved one.

    ``settings.effective_workspace_dir`` reads this override before the raw
    ``workspace_dir`` setting, so a workspace chosen at runtime has to be set
    here too, or it only takes effect after a restart.
    """
    _runtime_overrides["workspace_dir"] = path


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
                set_workspace_override(path)
                logger.info(f"Workspace directory set to: {path}")
            else:
                logger.warning(f"Configured workspace path does not exist: {path}")
        else:
            logger.info(f"No workspace path configured in database, using default: {settings.workspace_dir}")
    except Exception as e:
        logger.error(f"Failed to load workspace from database: {e}")
        # Database might not be initialized yet
        pass
