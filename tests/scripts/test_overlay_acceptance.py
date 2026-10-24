"""The runtime-overlay acceptance harness (scripts/release/overlay_acceptance.py, #194)."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest

from docker import application_updates as updates

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("overlay_acceptance",
                                              ROOT / "scripts/release/overlay_acceptance.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def archive(path: Path, entries: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as output:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            output.addfile(info, io.BytesIO(data))
    return path


def test_descriptor_bounds_the_whole_tar_stream_the_updater_reads(tmp_path: Path) -> None:
    # A real runtime has thousands of members; their headers count against the
    # signed unpacked_size, so a bound of file bytes alone is refused.
    entries = {f"python/lib/module_{index}.py": b"x" for index in range(300)}
    entries["python/bin/python3"] = b"#!/bin/sh\n"
    path = archive(tmp_path / "runtime.tar.gz", entries)
    item = harness.describe(path, "python", "python-3.13", "linux-x86_64", "python/bin/python3")
    assert item["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert item["size"] == path.stat().st_size
    overlays = updates.RuntimeOverlays(tmp_path / "store", threading.Event())
    overlays.unpack(path, (tmp_path / "ok").resolve(), item)
    too_small = dict(item, unpacked_size=sum(len(data) for data in entries.values()))
    (tmp_path / "small").mkdir()
    with pytest.raises(updates.UpdateError, match="decompressed size"):
        overlays.unpack(path, (tmp_path / "small").resolve(), too_small)


def test_defect_overlay_is_the_same_runtime_plus_a_marker(tmp_path: Path) -> None:
    source = archive(tmp_path / "runtime.tar.gz",
                     {"python/bin/python3": b"#!/bin/sh\n", "python/lib/os.py": b"# os"})
    defect = harness.with_defect(source, "python/bin/python3", tmp_path / "defect.tar.gz")
    with tarfile.open(source) as original, tarfile.open(defect) as changed:
        assert changed.getnames() == original.getnames() + ["python/ACCEPTANCE-DEFECT"]
        assert changed.extractfile("python/lib/os.py").read() == b"# os"
    assert defect.read_bytes() != source.read_bytes()


@pytest.mark.skipif(sys.platform != "linux", reason="executes a Linux ELF")
def test_foreign_binary_names_the_other_processor_and_does_not_run(tmp_path: Path) -> None:
    binary = harness.foreign_binary()
    machine = int.from_bytes(binary[18:20], "little")
    assert machine == (183 if platform.machine().lower() in ("x86_64", "amd64") else 62)
    path = tmp_path / "python3"
    path.write_bytes(binary)
    path.chmod(0o755)
    try:
        result = subprocess.run([str(path)], capture_output=True, timeout=30)
    except OSError:
        return  # ENOEXEC: the kernel refused it
    assert result.returncode != 0  # an emulator registered for that processor refused it


@pytest.mark.skipif(sys.platform != "linux", reason="loads the Linux container supervisor")
@pytest.mark.parametrize("outcome", ["passed", "setup-failed"])
def test_run_removes_only_its_own_directory_inside_work(
        tmp_path: Path, outcome: str) -> None:
    work = tmp_path / "work"
    work.mkdir()
    saved = work / "candidate.tar.gz"
    saved.write_bytes(b"the caller's archive")
    args = SimpleNamespace(docker_dir=ROOT / "docker", overlay=saved, capability="python-test",
                           executable="python/bin/python3", work=work, keep=False, timeout=5.0)
    run = harness.Acceptance(args)
    assert run.work.parent == work.resolve()

    def setup() -> None:
        # What an installed overlay leaves: read-only payload directories.
        payload = run.work / "data/application-updates/runtime-overlays/x/payload/lib"
        payload.mkdir(parents=True)
        (payload / "os.py").write_text("# stdlib")
        payload.chmod(0o555)
        payload.parent.chmod(0o555)
        if outcome == "setup-failed":
            raise RuntimeError("synthetic setup failure")
        run.db = SimpleNamespace(query=lambda sql: "16", close=lambda: None)
        run.candidate = {"sha256": "0" * 64, "url": "https://overlays.invalid/x"}

    run.setup = setup
    run.apply = run.rollback = run.refuse = lambda: {}
    report = run.run()
    assert report["passed"] is (outcome == "passed")
    assert ("setup_error" in report) is (outcome == "setup-failed")
    assert sorted(work.iterdir()) == [saved]
    assert saved.read_bytes() == b"the caller's archive"


def postgres_16() -> bool:
    initdb = shutil.which("initdb")
    if not initdb:
        return False
    version = subprocess.run([initdb, "--version"], capture_output=True, text=True).stdout
    return " 16." in version


@pytest.mark.skipif(os.environ.get("OVERLAY_ACCEPTANCE") != "1" or sys.platform != "linux"
                    or not postgres_16() or os.geteuid() == 0,
                    reason="opt-in (OVERLAY_ACCEPTANCE=1): PostgreSQL 16, non-root Linux user")
def test_harness_passes_every_path_with_this_interpreter_as_the_overlay(tmp_path: Path) -> None:
    # The interpreter binary alone, as the updater tests use it: it finds this
    # host's standard library, which is enough to exercise every path.
    base = Path(sys._base_executable).resolve()
    overlay = archive(tmp_path / "python.tar.gz", {"python/bin/python3": base.read_bytes()})
    code = harness.main(["--docker-dir", str(ROOT / "docker"), "--overlay", str(overlay),
                         "--capability", "python-test", "--work", str(tmp_path / "work"),
                         "--timeout", "180"])
    assert code == 0
