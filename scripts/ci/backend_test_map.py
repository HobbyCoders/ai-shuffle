"""Select the backend tests a change can affect from a recorded test map.

A full local run (``python scripts/local_ci.py full``) records which
repository functions each test file runs (``tests/_test_map.py``) and
``merge`` turns those records into a map. Before a push, ``select`` compares
every changed Python file with its base version, function by function, and
picks the test files that ran a changed function. Edge publication still waits
for a full local pass on main, so a test this misses is caught before an
installed app updates.

Fails safe: a missing or unreadable map, a dependency or pytest config change,
a change to code that session-wide fixtures run, a function the map cannot
place, or a selection that is most of the suite anyway prints ``all``.

A changed file that is not Python selects the tests that name it and the tests
of the code that names it in a string literal: the function holding the
literal, the users of a module constant bound to it, or the whole file when it
is named in code that runs at import. Docstrings, comments and messages (a
literal with whitespace) only mention a file; they do not read it. Tests that
list a folder below the top level of it count as naming it; tests that list
the whole checkout or a top-level folder run too, but cannot narrow the
choice. A file nothing names that way, any conftest, and any file under
tests/ that is not Python (fixtures, data, snapshots) print ``all``.

In CI a pull request's change starts at the map's commit, so it holds what
main changed since. Of those files, CI plumbing and files nothing names do not
print ``all``: main's full run tested them, and they move no test onto or off
the pull request's files.

    python scripts/ci/backend_test_map.py merge --hits DIR --out MAP --commit SHA
    python scripts/ci/backend_test_map.py select --map MAP --base REV [--head REV]

``select`` prints ``all``, ``none`` or one test file per line and explains its
choice on stderr.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from fnmatch import fnmatch
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
VERSION = 1
SHARED = "<shared>"

# The local backend fast subset. Keep in step with scripts/local_ci.py.
FAST_SUBSET_DIRS = (
    "tests/api", "tests/core", "tests/db", "tests/middleware", "tests/contract",
    "tests/performance", "tests/mcp", "tests/services", "tests/unit", "tests/scripts",
)
# Run by dedicated steps (backend-linux in local_ci), not parallel fast tests:
# the container supervisor has global state and idle/disk timing measurements.
FAST_SUBSET_EXCLUDED = {
    "tests/test_scan_blocking_db.py",
    "tests/core/test_container_runtime.py",
    "tests/core/test_container_image_contract.py",
}
# Changes the map cannot see through.
FULL_SUITE_PATTERNS = (
    "requirements*.txt", "requirements*.lock", "pytest.ini", "setup.cfg", "pyproject.toml",
    "fluid/pyproject.toml", ".github/workflows/ci-backend.yml",
    "scripts/ci/backend_test_map.py", "tests/_test_map.py", "tests/__init__.py",
    # How CI Backend splits, reruns and judges its shards.
    "scripts/ci/backend_shard*.py", "scripts/ci/backend_gate.py", "scripts/ci/record_failures.py",
)
# The ones above that decide how CI Backend runs, records and judges tests, not
# what the code does. A pull request changing one runs everything to prove it;
# one only main changed since the map was proven by main's own full run, and
# cannot change which tests reach the pull request's files.
CI_PLUMBING_PATTERNS = (
    ".github/workflows/ci-backend.yml", "scripts/ci/backend_test_map.py", "tests/_test_map.py",
    "scripts/ci/backend_shard*.py", "scripts/ci/backend_gate.py", "scripts/ci/record_failures.py",
)
# Package manifests and lockfiles anywhere: tests read them by paths built at
# run time (the version source reads frontend/package-lock.json), and a
# dependency change reaches code no map follows.
MANIFEST_NAMES = ("package.json", "package-lock.json", "npm-shrinkwrap.json", "pyproject.toml",
                  "requirements*.txt", "requirements*.in", "requirements*.lock", "uv.lock",
                  "poetry.lock", "Pipfile", "Pipfile.lock", "setup.py", "setup.cfg")
SOURCE_DIRS = ("app", "fluid/fluid", "tests", "scripts", "docker")
ROOT_SCRIPTS = ("run_local.py",)
# At this share of the fast subset a selected run saves little; run it all.
FULL_SUITE_SHARE = 0.6
# A map covering fewer test files than this is broken, not small.
MIN_MAPPED_TESTS = 100
# Names too common to say which file a test means.
GENERIC_NAMES = {"__init__.py", "README.md", "package.json", "package-lock.json", "index.ts",
                 "index.js", "main.py", "config.py", "utils.py", "base.py", "models.py"}
# Code that lists files rather than naming them, and code that lists the whole checkout.
WALKS = re.compile(r"\.(?:rglob|glob|iterdir|walk)\(|os\.(?:walk|listdir|scandir)\(|ls-files|ls-tree")
REPO_LISTING = re.compile(r"ls-files|ls-tree|ROOT\.rglob\(|ROOT\.walk\(|os\.walk\(\s*ROOT\b")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
STATEMENT_LISTS = ("body", "orelse", "finalbody", "handlers", "cases")
MAX_NAME_DEPTH = 3


class FullSuite(Exception):
    """The change needs every test; the message says why."""


# ---------------------------------------------------------------------------
# source structure
# ---------------------------------------------------------------------------

def module_name(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.suffix != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if parts[:2] == ["fluid", "fluid"]:
        parts = parts[1:]
    elif parts and parts[0] == "fluid":
        return None
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def names_folder(text: str, folders: set[str]) -> bool:
    """Whether ``text`` names one of ``folders`` as a quoted path or the start of one."""
    return any(f"{quote}{folder}{end}" in text for folder in folders for quote in "\"'" for end in (quote, "/"))


def fast_subset(path: str) -> bool:
    p = PurePosixPath(path)
    if not p.name.startswith("test_") or p.suffix != ".py" or path in FAST_SUBSET_EXCLUDED:
        return False
    if len(p.parts) == 2 and p.parts[0] == "tests":
        return True
    return any(path.startswith(d + "/") for d in FAST_SUBSET_DIRS)


class Blocks:
    """A Python file split into the units a change is compared by.

    functions: qualname (as ``co_qualname`` names it, nesting folded into the
    outer function) -> AST dump; classes: qualname -> dump of the class
    header and its non-method statements; names: module-level name -> dump of
    the statements binding it; loose: dumps of module-level statements that
    bind no name (calls run at import, ``if`` tests).
    """

    def __init__(self, source: str | None):
        self.functions: dict[str, str] = {}
        self.classes: dict[str, str] = {}
        self.names: dict[str, str] = defaultdict(str)
        self.loose: list[str] = []
        self.valid = True
        if source is None:
            return
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            self.valid = False
            return
        self._visit(tree.body, "", module_level=True)
        self.loose.sort()

    def _visit(self, body, prefix: str, module_level: bool) -> None:
        for node in body:
            if isinstance(node, FUNCTIONS):
                self.functions[prefix + node.name] = self.functions.get(prefix + node.name, "") + ast.dump(node)
            elif isinstance(node, ast.ClassDef):
                qualname = prefix + node.name
                header = [n for n in node.body if not isinstance(n, (*FUNCTIONS, ast.ClassDef))]
                self.classes[qualname] = ast.dump(ast.ClassDef(
                    name=node.name, bases=node.bases, keywords=node.keywords,
                    decorator_list=node.decorator_list, body=header, type_params=[]))
                self._visit([n for n in node.body if isinstance(n, (*FUNCTIONS, ast.ClassDef))],
                            qualname + ".", module_level=False)
            elif not module_level:
                continue
            elif any(isinstance(getattr(node, field, None), list) and getattr(node, field)
                     and isinstance(getattr(node, field)[0], (ast.stmt, ast.excepthandler, ast.match_case))
                     for field in STATEMENT_LISTS):
                # if/try/with/for at module level: compare the statement's own
                # parts, then its nested statements as module-level ones.
                shell = {field: value for field, value in ast.iter_fields(node) if field not in STATEMENT_LISTS}
                self.loose.append(type(node).__name__ + repr({field: dump(value) for field, value in shell.items()}))
                for field in STATEMENT_LISTS:
                    for child in getattr(node, field, None) or []:
                        self._visit(getattr(child, "body", None) if isinstance(child, (ast.excepthandler, ast.match_case)) else [child],
                                    prefix, module_level=True)
            else:
                bound = bound_names(node)
                for name in bound:
                    self.names[name] += ast.dump(node)
                if not bound:
                    self.loose.append(ast.dump(node))


def mention_scopes(source: str, needles: set[str]) -> tuple[set[str], set[str], bool] | None:
    """Where the code of ``source`` names a needle in a string literal.

    ``(functions, names, loose)``: the functions (qualnames as ``Blocks``
    names them) whose bodies hold such a string, the module-level names whose
    binding statements do, and whether one sits in other code that runs at
    import (a bare module statement, a class body, a decorator or default).
    Prose is not a read and does not count: docstrings, other bare string
    statements, comments, and any literal with whitespace in it (a message or
    note naming the file; a path built in an f-string is a literal of its own).
    ``None`` when the source does not parse.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    prose = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}

    def names_one(*nodes) -> bool:
        return any(isinstance(item, ast.Constant) and isinstance(item.value, str) and id(item) not in prose
                   and not any(c.isspace() for c in item.value) and any(needle in item.value for needle in needles)
                   for node in nodes if isinstance(node, ast.AST) for item in ast.walk(node))

    functions: set[str] = set()
    names: set[str] = set()
    loose = False

    def visit(body, prefix: str, module_level: bool) -> None:
        nonlocal loose
        for node in body:
            if isinstance(node, FUNCTIONS):
                if names_one(*node.body):
                    functions.add(prefix + node.name)
                loose |= names_one(*node.decorator_list, node.args, node.returns)
            elif isinstance(node, ast.ClassDef):
                loose |= names_one(*node.bases, *node.keywords, *node.decorator_list,
                                   *(n for n in node.body if not isinstance(n, (*FUNCTIONS, ast.ClassDef))))
                visit([n for n in node.body if isinstance(n, (*FUNCTIONS, ast.ClassDef))], prefix + node.name + ".", False)
            elif not module_level:
                continue
            elif any(getattr(node, field, None) for field in STATEMENT_LISTS):
                # if/try/with/for at module level: its own parts run at import,
                # its nested statements are module-level ones.
                loose |= names_one(*(v for f, v in ast.iter_fields(node) if f not in STATEMENT_LISTS),
                                   *(i for f, v in ast.iter_fields(node) if f not in STATEMENT_LISTS
                                     and isinstance(v, list) for i in v))
                for field in STATEMENT_LISTS:
                    for child in getattr(node, field, None) or []:
                        if isinstance(child, (ast.excepthandler, ast.match_case)):
                            loose |= names_one(*(v for f, v in ast.iter_fields(child) if f != "body"))
                            visit(child.body, prefix, True)
                        else:
                            visit([child], prefix, True)
            elif names_one(node):
                bound = bound_names(node)
                names.update(bound)
                loose |= not bound

    visit(tree.body, "", True)
    return functions, names, loose


