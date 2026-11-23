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
    # Reads every file of notes/2026/ without naming one.
    "tests/core/test_notes_walk.py": "def test_notes():\n    assert list((ROOT / \"notes/2026\").rglob(\"*.md\"))\n",
}
NOTES_WALKER = "tests/core/test_notes_walk.py"


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
GUIDE_TEST = "tests/core/test_guide.py"
GUIDE_TEXT = f"def test_guide():\n    assert (ROOT / '{GUIDE}').read_text()\n"


@pytest.mark.parametrize("addition", [
    '\ndef h():\n    """Reads nothing; see docs/guide.md."""\n    return 0\n',
    "\n# docs/guide.md explains LIMIT\n",
    '\nprint(note="Read docs/guide.md before changing LIMIT.")\n',
    '\nclass Note:\n    text = "see docs/guide.md for the rules"\n',
], ids=["docstring", "comment", "import-time-prose", "class-prose"])
def test_a_file_named_only_in_prose_selects_no_tests_of_that_code(addition):
    # app.a runs in every mapped test: reading it file-wide would be the suite.
    chosen, reasons = select([GUIDE], **{"app/a.py": BASE["app/a.py"] + addition, GUIDE_TEST: GUIDE_TEXT})
    assert chosen == [GUIDE_TEST]
    assert any("0 of 1 naming sources read it in code" in reason for reason in reasons)
    # With no test naming it, prose is no reader either: nothing says what reads it.
    assert select([GUIDE], **{"app/a.py": BASE["app/a.py"] + addition})[0] == "all"


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


@pytest.mark.parametrize("changed", [
    "conftest.py", "tests/conftest.py", "tests/core/network/conftest.py",
    # Pytest loads a conftest in a test* folder even with no test file beneath it.
    "tests/api/test_support/conftest.py",
    # Only the two named Fluid conftests are exempt, not every conftest under fluid/.
    "fluid/fluid/conftest.py", "fluid/tests/unit/conftest.py",
])
def test_any_conftest_runs_everything(changed):
    # The root conftest is outside the source folders; it reaches every test all the same.
    result, reasons = select([changed], **{changed: "import os\n"})
    assert result == "all" and reasons == [f"{changed} is a conftest"]
    # Even when only main changed it.
    result, reasons = tm.select([changed], fake_map(), head_files(**{changed: "import os\n"}), BASE.get,
                                main_only={changed})
    assert result == "all" and reasons == [f"{changed} is a conftest"]


@pytest.mark.parametrize("changed", sorted(tm.FLUID_CONFTESTS))
def test_a_fluid_conftest_is_a_file_like_any_other(changed):
    # The backend suite never loads it; CI Fluid runs the tests that do.
    result, reasons = select([changed], **{changed: "import os\n"})
    assert result == "all" and "no code reads it by name" in reasons[0]
    # Main changed it and nothing names it: it keeps the pull request's choice.
    source = BASE["app/a.py"].replace("return 1\n\ndef g", "return 3\n\ndef g")
    chosen, reasons = tm.select(["app/a.py", changed], fake_map(), head_files(**{"app/a.py": source}), BASE.get,
                                main_only={changed})
    assert chosen == sorted(A_TESTS)
    assert any(f"{changed}: only main changed it" in reason for reason in reasons)
    # Only its full path names it, never the bare name every conftest shares.
    named = {FILLER[5]: "def test_x():\n    assert 'conftest.py'\n"}
    assert select([changed], **{changed: "", **named})[0] == "all"
    assert select([changed], **{changed: "", FILLER[5]: f"def test_x():\n    assert '{changed}'\n"})[0] == [FILLER[5]]


def test_a_file_nothing_names_runs_everything():
    # Not named by any code or test, nor is any folder of it: nothing says it is safe.
    result, reasons = select(["ops/cron.d/backup"])
    assert result == "all" and "no code reads it by name" in reasons[0]


def test_a_file_a_test_lists_by_its_own_folder_selects_that_test():
    assert select(["notes/2026/today.md"])[0] == [NOTES_WALKER]


