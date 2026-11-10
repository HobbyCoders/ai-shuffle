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
what that variable holds: a literal, a loop over literals, or a constant in the
same file (the retired-table tuples in app/db/database.py). A name the script
cannot follow (a function parameter, an import, an attribute) fails the check:
name the table in the statement or in a constant beside it.

What it cannot see: SQL joined with + or built by format() or %, and data
rewrites (UPDATE or DELETE that changes what a column means). A change like
that raises the level by hand: edit "level" in the file.
"""
from __future__ import annotations

import argparse
import ast
import builtins
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


# Marks a name in a destructive statement whose value is not in the file.
UNRESOLVED = "<not in this file>"


def _text(node):
    """The literal text of a string or f-string node, each substituted value
    shown as its source ({table}), or None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                       else "{" + ast.unparse(part.value) + "}" for part in node.values)
    return None


def _bound(name, node, parents):
    """The source of every value ``name`` can take where ``node`` is: loops and
    assignments in the enclosing functions, then the module. Empty when the
    name is a parameter, an import or otherwise not followable."""
    scope = node
    while scope is not None:
        scope = parents.get(id(scope))
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            continue
        found = []
        for inner in ast.walk(scope):
            if isinstance(inner, (ast.For, ast.AsyncFor)):
                targets, value, how = [inner.target], inner.iter, "in"
            elif isinstance(inner, ast.Assign):
                targets, value, how = inner.targets, inner.value, "="
            elif isinstance(inner, ast.AnnAssign) and inner.value is not None:
                targets, value, how = [inner.target], inner.value, "="
            else:
                continue
            if any(isinstance(leaf, ast.Name) and leaf.id == name for target in targets for leaf in ast.walk(target)):
                found.append((f"{name} {how} {ast.unparse(value)}", value))
        if found:
            return found
        if not isinstance(scope, ast.Module) and any(
                argument.arg == name for argument in ast.walk(scope.args) if isinstance(argument, ast.arg)):
            return []
    return []


def _values(expression, node, parents, seen):
    """What the names in ``expression`` hold, followed through constants. An
    expression without names needs no note: its source is already counted."""
    notes = []
    for leaf in ast.walk(expression):
        if not isinstance(leaf, ast.Name) or leaf.id in seen or hasattr(builtins, leaf.id):
            continue
        seen.add(leaf.id)
        bound = _bound(leaf.id, node, parents)
        if not bound:
            notes.append(f"{leaf.id} {UNRESOLVED}")
        for note, value in sorted(bound, key=lambda item: item[0]):
            notes.append(note)
            notes.extend(_values(value, node, parents, seen))
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
        substituted = ({"{" + ast.unparse(part.value) + "}": part.value for part in node.values
                        if isinstance(part, ast.FormattedValue)} if isinstance(node, ast.JoinedStr) else {})
        for start, end in sorted(spans):
            statement, seen, notes = " ".join(text[start:end].split()), set(), []
            for shown, expression in substituted.items():
                if shown in text[start:end]:
                    notes.extend(_values(expression, node, parents, seen))
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
        f"{file}: cannot tell what this statement removes or renames, because a name in it is not set in the "
        f"file: {statement}. Name the table or column in the statement, or in a constant beside it."
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