def bound_names(node) -> list[str]:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(alias.asname or alias.name).split(".")[0] for alias in node.names if alias.name != "*"]
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, ast.TypeAlias):
        targets = [node.name]
    names = []
    for target in targets:
        for item in ast.walk(target):
            if isinstance(item, ast.Name):
                names.append(item.id)
            elif isinstance(item, (ast.Attribute, ast.Subscript)):
                return []  # mutates something else: compare as a loose statement
    return names


def dump(value) -> object:
    if isinstance(value, ast.AST):
        return ast.dump(value)
    if isinstance(value, list):
        return [dump(item) for item in value]
    return value


def differing(old: dict[str, str], new: dict[str, str]) -> set[str]:
    return {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}


# ---------------------------------------------------------------------------
# the map
# ---------------------------------------------------------------------------

def file_seconds(durations_dir: Path | None) -> dict[str, float]:
    """Seconds each test file took, summed from the shards' ``durations.json`` (node id -> seconds)."""
    seconds: dict[str, float] = defaultdict(float)
    for path in sorted(durations_dir.glob("**/durations.json")) if durations_dir else ():
        for node, value in json.loads(path.read_text(encoding="utf-8")).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                seconds[node.split("::", 1)[0]] += float(value)
    return {name: round(value, 3) for name, value in sorted(seconds.items())}