LISTER = "tests/core/test_tracked.py"
LISTER_TEXT = "import subprocess\n\ndef test_x():\n    subprocess.run(['git', 'ls-files'])\n"


def test_a_test_listing_the_whole_checkout_runs_but_never_narrows():
    # It reads every file, so it runs whenever a named or folder-listing test narrows the choice.
    assert select(["notes/2026/a.md"], **{LISTER: LISTER_TEXT})[0] == sorted([LISTER, NOTES_WALKER])
    # Alone it says nothing about this file: everything runs.
    assert select(["ops/cron.d/backup"], **{LISTER: LISTER_TEXT})[0] == "all"


def test_a_test_listing_a_top_level_folder_never_narrows():
    # A file outside notes/2026/ is not one NOTES_WALKER lists.
    result, reasons = select(["notes/a.md"])
    assert result == "all" and "no code reads it by name" in reasons[0]
    top = {"tests/core/test_frontend_walk.py": "def test_x():\n    list((ROOT / 'frontend').rglob('*'))\n"}
    assert select(["frontend/src/a.svelte"], **top)[0] == "all"


@pytest.mark.parametrize("path", ["frontend/package-lock.json", "frontend/package.json",
                                  "browser-mcp/npm-shrinkwrap.json", "fluid/pyproject.toml",
                                  "requirements-dev.txt", "docker/requirements-extra.txt", "tools/uv.lock"])
def test_a_package_manifest_or_lockfile_anywhere_runs_everything(path):
    # test_version_source builds the path to frontend/package-lock.json at run time.
    named = {"tests/core/test_version_source.py":
             "def test_x():\n    (ROOT / 'frontend' / ('package' + '-lock.json')).read_text()\n"}
    result, reasons = select([path], **named)
    assert result == "all" and ("manifest or lockfile" in reasons[0] or "dependencies" in reasons[0])


@pytest.mark.parametrize("path", ["tests/db/fixtures/schema_v88.sql", "tests/fixtures/x.json",
                                  "tests/core/__snapshots__/a.snap"])
def test_test_data_runs_everything(path):
    # A loader builds the name at run time (schema_v{n}.sql): no map follows it.
    named = {"tests/db/test_schema_lifecycle.py": "def test_x():\n    load('schema_v88.sql')\n"}
    result, reasons = select([path], **named)
    assert result == "all" and "test data" in reasons[0]


def test_a_name_that_is_not_utf8_runs_everything():
    assert select(["tests/core/test_\ufffd.py"])[0] == "all"


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
])
def test_fast_subset_matches_the_workflow(path, expected):
    assert tm.fast_subset(path) is expected


def test_fast_subset_dirs_match_the_workflow():
    workflow = (ROOT / ".github/workflows/ci-backend.yml").read_text(encoding="utf-8")
    for directory in tm.FAST_SUBSET_DIRS:
        assert f"{directory}/" in workflow or f"{directory} " in workflow
    for excluded in tm.FAST_SUBSET_EXCLUDED:
        assert excluded in workflow
    # A pull request's chosen files are passed by name, so --ignore cannot drop them:
    # the files the shards never run must never be chosen.
    for excluded in tm.SHARD_EXCLUDED:
        assert f"--ignore={excluded}" in workflow or f"--deselect={excluded}" in workflow
        assert not tm.in_shards(excluded)
    for directory in tm.SHARD_EXCLUDED_DIRS:
        assert f"--ignore={directory.rstrip('/')} " in workflow


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



def test_a_source_dir_file_no_code_names_runs_everything():
    # Code beside it may load it by folder, and so may code anywhere else: unknown.
    sibling = "def load():\n    # app/prompts/other.md is read with the rest of the folder\n    return 1\n"
    test_map = fake_map()
    test_map["covered"]["app/prompts/sibling.py"] = {"load": [test_map["tests"].index(B_TESTS[0])]}
    test_map["known"]["app/prompts/sibling.py"] = ["load"]
    chosen, reasons = tm.select(["app/prompts/other.md"], test_map,
                                head_files(**{"app/prompts/sibling.py": sibling}), BASE.get)
    assert chosen == "all"
    assert "no code reads it by name" in reasons[0]


