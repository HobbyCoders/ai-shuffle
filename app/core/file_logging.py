"""Persistent, crash-surviving on-disk logging for AI Shuffle.

Why this exists
---------------
The two existing log sinks are both volatile:

* the in-memory ring buffer behind ``/api/v1/logs`` (``app/api/logs.py``) is
  lost the instant the process dies, and
* stdout goes to ``docker logs`` — outside the container, and gone on container
  recreation.

Only the Postgres ``error_log`` table survives, and it only captures ERROR/
WARNING records routed through ``ErrorLogDBHandler``. After a hard crash there
was no on-disk, human-readable trail to post-mortem from inside the app
environment.

This module adds — purely additively, it never touches the ring buffer or the
DB handler:

1. A ``RotatingFileHandler`` on the ROOT logger so every app/uvicorn/third-party
   record lands in ``<data_dir>/logs/app.log`` (rotated, UTF-8).
2. ``faulthandler`` pointed at ``<data_dir>/logs/fatal.log`` (kept open for the
   process lifetime) so a segfault / fatal signal leaves a native traceback
   that survives the crash.
3. A ``sys.excepthook`` / asyncio loop exception handler so unhandled Python
   exceptions reach the file (and, being logged at ERROR, the ``error_log`` DB).
4. A startup banner + boot-check that rotates aside any fatal trace left by a
   previous run so each crash's trace is preserved exactly one generation.

Layout — the log directory lives under the durable data dir, namespaced PER
INSTANCE so multiple installs sharing one data dir never fight over rotation
(two processes rotating the same app.log — or truncating each other's
fatal.log — would corrupt both, and Windows raises on rename-while-open)::

    <data_dir>/logs/<instance_id>/app.log        (+ .1 .. .N rotations)
    <data_dir>/logs/<instance_id>/fatal.log      (faulthandler target)
    <data_dir>/logs/<instance_id>/fatal.log.prev (previous crash's trace)

``<data_dir>`` is ``/data`` in Docker (the ``ai-shuffle-data`` named volume,
durable across container recreation) and ``%USERPROFILE%\\.ai-shuffle`` on a
native Windows install. All paths use ``pathlib`` (Windows-safe).

Setup runs from the lifespan startup, NOT at import time: importing
``app.main`` must never create directories or truncate a previous crash's
``fatal.log`` — pytest collection imports the module with the REAL settings
singleton, and a test run destroying a genuine production crash trace is
exactly the failure mode this module exists to prevent. The ``TESTING=1`` env
guard in ``setup_file_logging`` is a second line of defense.
"""

from __future__ import annotations

import asyncio
import atexit
import faulthandler
import json
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Format matches the existing console/ring-buffer format so all three sinks read
# identically. asctime is ISO-ish (YYYY-MM-DD HH:MM:SS,ms) via the default
# logging datefmt.
_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Module-level singletons kept alive for the whole process lifetime.
#  - _file_handler: so setup is idempotent (guarded re-import under uvicorn).
#  - _fatal_file: faulthandler writes to this fd for the process lifetime; if it
#    were garbage-collected the fd would close and the native traceback on a
#    fatal signal would go nowhere.
_file_handler: Optional[RotatingFileHandler] = None
_fatal_file = None
_prev_excepthook = None

# Set by the boot-check when a previous run left a non-empty fatal trace. The
# WARNING is emitted later (once the error_log DB handler is installed) so it
# also lands durably in the error_log table — the boot-check runs at the top of
# lifespan startup, before the DB pool exists. (message, size, path).
_pending_fatal_warning: "Optional[tuple[str, int, Path]]" = None


def get_log_dir() -> Path:
    """Return the durable log directory (``<data_dir>/logs/<instance_id>``).

    Namespaced per instance: multiple installs can share one data dir (shared
    volume / shared %USERPROFILE%), and two processes rotating the same
    app.log or truncating each other's fatal.log would corrupt both — on
    Windows, renaming a file another process holds open raises outright.
    ``settings.effective_instance_id`` is stable across restarts (persisted
    ``.instance_id`` file in the data dir), so each install keeps one
    consistent log lineage.

    Deferred settings import keeps this module importable in isolation (tests)
    without eagerly constructing the Settings singleton.
    """
    from app.core.config import settings

    try:
        instance = settings.effective_instance_id
    except Exception:  # noqa: BLE001 — a broken id file must not kill logging
        instance = settings.service_name
    return settings.effective_data_dir / "logs" / instance


