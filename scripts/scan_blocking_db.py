#!/usr/bin/env python3
"""Find synchronous DB calls that run directly on the asyncio event loop.

Every module under ``app.db`` (except ``app.db.async_database``) is a
synchronous psycopg2 facade. A call into one of those from *directly* inside an
``async def`` blocks the event loop for the duration of the query. While it
blocks, every other request, WebSocket frame and SSE stream on that worker is
stalled — which is why a handful of these present as "the whole app goes slow
at once".

The scanner is alias-agnostic: instead of matching a hardcoded list of module
names it reads each file's imports and learns which local names are bound to
``app.db`` modules or to functions pulled out of them. So all of these are
caught, regardless of what the importer chose to call them::

    from app.db import database
    from app.db import database as db
    from app.db import notifications as notifications_db
    from app.db.error_logs import record_error
    import app.db.turns as turns_db

Calls nested inside a sync closure (the body handed to ``run_sync``) are NOT
reported: the innermost enclosing function wins, and that one is sync. Calls
whose source line carries an offload hint (``await``, ``run_sync``,
``to_thread``, ``run_in_executor``) are likewise skipped.

Known blind spot (one hop):
    The scanner only sees calls written *literally* inside an ``async def``.
    A plain ``def`` helper that itself calls the DB and is invoked from an
    ``async def`` blocks the loop just the same, but is invisible here — the
    call site looks like an ordinary local function call. Resolving that needs
    a whole-program call graph, which this script deliberately does not build.
    ``app/core/rate_limiter.py``'s ``get_limit_config`` was one such case. If
    you add a sync DB helper, offload it at its async call sites by hand.

Usage:
    python scripts/scan_blocking_db.py                # whole app/, summary
    python scripts/scan_blocking_db.py app/api/x.py   # specific paths, detailed
    python scripts/scan_blocking_db.py --json         # machine readable
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import pathlib
import sys

# The package whose modules are all synchronous DB facades.
DB_PACKAGE = "app.db"

# ...except this one, which is the async offload wrapper itself.
ASYNC_DB_MODULE = "app.db.async_database"

# Names exported from app.db that are not blocking DB calls: pure helpers,
# in-process stat readers and local-only identity lookups. Kept deliberately
# small — anything not listed is assumed to touch the database.
ALLOWLIST_NAMES = frozenset(
    {
        "ph",  # app.db.compat — returns the SQL placeholder string, no I/O
        "phs",
        "pool_stats",
        "pressure_stats",
        "public_device_id",
        "local_device_id",
        "row_to_dict",
        "rows_to_list",
        # Pool lifecycle, called once at startup/shutdown before or after
        # the loop serves traffic; not a query.
        "init_pool",
        "close_pool",
    }
)

# Prefix-matched allowlist entries (pure functions, no I/O).
ALLOWLIST_PREFIXES = ("hash_",)

# If any of these appear on the call's source line, it is already offloaded
# (or awaited via an async wrapper) and is not a finding.
OFFLOAD_HINTS = ("await", "run_sync", "to_thread", "run_in_executor")


def _is_allowlisted(name: str) -> bool:
    """True for known non-DB names: helpers, stats, classes and constants.

    Anything starting with an uppercase letter is a class, exception or
    module-level constant (``WorktreeStatus``, ``SessionRow``,
    ``WebSessionReservationConflict``, ``DEFAULT_LIMITS``) — constructing one
    does not hit the database.
    """
    if not name:
        return True
    if name[0].isupper() or name[0] == "_":
        return True
    if name in ALLOWLIST_NAMES:
        return True
    return name.startswith(ALLOWLIST_PREFIXES)


def _dotted(node: ast.AST) -> str | None:
    """Render an attribute/name chain as a dotted string, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def collect_db_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (module_aliases, bare_funcs) bound to synchronous app.db code.

    ``module_aliases`` are local names standing for an ``app.db`` module, so
    ``alias.anything()`` is a DB call. ``bare_funcs`` are local names bound
    directly to a function pulled out of an ``app.db`` module.
    """
    module_aliases: set[str] = set()
    bare_funcs: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # Relative imports (level > 0) can't be resolved without knowing
            # the importing package; app/ uses absolute imports throughout.
            if node.level:
                continue
            if mod == DB_PACKAGE:
                # from app.db import <module> [as alias]
                for alias in node.names:
                    if f"{DB_PACKAGE}.{alias.name}" == ASYNC_DB_MODULE:
                        continue
                    local = alias.asname or alias.name
                    if _is_allowlisted(alias.name):
                        continue
                    module_aliases.add(local)
            elif mod.startswith(DB_PACKAGE + "."):
                # from app.db.<module> import fn [as g]
                if mod == ASYNC_DB_MODULE:
                    continue
                for alias in node.names:
                    if _is_allowlisted(alias.name):
                        continue
                    bare_funcs.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if not name.startswith(DB_PACKAGE + "."):
                    continue
                if name == ASYNC_DB_MODULE:
                    continue
                if alias.asname:
                    # import app.db.turns as turns_db
                    module_aliases.add(alias.asname)
                # Plain `import app.db.turns` binds `app`; those call sites are
                # matched by their full dotted path instead (see _call_label).

    return module_aliases, bare_funcs


def _call_label(
    node: ast.Call, module_aliases: set[str], bare_funcs: set[str]
) -> str | None:
    """Label for a call into synchronous app.db code, or None if it isn't one."""
    func = node.func

    if isinstance(func, ast.Name):
        return f"{func.id}()" if func.id in bare_funcs else None

    if not isinstance(func, ast.Attribute):
        return None

    attr = func.attr
    if _is_allowlisted(attr):
        return None

    base = func.value
    if isinstance(base, ast.Name) and base.id in module_aliases:
        return f"{base.id}.{attr}"

    # Fully qualified: app.db.<module>.fn()
    dotted = _dotted(base)
    if (
        dotted
        and dotted.startswith(DB_PACKAGE + ".")
        and dotted != ASYNC_DB_MODULE
    ):
        return f"{dotted}.{attr}"

    return None


