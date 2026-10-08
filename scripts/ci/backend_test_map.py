"""Select the backend tests a pull request can affect from a recorded test map.

The full CI Backend run on main records which repository functions each test
file runs (``tests/_test_map.py``) and ``merge`` turns those records into a
map. On a pull request, ``select`` compares every changed Python file with its
base version, function by function, and picks the test files that ran a
changed function. Edge publication still waits for a full run on main, so a
test this misses is caught before an installed app updates.

Fails safe: a missing or unreadable map, a dependency or pytest config change,
a change to code that session-wide fixtures run, a function the map cannot
place, or a selection that is most of the suite anyway prints ``all``.

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
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
VERSION = 1
SHARED = "<shared>"

# The CI Backend fast subset. Keep in step with .github/workflows/ci-backend.yml.
FAST_SUBSET_DIRS = (
    "tests/api", "tests/core", "tests/db", "tests/middleware", "tests/contract",
    "tests/performance", "tests/mcp", "tests/services", "tests/unit", "tests/scripts",
)
# Run in full by dedicated steps of the same job on every run.
FAST_SUBSET_EXCLUDED = {
    "tests/test_scan_blocking_db.py",
    "tests/core/test_chat_shell.py",
    "tests/core/test_container_runtime.py",
    "tests/core/test_container_image_contract.py",
}
# Changes the map cannot see through.
FULL_SUITE_PATTERNS = (
    "requirements*.txt", "requirements*.lock", "pytest.ini", "setup.cfg", "pyproject.toml",
    "fluid/pyproject.toml", ".github/workflows/ci-backend.yml",
    "scripts/ci/backend_test_map.py", "tests/_test_map.py", "tests/__init__.py",
)
SOURCE_DIRS = ("app", "fluid/fluid", "tests", "scripts", "docker")
ROOT_SCRIPTS = ("run_local.py",)
# At this share of the fast subset a selected run saves little; run it all.
FULL_SUITE_SHARE = 0.6
# A map covering fewer test files than this is broken, not small.
MIN_MAPPED_TESTS = 100
# Names too common to say which file a test means.
GENERIC_NAMES = {"__init__.py", "README.md", "package.json", "package-lock.json", "index.ts",
                 "index.js", "main.py", "config.py", "utils.py", "base.py", "models.py"}
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

def merge(hits_dir: Path, out: Path, commit: str) -> dict:
    contexts: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for path in sorted(hits_dir.glob("hits-*.json")):
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

    def other_change(self, path: str) -> None:
        p = PurePosixPath(path)
        needles = {path} | ({p.name} if p.name not in GENERIC_NAMES and len(p.name) >= 6 else set())
        tests = {t for t in self.files if fast_subset(t) and any(n in self.files[t] for n in needles)}
        sources = {f for f in self.files if not fast_subset(f) and any(n in self.files[f] for n in needles)}
        if not sources and p.parts and p.parts[0] in {"app", "fluid", "docker", "scripts"}:
            sources = {f for f in self.files if PurePosixPath(f).parent == p.parent}
        self.selected |= tests
        for source in sorted(sources):
            self.file_level(source, f"reads changed {path}")
        self.reasons.append(f"{path}: {len(tests)} tests and {len(sources)} sources name it")

    def subprocess_users(self, path: str) -> None:
        module = module_name(path)
        needles = {path} | ({module} if module else set())
        for test, text in self.files.items():
            if (fast_subset(test) and ("subprocess" in text or "sys.executable" in text)
                    and any(n in text for n in needles)):
                self.selected.add(test)


def select(changed: list[str], test_map: dict | None, files: dict[str, str], base_source) -> tuple[str | list[str], list[str]]:
    """``("all" | [tests], reasons)``."""
    try:
        if test_map is None:
            raise FullSuite("no test map from a full run on main")
        for path in changed:
            if any(fnmatch(path, pattern) for pattern in FULL_SUITE_PATTERNS):
                raise FullSuite(f"{path} changes the test setup or dependencies")
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
                selector.other_change(path)
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Backend test map: merge records, select tests.")
    commands = parser.add_subparsers(dest="command", required=True)
    merge_cmd = commands.add_parser("merge")
    merge_cmd.add_argument("--hits", type=Path, required=True)
    merge_cmd.add_argument("--out", type=Path, required=True)
    merge_cmd.add_argument("--commit", required=True)
    select_cmd = commands.add_parser("select")
    select_cmd.add_argument("--map", type=Path, required=True)
    select_cmd.add_argument("--base", required=True)
    select_cmd.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)

    if args.command == "merge":
        result = merge(args.hits, args.out, args.commit)
        print(f"Test map: {len(result['tests'])} test files, {len(result['covered'])} source files, "
              f"{sum(len(q) for q in result['shared'].values())} shared functions", file=sys.stderr)
        return 0

    try:
        base = git("rev-parse", args.base).strip()
        changed = [line for line in git("diff", "--name-only", "--no-renames", f"{base}...{args.head}").splitlines() if line]
        test_map = load_map(args.map) if args.map.is_file() else None
    except (subprocess.CalledProcessError, OSError, ValueError, FullSuite) as error:
        print(f"Running the full suite: {error}", file=sys.stderr)
        print("all")
        return 0

    def base_source(path: str) -> str | None:
        try:
            return git("show", f"{base}:{path}")
        except subprocess.CalledProcessError:
            return None

    result, reasons = select(changed, test_map, load_sources(ROOT), base_source)
    if test_map is not None:
        print(f"Test map from {test_map.get('commit', '?')[:12]}; {len(changed)} changed files.", file=sys.stderr)
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