def _boot_check_fatal_log(log_dir: Path) -> None:
    """If a previous run left a non-empty ``fatal.log``, record it and rotate aside.

    A non-empty ``fatal.log`` means the previous process died via a fatal signal
    (segfault, abort, OS kill with a native trace) — faulthandler wrote a native
    traceback that the normal shutdown path never got to clear. We rename it to
    ``fatal.log.prev`` (overwriting any older ``.prev``) so each crash's trace is
    preserved exactly one generation before the new run reopens a fresh
    ``fatal.log``, and stash a pending WARNING to be emitted once the error_log
    DB handler is up (so it also persists to ``error_log``) — see
    ``emit_pending_fatal_warning``.
    """
    global _pending_fatal_warning
    fatal_path = log_dir / "fatal.log"
    try:
        if not fatal_path.exists():
            return
        size = fatal_path.stat().st_size
        if size <= 0:
            return
        prev_path = log_dir / "fatal.log.prev"
        try:
            # os.replace is atomic and overwrites an existing destination on
            # both POSIX and Windows (unlike Path.rename on Windows).
            os.replace(fatal_path, prev_path)
        except OSError:
            logger.exception("Failed to rotate previous fatal.log aside")
            prev_path = fatal_path  # report where it still is
        message = (
            "Previous run left a fatal crash trace (%d bytes), preserved as %s. "
            "This indicates the last process died via a fatal signal "
            "(segfault/abort/OOM-kill)." % (size, prev_path)
        )
        _pending_fatal_warning = (message, size, prev_path)
        # Emit to the file/console immediately too; the DB re-emit happens later.
        logger.warning(message)
    except Exception:  # noqa: BLE001 — boot-check must never block startup
        logger.exception("fatal.log boot-check failed (non-fatal)")


def emit_pending_fatal_warning() -> bool:
    """Re-emit the boot-check fatal warning once the error_log DB handler is up.

    ``setup_file_logging`` runs at the top of the lifespan startup, before the
    DB pool and ``ErrorLogDBHandler`` exist, so the boot-check WARNING can't
    reach the ``error_log`` table then. Call this from the lifespan startup
    AFTER ``install_error_log_handler()`` to persist it durably. No-ops if
    there was no previous crash. Returns True if a warning was emitted.
    """
    global _pending_fatal_warning
    pending = _pending_fatal_warning
    if pending is None:
        return False
    _pending_fatal_warning = None
    message, _size, _path = pending
    logger.warning(message)
    return True


