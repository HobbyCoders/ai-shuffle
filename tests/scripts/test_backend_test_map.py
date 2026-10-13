"""Backend test selection picks the tests a change reaches and fails safe."""
import gzip
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("backend_test_map", ROOT / "scripts/ci/backend_test_map.py")
tm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tm)

FILLER = [f"tests/core/test_filler_{n:03}.py" for n in range(120)]
A_TESTS = ["tests/core/test_a.py", "tests/api/test_a_api.py"]
B_TESTS = ["tests/core/test_b.py"]
SHARED_TESTS = ["tests/core/test_shared.py"]

BASE = {
    "app/a.py": "LIMIT = 1\n\ndef f():\n    return 1\n\ndef g():\n    return 2\n\nclass Box:\n    size = 1\n    def open(self):\n        return self.size\n",
    "app/b.py": "from app.a import LIMIT\n\ndef uses_limit():\n    return LIMIT\n",
    "app/shared.py": "def setup():\n    return 1\n",
    "app/unmapped.py": "def lonely():\n    return 1\n",
    "app/prompts/system.md": "hello\n",
    "app/prompt_loader.py": "def load():\n    return open('system.md').read()\n",
    "tests/conftest.py": "def helper():\n    return 1\n",
}


def head_files(**changes):
    files = {**BASE, **{t: "" for t in FILLER + A_TESTS + B_TESTS + SHARED_TESTS}}
    files.update(changes)
    return files


def fake_map():
    tests = sorted(FILLER + A_TESTS + B_TESTS + SHARED_TESTS)
    index = {t: i for i, t in enumerate(tests)}
    covered = {
        "app/a.py": {"f": [index[t] for t in A_TESTS], "g": [index[B_TESTS[0]]],
                     "Box.open": [index[FILLER[0]]], "<module>": [index[t] for t in tests]},
        "app/b.py": {"uses_limit": [index[B_TESTS[0]]]},
        "app/prompt_loader.py": {"load": [index[FILLER[1]]]},
    }
    return {
        "version": tm.VERSION, "commit": "c" * 40, "tests": tests, "covered": covered,
        "shared": {"app/shared.py": ["setup"]},
        "known": {"app/a.py": ["Box", "Box.open", "f", "g"], "app/b.py": ["uses_limit"],
                  "app/shared.py": ["setup"], "app/prompt_loader.py": ["load"]},
    }


def select(changed, **files):
    return tm.select(changed, fake_map(), head_files(**files), BASE.get)


def test_a_changed_function_selects_only_the_tests_that_ran_it():
    source = BASE["app/a.py"].replace("return 1\n\ndef g", "return 3\n\ndef g")
    chosen, _ = select(["app/a.py"], **{"app/a.py": source})
    assert chosen == sorted(A_TESTS)


def test_a_changed_method_selects_its_tests():
    source = BASE["app/a.py"].replace("return self.size", "return self.size + 1")
    assert select(["app/a.py"], **{"app/a.py": source})[0] == [FILLER[0]]


def test_a_changed_module_constant_follows_its_importers():
    source = BASE["app/a.py"].replace("LIMIT = 1", "LIMIT = 2")
    assert select(["app/a.py"], **{"app/a.py": source})[0] == B_TESTS


def test_a_new_function_alone_needs_no_test():
    source = BASE["app/a.py"] + "\ndef added():\n    return 4\n"
    assert select(["app/a.py"], **{"app/a.py": source})[0] == []


def test_a_changed_test_file_runs_itself():
    assert select([B_TESTS[0]], **{B_TESTS[0]: "def test_x():\n    pass\n"})[0] == B_TESTS


def test_a_non_python_file_selects_the_tests_of_code_that_reads_it():
    chosen, reasons = select(["app/prompts/system.md"])
    assert chosen == [FILLER[1]]
    assert any("system.md" in reason for reason in reasons)


GUIDE = "docs/guide.md"


