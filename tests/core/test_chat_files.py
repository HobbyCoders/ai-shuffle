"""Linux file-fence tests, also runnable directly with stdlib unittest."""
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# Direct execution supports a WSL/Linux confinement probe without app dependencies.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.core import chat_files as files


@unittest.skipUnless(files.available(), "Linux openat2 required")
class ChatFileFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "grant"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.write_text("do not change")
        self.fd = files.open_root(str(self.root))

    def tearDown(self):
        os.close(self.fd)
        self.temp.cleanup()

    def test_read_list_write(self):
        (self.root / "sub").mkdir()
        files.write(self.fd, "sub/file", "hello")
        self.assertEqual(files.read(self.fd, "sub/file"), "hello")
        self.assertEqual(files.listing(self.fd, "sub"), ["file"])
        files.write(self.fd, "sub/file", "new")
        self.assertEqual(files.read(self.fd, "sub/file"), "new")

    def test_links_and_traversal(self):
        (self.root / "sym").symlink_to(self.outside)
        (self.root / "dir").symlink_to(self.base, target_is_directory=True)
        os.link(self.outside, self.root / "hard")
        for path in ("sym", "dir/outside", "hard", "../outside", str(self.outside)):
            with self.subTest(path=path):
                with self.assertRaises((files.AccessDenied, OSError)):
                    files.read(self.fd, path)
                with self.assertRaises((files.AccessDenied, OSError)):
                    files.write(self.fd, path, "bad")
        self.assertEqual(self.outside.read_text(), "do not change")

    def test_symlink_swapped_after_write_validation_is_replaced_not_followed(self):
        leaf = self.root / "file"
        leaf.write_text("old")
        rename = os.rename
        def race(src, dst, **kwargs):
            leaf.unlink()
            leaf.symlink_to(self.outside)
            rename(src, dst, **kwargs)
        with patch.object(files.os, "rename", side_effect=race):
            files.write(self.fd, "file", "replacement")
        self.assertFalse(leaf.is_symlink())
        self.assertEqual(leaf.read_text(), "replacement")
        self.assertEqual(self.outside.read_text(), "do not change")

    def test_kernel_blocks_mount_crossing(self):
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            # /proc is a separate mount in the Linux environments we support.
            with self.assertRaises(OSError) as error:
                files.confined_open(root, "proc", os.O_RDONLY | os.O_DIRECTORY)
            self.assertEqual(error.exception.errno, errno.EXDEV)
        finally:
            os.close(root)

    def test_special_files_and_bounds(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(files.AccessDenied):
            files.read(self.fd, "pipe")
        with self.assertRaises(files.AccessDenied):
            files.write(self.fd, "pipe", "no")
        (self.root / "huge").write_bytes(b"x" * (files.LIMIT + 1))
        with self.assertRaises(files.AccessDenied):
            files.read(self.fd, "huge")
        with self.assertRaises(files.AccessDenied):
            files.write(self.fd, "huge", "x" * (files.LIMIT + 1))

    def test_unsupported_backend_has_no_fallback(self):
        with patch.object(files, "available", return_value=False):
            with self.assertRaises(files.AccessDenied):
                files.open_root(str(self.root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
