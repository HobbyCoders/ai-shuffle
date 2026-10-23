"""Static boundary check shared by focused tests; never reads process env."""
import ast
import re
from pathlib import Path

PREFIX = r"(?:AI_SHUFFLE_|FLUID_|SHUFFLE_|AIH_|AIHUB_|AIS_|_AIS_|VITE_)"
NAME = re.compile(r"\b" + PREFIX + r"[A-Z][A-Z0-9_]*\b")
ROOTS = ("app", "fluid/fluid", "browser-mcp", "tools", "desktop", "frontend", "docker", "scripts")
EXTENSIONS = {".py", ".js", ".ts", ".mjs", ".cjs", ".svelte", ".sh", ".ps1", ".json", ".yml"}
SKIP = {"node_modules", ".git", ".svelte-kit", "dist", "build", "tests", "__pycache__", "__tests__"}


def runtime_files(root):
    for directory in ROOTS:
        for path in (root / directory).rglob("*"):
            if path.is_file() and path.suffix in EXTENSIONS and not SKIP.intersection(path.relative_to(root).parts):
                if not any(part.endswith((".test.ts", ".spec.ts", ".test.js", ".spec.js")) for part in path.parts):
                    yield path
    for name in ("entrypoint.sh", "docker-compose.yml", "Dockerfile"):
        yield root / name


def own_names(source):
    # Conservative by design: includes literal helper arguments, JS/Python env
    # attributes, shell expansions and child exports, not just os.getenv calls.
    return set(NAME.findall(source))


def basesettings_names(source):
    tree = ast.parse(source)
    names = set()
    base_names = {"BaseSettings"}
    base_names.update(alias.asname or alias.name for node in ast.walk(tree)
                      if isinstance(node, ast.ImportFrom) and node.module == "pydantic_settings"
                      for alias in node.names if alias.name == "BaseSettings")
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    # Follow local derived classes as well as aliased/qualified BaseSettings.
    def is_settings(base):
        return (isinstance(base, ast.Name) and base.id in base_names
                or isinstance(base, ast.Attribute) and base.attr == "BaseSettings")
    for _ in classes:
        base_names.update(node.name for node in classes if any(is_settings(base) for base in node.bases))
    for node in classes:
        if not any(is_settings(base) for base in node.bases):
            continue
        for field in node.body:
            if not isinstance(field, ast.AnnAssign) or not isinstance(field.target, ast.Name):
                continue
            if "ClassVar" in ast.unparse(field.annotation):
                continue
            names.add(field.target.id.upper())
            if isinstance(field.value, ast.Call):
                for kw in field.value.keywords:
                    if kw.arg in {"alias", "validation_alias"}:
                        names.update(n.value.upper() for n in ast.walk(kw.value)
                                     if isinstance(n, ast.Constant) and isinstance(n.value, str))
    return names


def discovered(root):
    for path in runtime_files(root):
        source = path.read_text(encoding="utf-8")
        names = own_names(source)
        if path.suffix == ".py":
            names |= basesettings_names(source)
        for name in names:
            yield path.relative_to(root).as_posix(), name
