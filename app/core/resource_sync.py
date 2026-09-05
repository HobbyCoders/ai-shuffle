"""Explicit post-commit invalidation boundaries shared by REST, tools and workers.

Only registered mutations are observed, never arbitrary HTTP requests or token
writes. Row counts detect missing rows/conflict no-ops. Transactions count only
once get_db has successfully committed. Payloads contain resource names only.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import logging
import re

_active = ContextVar("resource_mutation", default=None)
logger = logging.getLogger(__name__)


class _Cursor:
    def __init__(self, cursor, transaction):
        self._cursor, self._transaction = cursor, transaction

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, query, *args, **kwargs):
        self._writing = bool(re.match(r"\s*(INSERT|UPDATE|DELETE)\b", str(query), re.I))
        result = self._cursor.execute(query, *args, **kwargs)
        self._observe()
        return result

    def _observe(self):
        if getattr(self, "_writing", False) and self._cursor.rowcount > 0:
            self._transaction[0] = True

    def executemany(self, query, *args, **kwargs):
        self._writing = bool(re.match(r"\s*(INSERT|UPDATE|DELETE)\b", str(query), re.I))
        result = self._cursor.executemany(query, *args, **kwargs)
        self._observe()
        return result

    def fetchone(self):
        result = self._cursor.fetchone()
        self._observe()
        if self._writing and result is not None:
            self._transaction[0] = True
        return result

    def fetchall(self):
        result = self._cursor.fetchall()
        self._observe()
        if self._writing and result:
            self._transaction[0] = True
        return result

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self._cursor.__exit__(*args)


class _Connection:
    def __init__(self, connection, transaction):
        self._connection, self._transaction = connection, transaction

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def cursor(self, *args, **kwargs):
        return _Cursor(self._connection.cursor(*args, **kwargs), self._transaction)


def observe_transactions(get_db):
    @wraps(get_db)
    @contextmanager
    def observed(*args, **kwargs):
        frame = _active.get()
        transaction = [False]
        with get_db(*args, **kwargs) as conn:
            yield _Connection(conn, transaction) if frame is not None else conn
        # Unreachable on body failure OR commit failure.
        if frame is not None and transaction[0]:
            frame["committed"] = True
    return observed


def _emit(resource, scope, arguments, before, result):
    from app.core.global_events import global_events
    if scope == "session":
        row = before or (result if isinstance(result, dict) else {})
        owner = row.get("api_user_id", arguments.get("api_user_id"))
        global_events.resources_changed(resource)
        if owner is not None:
            global_events.resources_changed(resource, user_type="api_user", user_id=owner)
    elif scope == "notification":
        owner = arguments.get("user_id")
        kind = getattr(owner, "user_type", None) or ("admin" if owner in (None, "admin") else "api_user")
        global_events.resources_changed(resource, user_type=kind,
                                        user_id=owner if kind == "api_user" else None)
    else:
        global_events.resources_changed(resource)
        if scope == "shared":
            # Catalogs have require_auth read endpoints (some filter rows).
            # Authorized snapshots, not events, remain the authority.
            global_events.resources_changed(resource, user_type="api_user")


def mutation(resource, *, scope="admin", database=True):
    def decorate(function):
        signature = inspect.signature(function)

        def prepare(args, kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            before = None
            if scope == "session" and arguments.get("session_id"):
                from app.db.sessions import get_session
                before = get_session(arguments["session_id"])
            return arguments, before

        def finish(frame, arguments, before, result):
            success = frame["committed"] if database else bool(result) and not frame["failed"]
            if not database and isinstance(result, dict):
                success = success and result.get("success", True) and not result.get("error")
                # Marketplace batch sync returns {marketplace_id: bool}.
                if result and all(isinstance(value, bool) for value in result.values()):
                    success = success and any(result.values())
            if success:
                try:
                    _emit(resource, scope, arguments, before, result)
                except Exception:
                    logger.warning("Resource invalidation failed", exc_info=True)

        def complete(parent, frame, arguments, before, result):
            if parent is not None:
                parent["committed"] |= frame["committed"]
                parent["failed"] |= frame["failed"]
            else:
                finish(frame, arguments, before, result)
            return result

        @wraps(function)
        def wrapped(*args, **kwargs):
            arguments, before = prepare(args, kwargs)
            parent = _active.get()
            frame = {"committed": False, "failed": False}
            token = _active.set(frame)
            try:
                result = function(*args, **kwargs)
            finally:
                _active.reset(token)
            return complete(parent, frame, arguments, before, result)

        @wraps(function)
        async def async_wrapped(*args, **kwargs):
            arguments, before = prepare(args, kwargs)
            parent = _active.get()
            frame = {"committed": False, "failed": False}
            token = _active.set(frame)
            try:
                result = await function(*args, **kwargs)
            finally:
                _active.reset(token)
            return complete(parent, frame, arguments, before, result)

        wrapper = async_wrapped if inspect.iscoroutinefunction(function) else wrapped
        wrapper.resource_sync = (resource, scope)
        return wrapper
    return decorate


def install_database_mutations(namespace, resource, names, *, scope="admin"):
    """Install an explicit inventory at the implementation, before facade exports."""
    if getattr(namespace[names.split()[0]], "resource_sync", None):
        return
    namespace["get_db"] = observe_transactions(namespace["get_db"])
    for name in names.split():
        namespace[name] = mutation(resource, scope=scope)(namespace[name])


def install_service_mutations(cls, resource, names):
    # MCP and plugin catalogs are readable by all authenticated users.
    write = cls._write_json_file

    @wraps(write)
    def checked_write(*args, **kwargs):
        result = write(*args, **kwargs)
        frame = _active.get()
        if frame is not None and not result:
            frame["failed"] = True
        return result

    cls._write_json_file = checked_write
    for name in names.split():
        setattr(cls, name, mutation(resource, scope="shared", database=False)(getattr(cls, name)))