def _enclosing(funcs, line):
    """Innermost function containing `line`, as (start, end, name, is_async)."""
    best = None
    for start, end, name, is_async in funcs:
        if start <= line <= end and (best is None or start > best[0]):
            best = (start, end, name, is_async)
    return best


def scan_source(src: str, path: str = "<string>") -> list[dict]:
    """Scan Python source text; returns a list of finding dicts."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    module_aliases, bare_funcs = collect_db_names(tree)
    if not module_aliases and not bare_funcs:
        return []

    lines = src.splitlines()
    # A lambda is a sync scope like any other `def`: `run_sync(lambda: db.f())`
    # is offloaded, and the innermost-scope rule must see that.
    funcs = [
        (
            n.lineno,
            n.end_lineno,
            getattr(n, "name", "<lambda>"),
            isinstance(n, ast.AsyncFunctionDef),
        )
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    ]

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = _call_label(node, module_aliases, bare_funcs)
        if label is None:
            continue
        enc = _enclosing(funcs, node.lineno)
        if not (enc and enc[3]):  # only calls directly inside an async def
            continue
        line = lines[node.lineno - 1].strip()
        if any(h in line for h in OFFLOAD_HINTS):
            continue
        out.append(
            {
                "file": str(path),
                "line": node.lineno,
                "call": label,
                "handler": enc[2],
            }
        )
    return out


def scan_file(path: pathlib.Path) -> list[dict]:
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_source(src, str(path))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.paths:
        targets: list[pathlib.Path] = []
        for p in args.paths:
            path = pathlib.Path(p)
            targets.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
        detailed = True
    else:
        targets = sorted(pathlib.Path("app").rglob("*.py"))
        detailed = False

    findings = [f for t in targets for f in scan_file(t)]

    if args.json:
        json.dump(findings, sys.stdout, indent=2)
        print()
        return 1 if findings else 0

    if not findings:
        print("No blocking sync DB calls inside async def. Clean.")
        return 0

    per_file = collections.Counter(f["file"] for f in findings)
    print(f"TOTAL blocking sync DB calls inside async def: {len(findings)}\n")
    for path, count in per_file.most_common():
        print(f"{count:>4}  {path}")
        if detailed:
            for f in sorted(
                (f for f in findings if f["file"] == path), key=lambda x: x["line"]
            ):
                print(f"        L{f['line']:<6} {f['call']:<40} in {f['handler']}")
    return 1


if __name__ == "__main__":
    # Piping into `head` closes stdout early; exit quietly rather than dumping a
    # BrokenPipeError traceback over the results.
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        try:
            sys.stdout.close()
        finally:
            raise SystemExit(0)
