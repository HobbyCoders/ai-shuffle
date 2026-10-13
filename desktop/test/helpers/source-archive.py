"""Build/extract application ZIPs without executing archive-provided code."""
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import zipfile

MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 100000


def extract(archive, destination):
    root = Path(destination)
    if root.exists():
        raise ValueError("Extraction destination already exists")
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_FILES or sum(i.file_size for i in entries) > MAX_BYTES:
            raise ValueError("Application archive exceeds limits")
        names = set()
        for item in entries:
            if item.orig_filename != item.filename:
                raise ValueError('Unsafe application archive path normalization')
            parts = item.filename.rstrip('/').split('/')
            if any(not p or p in ('.', '..') or p.endswith((' ', '.'))
                   or re.search(r'[\\:<>"|?*\x00-\x1f]', p)
                   or re.match(r'(?i)^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)', p)
                   for p in parts):
                raise ValueError("Unsafe application archive path")
            key = '/'.join(parts).casefold()
            mode = item.external_attr >> 16
            if key in names or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("Duplicate path or special file in application archive")
            names.add(key)
            if item.flag_bits & 1:
                raise ValueError("Encrypted application archives are unsupported")
        root.mkdir(parents=True)
        try:
            for item in entries:
                target = root.joinpath(*item.filename.rstrip('/').split('/'))
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(item) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output)
                    if os.name != 'nt':
                        target.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
        except BaseException:
            shutil.rmtree(root)
            raise


def pack(source, archive):
    root = Path(source)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(root.rglob('*')):
            if item.is_symlink():
                raise ValueError("Application packages cannot contain symlinks")
            if item.is_file():
                bundle.write(item, item.relative_to(root).as_posix())


if __name__ == '__main__':
    operation, source, destination = sys.argv[1:]
    if operation == 'pack':
        pack(source, destination)
    elif operation == 'extract':
        extract(source, destination)
    else:
        raise ValueError("Expected pack or extract")
