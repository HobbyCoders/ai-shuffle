"""Read an APK's package name without aapt (no build-tools on the worker).

An APK is a zip whose ``AndroidManifest.xml`` is Android's binary XML: a
string pool, then element chunks. The package name is the ``package``
attribute of the first ``<manifest>`` element.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path
from typing import List

MAX_MANIFEST_BYTES = 8 * 1024 * 1024

_RES_STRING_POOL = 0x0001
_RES_XML = 0x0003
_RES_XML_START_ELEMENT = 0x0102
_UTF8_FLAG = 0x100
_TYPE_STRING = 0x03


class ApkError(ValueError):
    pass


def _strings(buf: bytes, start: int) -> List[str]:
    _type, header_size, _size = struct.unpack_from("<HHI", buf, start)
    count, _styles, flags, strings_start, _styles_start = struct.unpack_from("<IIIII", buf, start + 8)
    offsets = struct.unpack_from(f"<{count}I", buf, start + header_size)
    base = start + strings_start
    utf8 = bool(flags & _UTF8_FLAG)
    out = []
    for offset in offsets:
        pos = base + offset
        if utf8:
            # utf-16 length, then utf-8 length, each 1 or 2 bytes.
            pos += 2 if buf[pos] & 0x80 else 1
            length = buf[pos]
            if length & 0x80:
                length = ((length & 0x7F) << 8) | buf[pos + 1]
                pos += 2
            else:
                pos += 1
            out.append(buf[pos:pos + length].decode("utf-8", "replace"))
        else:
            length = struct.unpack_from("<H", buf, pos)[0]
            pos += 2
            if length & 0x8000:
                length = ((length & 0x7FFF) << 16) | struct.unpack_from("<H", buf, pos)[0]
                pos += 2
            out.append(buf[pos:pos + length * 2].decode("utf-16-le", "replace"))
    return out


def manifest_package(manifest: bytes) -> str:
    """The ``package`` attribute of a binary AndroidManifest.xml."""
    try:
        kind, header_size, _size = struct.unpack_from("<HHI", manifest, 0)
        if kind != _RES_XML:
            raise ApkError("AndroidManifest.xml is not binary XML")
        pos = header_size
        strings: List[str] = []
        while pos + 8 <= len(manifest):
            chunk_type, chunk_header, chunk_size = struct.unpack_from("<HHI", manifest, pos)
            if chunk_size < 8:
                break
            if chunk_type == _RES_STRING_POOL:
                strings = _strings(manifest, pos)
            elif chunk_type == _RES_XML_START_ELEMENT:
                _ns, name, attr_start, attr_size, attr_count = struct.unpack_from("<IIHHH", manifest, pos + 16)
                if strings and strings[name] == "manifest":
                    first = pos + 16 + attr_start
                    for i in range(attr_count):
                        at = first + i * attr_size
                        _a_ns, a_name, raw, _vsize, _res0, vtype, data = struct.unpack_from("<IIIHBBI", manifest, at)
                        if strings[a_name] != "package":
                            continue
                        index = raw if raw != 0xFFFFFFFF else (data if vtype == _TYPE_STRING else None)
                        if index is None:
                            break
                        return strings[index]
                    raise ApkError("the manifest has no package attribute")
            pos += chunk_size
    except (struct.error, IndexError) as exc:
        raise ApkError(f"AndroidManifest.xml could not be read: {exc}") from None
    raise ApkError("the manifest has no <manifest> element")


def apk_package(path: Path) -> str:
    """The package name of the APK at ``path``; raises ApkError if it is not an APK."""
    try:
        with zipfile.ZipFile(path) as zf:
            try:
                info = zf.getinfo("AndroidManifest.xml")
            except KeyError:
                raise ApkError("not an APK: it has no AndroidManifest.xml") from None
            if info.file_size > MAX_MANIFEST_BYTES:
                raise ApkError("AndroidManifest.xml is too large")
            return manifest_package(zf.read(info))
    except zipfile.BadZipFile:
        raise ApkError("not an APK: the file is not a zip archive") from None