@pytest.mark.parametrize("addition", [
    '\ndef h():\n    """Reads nothing; see docs/guide.md."""\n    return 0\n',
    "\n# docs/guide.md explains LIMIT\n",
    '\nprint(note="Read docs/guide.md before changing LIMIT.")\n',
    '\nclass Note:\n    text = "see docs/guide.md for the rules"\n',
], ids=["docstring", "comment", "import-time-prose", "class-prose"])
def test_a_file_named_only_in_prose_selects_nothing(addition):
    # app.a runs in every mapped test: reading it file-wide would be the suite.
    chosen, reasons = select([GUIDE], **{"app/a.py": BASE["app/a.py"] + addition})
    assert chosen == []
    assert any("0 of them in code" in reason for reason in reasons)


def test_a_file_read_in_a_function_selects_only_that_functions_tests():
    source = BASE["app/a.py"].replace("def g():\n    return 2", f"def g():\n    return open('{GUIDE}')")
    assert select([GUIDE], **{"app/a.py": source})[0] == B_TESTS


def test_a_file_named_by_an_fstring_path_counts_as_read():
    source = BASE["app/a.py"].replace("def g():\n    return 2", "def g(root):\n    return open(f\"{root}/guide.md\")")
    assert select(["docs/guide.md"], **{"app/a.py": source})[0] == B_TESTS


def test_a_module_constant_naming_a_file_follows_its_users():
    source = f"GUIDE = '{GUIDE}'\n\ndef uses_limit():\n    return open(GUIDE).read()\n"
    assert select([GUIDE], **{"app/b.py": source})[0] == B_TESTS


@pytest.mark.parametrize("addition", [
    f"\nopen('{GUIDE}')\n",
    f"\n@register('{GUIDE}')\ndef h():\n    return 0\n",
    f"\nclass Loader:\n    path = '{GUIDE}'\n",
], ids=["import-time-read", "decorator", "class-attribute"])
def test_a_file_read_at_import_runs_every_test_of_the_reader(addition):
    result, reasons = select([GUIDE], **{"app/a.py": BASE["app/a.py"] + addition})
    assert result == "all"
    assert "of" in reasons[0]


def test_a_reader_that_does_not_parse_is_read_file_wide():
    assert select([GUIDE], **{"app/a.py": BASE["app/a.py"] + f"\nx = '{GUIDE}'\ndef broken(:\n"})[0] == "all"


@pytest.mark.parametrize("changed, files, reason", [
    (["requirements.txt"], {}, "dependencies"),
    ([".github/workflows/ci-backend.yml"], {}, "dependencies"),
    (["app/shared.py"], {"app/shared.py": "def setup():\n    return 2\n"}, "session-wide"),
    (["tests/conftest.py"], {"tests/conftest.py": "def helper():\n    return 2\n"}, "conftest"),
    (["app/a.py"], {"app/a.py": "def f(:\n"}, "does not parse"),
    (["app/unmapped.py"], {"app/unmapped.py": "def lonely():\n    return 2\n"}, "no test names it"),
])
def test_changes_the_map_cannot_see_through_run_everything(changed, files, reason):
    result, reasons = select(changed, **files)
    assert result == "all"
    assert reason in reasons[0]


def test_a_module_level_statement_change_runs_every_test_of_the_file():
    source = BASE["app/a.py"] + "\nprint('import side effect')\n"
    result, reasons = select(["app/a.py"], **{"app/a.py": source})
    # Every mapped test imported app.a: that is most of the suite.
    assert result == "all"
    assert "of" in reasons[0]


def test_no_map_runs_everything():
    assert tm.select(["app/a.py"], None, head_files(), BASE.get) == ("all", ["no test map from a full run on main"])


def test_a_small_or_foreign_map_is_refused(tmp_path):
    path = tmp_path / "map.json.gz"
    small = {**fake_map(), "tests": FILLER[:10]}
    path.write_bytes(gzip.compress(json.dumps(small).encode()))
    with pytest.raises(tm.FullSuite):
        tm.load_map(path)