def _install_excepthook() -> None:
    """Route unhandled exceptions through logging so they reach the file + DB.

    The app has no existing logging ``sys.excepthook`` (only the per-request
    handler in ``app/main.py``, which is HTTP-scoped). We chain to the previous
    hook so nothing is lost, and skip ``KeyboardInterrupt`` so Ctrl+C stays
    clean.
    """
    global _prev_excepthook
    if _prev_excepthook is not None:
        return
    _prev_excepthook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical(
                "Unhandled exception (sys.excepthook)",
                exc_info=(exc_type, exc_value, exc_tb),
            )
        if _prev_excepthook is not None:
            _prev_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def install_asyncio_exception_handler(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Install a loop exception handler that logs unhandled async exceptions.

    Called from within the running lifespan startup, so the running loop is
    used directly (``get_running_loop``, not the deprecated-in-this-context
    ``get_event_loop``). A failure to install is logged as a WARNING rather
    than silently swallowed — otherwise unhandled async exceptions would
    quietly stop reaching the file/DB. Chains to any pre-existing handler so
    the standard "Task exception was never retrieved" behavior is preserved in
    addition to reaching the file/DB.
    """
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "install_asyncio_exception_handler called with no running event "
            "loop — unhandled async exceptions will NOT reach the log file"
        )
        return

    default_handler = loop.get_exception_handler()

    def _handler(loop_, context):
        msg = context.get("message", "Unhandled exception in asyncio loop")
        exc = context.get("exception")
        # Benign Windows-only artifact: the CPython proactor event loop raises
        # ConnectionResetError (WinError 10054) from
        # ``_ProactorBasePipeTransport._call_connection_lost`` during ordinary
        # transport teardown when the peer resets the connection. It is a
        # documented, harmless quirk (see CPython issue bpo tracking of
        # ``_ProactorBasePipeTransport._call_connection_lost``). Logging it at
        # ERROR — and chaining to the default handler, which re-logs and feeds
        # the error_log DB — is pure noise. Match TIGHTLY so any genuine
        # ConnectionResetError from elsewhere keeps the ERROR path below.
        if isinstance(exc, ConnectionResetError):
            is_winerror_10054 = getattr(exc, "winerror", None) == 10054
            marker = f"{msg} {context.get('source_traceback') or ''}"
            is_proactor_teardown = (
                "_ProactorBasePipeTransport._call_connection_lost" in marker
            )
            if is_winerror_10054 or is_proactor_teardown:
                logger.debug(
                    "Ignoring benign proactor transport-teardown "
                    "ConnectionResetError (WinError 10054): %s",
                    msg,
                    exc_info=exc,
                )
                return
        if exc is not None:
            logger.error(msg, exc_info=exc)
        else:
            logger.error("%s | context=%r", msg, context)
        if default_handler is not None:
            default_handler(loop_, context)
        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def setup_file_logging() -> Optional[Path]:
    """Attach the rotating file handler + faulthandler + excepthook (idempotent).

    Called from the lifespan startup (never at import time — see module
    docstring). Returns the app.log path if enabled, else ``None`` (disabled).
    Safe to call more than once; the second call no-ops. Never raises — a
    logging setup failure must not stop the app from booting.
    """
    global _file_handler, _fatal_file

    from app.core.config import settings

    # Belt-and-braces: never touch the real data dir from a test run. The
    # pytest conftest sets TESTING=1 before any app import; without this guard
    # a collection-time import could truncate a genuine production fatal.log.
    if os.environ.get("TESTING") == "1":
        return None

    if not settings.file_logging_enabled:
        return None

    if _file_handler is not None:
        return Path(_file_handler.baseFilename)

    try:
        log_dir = get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception("Could not create log directory — file logging disabled")
        return None

    # Boot-check BEFORE reopening fatal.log so we can see the previous run's
    # trace and rotate it aside.
    _boot_check_fatal_log(log_dir)

    file_level = getattr(logging, str(settings.file_log_level).upper(), logging.INFO)

    try:
        handler = RotatingFileHandler(
            filename=str(log_dir / "app.log"),
            maxBytes=int(settings.file_log_max_bytes),
            backupCount=int(settings.file_log_backup_count),
            encoding="utf-8",
        )
        handler.setLevel(file_level)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    except Exception:  # noqa: BLE001
        logger.exception("Could not open app.log — file logging disabled")
        return None

    root = logging.getLogger()
    # The root logger's own level may sit above our desired file level (it's set
    # from settings.log_level in app/main.py). Lower it so records the file
    # handler wants are not filtered out at the logger before reaching handlers;
    # the console/ring handlers keep their own higher levels.
    if root.level > file_level:
        root.setLevel(file_level)
    root.addHandler(handler)
    _file_handler = handler

    # Durability on teardown: flush+close the handler at interpreter exit so
    # the tail of the log hits disk even when shutdown is cut short (SIGTERM →
    # uvicorn graceful exit → atexit). The stdlib registers logging.shutdown()
    # itself, but only for handlers existing when logging was imported wraps
    # via weakrefs — registering our own explicit flush is cheap insurance and
    # makes the guarantee local and testable.
    atexit.register(_flush_and_close_file_handler)

    # faulthandler: native traceback on a fatal signal. Keep the fd open for the
    # process lifetime (module-level ref) so it is valid when a crash fires.
    try:
        _fatal_file = open(log_dir / "fatal.log", "w", encoding="utf-8")
        faulthandler.enable(file=_fatal_file, all_threads=True)
    except Exception:  # noqa: BLE001
        logger.exception("Could not enable faulthandler (non-fatal)")

    _install_excepthook()

    logger.info("File logging enabled — app.log at %s (level=%s)",
                log_dir / "app.log", settings.file_log_level.upper())
    return Path(handler.baseFilename)


# The directory that holds the ``app`` package: ``server/`` in a packaged
# desktop worker (``resources/worker/server``), ``/app`` in the container image.
_APP_ROOT = Path(__file__).resolve().parents[2]
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _file_revision(app_root: Path) -> str:
    """The revision stamped next to the code, or ``""`` when there is none.

    Packaged desktop workers stamp metadata one level above ``server/``:
    ``desktop-build.json`` on Windows and ``build-source.json`` on Linux/macOS.
    Prefer the Windows stamp, then the container image's ``GIT_SHA``, then the
    Linux/macOS stamp. A missing, unreadable or malformed file is ignored.
    """
    try:
        data = json.loads((app_root.parent / "desktop-build.json").read_text(encoding="utf-8"))
        value = str(data.get("revision") or "").strip() if isinstance(data, dict) else ""
        if _REVISION_RE.match(value):
            return value
    except (OSError, ValueError):
        pass
    try:
        value = (app_root / "GIT_SHA").read_text(encoding="utf-8").strip()
        if _REVISION_RE.match(value):
            return value
    except (OSError, ValueError):
        pass
    try:
        data = json.loads((app_root.parent / "build-source.json").read_text(encoding="utf-8"))
        value = str(data.get("sourceCommit") or "").strip() if isinstance(data, dict) else ""
        if _REVISION_RE.match(value):
            return value
    except (OSError, ValueError):
        pass
    return ""


def source_revision(app_root: Optional[Path] = None) -> str:
    """The source revision this process runs, for startup log lines.

    The desktop app sets ``SHUFFLE_SOURCE_REVISION`` for a staged or selected
    source build, and container images carry ``GIT_SHA``. The installed
    desktop baseline sets neither, so the fallback reads the revision file
    shipped beside the code (see :func:`_file_revision`). The app version
    alone spans many commits, so without this the logs cannot say which build
    ran. Returns ``"unknown"`` when nothing names a revision.
    """
    for name in ("SHUFFLE_SOURCE_REVISION", "GIT_SHA"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return _file_revision(_APP_ROOT if app_root is None else app_root) or "unknown"


def log_startup_banner() -> None:
    """Log a clear startup banner so restart boundaries are obvious in the file.

    Includes pid / version / source revision / instance_id. The LAST banner
    before a gap or a fatal.log trace pinpoints the run that crashed, and the
    revision names the build (see :func:`source_revision`).
    """
    from app.core.config import settings

    try:
        instance = settings.effective_instance_id
    except Exception:  # noqa: BLE001
        instance = settings.service_name

    logger.info(
        "=== AI Shuffle starting (pid=%s, version=%s, revision=%s, instance_id=%s) ===",
        os.getpid(),
        settings.version,
        source_revision(),
        instance,
    )


def _flush_and_close_file_handler() -> None:
    """Flush and close the rotating file handler (atexit / shutdown safety).

    Idempotent and never raises: Handler.close() tolerates repeat calls, and a
    handler that already lost its stream must not turn process exit into a
    traceback.
    """
    handler = _file_handler
    if handler is None:
        return
    try:
        handler.flush()
        handler.close()
    except Exception:  # noqa: BLE001
        pass


def log_shutdown_banner() -> None:
    """Log a clean shutdown line so a normal stop is distinguishable from a crash.

    Flushes the file handler immediately: shutdown can be cut short (SIGTERM
    grace window), and the whole point of the banner is that its presence or
    absence on disk distinguishes a clean stop from a crash.
    """
    logger.info("=== AI Shuffle shutting down (pid=%s) ===", os.getpid())
    if _file_handler is not None:
        try:
            _file_handler.flush()
        except Exception:  # noqa: BLE001
            pass