# ---------------------------------------------------------------------------
# CI Backend: a pull request's shards (ci-select)
# ---------------------------------------------------------------------------

def test_a_full_run_uses_every_shard_and_names_no_files():
    assert tm.shard_plan("all", None) == {"selection": "all", "shards": list(range(1, 9)), "tests": []}


def test_tests_run_outside_the_shards_choose_no_shard():
    found = tm.shard_plan(["tests/scripts/test_ci_plan.py", "tests/core/test_docs_content.py"], fake_map())
    assert found == {"selection": "none", "shards": [], "tests": []}


def test_the_shards_are_sized_by_the_chosen_files_recorded_seconds():
    chosen = [f"tests/core/test_{n}.py" for n in range(10)]
    timed = {**fake_map(), "seconds": {path: 100.0 for path in chosen}}
    found = tm.shard_plan(chosen, timed)
    assert found["shards"] == [1, 2, 3, 4] and found["tests"] == chosen
    assert found["selection"].startswith("files-")
    # Unknown files count a fixed time each; never more shards than files or than a full run.
    assert tm.shard_plan(chosen[:2], fake_map())["shards"] == [1]
    assert tm.shard_plan(chosen, {**timed, "seconds": {p: 9999.0 for p in chosen}})["shards"] == list(range(1, 9))
    assert tm.shard_plan(chosen[:3], {**timed, "seconds": {p: 9999.0 for p in chosen}})["shards"] == [1, 2, 3]


def test_the_selection_names_exactly_the_chosen_files():
    one = tm.shard_plan(["tests/core/test_a.py"], fake_map())["selection"]
    assert one == tm.shard_plan(["tests/core/test_a.py", "tests/scripts/test_x.py"], fake_map())["selection"]
    assert one != tm.shard_plan(["tests/core/test_b.py"], fake_map())["selection"]


def run_git(repo, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def pull_request(tmp_path, monkeypatch):
    """A repository on main whose map was recorded at main's first commit."""
    repo = tmp_path / "repo"
    for path, text in head_files().items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text, encoding="utf-8")
    run_git(tmp_path, "init", "-q", "-b", "main", str(repo))
    run_git(repo, "config", "user.email", "t@example.com")
    run_git(repo, "config", "user.name", "t")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "base")
    recorded = run_git(repo, "rev-parse", "HEAD")
    map_path = tmp_path / "map.json.gz"
    map_path.write_bytes(gzip.compress(json.dumps({**fake_map(), "commit": recorded}).encode()))
    monkeypatch.setattr(tm, "ROOT", repo)
    monkeypatch.chdir(repo)

    run_git(repo, "branch", "pr")

    def commit(branch, path, text):
        run_git(repo, "checkout", "-q", branch)
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text, encoding="utf-8")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", path)

    def merge():
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")

    return repo, map_path, commit, merge


PR = {"GITHUB_EVENT_NAME": "pull_request"}
CHANGED_F = BASE["app/a.py"].replace("return 1\n\ndef g", "return 3\n\ndef g")


def test_a_pull_request_runs_the_tests_of_what_it_changed(pull_request):
    _, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", CHANGED_F)
    commit("main", "notes/2026/main.md", "main moved on\n")
    merge()
    found, reasons = tm.ci_select(map_path, PR)
    expected = sorted(A_TESTS + [NOTES_WALKER])
    assert found["tests"] == expected and found["shards"] == [1]
    assert found["selection"] == tm.shard_plan(expected, None)["selection"]
    assert reasons[0].startswith("Test map from ")


def test_a_pull_request_also_runs_the_tests_of_what_main_changed_since_the_map(pull_request):
    _, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", CHANGED_F)
    commit("main", "app/b.py", BASE["app/b.py"].replace("return LIMIT", "return LIMIT + 1"))
    merge()
    found, _ = tm.ci_select(map_path, PR)
    assert found["tests"] == sorted(A_TESTS + B_TESTS)


UNREAD_DOC = "misc/deep/unread.md"


