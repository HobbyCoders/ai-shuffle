"""Filesystem cleanup shared by Git and plugin installation."""

import os
import shutil
import stat
import time


def force_rmtree(path, ignore_errors=False):
    """Remove a tree, including Git's read-only files on Windows/Python 3.12.

    A refused delete is retried once after clearing the read-only bit. With
    ``ignore_errors`` an entry that still cannot be removed is skipped and the
    rest of the tree is still removed, like ``shutil.rmtree(ignore_errors=True)``.
    """
    def _on_error(func, failed_path, exc):
        if isinstance(exc, FileNotFoundError):
            return  # already gone (or never there): the goal is met
        if isinstance(exc, PermissionError) and func in (os.unlink, os.remove, os.rmdir):
            try:
                mode = os.lstat(failed_path).st_mode
                if not stat.S_ISLNK(mode):  # chmod would follow a link
                    os.chmod(failed_path, mode | stat.S_IWRITE)
                func(failed_path)
                return
            except FileNotFoundError:
                return
            except OSError as retry_error:
                exc = retry_error
        if not ignore_errors:
            raise exc

    shutil.rmtree(path, onexc=_on_error)


# Windows refuses to open a file while another thread or process renames over
# it (MoveFileEx), and refuses that rename while the file is open without
# delete sharing: ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION or
# ERROR_LOCK_VIOLATION. POSIX never does; the hold lasts milliseconds.
_SHARING_WINERRORS = frozenset({5, 32, 33})
_SHARING_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.3)
_sleep = time.sleep


def is_sharing_violation(exc):
    """Whether ``exc`` is a transient Windows hold on a file, not a real failure."""
    return isinstance(exc, PermissionError) and getattr(exc, "winerror", None) in _SHARING_WINERRORS


def retry_sharing_violation(operation):
    """Run ``operation()``, retrying a Windows sharing violation for under a second.

    Any other error, and a hold that outlasts the retries, is raised as is.
    """
    for delay in _SHARING_RETRY_DELAYS:
        try:
            return operation()
        except PermissionError as exc:
            if not is_sharing_violation(exc):
                raise
            _sleep(delay)
    return operation()