def merge(hits_dir: Path, out: Path, commit: str, durations_dir: Path | None = None) -> dict:
    contexts: dict[str, set[tuple[str, str]]] = defaultdict(set)
    # CI keeps each shard's records in its own directory.
    for path in sorted(hits_dir.glob("**/hits-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for context, hits in data["hits"].items():
            contexts[context].update(tuple(hit) for hit in hits)
    tests = sorted(context for context in contexts if context != SHARED)
    index = {test: number for number, test in enumerate(tests)}
    covered: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for test in tests:
        for file, qualname in contexts[test]:
            covered[file][qualname].append(index[test])
    shared: dict[str, set[str]] = defaultdict(set)
    for file, qualname in contexts.get(SHARED, ()):
        shared[file].add(qualname)
    known = {}
    for rel, source in load_sources(ROOT).items():
        blocks = Blocks(source)
        known[rel] = sorted(blocks.functions.keys() | blocks.classes.keys())
    result = {
        "version": VERSION,
        "commit": commit,
        "tests": tests,
        "covered": {file: {q: sorted(set(ids)) for q, ids in quals.items()} for file, quals in covered.items()},
        "shared": {file: sorted(quals) for file, quals in shared.items()},
        "known": known,
        # Only to size a pull request's shards; never to choose its tests.
        "seconds": file_seconds(durations_dir),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as handle:
        json.dump(result, handle, separators=(",", ":"))
    return result


def load_map(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("version") != VERSION or len(data.get("tests", ())) < MIN_MAPPED_TESTS:
        raise FullSuite(f"test map {path.name} is from another version or covers too few tests")
    return data


def load_sources(root: Path) -> dict[str, str]:
    files = {}
    paths = [root / name for name in ROOT_SCRIPTS]
    for directory in SOURCE_DIRS:
        paths.extend((root / directory).rglob("*.py"))
    for path in paths:
        if path.is_file() and "node_modules" not in path.parts:
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return files


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

class References:
    """Which functions of a file use a name, parsed on demand."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self._parsed: dict[str, tuple[dict, dict, dict, dict] | None] = {}

    def parse(self, path: str):
        if path not in self._parsed:
            self._parsed[path] = self._build(path)
        return self._parsed[path]

    def _build(self, path: str):
        try:
            tree = ast.parse(self.files[path])
        except (SyntaxError, ValueError, KeyError):
            return None
        uses: dict[str, set[str]] = defaultdict(set)        # identifier -> qualnames
        attrs: dict[tuple[str, str], set[str]] = defaultdict(set)  # (owner, attr) -> qualnames
        module_uses: dict[str, set[str]] = defaultdict(set)  # identifier -> module-level names bound with it
        imports: dict[str, tuple[str, str | None]] = {}      # local name -> (module, imported name or None)

        def scan(node, qualname):
            for item in ast.walk(node):
                if isinstance(item, ast.Name):
                    uses[item.id].add(qualname)
                elif isinstance(item, ast.Attribute):
                    try:
                        attrs[(ast.unparse(item.value), item.attr)].add(qualname)
                    except Exception:  # noqa: BLE001 - unparse of exotic nodes
                        pass

        def visit(body, prefix, module_level):
            for node in body:
                if isinstance(node, FUNCTIONS):
                    scan(node, prefix + node.name)
                elif isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if not isinstance(item, (*FUNCTIONS, ast.ClassDef)):
                            scan(item, prefix + node.name)
                    for base in [*node.bases, *node.decorator_list]:
                        scan(base, prefix + node.name)
                    visit([n for n in node.body if isinstance(n, (*FUNCTIONS, ast.ClassDef))],
                          prefix + node.name + ".", False)
                elif module_level:
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports[alias.asname or alias.name] = (alias.name, None)
                    elif isinstance(node, ast.ImportFrom):
                        base = resolve_relative(path, node.level, node.module)
                        for alias in node.names:
                            imports[alias.asname or alias.name] = (base, alias.name)
                    nested = [child for field in STATEMENT_LISTS for child in getattr(node, field, None) or []
                              if isinstance(child, ast.stmt)]
                    for child in nested:
                        visit([child], prefix, True)
                    for name in bound_names(node):
                        for item in ast.walk(node):
                            if isinstance(item, ast.Name) and item.id != name:
                                module_uses[item.id].add(name)
        visit(tree.body, "", True)
        return uses, attrs, module_uses, imports

    def users(self, path: str, module: str | None, name: str):
        """(file, qualname, module-level name) uses of ``name`` defined in ``path``."""
        found = []
        parsed = self.parse(path)
        if parsed:
            uses, module_uses = parsed[0], parsed[2]
            found += [(path, q, None) for q in uses.get(name, ())]
            found += [(path, None, n) for n in module_uses.get(name, ())]
        if not module:
            return found
        leaf = module.rsplit(".", 1)[-1]
        for other, text in self.files.items():
            if other == path or name not in text or leaf not in text:
                continue
            parsed = self.parse(other)
            if not parsed:
                continue
            uses, attrs, module_uses, imports = parsed
            for local, (source, imported) in imports.items():
                if imported == name and source == module:
                    found += [(other, q, None) for q in uses.get(local, ())]
                    found += [(other, None, n) for n in module_uses.get(local, ())]
                elif (imported is None and source == module) or (imported == leaf and source + "." + leaf == module):
                    found += [(other, q, None) for q in attrs.get((local, name), ())]
                elif imported is None and module.startswith(source + "."):
                    found += [(other, q, None) for q in attrs.get((module, name), ())]
        return found


def resolve_relative(path: str, level: int, module: str | None) -> str:
    if not level:
        return module or ""
    parts = (module_name(path) or "").split(".")
    if PurePosixPath(path).name != "__init__.py":
        parts = parts[:-1]
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    return ".".join([*parts, module] if module else parts)


class Selector:
    def __init__(self, test_map: dict, files: dict[str, str], base_source):
        self.map = test_map
        self.files = files
        self.base_source = base_source
        self.tests_by_index = test_map["tests"]
        self.selected: set[str] = set()
        self.reasons: list[str] = []
        self.refs = References(files)

    # lookups ---------------------------------------------------------------

    def _covered(self, path: str, qualname: str) -> bool:
        if qualname in self.map["shared"].get(path, ()):
            raise FullSuite(f"{path}::{qualname} runs in a session-wide fixture")
        ids = self.map["covered"].get(path, {}).get(qualname)
        if ids:
            self.selected.update(self.tests_by_index[i] for i in ids)
        return qualname in self.map["known"].get(path, ()) or bool(ids)

    def file_level(self, path: str, why: str) -> None:
        if PurePosixPath(path).name == "conftest.py":
            raise FullSuite(f"{path}: {why} in a conftest")
        if path in self.map["shared"]:
            raise FullSuite(f"{path}: {why} in a module session-wide fixtures run")
        quals = self.map["covered"].get(path)
        if quals is None and path not in self.map["known"]:
            self.text_fallback(path, why)
            return
        for ids in (quals or {}).values():
            self.selected.update(self.tests_by_index[i] for i in ids)
        self.reasons.append(f"{path}: {why}; every test that ran it")

    def text_fallback(self, path: str, why: str) -> None:
        module = module_name(path)
        needles = {path, *( [module] if module else [])}
        hits = {t for t in self.files if fast_subset(t) and any(n in self.files[t] for n in needles)}
        if not hits:
            raise FullSuite(f"{path} ({why}) is not in the test map and no test names it")
        self.selected |= hits
        self.reasons.append(f"{path}: {why}; not in the map, {len(hits)} tests that name it")

    # change handling ---------------------------------------------------------

    def python_change(self, path: str) -> None:
        base = Blocks(self.base_source(path))
        head = Blocks(self.files.get(path))
        if not base.valid or not head.valid:
            raise FullSuite(f"{path} does not parse")
        module = module_name(path)
        for qualname in sorted(differing(base.functions, head.functions)):
            if qualname not in base.functions:
                continue  # new code: tests reach it through the changed callers
            if not self._covered(path, qualname):
                self.file_level(path, f"{qualname} changed but the map does not know it")
        changed_names = differing(base.names, head.names)
        for qualname in sorted(differing(base.classes, head.classes)):
            if qualname in base.classes:
                # A class header or attribute change reaches every method.
                prefix = qualname + "."
                for method, ids in self.map["covered"].get(path, {}).items():
                    if method == qualname or method.startswith(prefix):
                        self.selected.update(self.tests_by_index[i] for i in ids)
                if "." not in qualname:
                    changed_names.add(qualname)
        if base.loose != head.loose:
            self.file_level(path, "module-level statement changed")
        if changed_names and PurePosixPath(path).name == "conftest.py":
            raise FullSuite(f"{path}: module-level names changed in a conftest")
        self.name_changes(path, module, changed_names)

    def name_changes(self, path: str, module: str | None, names: set[str]) -> None:
        pending = [(path, module, name, 0) for name in sorted(names)]
        seen = set()
        while pending:
            file, mod, name, depth = pending.pop()
            if (file, name) in seen:
                continue
            seen.add((file, name))
            if depth > MAX_NAME_DEPTH:
                self.file_level(file, f"name {name} changed (deeply re-exported)")
                continue
            for user_file, qualname, module_level_name in self.refs.users(file, mod, name):
                if fast_subset(user_file):
                    self.selected.add(user_file)
                elif qualname is not None:
                    if not self._covered(user_file, qualname):
                        self.file_level(user_file, f"{qualname} uses changed {name}")
                else:
                    pending.append((user_file, module_name(user_file), module_level_name, depth + 1))

    def other_change(self, path: str, main_only: bool = False) -> None:
        p = PurePosixPath(path)
        needles = {path} | ({p.name} if p.name not in GENERIC_NAMES and len(p.name) >= 6 else set())
        tests = {t for t in self.files if fast_subset(t) and any(n in self.files[t] for n in needles)}
        sources = {f for f in self.files if not fast_subset(f) and any(n in self.files[f] for n in needles)}
        # Tests that list a folder of it, or the whole checkout, read it
        # unnamed. One naming a folder below the top level narrows the choice
        # like a test naming the file; a whole-checkout lister or a top-level
        # folder ("frontend") reads too much to say anything about this file.
        specific = {str(parent) for parent in p.parents if len(parent.parts) >= 2}
        top = {p.parts[0]} if len(p.parts) > 1 else set()
        listers = {t for t in self.files if fast_subset(t) and WALKS.search(self.files[t])}
        narrow = {t for t in listers if names_folder(self.files[t], specific)}
        broad = {t for t in listers - narrow
                 if REPO_LISTING.search(self.files[t]) or names_folder(self.files[t], top)}
        readers = 0
        for source in sorted(sources):
            found = mention_scopes(self.files[source], needles)
            if found is None or found[2]:
                readers += 1
                self.file_level(source, f"reads changed {path}")
                continue
            # Only code that names the file can read it: the functions whose
            # bodies do, and the users of a module-level name bound to it. A
            # docstring, comment or message naming it reads nothing.
            functions, names, _ = found
            readers += bool(functions or names)
            for qualname in sorted(functions):
                if not self._covered(source, qualname):
                    self.file_level(source, f"{qualname} reads changed {path} but the map does not know it")
            self.name_changes(source, module_name(source), names)
        if not tests and not narrow and not readers:
            if main_only:
                # No code names it, so it moved no function the map follows
                # into or out of a test; main's full run tested it.
                self.reasons.append(f"{path}: only main changed it and no code reads it by name; "
                                    f"main's full run tested it")
                return
            # Nothing names it in code: whatever reads it (code loading its
            # folder, a path built at run time) is unknown.
            raise FullSuite(f"{path}: no code reads it by name and no test names it or its folder")
        self.selected |= tests | narrow | broad
        self.reasons.append(f"{path}: {len(tests)} tests name it, {len(narrow)} list its folder, "
                            f"{len(broad)} list everything; {readers} of {len(sources)} naming sources read it in code")

    def subprocess_users(self, path: str) -> None:
        module = module_name(path)
        needles = {path} | ({module} if module else set())
        for test, text in self.files.items():
            if (fast_subset(test) and ("subprocess" in text or "sys.executable" in text)
                    and any(n in text for n in needles)):
                self.selected.add(test)


def select(changed: list[str], test_map: dict | None, files: dict[str, str], base_source,
           main_only: frozenset[str] | set[str] = frozenset()) -> tuple[str | list[str], list[str]]:
    """``("all" | [tests], reasons)``.

    ``main_only`` names the changed files only main changed since the map,
    which the pull request leaves as main has them. Main's full run tested
    those, so they count only where they can make the map's view of the
    pull request's files stale: Python, conftests, dependencies and setup.
    """
    try:
        if test_map is None:
            raise FullSuite("no test map from a full run on main")
        for path in changed:
            if path in main_only and any(fnmatch(path, pattern) for pattern in CI_PLUMBING_PATTERNS):
                continue
            if any(fnmatch(path, pattern) for pattern in FULL_SUITE_PATTERNS):
                raise FullSuite(f"{path} changes the test setup or dependencies")
            if any(fnmatch(PurePosixPath(path).name, name) for name in MANIFEST_NAMES):
                raise FullSuite(f"{path} is a package manifest or lockfile")
            # Any conftest, the root one too: pytest loads it for every test beneath it.
            if PurePosixPath(path).name == "conftest.py":
                raise FullSuite(f"{path} is a conftest")
            if "\ufffd" in path:
                raise FullSuite(f"{path!r} is not a UTF-8 file name")
            # Fixtures, data and snapshots under tests/ are read by paths tests
            # build at run time; only test modules and helpers are mapped.
            if path.startswith("tests/") and PurePosixPath(path).suffix != ".py":
                raise FullSuite(f"{path} is test data, read by paths no map can follow")
        selector = Selector(test_map, files, base_source)
        for path in changed:
            p = PurePosixPath(path)
            tracked = p.suffix == ".py" and (path in ROOT_SCRIPTS or any(path.startswith(d + "/") for d in SOURCE_DIRS))
            if fast_subset(path):
                if path in files:
                    selector.selected.add(path)
            elif tracked:
                selector.python_change(path)
                selector.subprocess_users(path)
            else:
                selector.other_change(path, main_only=path in main_only and p.suffix != ".py")
        tests = [t for t in files if fast_subset(t)]
        chosen = sorted(t for t in selector.selected if t in files and fast_subset(t))
        if len(chosen) >= FULL_SUITE_SHARE * len(tests):
            raise FullSuite(f"{len(chosen)} of {len(tests)} test files are affected")
        return chosen, selector.reasons
    except FullSuite as reason:
        return "all", [str(reason)]


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace")


def changed_names(base: str, head: str) -> list[str]:
    """The files changed from ``base`` to ``head``, read as bytes and split on NUL.

    git quotes an unusual name in its plain output, which then matches no file.
    A name that is not UTF-8 or holds a line break raises ValueError: run everything.
    """
    raw = subprocess.check_output(["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"], cwd=ROOT)
    names = [part.decode("utf-8") for part in raw.split(b"\0") if part]
    odd = [name for name in names if "\n" in name or "\r" in name]
    if odd:
        raise ValueError(f"a changed file name holds a line break: {odd[0]!r}")
    return names


def choose(map_path: Path, base_rev: str, head: str = "HEAD",
           main_rev: str | None = None) -> tuple[str | list[str], list[str], dict | None]:
    """``select`` for the checkout: the change from ``base_rev`` to ``head``, against the map at ``map_path``.

    With ``main_rev`` (main's tip, which ``head`` merges), the files ``head``
    leaves as main has them are the ones only main changed since ``base_rev``.
    Returns the selection, the reasons and the map it was made from.
    """
    try:
        base = git("rev-parse", base_rev).strip()
        changed = changed_names(base, head)
        own = set(changed_names(git("rev-parse", main_rev).strip(), head)) if main_rev else set(changed)
        main_only = frozenset(path for path in changed if path not in own)
        # A file the pull request sets back to the map's version is missing
        # from the change since the map, yet the pull request still changes it.
        changed += sorted(own - set(changed))
        test_map = load_map(map_path) if map_path.is_file() else None
    except (subprocess.CalledProcessError, OSError, EOFError, ValueError, FullSuite) as error:
        return "all", [f"Running the full suite: {error}"], None

    def base_source(path: str) -> str | None:
        # A file the change adds has no base version; git's "fatal:" line would only mislead the log.
        shown = subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True, check=False,
                               text=True, encoding="utf-8", errors="replace")
        return shown.stdout if shown.returncode == 0 else None

    result, reasons = select(changed, test_map, load_sources(ROOT), base_source, main_only)
    if test_map is not None:
        reasons.insert(0, f"Test map from {str(test_map.get('commit', '?'))[:12]}; {len(changed)} changed files, "
                          f"{len(main_only)} of them only main changed since.")
    return result, reasons, test_map


# ---------------------------------------------------------------------------
# CI Backend's shards (.github/workflows/ci-backend.yml)
# ---------------------------------------------------------------------------

# The shards a full run uses; scripts/ci/backend_gate.py's SHARD_COUNT.
FULL_SHARDS = 8
# Test files of the fast subset that CI Backend runs outside its shards (the
# policy step of backend-security runs tests/scripts and the docs tests).
SHARD_EXCLUDED_DIRS = ("tests/scripts/",)
SHARD_EXCLUDED = FAST_SUBSET_EXCLUDED | {"tests/core/test_docs_content.py", "tests/test_buyer_docs.py"}
# Measured test seconds one two-worker shard is given (about two and a half
# minutes of wall time), and what a file the map has no time for is assumed to take.
SHARD_SECONDS = 300.0
UNKNOWN_FILE_SECONDS = 20.0


def in_shards(path: str) -> bool:
    return fast_subset(path) and path not in SHARD_EXCLUDED and not path.startswith(SHARD_EXCLUDED_DIRS)


def shard_plan(result: str | list[str], test_map: dict | None) -> dict:
    """What CI Backend's shards run: {selection, shards, tests}.

    ``selection`` is ``all``, ``none`` or a digest of the chosen files; the
    receipts carry it, so the gate can tell a full run from a selected one.
    """
    if result == "all":
        return {"selection": "all", "shards": list(range(1, FULL_SHARDS + 1)), "tests": []}
    tests = sorted(path for path in result if in_shards(path))
    if not tests:
        return {"selection": "none", "shards": [], "tests": []}
    seconds_by_file = (test_map or {}).get("seconds") or {}
    seconds = sum(float(seconds_by_file.get(path, UNKNOWN_FILE_SECONDS)) for path in tests)
    count = max(1, min(FULL_SHARDS, len(tests), math.ceil(seconds / SHARD_SECONDS)))
    digest = hashlib.sha256("\n".join(tests).encode("utf-8")).hexdigest()[:16]
    return {"selection": f"files-{digest}", "shards": list(range(1, count + 1)), "tests": tests}


def ci_select(map_path: Path, environ=os.environ) -> tuple[dict, list[str]]:
    """The shard plan of this CI run. Only a pull request selects; anything else runs everything.

    The change is read from the commit the map was recorded on (a full run on
    main) to the pull request's merge into main, so the functions compared are
    the ones the map saw run, and whatever main changed since is chosen too.
    """
    event = environ.get("GITHUB_EVENT_NAME", "")
    if event != "pull_request":
        return shard_plan("all", None), [f"A {event or 'local'} run tests everything."]
    try:
        parents = git("rev-list", "--parents", "-n", "1", "HEAD").split()
        # GitHub tests a pull request on its merge into main: main is the first parent.
        if len(parents) != 3:
            return shard_plan("all", None), ["HEAD is not the pull request's merge commit; running everything."]
        if not map_path.is_file():
            return shard_plan("all", None), ["No backend test map from a full run on main; running everything."]
        recorded = str(load_map(map_path).get("commit") or "")
        if not recorded or subprocess.run(["git", "merge-base", "--is-ancestor", recorded, parents[1]],
                                          cwd=ROOT, capture_output=True, check=False).returncode != 0:
            return shard_plan("all", None), [f"The test map's commit {recorded[:12] or '?'} is not on main; running everything."]
        result, reasons, test_map = choose(map_path, recorded, parents[0], main_rev=parents[1])
        return shard_plan(result, test_map), reasons
    except Exception as error:  # noqa: BLE001 - any doubt runs every test
        return shard_plan("all", None), [f"Selection failed, running everything: {type(error).__name__}: {error}"]


def write_ci_select(map_path: Path, environ=os.environ, out=sys.stdout) -> dict:
    found, reasons = ci_select(map_path, environ)
    print(f"selection={found['selection']}", file=out)
    print(f"shards={json.dumps(found['shards'])}", file=out)
    # The partition size; a rerun of only the failed shards must not change it.
    print(f"count={len(found['shards'])}", file=out)
    print(f"tests={json.dumps(found['tests'])}", file=out)
    lines = ["### Backend tests this run", ""]
    if found["selection"] == "all":
        lines.append(f"Every test, in {len(found['shards'])} shards.")
    elif found["selection"] == "none":
        lines.append("No backend test in the shards can reach this change; the shards do not run.")
    else:
        lines.append(f"{len(found['tests'])} test files the change can reach, in {len(found['shards'])} shards. "
                     "main runs every test after the merge.")
    lines += ["", *[f"- {reason}" for reason in reasons[:50]]]
    if found["tests"]:
        lines += ["", "<details><summary>Selected test files</summary>", "",
                  *[f"- `{path}`" for path in found["tests"]], "", "</details>"]
    summary = environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    for reason in reasons:
        print(f"  {reason}", file=sys.stderr)
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Backend test map: merge records, select tests.")
    commands = parser.add_subparsers(dest="command", required=True)
    merge_cmd = commands.add_parser("merge")
    merge_cmd.add_argument("--hits", type=Path, required=True)
    merge_cmd.add_argument("--out", type=Path, required=True)
    merge_cmd.add_argument("--commit", required=True)
    merge_cmd.add_argument("--durations", type=Path, help="Shard results holding durations.json files")
    select_cmd = commands.add_parser("select")
    select_cmd.add_argument("--map", type=Path, required=True)
    select_cmd.add_argument("--base", required=True)
    select_cmd.add_argument("--head", default="HEAD")
    ci_cmd = commands.add_parser("ci-select", help="CI Backend's shard plan, as $GITHUB_OUTPUT lines")
    ci_cmd.add_argument("--map", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "merge":
        result = merge(args.hits, args.out, args.commit, args.durations)
        print(f"Test map: {len(result['tests'])} test files, {len(result['covered'])} source files, "
              f"{sum(len(q) for q in result['shared'].values())} shared functions, "
              f"{len(result['seconds'])} timed test files", file=sys.stderr)
        return 0
    if args.command == "ci-select":
        write_ci_select(args.map)
        return 0

    result, reasons, _ = choose(args.map, args.base, args.head)
    for reason in reasons:
        print(f"  {reason}", file=sys.stderr)
    if result == "all":
        print("Running the full suite.", file=sys.stderr)
        print("all")
    elif not result:
        print("No backend test can reach this change.", file=sys.stderr)
        print("none")
    else:
        print(f"Running {len(result)} test files.", file=sys.stderr)
        print("\n".join(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