@pytest.mark.parametrize("path, expected", [
    ("app/core/x.py", "app.core.x"),
    ("fluid/fluid/context/peer.py", "fluid.context.peer"),
    ("app/core/__init__.py", "app.core"),
    ("fluid/tests/unit/test_x.py", None),
    ("app/prompts/system.md", None),
])
def test_module_names(path, expected):
    assert tm.module_name(path) == expected


@pytest.mark.parametrize("path, expected", [
    ("tests/core/test_x.py", True),
    ("tests/test_top.py", True),
    ("tests/core/helpers.py", False),
    ("tests/integration/test_slow.py", False),
    ("tests/core/test_chat_shell.py", False),
])
def test_fast_subset_matches_the_workflow(path, expected):
    assert tm.fast_subset(path) is expected


def test_fast_subset_dirs_match_the_workflow():
    workflow = (ROOT / ".github/workflows/ci-backend.yml").read_text(encoding="utf-8")
    for directory in tm.FAST_SUBSET_DIRS:
        assert f"{directory}/" in workflow or f"{directory} " in workflow
    for excluded in tm.FAST_SUBSET_EXCLUDED:
        assert excluded in workflow


def test_merge_combines_worker_records(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app/a.py").write_text(BASE["app/a.py"], encoding="utf-8")
    monkeypatch.setattr(tm, "ROOT", repo)
    hits = tmp_path / "hits"
    hits.mkdir()
    (hits / "hits-gw0-1.json").write_text(json.dumps({"worker": "gw0", "hits": {
        "tests/core/test_a.py": [["app/a.py", "f"]],
        tm.SHARED: [["app/a.py", "g"]]}}), encoding="utf-8")
    (hits / "hits-gw1-2.json").write_text(json.dumps({"worker": "gw1", "hits": {
        "tests/core/test_b.py": [["app/a.py", "f"], ["app/a.py", "Box.open"]]}}), encoding="utf-8")
    out = tmp_path / "map.json.gz"
    tm.merge(hits, out, "abc")
    with gzip.open(out, "rt", encoding="utf-8") as handle:
        result = json.load(handle)
    assert result["tests"] == ["tests/core/test_a.py", "tests/core/test_b.py"]
    assert result["covered"]["app/a.py"] == {"f": [0, 1], "Box.open": [1]}
    assert result["shared"] == {"app/a.py": ["g"]}
    assert result["known"]["app/a.py"] == ["Box", "Box.open", "f", "g"]


def _recorded_helper():
    def nested():
        return 1
    return nested()


@pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")
def test_recorder_attributes_calls_to_the_running_test_file(tmp_path):
    from tests import _test_map

    if _test_map._recorder is not None:
        pytest.skip("this run is recording the map itself")
    recorder = _test_map.Recorder(str(tmp_path))
    recorder.start()
    try:
        recorder.switch("tests/core/test_example.py")
        _recorded_helper()
        recorder.switch(None)
    finally:
        recorder.stop()
    recorder.write()
    data = json.loads(next(tmp_path.glob("hits-*.json")).read_text(encoding="utf-8"))
    hits = {tuple(hit) for hit in data["hits"]["tests/core/test_example.py"]}
    # The nested function folds into the function that defines it.
    assert ("tests/scripts/test_backend_test_map.py", "_recorded_helper") in hits
    assert all(not qualname.endswith("nested") for _, qualname in hits)



def test_a_source_dir_file_no_code_names_selects_the_code_beside_it():
    # Code beside a file may load it by folder; a comment naming it is no read.
    sibling = "def load():\n    # app/prompts/other.md is read with the rest of the folder\n    return 1\n"
    test_map = fake_map()
    test_map["covered"]["app/prompts/sibling.py"] = {"load": [test_map["tests"].index(B_TESTS[0])]}
    test_map["known"]["app/prompts/sibling.py"] = ["load"]
    chosen, reasons = tm.select(["app/prompts/other.md"], test_map,
                                head_files(**{"app/prompts/sibling.py": sibling}), BASE.get)
    assert chosen == B_TESTS
    assert any("beside" in reason for reason in reasons)
