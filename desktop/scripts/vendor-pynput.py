"""Reproduce the metadata-only native pynput wheel; never patch installed packages.

Input: PyPI pynput 1.8.2 wheel, verified against its published SHA256.
Output: local-version wheel, unchanged code/license, consistent evdev dependency.
Usage: python desktop/scripts/vendor-pynput.py <upstream.whl> <output-directory>
"""
import base64
import csv
import hashlib
import io
from pathlib import Path
import sys
import zipfile

UPSTREAM_SHA256 = "8cc38cf13a6ab2749cb375678be8a0fd705d7ce49c8001ff5db4007a723bbef1"
VERSION = "1.8.2+shuffle1"
WHEEL = f"pynput-{VERSION}-py2.py3-none-any.whl"


def vendor(upstream: Path, output: Path) -> Path:
    data = upstream.read_bytes()
    if hashlib.sha256(data).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Upstream pynput wheel hash mismatch")
    old = "pynput-1.8.2.dist-info/"
    new = f"pynput-{VERSION}.dist-info/"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {name.replace(old, new): archive.read(name) for name in archive.namelist()
                 if not name.endswith("/RECORD")}
    metadata = files[new + "METADATA"].decode()
    dependency = 'Requires-Dist: evdev>=1.3; "linux" in sys_platform'
    if metadata.count(dependency) != 1 or metadata.count("Version: 1.8.2\n") != 1:
        raise ValueError("Unexpected upstream pynput metadata")
    metadata = metadata.replace("Version: 1.8.2\n", f"Version: {VERSION}\n").replace(
        dependency,
        dependency + ' and platform_machine != "x86_64"\n'
        'Requires-Dist: evdev-binary==2.0.0; "linux" in sys_platform and platform_machine == "x86_64"',
    )
    files[new + "METADATA"] = metadata.encode()
    files[new + "SHUFFLE_PATCH.txt"] = (
        "Metadata-only local revision of upstream pynput 1.8.2.\n"
        f"Upstream wheel SHA256: {UPSTREAM_SHA256}\n"
        "Code and LGPL license are unchanged. Linux x64 uses evdev-binary;\n"
        "other Linux architectures retain the original evdev dependency.\n"
        "Reproduction: desktop/scripts/vendor-pynput.py in the signed source.\n"
    ).encode()
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, content in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow([name, "sha256=" + digest, len(content)])
    writer.writerow([new + "RECORD", "", ""])
    files[new + "RECORD"] = record.getvalue().encode()
    output.mkdir(parents=True, exist_ok=True)
    target = output / WHEEL
    # Stored members and fixed headers avoid zlib/version/time-dependent output.
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return target


if __name__ == "__main__":
    target = vendor(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {target}")