@pytest.mark.parametrize("path, text", [
    (".github/workflows/ci-backend.yml", "jobs: {}\n"),
    (UNREAD_DOC, "nothing reads this\n"),
    # The Fluid harness's own conftest, which no backend run loads (PR #1943's case).
    ("fluid/tests/conftest.py", "import os\n"),
])
def test_ci_plumbing_or_an_unread_file_only_main_changed_keeps_the_selection(pull_request, path, text):
    # Main's full run tested it; it cannot change which tests reach the pull request's files.
    _, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", CHANGED_F)
    commit("main", path, text)
    merge()
    found, reasons = tm.ci_select(map_path, PR)
    assert found["selection"] != "all", reasons
    assert set(A_TESTS) <= set(found["tests"])
    assert "1 of them only main changed since" in reasons[0]


@pytest.mark.parametrize("path, text", [
    (".github/workflows/ci-backend.yml", "jobs: {}\n"),
    (UNREAD_DOC, "nothing reads this\n"),
])
def test_the_pull_request_changing_ci_plumbing_or_an_unread_file_runs_everything(pull_request, path, text):
    _, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", CHANGED_F)
    commit("pr", path, text)
    merge()
    assert tm.ci_select(map_path, PR)[0]["selection"] == "all"


@pytest.mark.parametrize("path, text", [
    ("requirements.txt", "requests==2\n"),
    ("tests/conftest.py", "X = 1\n"),
    ("tests/data/fixture.json", "{}\n"),
])
def test_dependencies_conftests_and_test_data_main_changed_still_run_everything(pull_request, path, text):
    # Only CI plumbing and unread files are let through; these still run everything.
    _, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", CHANGED_F)
    commit("main", path, text)
    merge()
    assert tm.ci_select(map_path, PR)[0]["selection"] == "all"


def test_main_only_python_nothing_names_still_runs_everything(pull_request):
    # Python outside the mapped folders is not an unread file: something may import it.
    _, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", CHANGED_F)
    commit("main", "tools/research/probe.py", "X = 1\n")
    merge()
    assert tm.ci_select(map_path, PR)[0]["selection"] == "all"


def test_the_pull_request_setting_a_file_back_to_the_maps_version_still_counts(pull_request):
    # Main adds the workflow after the map; the pull request removes it again,
    # so the change since the map no longer holds it, but the pull request changes it.
    repo, map_path, commit, merge = pull_request
    commit("main", ".github/workflows/ci-backend.yml", "jobs: {}\n")
    commit("main", UNREAD_DOC, "nothing reads this\n")
    commit("pr", "app/a.py", CHANGED_F)
    run_git(repo, "merge", "-q", "-m", "sync", "main")
    run_git(repo, "rm", "-q", ".github/workflows/ci-backend.yml")
    run_git(repo, "commit", "-q", "-m", "revert workflow")
    merge()
    found, reasons = tm.ci_select(map_path, PR)
    assert found["selection"] == "all" and "ci-backend.yml" in reasons[1]


def test_a_file_both_changed_counts_as_the_pull_requests(pull_request):
    _, map_path, commit, merge = pull_request
    commit("main", UNREAD_DOC, "main's text\n")
    commit("pr", UNREAD_DOC, "the pull request's text\n")
    run_git(map_path.parent / "repo", "checkout", "-q", "pr")
    run_git(map_path.parent / "repo", "merge", "-q", "-X", "theirs", "-m", "sync", "main")
    commit("pr", UNREAD_DOC, "the pull request's final text\n")
    merge()
    assert tm.ci_select(map_path, PR)[0]["selection"] == "all"


def test_a_pull_request_no_shard_test_reaches_runs_no_shard(pull_request):
    _, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", BASE["app/a.py"] + "\ndef new():\n    return 4\n")
    merge()
    found, _ = tm.ci_select(map_path, PR)
    assert found == {"selection": "none", "shards": [], "tests": []}


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch", ""])
def test_every_run_but_a_pull_request_runs_everything(pull_request, event):
    _, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", CHANGED_F)
    merge()
    assert tm.ci_select(map_path, {"GITHUB_EVENT_NAME": event})[0]["selection"] == "all"


