"""Linux and macOS file-fence tests, also runnable directly with stdlib unittest."""
import errno
import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from unittest.mock import patch

# Direct execution supports a native confinement probe without app dependencies.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.core import chat_files as files


DARWIN = sys.platform == "darwin"


@unittest.skipUnless(DARWIN, "macOS kernel probe")
class DarwinBackendTests(unittest.TestCase):
    def test_backend_is_available_with_kernel_enforced_flags(self):
        # The fence tests below skip when unavailable; on macOS that is a failure.
        self.assertTrue(files.available())
        flags = files._darwin_flags()
        self.assertTrue(flags & files._NOFOLLOW_ANY)
        release = tuple(int(n) for n in platform.mac_ver()[0].split(".")[:2])
        if release >= (15, 4):
            self.assertTrue(flags & files._RESOLVE_BENEATH)
        if release >= (26, 0):
            self.assertTrue(flags & files._UNIQUE)

    def test_unenforced_flag_fails_closed(self):
        # A kernel that ignores O_NOFOLLOW_ANY must disable folders, not fall back.
        files._darwin_flags.cache_clear()
        try:
            with patch.object(files, "_NOFOLLOW_ANY", 0):
                self.assertIsNone(files._darwin_flags())
                root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(files.AccessDenied):
                        files.confined_open(root, "tmp", os.O_RDONLY)
                finally:
                    os.close(root)
        finally:
            files._darwin_flags.cache_clear()
        self.assertIsNotNone(files._darwin_flags())


@unittest.skipUnless(files.available(), "Linux openat2 or the macOS descriptor walk required")
class ChatFileFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS temp paths sit behind the /var symlink, which open_root refuses.
        self.base = Path(os.path.realpath(self.temp.name))
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
        (self.root / "sub").mkdir()
        (self.root / "sub" / "file").write_text("inside")
        (self.root / "inner").symlink_to("sub", target_is_directory=True)
        os.link(self.outside, self.root / "hard")
        for path in ("sym", "dir/outside", "hard", "../outside", str(self.outside),
                     "inner/file", "sub/../sub/file", "./sub/file", "sub//file"):
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

    @unittest.skipIf(DARWIN, "Linux /proc mount")
    def test_kernel_blocks_mount_crossing(self):
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            # /proc is a separate mount in the Linux environments we support.
            with self.assertRaises(OSError) as error:
                files.confined_open(root, "proc", os.O_RDONLY | os.O_DIRECTORY)
            self.assertEqual(error.exception.errno, errno.EXDEV)
        finally:
            os.close(root)

    @unittest.skipUnless(DARWIN, "macOS volumes and firmlinks")
    def test_mount_and_firmlink_crossing_is_refused(self):
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            # devfs and the Data volume are separate mounts below the system volume.
            for path in ("dev", "System/Volumes/Data"):
                with self.subTest(path=path), self.assertRaises(OSError) as error:
                    files.confined_open(root, path, os.O_RDONLY | os.O_DIRECTORY)
                self.assertEqual(error.exception.errno, errno.EXDEV)
            # Firmlinks such as /Users resolve onto the Data volume.
            crossed = [name for name in ("Users", "Applications", "private")
                       if os.stat("/" + name).st_dev != os.fstat(root).st_dev]
            self.assertTrue(crossed)
            for name in crossed:
                with self.subTest(firmlink=name), self.assertRaises(OSError) as error:
                    files.confined_open(root, name, os.O_RDONLY | os.O_DIRECTORY)
                self.assertEqual(error.exception.errno, errno.EXDEV)
            # Same-volume components remain reachable from the same root.
            os.close(files.confined_open(root, "System", os.O_RDONLY | os.O_DIRECTORY))
        finally:
            os.close(root)

    @unittest.skipUnless(DARWIN, "macOS resolves one component per openat")
    def test_directory_moved_out_during_walk_is_refused(self):
        operations = {
            "read": lambda: files.read(self.fd, "sub/deeper/file"),
            "list": lambda: files.listing(self.fd, "sub/deeper"),
            "write": lambda: files.write(self.fd, "sub/deeper/new", "bad"),
        }
        real_open = os.open
        for label, operation in operations.items():
            with self.subTest(operation=label):
                moved = self.base / ("moved-" + label)
                (self.root / "sub" / "deeper").mkdir(parents=True)
                (self.root / "sub" / "deeper" / "file").write_text("moved")
                raced = False
                def race(name, *args, **kwargs):
                    nonlocal raced
                    # The walk already holds "sub"; move it out before the next step.
                    if name == "deeper" and not raced:
                        raced = True
                        (self.root / "sub").rename(moved)
                    return real_open(name, *args, **kwargs)
                with patch.object(files.os, "open", side_effect=race):
                    with self.assertRaises(OSError) as error:
                        operation()
                self.assertTrue(raced)
                self.assertEqual(error.exception.errno, errno.EXDEV)
                self.assertFalse((moved / "deeper" / "new").exists())

    @unittest.skipUnless(DARWIN, "macOS kernel link checks")
    def test_kernel_refuses_links_before_userspace_checks(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "file").write_text("inside")
        os.link(self.outside, self.root / "hard")
        (self.root / "leaf").symlink_to("sub/file")
        (self.root / "inner").symlink_to("sub", target_is_directory=True)
        cases = [("leaf", errno.ELOOP), ("inner/file", errno.ELOOP)]
        if files._darwin_flags() & files._UNIQUE:
            cases.append(("hard", None))
        for path, code in cases:
            with self.subTest(path=path), self.assertRaises(OSError) as error:
                os.close(files.confined_open(self.fd, path, os.O_RDONLY | os.O_NONBLOCK))
            if code is not None:
                self.assertEqual(error.exception.errno, code)
        # A new name next to a dangling link is still created exclusively.
        (self.root / "dangling").symlink_to(self.base / "absent")
        with self.assertRaises(FileExistsError):
            files.confined_open(self.fd, "dangling", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.assertFalse((self.base / "absent").exists())

    @unittest.skipUnless(DARWIN, "macOS resolves one component per openat")
    def test_component_swapped_to_symlink_during_walk_is_refused(self):
        (self.root / "sub").mkdir()
        real_open = os.open
        swapped = False
        def race(name, *args, **kwargs):
            nonlocal swapped
            if name == "sub" and not swapped:
                swapped = True
                (self.root / "sub").rename(self.root / "old")
                (self.root / "sub").symlink_to(self.base, target_is_directory=True)
            return real_open(name, *args, **kwargs)
        with patch.object(files.os, "open", side_effect=race):
            with self.assertRaises(OSError) as error:
                files.write(self.fd, "sub/outside", "bad")
        self.assertTrue(swapped)
        self.assertEqual(error.exception.errno, errno.ELOOP)
        self.assertEqual(self.outside.read_text(), "do not change")

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
