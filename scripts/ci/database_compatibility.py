"""Keep app/db/compatibility.json honest: the level an update compares to decide
whether it takes a copy of the database first.

An update takes no database copy when the release it installs and the version
it replaces declare the same level (docker/application_updates.py:
restore_point_needed, and the desktop launcher). That is only safe while
migrations add to the database and never remove, rename or retype what the
previous version reads: the previous version is then started again over the
migrated database when a first start fails.

This script finds the SQL in app/ and fluid/ that breaks that promise and pins
a digest of it in the file. A change that adds, edits or removes such a
statement changes the digest, and the check then asks for a higher level, so
that release's update keeps a copy of the database once.

    python scripts/ci/database_compatibility.py            # check the pin
    python scripts/ci/database_compatibility.py --base origin/main
                                                           # and that a changed pin raised the level
    python scripts/ci/database_compatibility.py --pin      # after a destructive change:
                                                           # raise the level and pin the new digest
    python scripts/ci/database_compatibility.py --list     # the statements counted

A statement that names its table or column through a variable is counted with
what that variable holds, when the variable is set only by plain assignments
and for loops, in the function the statement is in or at the top of the same
file (the retired-table tuples in app/db/database.py). Anything else fails the
check: a parameter, an import, an attribute, a call, a comprehension or
augmented assignment, a global declaration, a format specification. Name the
table in the statement, or in a plain constant or loop beside it.

What it cannot see: SQL joined with + or built by format() or %, and data
rewrites (UPDATE or DELETE that changes what a column means). A change like
that raises the level by hand: edit "level" in the file.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
FILE = "app/db/compatibility.json"
SCANNED = ("app", "fluid")
# SQL after which code written before the change can fail over the same database.
# Renaming an index, and VACUUM's TRUNCATE option, change nothing older code reads.
DESTRUCTIVE = re.compile(
    r"\bDROP\s+TABLE\b"
    r"|\bALTER\s+TABLE\b[^;]*?\b(?:DROP\s+COLUMN|RENAME\s+(?:TO|COLUMN)|ALTER\s+COLUMN\b[^;]*?\b(?:TYPE|SET\s+NOT\s+NULL))\b"
    r"|\bALTER\s+COLUMN\b[^;]*?\b(?:TYPE|SET\s+NOT\s+NULL)\b",
    re.IGNORECASE | re.DOTALL)
# Upper case only: "truncate" is also an ordinary word in messages and patterns.
EMPTIED = re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?(?!FALSE\b|TRUE\b)[A-Za-z_\"{]")
# A new column older code does not fill, and the database refuses without.
REQUIRED_COLUMN = re.compile(r"\bADD\s+COLUMN\b(?:(?!\bADD\s+COLUMN\b)[^;,])*?\bNOT\s+NULL\b(?:(?!\bADD\s+COLUMN\b)[^;,])*",
                             re.IGNORECASE | re.DOTALL)


# Marks a destructive statement whose table or column the script cannot follow.
UNRESOLVED = "<cannot be followed>"
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Module)


def _text(node):
    """The literal text of a string or f-string node, each substituted value
    shown as its source ({table}), or None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                       else "{" + ast.unparse(part.value) + "}" for part in node.values)
    return None


def _own(scope):
    """The nodes of a scope, without those of a function, lambda or class in
    it. A comprehension's nodes count as the scope's own, so its variable is a
    binding the script does not follow."""
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, SCOPES):
            pending.extend(ast.iter_child_nodes(node))


def _binds(node, name):
    if isinstance(node, ast.Name):
        return node.id == name and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.arg):
        return node.arg == name
    if isinstance(node, ast.alias):
        return node.name == "*" or (node.asname or node.name.split(".")[0]) == name
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler,
                         ast.MatchAs, ast.MatchStar)):
        return node.name == name
    return isinstance(node, ast.MatchMapping) and node.rest == name


