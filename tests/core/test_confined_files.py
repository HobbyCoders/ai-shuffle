"""Real filesystem confinement probes; runnable directly on Windows and Linux."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.core import confined_files as files


class ConfinedFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.secret = self.outside / 'secret.txt'
        self.secret.write_text('outside marker')

    def tearDown(self):
        self.temp.cleanup()

    def link(self, source, target, directory=False):
        if os.name == 'nt' and directory:
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(source), str(target)],
                           check=True, capture_output=True)
        else:
            try:
                source.symlink_to(target, target_is_directory=directory)
            except OSError as exc:
                self.skipTest(f'Symlink unavailable: {exc}')

    @unittest.skipUnless(os.name == 'nt', 'Windows DOS aliases required')
    def test_short_root_spelling_uses_same_pinned_directory(self):
        import ctypes
        from ctypes import wintypes
        short_path = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
        short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        short_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        self.assertGreater(short_path(str(self.root), buffer, len(buffer)), 0)
        short = Path(buffer.value)
        inside = files.upload(short, '', 'file.txt', b'inside')
        with files.open_attachment(short, inside.resolve()) as stream:
            self.assertEqual(stream.read(), b'inside')

    def test_duplicate_and_hardlink_destination_are_not_overwritten(self):
        os.link(self.secret, self.root / 'file.txt')
        new = files.upload(self.root, '', 'file.txt', b'new')
        self.assertEqual(new.name, 'file_1.txt')
        self.assertEqual(self.secret.read_text(), 'outside marker')
        self.assertEqual(new.read_bytes(), b'new')
        with self.assertRaises((ValueError, OSError)):
            with files.open_attachment(self.root, self.root / 'file.txt'):
                self.fail('hardlink opened')

    def test_dangling_destination(self):
        missing = self.outside / 'absent'
        self.link(self.root / 'file.txt', missing)
        new = files.upload(self.root, '', 'file.txt', b'new')
        self.assertEqual(new.name, 'file_1.txt')
        self.assertFalse(missing.exists())

    def test_parent_link_or_junction(self):
        link = self.root / 'sub'
        self.link(link, self.outside, directory=True)
        try:
            with self.assertRaises((ValueError, OSError)):
                files.upload(self.root, 'sub', 'new.txt', b'bad')
            with self.assertRaises((ValueError, OSError)):
                with files.open_attachment(self.root, link / 'secret.txt'):
                    self.fail('outside opened')
            self.assertFalse((self.outside / 'new.txt').exists())
        finally:
            if os.name == 'nt':
                link.rmdir()  # Remove only the junction, never its target.

    def test_in_directory_alias(self):
        inside = files.upload(self.root, 'sub', 'file.txt', b'inside')
        alias = self.root / 'alias'
        self.link(alias, inside.parent, directory=True)
        try:
            with files.open_attachment(self.root, alias / inside.name) as stream:
                self.assertEqual(stream.read(), b'inside')
        finally:
            if os.name == 'nt':
                alias.rmdir()

    def test_traversal_and_nt_aliases(self):
        for path in ('../outside', '/absolute', 'a/../b', 'a\\b', 'a:', 'a.', 'NUL'):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                files.upload(self.root, path, 'new.txt', b'bad')
        for name in ('../secret.txt', 'x/y', 'file:stream', 'NUL.txt', 'file.'):
            with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                files.upload(self.root, '', name, b'bad')

    def test_concurrent_duplicate_uploads(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda i: files.upload(self.root, 'nested', 'same.txt', str(i).encode()), range(24)))
        self.assertEqual(len(set(paths)), 24)
        self.assertEqual({p.read_text() for p in paths}, {str(i) for i in range(24)})

    def test_leaf_replacement_before_create(self):
        original = files._windows_handle if os.name == 'nt' else files.chat_files.confined_open
        raced = False
        def opening(*args, **kwargs):
            nonlocal raced
            creating = kwargs.get('create_new', False) if os.name == 'nt' else args[2] & os.O_CREAT
            if creating and not raced:
                raced = True
                os.link(self.secret, self.root / 'file.txt')
            return original(*args, **kwargs)
        target = 'app.core.confined_files._windows_handle' if os.name == 'nt' else 'app.core.chat_files.confined_open'
        with patch(target, side_effect=opening):
            new = files.upload(self.root, '', 'file.txt', b'new')
        self.assertTrue(raced)
        self.assertEqual(new.name, 'file_1.txt')
        self.assertEqual(self.secret.read_text(), 'outside marker')

    def test_parent_replacement_before_create(self):
        sub = self.root / 'sub'
        sub.mkdir()
        original = files._windows_handle if os.name == 'nt' else files.chat_files.confined_open
        raced = False
        blocked = False
        def opening(*args, **kwargs):
            nonlocal raced, blocked
            creating = kwargs.get('create_new', False) if os.name == 'nt' else args[2] & os.O_CREAT
            if creating and not raced:
                raced = True
                try:
                    sub.rename(self.root / 'old')
                except PermissionError:
                    blocked = True
                else:
                    self.link(sub, self.outside, directory=True)
            return original(*args, **kwargs)
        target = 'app.core.confined_files._windows_handle' if os.name == 'nt' else 'app.core.chat_files.confined_open'
        try:
            with patch(target, side_effect=opening):
                if os.name == 'nt':
                    files.upload(self.root, 'sub', 'new.txt', b'new')
                    self.assertTrue(blocked)
                else:
                    with self.assertRaises(OSError):
                        files.upload(self.root, 'sub', 'new.txt', b'new')
            self.assertTrue(raced)
            self.assertFalse((self.outside / 'new.txt').exists())
        finally:
            if os.name == 'nt' and sub.is_junction():
                sub.rmdir()

    def test_read_replacement_after_resolution(self):
        leaf = files.upload(self.root, '', 'file.txt', b'inside')
        resolve = Path.resolve
        raced = False
        def resolving(path, *args, **kwargs):
            nonlocal raced
            result = resolve(path, *args, **kwargs)
            if path == leaf and not raced:
                raced = True
                leaf.unlink()
                os.link(self.secret, leaf)
            return result
        with patch.object(Path, 'resolve', resolving):
            with self.assertRaises((ValueError, OSError)):
                with files.open_attachment(self.root, leaf):
                    self.fail('replacement hardlink opened')
        self.assertTrue(raced)


if __name__ == '__main__':
    unittest.main()