def test_doubt_runs_everything(pull_request, tmp_path):
    repo, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", CHANGED_F)
    # Not a merge commit.
    assert tm.ci_select(map_path, PR)[0]["selection"] == "all"
    merge()
    assert tm.ci_select(tmp_path / "absent.json.gz", PR)[0]["selection"] == "all"
    broken = tmp_path / "broken.json.gz"
    broken.write_bytes(b"not gzip")
    assert tm.ci_select(broken, PR)[0]["selection"] == "all"
    # A map recorded on a commit main does not have (here: the pull request's own).
    foreign = tmp_path / "foreign.json.gz"
    foreign.write_bytes(gzip.compress(json.dumps({**fake_map(), "commit": run_git(repo, "rev-parse", "pr")}).encode()))
    found, reasons = tm.ci_select(foreign, PR)
    assert found["selection"] == "all" and "not on main" in reasons[0]
    unknown = tmp_path / "unknown.json.gz"
    unknown.write_bytes(gzip.compress(json.dumps({**fake_map(), "commit": "f" * 40}).encode()))
    assert tm.ci_select(unknown, PR)[0]["selection"] == "all"


def test_a_file_name_with_a_line_break_runs_everything(pull_request):
    _, map_path, commit, merge = pull_request
    commit("pr", "app/odd\nname.md", "x\n")
    merge()
    found, reasons = tm.ci_select(map_path, PR)
    assert found["selection"] == "all" and "line break" in reasons[0]


def test_a_non_ascii_test_file_name_is_read_and_chosen(pull_request):
    # git quotes such a name in its plain output; it would then match no file.
    _, map_path, commit, merge = pull_request
    commit("pr", "tests/unit/test_caf\u00e9.py", "def test_x():\n    pass\n")
    merge()
    found, _ = tm.ci_select(map_path, PR)
    assert found["tests"] == ["tests/unit/test_caf\u00e9.py"] and found["shards"] == [1]


def test_a_change_to_how_the_shards_run_runs_everything(pull_request):
    _, map_path, commit, merge = pull_request
    commit("pr", "scripts/ci/backend_shards.py", "SHARD_COUNT = 8\n")
    merge()
    assert tm.ci_select(map_path, PR)[0]["selection"] == "all"


def test_ci_select_writes_the_three_outputs(pull_request, tmp_path, capsys):
    import io
    _, map_path, commit, merge = pull_request
    commit("pr", "app/a.py", CHANGED_F)
    merge()
    out, summary = io.StringIO(), tmp_path / "summary.md"
    found = tm.write_ci_select(map_path, {**PR, "GITHUB_STEP_SUMMARY": str(summary)}, out)
    assert out.getvalue().splitlines() == [
        f"selection={found['selection']}", "shards=[1]", "count=1", f"tests={json.dumps(sorted(A_TESTS))}"]
    assert "main runs every test after the merge" in summary.read_text(encoding="utf-8")


def test_merge_reads_each_shards_records_and_times(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    monkeypatch.setattr(tm, "ROOT", repo)
    for shard, test in ((1, "tests/core/test_a.py"), (2, "tests/core/test_b.py")):
        (tmp_path / "hits" / f"backend-map-hits-{shard}").mkdir(parents=True)
        (tmp_path / "hits" / f"backend-map-hits-{shard}" / "hits-gw0-1.json").write_text(
            json.dumps({"worker": "gw0", "hits": {test: [["app/a.py", "f"]]}}), encoding="utf-8")
        (tmp_path / "shards" / f"backend-shard-{shard}").mkdir(parents=True)
        (tmp_path / "shards" / f"backend-shard-{shard}" / "durations.json").write_text(json.dumps(
            {f"{test}::test_one": 1.5, f"{test}::test_two[x]": 2.0, f"{test}::bad": "n/a"}), encoding="utf-8")
    result = tm.merge(tmp_path / "hits", tmp_path / "map.json.gz", "abc", tmp_path / "shards")
    assert result["tests"] == ["tests/core/test_a.py", "tests/core/test_b.py"]
    assert result["seconds"] == {"tests/core/test_a.py": 3.5, "tests/core/test_b.py": 3.5}
