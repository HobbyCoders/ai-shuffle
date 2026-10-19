"""Filesystem cleanup shared by Git and plugin installation."""

import os
import shutil
import stat


def force_rmtree(path):
    """Remove a tree, including Git's read-only files on Windows/Python 3.12."""
    def _on_error(_func, _path, _exc_info):
        os.chmod(_path, stat.S_IWRITE)
        os.unlink(_path)
    shutil.rmtree(path, onerror=_on_error)