def _bound(name, site, parents, declared):
    """(note, value, binding statement) for every value ``name`` can take at
    ``site``, from the nearest scope that binds it. None when it cannot be
    followed: the scope binds it any other way than plain assignment or a for
    loop, a class binds it, a global or nonlocal statement names it, the
    file writes into it (``declared``), or nothing in the file binds it."""
    if name in declared:
        return None
    scope = parents.get(id(site))
    while scope is not None:
        if isinstance(scope, SCOPES):
            found, followed = [], set()
            for node in _own(scope):
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    targets, value, how = [node.target], node.iter, "in"
                elif isinstance(node, ast.Assign):
                    targets, value, how = node.targets, node.value, "="
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets, value, how = [node.target], node.value, "="
                else:
                    continue
                for leaf in (leaf for target in targets for leaf in ast.walk(target)):
                    if isinstance(leaf, ast.Name) and leaf.id == name and isinstance(leaf.ctx, ast.Store):
                        followed.add(id(leaf))
                        found.append((f"{name} {how} {ast.unparse(value)}", value, node))
            if any(_binds(node, name) and id(node) not in followed for node in _own(scope)):
                return None
            if found:
                return None if isinstance(scope, ast.ClassDef) else found
        scope = parents.get(id(scope))
    return None


def _values(expression, site, parents, declared, seen):
    """What the names in ``expression`` hold where ``site`` is, each bound
    value followed from where it is bound. An expression without names needs
    no note: its source is already counted."""
    notes = []
    for name in sorted({leaf.id for leaf in ast.walk(expression) if isinstance(leaf, ast.Name)}):
        bound = _bound(name, site, parents, declared)
        if bound is None:
            notes.append(f"{name} {UNRESOLVED}")
            continue
        for note, value, statement in sorted(bound, key=lambda item: item[0]):
            if (name, id(statement)) in seen:
                continue
            seen.add((name, id(statement)))
            notes.append(note)
            notes.extend(_values(value, statement, parents, declared, seen))
    return notes


