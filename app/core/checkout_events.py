"""Short-lived, debounced file watches for checkouts visible as active in the UI.

Use watchfiles (already supplied by uvicorn[standard]), not a git/filesystem scan
of every historical checkout. Each request renews only the paths it needs;
independent path leases stop watches when no client renews them. No app lifecycle
hook or permanent background executor is needed.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from watchfiles import Change, DefaultFilter, watch

logger = logging.getLogger(__name__)
_LEASE_SECONDS = 30.0
_DEBOUNCE_MS = 500
# Do not descend into nested worktrees, Git internals, or dependency caches.
_FILTER = DefaultFilter(ignore_dirs=(*DefaultFilter.ignore_dirs, ".fluid", "venv"))


def _directories(root: str) -> list[str]:
    """Watch directories non-recursively so ignored trees cost no native watches."""
    paths = []
    for parent, dirs, _ in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs
                   if name not in _FILTER.ignore_dirs
                   and not os.path.islink(os.path.join(parent, name))]
        paths.append(parent)
    return paths


@dataclass
class _CheckoutWatch:
    expires: float = 0.0
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class CheckoutEvents:
    def __init__(self, invalidate: Callable[[str], None]):
        self._invalidate = invalidate
        self._lock = threading.Lock()
        self._watches: dict[str, _CheckoutWatch] = {}

    def update(self, paths: set[str]) -> None:
        """Renew independent path leases without cancelling another client's set.

        HTTP polls have no reliable client identity. An omitted path may still be
        focused elsewhere, so only expiry (not another request) releases it.
        One watch is shared by all requests for a path, bounded to paths renewed
        within the lease window rather than all checkouts ever seen.
        """
        paths = {os.path.abspath(path) for path in paths}
        now = time.monotonic()
        with self._lock:
            for path, entry in list(self._watches.items()):
                if path not in paths and entry.expires <= now:
                    entry.stop.set()
                    del self._watches[path]
            for path in paths:
                entry = self._watches.get(path)
                if entry is None or entry.stop.is_set():
                    entry = self._watches[path] = _CheckoutWatch(expires=now + _LEASE_SECONDS)
                    entry.thread = threading.Thread(
                        target=self._run, args=(path, entry),
                        name="checkout-events", daemon=True,
                    )
                    entry.thread.start()
                else:
                    entry.expires = now + _LEASE_SECONDS

    def _live(self, entry: _CheckoutWatch) -> bool:
        with self._lock:
            if entry.expires <= time.monotonic():
                # Prevent a renewal from reviving a thread already exiting.
                entry.stop.set()
            return not entry.stop.is_set()

    def _run(self, path: str, entry: _CheckoutWatch) -> None:
        try:
            while self._live(entry):
                directories = _directories(path)
                if not directories:
                    return
                # Ignored directories must be pruned BEFORE registering: a filter
                # alone hides their events but still recursively watches them.
                for changes in watch(
                    *directories, recursive=False, watch_filter=_FILTER,
                    debounce=_DEBOUNCE_MS, step=_DEBOUNCE_MS,
                    rust_timeout=1000, yield_on_timeout=True, stop_event=entry.stop,
                    force_polling=False, poll_delay_ms=5000, debug=False,
                    ignore_permission_denied=True,
                ):
                    if not self._live(entry):
                        return
                    if changes:
                        self._invalidate(path)
                    if any((change == Change.added and os.path.isdir(name))
                           or (change == Change.deleted and name in directories)
                           for change, name in changes):
                        # Include new directories (and stop watching removed ones).
                        # Invalidation above also covers edits before registration.
                        break
        except Exception:  # a missing/unwatchable checkout is advisory
            logger.debug("Checkout file watch stopped for %s", path, exc_info=True)
        finally:
            with self._lock:
                if self._watches.get(path) is entry:
                    del self._watches[path]

    def close(self) -> None:
        """Release watches, including native handles (also used by isolated tests)."""
        with self._lock:
            entries = list(self._watches.values())
            self._watches.clear()
            for entry in entries:
                entry.stop.set()
        for entry in entries:
            if entry.thread is not None:
                entry.thread.join(timeout=2)


def _invalidate(path: str) -> None:
    from app.core.git_service import git_service
    git_service.invalidate_status_cache(path)


checkout_events = CheckoutEvents(_invalidate)
