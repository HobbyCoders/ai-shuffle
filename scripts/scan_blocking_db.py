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
        "research_limits",  # app.db.owner_state — returns the fixed limits, no I/O
        "research_allowed",  # app.db.owner_state — reads a state dict, no I/O
        "decode_pull_requests",  # app.db.sessions — decodes a JSON value, no I/O
        # Pool lifecycle, called once at startup/shutdown before or after
        # the loop serves traffic; not a query.
        "init_pool",
        "close_pool",
    }
)

# Exact pure exports, not name-wide exceptions: base.parse_jsonb only decodes
# JSON/pass-through values and performs no I/O. Other DB modules/aliases with
# the same function name must still be scanned as possible database calls.
PURE_DB_CALLS = frozenset({"app.db.base.parse_jsonb"})

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


def collect_db_names(tree: ast.AST) -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Return DB candidates and the calls with unchanged canonical pure bindings.

    Import collection is deliberately scope-agnostic: competing imports or any
    binding/shadowing write invalidate purity, even in another lexical scope.
    Keep pure imports as DB candidates so uncertainty cannot erase a finding.
    """
    module_aliases: dict[str, set[str]] = {}
    bare_funcs: set[str] = set()
    bindings: dict[str, set[str]] = collections.defaultdict(set)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                bindings[alias.asname or alias.name].add("." * node.level + f"{mod}.{alias.name}")
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
                    module_aliases.setdefault(local, set()).add(f"{DB_PACKAGE}.{alias.name}")
            elif mod and DB_PACKAGE.startswith(mod + "."):
                # A package alias can reach the same exports: from app import db.
                for alias in node.names:
                    canonical = f"{mod}.{alias.name}"
                    if canonical == DB_PACKAGE or DB_PACKAGE.startswith(canonical + "."):
                        module_aliases.setdefault(alias.asname or alias.name, set()).add(canonical)
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
                local = alias.asname or name.split(".")[0]
                bindings[local].add(name if alias.asname else local)
                if not (name == DB_PACKAGE or name.startswith(DB_PACKAGE + ".")
                        or DB_PACKAGE.startswith(name + ".")):
                    continue
                if name == ASYNC_DB_MODULE:
                    continue
                if alias.asname:
                    # import app.db.turns as turns_db
                    module_aliases.setdefault(alias.asname, set()).add(name)
                # Plain `import app.db.turns` binds `app`; those call sites are
                # matched by their full dotted path instead (see _call_label).

    pure: dict[str, str] = {}
    for canonical in PURE_DB_CALLS:
        for local in bare_funcs:
            if bindings[local] == {canonical}:
                pure[local] = canonical
        for local, modules in module_aliases.items():
            if len(modules) == 1 and bindings[local] == modules:
                origin = next(iter(modules))
                if canonical.startswith(origin + "."):
                    pure[local + canonical[len(origin):]] = canonical
        root = canonical.split(".")[0]
        if bindings[root] == {root}:
            pure[canonical] = canonical

    writes, exports = _binding_writes(tree, bindings)
    unchanged = {call for call, canonical in pure.items()
                 if "*" not in bindings
                 and not any(call == name or call.startswith(name + ".") for name in writes)
                 and not any(canonical == name or canonical.startswith(name + ".") for name in exports)}
    return module_aliases, bare_funcs, unchanged


def _canonical_paths(node: ast.AST | None, bindings) -> set[str]:
    """Normalize known import/package aliases, retaining every ambiguous candidate."""
    parts = []
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        if isinstance(node, ast.Attribute):
            parts.append(node.attr)
        node = node.value  # a mapping view may expose any of its namespace
    if not isinstance(node, ast.Name):
        return set()
    suffix = "".join("." + part for part in reversed(parts))
    dotted = node.id + suffix
    paths = {origin + suffix for origin in bindings.get(node.id, ())}
    if dotted == DB_PACKAGE or dotted.startswith(DB_PACKAGE + "."):
        paths.add(dotted)
    return paths


def _namespace_exports(node: ast.AST, bindings) -> set[str]:
    """Pure exports exposed by this direct namespace/callable/mapping reference."""
    exports: set[str] = set()
    for path in _canonical_paths(node, bindings):
        for canonical in PURE_DB_CALLS:
            module = canonical.rpartition(".")[0]
            if canonical == path or canonical.startswith(path + "."):
                exports.add(path)
            elif path.startswith(module + ".__dict__") or path.startswith(canonical + "."):
                exports.add(module)
    return exports


def _escaped_exports(tree: ast.AST, bindings) -> set[str]:
    """Find direct imported-object exposure across the whole syntax tree.

    Expression context must not hide a namespace or callable: iteration inputs,
    branches, defaults and literal containers can all expose the same object.
    Inspect direct references, not runtime aliases or call results. Qualification
    alone is not exposure, and calling the canonical pure helper exposes only
    its result; its arguments are still inspected independently.
    """
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    exports: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)) or not isinstance(node.ctx, ast.Load):
            continue
        parent = parents.get(node)
        if isinstance(parent, (ast.Attribute, ast.Subscript)) and parent.value is node:
            continue  # inspect the complete qualified reference instead
        if isinstance(parent, ast.Call) and parent.func is node:
            callee = _canonical_paths(node, bindings)
            if callee and callee <= PURE_DB_CALLS:
                continue
            if isinstance(node, ast.Attribute):
                # A namespace/view method may mutate its receiver.
                exports.update(_namespace_exports(node.value, bindings))
        exports.update(_namespace_exports(node, bindings))
    return exports


def _binding_writes(tree, bindings) -> tuple[set[str], set[str]]:
    """Bindings and imported objects that cannot retain an exact pure exemption.

    This is conservative invalidation, not flow analysis: assignments/deletions,
    parameters, defs, import conflicts, loop/with/walrus/comprehension targets and
    exception/pattern captures may all shadow a canonical binding. Mutating an
    imported DB object also invalidates equivalent aliases of its pure exports.
    """
    writes: set[str] = set()
    exports = _escaped_exports(tree, bindings)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            writes.add(node.id)
        elif isinstance(node, ast.arg):
            writes.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            writes.add(node.name)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            if node.name:
                writes.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            writes.add(node.rest)
        elif isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if isinstance(root, ast.Name):
                writes.add(root.id)
            exports.update(_namespace_exports(node, bindings))
    return writes, exports


def _call_label(
    node: ast.Call, module_aliases: dict[str, set[str]], bare_funcs: set[str], pure_calls: set[str]
) -> str | None:
    """Label for a call into synchronous app.db code, or None if it isn't one."""
    func = node.func
    if _dotted(func) in pure_calls:
        return None

    if isinstance(func, ast.Name):
        return f"{func.id}()" if func.id in bare_funcs else None

    if not isinstance(func, ast.Attribute):
        return None

    attr = func.attr
    if _is_allowlisted(attr):
        return None

    # Module aliases and package aliases use the same canonical interpretation
    # as export writes; a mutation cannot escape by changing the import spelling.
    for canonical in _canonical_paths(func, module_aliases):
        if canonical.startswith(DB_PACKAGE + ".") and not canonical.startswith(ASYNC_DB_MODULE + "."):
            return _dotted(func)
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

    module_aliases, bare_funcs, pure_calls = collect_db_names(tree)

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
        label = _call_label(node, module_aliases, bare_funcs, pure_calls)
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
