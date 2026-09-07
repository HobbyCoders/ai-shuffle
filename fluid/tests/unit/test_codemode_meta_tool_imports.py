"""Importing a top-level tool from inside a cell must correct, not just fail.

Seven of nine audited grok sessions opened a batched cell with
``from tools.read_tool_docs import read_tool_docs`` (one also reached for
``tools.search_tools``). ``_missing_message`` already had the right correction,
but it only fired on ATTRIBUTE access — the import path went through the finder,
found nothing, and the whole cell died on a bare ModuleNotFoundError.

Same shape for a tool the prompt advertises that this host never mounted: the
model has no way to tell "absent capability" from "typo" unless the error says
so, and retries a name no spelling will reach.
"""

import sys

import pytest

from fluid.codemode.vfs import (
    CAPABILITY_TOOL_REASONS,
    TOP_LEVEL_TOOL_NAMES,
    install_finder,
    missing_leaf_message,
)

_READ_STUB = {"Read": '"""Read a file."""\ndef Read(**kw):\n    return "ok"\n'}


@pytest.fixture
def vfs_finder():
    """Install the finder over a one-tool VFS and put sys.meta_path back."""
    saved = list(sys.meta_path)
    saved_modules = dict(sys.modules)
    try:
        yield install_finder(dict(_READ_STUB), lambda *a, **k: None)
    finally:
        sys.meta_path[:] = saved
        for name in list(sys.modules):
            if name == "tools" or name.startswith(("tools.", "skills.")):
                sys.modules.pop(name, None)
        sys.modules.update(
            {k: v for k, v in saved_modules.items() if k not in sys.modules}
        )


@pytest.mark.parametrize("source", [
    "from tools.read_tool_docs import read_tool_docs",
    "from tools import read_tool_docs",
    "import tools.read_tool_docs",
    "from tools import search_tools",
    "from tools.search_tools import search_tools",
    "from tools import list_tools",
])
def test_importing_a_catalog_tool_says_it_is_a_top_level_call(vfs_finder, source):
    with pytest.raises(ImportError) as excinfo:
        exec(compile(source, "<cell>", "exec"), {})
    message = str(excinfo.value)
    assert "top-level tool call" in message
    assert "cannot be reached from inside a cell by any import" in message
    assert "dir(tools)" in message


def test_importing_an_unmounted_capability_tool_names_the_capability(vfs_finder):
    with pytest.raises(ImportError) as excinfo:
        exec(compile("from tools import SendMessage", "<cell>", "exec"), {})
    message = str(excinfo.value)
    assert "SendMessage is not available on this host" in message
    assert "no agent messaging capability" in message
    assert "no import spelling will reach it" in message


def test_a_mounted_tool_still_imports_every_spelling(vfs_finder):
    ns: dict = {}
    exec(compile(
        "from tools import Read\n"
        "import tools\n"
        "from tools.Read import Read as Read2\n"
        "out = (Read is tools.Read, Read2.__name__)",
        "<cell>", "exec",
    ), ns)
    assert ns["out"] == (True, "Read")


def test_an_ordinary_unknown_name_keeps_the_plain_failure(vfs_finder):
    # Not a name fluid can say anything specific about: the finder declines,
    # and Python's own "cannot import name" stands. Raising here would turn
    # every probing import into a hard error.
    with pytest.raises(ImportError) as excinfo:
        exec(compile("from tools import Weather", "<cell>", "exec"), {})
    assert "cannot import name" in str(excinfo.value)


def test_attribute_access_and_import_give_the_same_correction(vfs_finder):
    import tools  # noqa: PLC0415 — the VFS package, installed by the fixture

    with pytest.raises(AttributeError) as attr:
        tools.read_tool_docs
    with pytest.raises(ImportError) as imp:
        exec(compile("import tools.read_tool_docs", "<cell>", "exec"), {})
    assert str(attr.value) == str(imp.value)




def test_skills_package_keeps_its_own_wording():
    assert "dir(skills)" in missing_leaf_message("skills", "anything")
    assert "skill 'anything'" in missing_leaf_message("skills", "anything")


def test_capability_table_matches_the_prompt_table():
    """The kernel's copy is a literal (it loads with no fluid imports), so a
    capability added to the prompt table has to be reflected here or the kernel
    goes back to a bare ModuleNotFoundError for it."""
    from fluid.cli.prompt import TOOL_CAPABILITIES

    assert set(CAPABILITY_TOOL_REASONS) == set(TOOL_CAPABILITIES)




def test_kernel_projected_tools_are_not_listed_as_top_level():
    # save_skill/delete_skill ARE mounted under `tools`, so `from tools import
    # save_skill` is the correct spelling — telling a model otherwise would
    # break the continual harness's own advice.
    assert "save_skill" not in TOP_LEVEL_TOOL_NAMES
    assert "delete_skill" not in TOP_LEVEL_TOOL_NAMES
    assert TOP_LEVEL_TOOL_NAMES == frozenset(
        {"list_tools", "search_tools", "read_tool_docs"}
    )
