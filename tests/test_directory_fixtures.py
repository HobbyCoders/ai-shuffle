"""A command line that leaves a folder and comes back keeps its conftest fixtures.

Runs pytest in a subprocess on a throwaway tree laid out like the failing
``tests/core/network`` + ``tests/core`` invocation, with the repository's hook
from tests/_directory_fixtures.py installed in the tree's root conftest.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _tree(root: Path, hook: bool) -> None:
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "conftest.py").write_text(
        "from tests._directory_fixtures import pytest_make_collect_report  # noqa: F401\n" if hook else "")
    pkg = root / "outer" / "inner"
    pkg.mkdir(parents=True)
    (root / "outer" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "conftest.py").write_text(
        "import pytest\n"
        "SEEN = []\n"
        "@pytest.fixture\n"
        "def thing():\n"
        "    return 42\n"
        "@pytest.fixture(autouse=True)\n"
        "def _mark():\n"
        "    SEEN.append(1)\n"
        "    yield\n"
        "    SEEN.pop()\n")
    body = (
        "from outer.inner.conftest import SEEN\n"
        "def test_it(thing):\n"
        "    assert thing == 42 and SEEN == [1]\n")
    (pkg / "test_first.py").write_text(body)
    (pkg / "test_last.py").write_text(body)
    (root / "outer" / "test_middle.py").write_text("def test_middle():\n    pass\n")


def _run(root: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), str(root)])}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         "outer/inner/test_first.py", "outer/test_middle.py", "outer/inner/test_last.py"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False)


def test_re_entered_directory_keeps_its_conftest_fixtures(tmp_path):
    _tree(tmp_path, hook=True)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout
