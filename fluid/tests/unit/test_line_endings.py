"""Edit and Write must never change a file's line endings (Windows included)."""

from fluid.tools.builtins.edit import handler as edit
from fluid.tools.builtins.write import handler as write
from fluid.tools.registry import ToolCtx


def _ctx(tmp_path):
    ctx = ToolCtx(cwd=str(tmp_path))
    return ctx


async def test_edit_keeps_crlf(tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_bytes(b"x = 1\r\ny = 2\r\n")
    ctx = _ctx(tmp_path)
    ctx.read_files.add(str(f))
    out = await edit({"file_path": "a.py", "old_string": "y = 2", "new_string": "y = 3"}, ctx)
    assert not out.is_error
    assert f.read_bytes() == b"x = 1\r\ny = 3\r\n"


async def test_edit_keeps_lf_on_every_platform(tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_bytes(b"x = 1\ny = 2\n")
    ctx = _ctx(tmp_path)
    ctx.read_files.add(str(f))
    await edit({"file_path": "a.py", "old_string": "x = 1\n", "new_string": "x = 0\nz = 9\n"}, ctx)
    assert f.read_bytes() == b"x = 0\nz = 9\ny = 2\n"


async def test_edit_accepts_crlf_in_old_string(tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_bytes(b"a\r\nb\r\n")
    ctx = _ctx(tmp_path)
    ctx.read_files.add(str(f))
    out = await edit({"file_path": "a.py", "old_string": "a\r\nb", "new_string": "c"}, ctx)
    assert not out.is_error and f.read_bytes() == b"c\r\n"


async def test_write_new_file_is_lf_unless_content_is_crlf(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    await write({"file_path": "n.txt", "content": "one\ntwo\n"}, ctx)
    assert (tmp_path / "n.txt").read_bytes() == b"one\ntwo\n"
    await write({"file_path": "c.txt", "content": "one\r\ntwo\r\n"}, ctx)
    assert (tmp_path / "c.txt").read_bytes() == b"one\r\ntwo\r\n"


async def test_write_existing_file_keeps_its_ending(tmp_path) -> None:
    f = tmp_path / "e.txt"
    f.write_bytes(b"old\r\n")
    ctx = _ctx(tmp_path)
    await write({"file_path": "e.txt", "content": "new\nlines\n"}, ctx)
    assert f.read_bytes() == b"new\r\nlines\r\n"