def statements_in(source):
    """Destructive SQL in one Python source text, whitespace normalised, each
    followed by what its substituted names hold. Docstrings and comments are
    not SQL the application runs."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    declared = {name for node in ast.walk(tree) if isinstance(node, (ast.Global, ast.Nonlocal)) for name in node.names}
    for node in ast.walk(tree):  # TABLES[1:] = ..., TABLES.x = ...: the value changes without a binding
        if isinstance(node, (ast.Subscript, ast.Attribute)) and not isinstance(node.ctx, ast.Load):
            while isinstance(node, (ast.Subscript, ast.Attribute)):
                node = node.value
            if isinstance(node, ast.Name):
                declared.add(node.id)
    docstrings = {id(node.value) for node in ast.walk(tree)
                  if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    inside = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
    found = []
    for node in ast.walk(tree):
        if id(node) in docstrings or id(node) in inside:
            continue
        text = _text(node)
        if not text:
            continue
        spans = {(_start(text, match.start()), _end(text, match.end()))
                 for pattern in (DESTRUCTIVE, EMPTIED) for match in pattern.finditer(text)}
        spans |= {(_start(text, match.start()), _end(text, match.end())) for match in REQUIRED_COLUMN.finditer(text)
                  if not re.search(r"\bDEFAULT\b", match.group(0), re.IGNORECASE)}
        substituted = ([part for part in node.values if isinstance(part, ast.FormattedValue)]
                       if isinstance(node, ast.JoinedStr) else [])
        for start, end in sorted(spans):
            statement, seen, notes = " ".join(text[start:end].split()), set(), []
            for part in substituted:
                shown = "{" + ast.unparse(part.value) + "}"
                if shown not in text[start:end]:
                    continue
                if part.conversion != -1 or part.format_spec is not None:
                    notes.append(f"{shown} formatted {UNRESOLVED}")
                notes.extend(note for note in _values(part.value, node, parents, declared, seen) if note not in notes)
            found.append(statement + (" -- " + "; ".join(notes) if notes else ""))
    return found


def _start(text, index):
    return max(text.rfind(";", 0, index), text.rfind("\n\n", 0, index), -1) + 1


def _end(text, index):
    ends = [position for position in (text.find(";", index), text.find("\n\n", index)) if position != -1]
    return min(ends) if ends else len(text)


def statements(root=None):
    """Every (file, statement) pair, sorted; a statement repeated in a file counts each time."""
    root = Path(root or ROOT)
    found = []
    for folder in SCANNED:
        for path in sorted((Path(root) / folder).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if "/tests/" in "/" + relative or path.is_symlink():
                continue
            try:
                source = path.read_text(encoding="utf8")
            except (OSError, UnicodeDecodeError):
                continue
            found.extend((relative, statement) for statement in statements_in(source))
    return sorted(found)


def digest(pairs):
    return hashlib.sha256("\n".join(f"{file}\0{statement}" for file, statement in pairs).encode()).hexdigest()


def read(text):
    """The declaration in a compatibility file's text; ValueError when malformed."""
    data = json.loads(text)
    if (not isinstance(data, dict) or data.get("format") != 1 or type(data.get("level")) is not int
            or not 0 < data["level"] < 2**31 or not isinstance(data.get("destructive"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", data["destructive"])):
        raise ValueError(f"{FILE} needs format 1, a positive integer level and a sha256 'destructive' digest")
    return data


def errors(current, actual, base=None, pairs=None):
    """Why the declaration is wrong, as messages; empty when it holds.
    ``base`` is the declaration on the branch this change merges into,
    ``pairs`` the statements, checked for names that could not be followed."""
    problems = [
        f"{file}: cannot tell what this statement removes or renames: {statement}. "
        f"Name the table or column in the statement, or in a plain constant or loop beside it."
        for file, statement in pairs or [] if UNRESOLVED in statement]
    if current["destructive"] != actual:
        problems.append(
            "A migration that removes, renames or retypes part of the database was added, changed or removed "
            f"(see --list), and {FILE} does not record it. Run: python scripts/ci/database_compatibility.py --pin "
            "(it raises the level, so this release's update keeps a copy of the database).")
    if base is not None:
        if current["level"] < base["level"]:
            problems.append(f"{FILE} lowers the level from {base['level']} to {current['level']}; a level never goes down.")
        elif current["destructive"] != base["destructive"] and current["level"] == base["level"]:
            problems.append(
                f"{FILE} records different destructive migrations than the base branch at the same level "
                f"{current['level']}. Raise the level: python scripts/ci/database_compatibility.py --pin")
    return problems


def base_declaration(ref, root=None):
    """The declaration at ``ref``, or None when that revision has no file."""
    root = root or ROOT
    result = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{FILE}"], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        present = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                                 capture_output=True, text=True, timeout=30)
        if present.returncode != 0:
            raise ValueError(f"Unknown base revision {ref}")
        return None
    return read(result.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pin", action="store_true", help="record the current statements; raises the level when they changed")
    parser.add_argument("--list", action="store_true", help="print the statements counted")
    parser.add_argument("--base", help="a revision to compare with: a changed pin must raise the level")
    args = parser.parse_args(argv)
    pairs = statements()
    if args.list:
        for file, statement in pairs:
            print(f"{file}: {statement}")
        return 0
    path = ROOT / FILE
    current = read(path.read_text())
    actual = digest(pairs)
    if args.pin:
        if current["destructive"] != actual:
            current = {"format": 1, "level": current["level"] + 1, "destructive": actual}
            path.write_text(json.dumps(current, indent=2) + "\n")
            print(f"{FILE}: level {current['level']}, {len(pairs)} destructive statements pinned")
        else:
            print(f"{FILE}: unchanged (level {current['level']})")
        return 0
    problems = errors(current, actual, base_declaration(args.base) if args.base else None, pairs)
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"{FILE}: level {current['level']}, {len(pairs)} destructive statements match the pin")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
