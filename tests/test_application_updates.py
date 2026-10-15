import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import zipfile

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core import application_updates as policy
from docker.application_updates import (ContainerUpdates, UpdateError, build_environment, extract_source,
                                        tree_lines, verify_manifest)

A, B, C = "a" * 40, "b" * 40, "c" * 40
RUNTIME = "d" * 64


def test_real_venvs_after_overlay_handover_use_image_python_and_survive_cleanup(feed, monkeypatch):
    from docker import application_updates as module
    updater, publish, *_ = feed
    base = Path(module.IMAGE_PYTHON).resolve()
    # A small real interpreter binary, using this test host's stdlib. No runtime
    # download or pip/npm install: only venv creation and interpreter execution.
    artifact = runtime_artifact("python", "test", executable="python/bin/python3",
                                entries={"python/bin/python3": base.read_bytes()})
    entries = {f"ai-shuffle-{B}/requirements.lock": "# fixture", **{
        f"ai-shuffle-{B}/{name}": "# verified" for name in module.HANDOVER_FILES}}
    offer_runtimes(feed, [artifact], entries=entries)
    monkeypatch.setattr(updater, "build", ContainerUpdates.build.__get__(updater))
    interpreters = []

    def run(command, cwd, env):
        if command[1:3] == ["-m", "venv"]:
            interpreters.append(command[0])
            subprocess.run([*command, "--without-pip"], env=env, check=True, capture_output=True)
        if command == ["npm", "run", "build"] and cwd.name == "frontend":
            (cwd / "build").mkdir(parents=True)
            (cwd / "build/index.html").write_text("built")

    monkeypatch.setattr(updater, "run_build", run)

    def activate():
        updater.check(download=True)
        updater.launch({}, activate=True)
        updater.activating()
        updater.healthy()

    activate()
    _, supervisor, _ = module.handover_target(updater.root, updater.image, updater.public_key)
    actual_base = subprocess.check_output([str(supervisor), "-c", "import sys; print(sys._base_executable)"], text=True).strip()
    assert artifact[0]["sha256"] in actual_base
    monkeypatch.setattr(module.sys, "executable", str(supervisor))
    monkeypatch.setenv("SHUFFLE_HANDED_OVER", B)
    for sequence, revision in ((3, C), (4, "d" * 40)):
        publish(revision=revision, sequence=sequence, container={"requires": ["base-1"]},
                entries={f"ai-shuffle-{revision}/requirements.lock": "# fixture"})
        activate()
    assert interpreters[1:] == [module.IMAGE_PYTHON, module.IMAGE_PYTHON]
    # A subsequent cold start no longer uses the old supervisor. Its overlay
    # and source can now leave retention without dangling either retained venv.
    monkeypatch.setenv("SHUFFLE_HANDED_OVER", C)
    monkeypatch.setattr(module.sys, "executable", str(updater.directory(C) / "venv/bin/python"))
    updater.prune()
    assert not updater.overlays.directory(artifact[0]).exists()
    for revision in (C, "d" * 40):
        python = updater.directory(revision) / "venv/bin/python"
        assert python.resolve() == base
        assert subprocess.check_output([str(python), "-c", "print('still works')"], text=True).strip() == "still works"
    updater.launch({})  # the actual active venv also passes the launch verifier


def test_inherited_overlay_is_retained_but_not_executed_without_signed_selection(feed):
    from docker import application_updates as module
    updater, publish, *_ = feed
    artifact = runtime_artifact("python", "test")
    signed = offer_runtimes(feed, [artifact], revision=B)
    updater.overlays.prepare([artifact[0]])
    # Model an already-built no-overlay release made by the buggy updater.
    signed = publish(revision=C, sequence=3, container={"requires": ["base-1"]})
    directory = updater.directory(C)
    directory.mkdir(parents=True)
    (directory / "release.json").write_bytes(signed)
    executable = updater.overlays.directory(artifact[0]) / "payload" / artifact[0]["executable"]
    stub_venv(directory, executable)
    updater.state = {"active": C}
    updater.prune_overlays()
    assert executable.exists(), "retained venv dependencies must not be deleted"
    with pytest.raises(UpdateError, match="signed runtime selection"):
        module.verify_venv_python(directory)


@pytest.mark.parametrize("kind", ["pax", "gnu-longname", "file", "padding", "headers", "pax-total"])
def test_runtime_bounds_precede_tar_metadata_parsing(feed, kind, monkeypatch):
    import gzip
    import tarfile
    from docker import application_updates as module
    updater = feed[0]
    item, _ = runtime_artifact()
    if kind in ("pax", "gnu-longname", "file"):
        member = tarfile.TarInfo("extended")
        member.type = {"pax": tarfile.XHDTYPE, "gnu-longname": tarfile.GNUTYPE_LONGNAME,
                       "file": tarfile.REGTYPE}[kind]
        member.size = 4 * 1024**2
        raw = member.tobuf() + b"x" * member.size + bytes(1024)
    elif kind == "padding":
        raw = bytes(4 * 1024**2)
    elif kind == "headers":
        monkeypatch.setattr(module, "MAX_TAR_HEADERS", 2)
        raw = b"".join(tarfile.TarInfo(str(i)).tobuf() for i in range(3)) + bytes(1024)
    else:
        # Valid, individually-small PAX records must also obey the aggregate cap.
        records = []
        for index in range(6):
            member = tarfile.TarInfo(f"entry-{index}")
            member.pax_headers = {"comment": "x" * 12000}
            records.append(member.tobuf(format=tarfile.PAX_FORMAT))
        raw = b"".join(records) + bytes(1024)
    data = gzip.compress(raw)
    item.update(size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                unpacked_size=1024**2 if kind == "pax-total" else 1)
    offer_runtimes(feed, [(item, data)])
    # Guard tarfile's allocation points: our raw-stream validator must reject
    # oversized metadata without allowing the parser to read its body at all.
    def no_large_metadata(member, *args, **kwargs):
        assert member.size <= module.MAX_TAR_METADATA
        return original_pax(member, *args, **kwargs)
    original_pax = tarfile.TarInfo._proc_pax
    monkeypatch.setattr(tarfile.TarInfo, "_proc_pax", no_large_metadata)
    with pytest.raises(UpdateError, match="metadata|expanded size|decompressed size|padding|headers"):
        updater.check(download=True)
    assert updater.state == {} and list(updater.overlays.root.iterdir()) == []


@pytest.mark.parametrize("sparse_format,header_type", [
    ("0.0", b"x"), ("0.1", b"x"), ("1.0", b"x"),
    ("0.0", b"g"), ("0.1", b"g"), ("1.0", b"g"), ("S", None),
])
def test_sparse_runtime_archives_rejected_before_sparse_parser(feed, monkeypatch, sparse_format, header_type):
    import gzip
    import tarfile
    from docker import application_updates as module
    updater = feed[0]
    item, _ = runtime_artifact()
    body = b"x"
    fields = {}
    if sparse_format == "0.0":
        fields = {"GNU.sparse.size": "1", "GNU.sparse.numblocks": "1",
                  "GNU.sparse.offset": "0", "GNU.sparse.numbytes": "1"}
    elif sparse_format == "0.1":
        fields = {"GNU.sparse.size": "1", "GNU.sparse.numblocks": "1", "GNU.sparse.map": "0,1"}
    elif sparse_format == "1.0":
        fields = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.realsize": "1"}
        # This large allocation was previously disguised as regular-file data
        # by a tiny PAX header. It exceeds the aggregate metadata cap, while the
        # gzip and PAX header themselves are small and inside their limits.
        sparse_map = b"20001\n" + b"0\n0\n" * 20000 + b"0\n1\n"
        sparse_map += bytes(-len(sparse_map) % 512)
        assert len(sparse_map) > module.MAX_TAR_METADATA_TOTAL
        body = sparse_map + body
    file = tarfile.TarInfo(item["executable"])
    file.mode, file.size = 0o755, len(body)
    if header_type is None:
        file.type = tarfile.GNUTYPE_SPARSE
        prefix = b""
    else:
        records = []
        for key, value in fields.items():
            text = (key + "=" + value + "\n").encode()
            length = len(text) + 2
            while len(str(length)) + 1 + len(text) != length:
                length = len(str(length)) + 1 + len(text)
            records.append(str(length).encode() + b" " + text)
        pax = b"".join(records)
        assert len(pax) < module.MAX_TAR_METADATA
        header = tarfile.TarInfo("PaxHeaders/runtime")
        header.type, header.size = header_type, len(pax)
        prefix = header.tobuf() + pax + bytes(-len(pax) % 512)
    raw = prefix + file.tobuf(format=tarfile.GNU_FORMAT) + body + bytes(-len(body) % 512) + bytes(1024)
    data = gzip.compress(raw)
    assert len(data) < 4096
    item.update(size=len(data), sha256=hashlib.sha256(data).hexdigest(), unpacked_size=len(raw))
    offer_runtimes(feed, [(item, data)])

    def must_not_parse_sparse(*args, **kwargs):
        pytest.fail("tarfile reached a sparse-map allocation routine")

    for method in ("_proc_sparse", "_proc_gnusparse_00", "_proc_gnusparse_01", "_proc_gnusparse_10"):
        monkeypatch.setattr(tarfile.TarInfo, method, must_not_parse_sparse)
    with pytest.raises(UpdateError, match="sparse files are not supported"):
        updater.check(download=True)
    assert updater.state == {} and list(updater.overlays.root.iterdir()) == []

    # Model a cache installed by the old updater. Re-verification must reject
    # it too, before interpreting sparse metadata or reading payload files.
    directory = updater.overlays.directory(item)
    directory.mkdir()
    (directory / "runtime.tar").write_bytes(data)
    (directory / "payload").mkdir()
    with pytest.raises(UpdateError, match="sparse files are not supported"):
        updater.overlays.installed(item)


@pytest.mark.parametrize("chunk", [1, 127, 10240])
def test_pax_sparse_detection_spans_stream_reads(chunk):
    import tarfile
    import threading
    from docker import application_updates as module
    member = tarfile.TarInfo("node/bin/node")
    member.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}
    raw = member.tobuf(format=tarfile.PAX_FORMAT) + bytes(1024)
    stream = module.BoundedTarStream(io.BytesIO(raw), len(raw), threading.Event())
    with pytest.raises(UpdateError, match="sparse files are not supported"):
        while stream.read(chunk):
            pass


@pytest.mark.parametrize("chunk", [1, 127, 10240])
def test_small_nonsparse_pax_remains_supported(chunk):
    import tarfile
    import threading
    from docker import application_updates as module
    member = tarfile.TarInfo("node/bin/node")
    member.pax_headers = {"path": "node/bin/node", "comment": "ordinary metadata", "mtime": "1.25"}
    raw = member.tobuf(format=tarfile.PAX_FORMAT) + bytes(1024)
    stream = module.BoundedTarStream(io.BytesIO(raw), len(raw), threading.Event())
    result = bytearray()
    while data := stream.read(chunk):
        result.extend(data)
    assert result == raw
    with tarfile.open(fileobj=io.BytesIO(result)) as archive:
        assert archive.getnames() == ["node/bin/node"]


def test_pax_padding_cannot_smuggle_sparse_metadata(monkeypatch, tmp_path):
    import tarfile
    import threading
    from docker import application_updates as module
    member = tarfile.TarInfo("node/bin/node")
    member.pax_headers = {"comment": "ordinary", "GNU.sparse.major": "1", "GNU.sparse.minor": "0"}
    raw = member.tobuf(format=tarfile.PAX_FORMAT) + bytes(1024)
    # Declare only the first, benign record; leave sparse records in padding.
    # tarfile consumes that entire block as metadata, so our guard must too.
    first_record_size = int(raw[512:].split(b" ", 1)[0])
    header = tarfile.TarInfo("PaxHeaders/runtime")
    header.type, header.size = tarfile.XHDTYPE, first_record_size
    raw = header.tobuf() + raw[512:]
    def must_not_parse_sparse(*args, **kwargs):
        pytest.fail("sparse metadata escaped through PAX padding")
    monkeypatch.setattr(tarfile.TarInfo, "_proc_gnusparse_10", must_not_parse_sparse)
    stream = module.BoundedTarStream(io.BytesIO(raw), len(raw), threading.Event())
    with pytest.raises(UpdateError, match="nonzero PAX padding"):
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            list(archive)


def stub_venv(directory, executable=None):
    """Model the base-interpreter links/config that real venv writes."""
    from docker.application_updates import IMAGE_PYTHON
    base = Path(executable or IMAGE_PYTHON).resolve()
    python = directory / "venv/bin/python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.symlink_to(base)
    (directory / "venv/pyvenv.cfg").write_text(f"home = {base.parent}\nexecutable = {base}\n")






def test_runtime_bootstrap_gate_and_signed_handover(feed):
    from docker.application_updates import HANDOVER_FILES, handover_target
    updater = feed[0]
    artifact = runtime_artifact()
    offer_runtimes(feed, [artifact], entries={f"ai-shuffle-{B}/source.txt": "content", **{
        f"ai-shuffle-{B}/{name}": "# verified" for name in HANDOVER_FILES}})
    updater.image["provides"] = ["base-1", "source-supervisor"]
    updater.check(download=True)
    assert "runtime-overlays-v1" in json.loads((updater.root / "status.json").read_text())["message"]
    assert updater.state == {}
    updater.image["provides"].append("runtime-overlays-v1")
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.activating()
    updater.healthy()
    assert handover_target(updater.root, updater.image, updater.public_key)[0] == B
    updater.overlays.directory(artifact[0]).joinpath("payload", artifact[0]["executable"]).write_text("tampered")
    with pytest.raises(UpdateError, match="signed archive"):
        handover_target(updater.root, updater.image, updater.public_key)


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink", "device", "duplicate"])
def test_runtime_unsafe_archives_never_leave_partial_overlays(feed, kind):
    import tarfile
    updater = feed[0]
    item, _ = runtime_artifact()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo({"traversal": "../escape", "absolute": "/escape"}.get(kind, "bin/node"))
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "../../escape"
        elif kind == "device":
            member.type = tarfile.CHRTYPE
        archive.addfile(member)
        if kind == "duplicate":
            archive.addfile(member)
    data = output.getvalue()
    item.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    offer_runtimes(feed, [(item, data)])
    with pytest.raises((UpdateError, ValueError), match="path|link|special|duplicate"):
        updater.check(download=True)
    assert updater.state == {} and list(updater.overlays.root.iterdir()) == []


def test_runtime_safe_node_links_and_offline_reuse(feed):
    import tarfile
    updater = feed[0]
    output = io.BytesIO()
    content = b"#!/bin/sh\necho npm\n"
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("node/lib/npm/cli.js")
        member.size, member.mode = len(content), 0o755
        archive.addfile(member, io.BytesIO(content))
        member = tarfile.TarInfo("node/bin/npm")
        member.type, member.linkname = tarfile.SYMTYPE, "../lib/npm/cli.js"
        archive.addfile(member)
    item, _ = runtime_artifact(executable="node/bin/npm")
    data = output.getvalue()
    item.update(size=len(data), sha256=hashlib.sha256(data).hexdigest(), unpacked_size=len(content))
    offer_runtimes(feed, [(item, data)])
    updater.check(download=True)
    requests = len(feed[3])
    updater.overlays.prepare([item])
    assert len(feed[3]) == requests
    env, _ = updater.overlays.environment([item], {"PATH": "/bin"})
    assert "node/bin" in env["PATH"]


def test_runtime_prune_keeps_live_supervisor_previous_and_staged(feed, monkeypatch):
    updater = feed[0]
    artifacts = [runtime_artifact(version=str(n)) for n in (20, 22, 24, 26)]
    for revision, artifact in zip((A, B, C, "e" * 40), artifacts):
        signed = offer_runtimes(feed, [artifact], revision=revision)
        directory = updater.directory(revision)
        directory.mkdir(parents=True)
        (directory / "release.json").write_bytes(signed)
        updater.overlays.prepare([artifact[0]])
    updater.state = {"active": B, "previous": A, "pending": C}
    monkeypatch.setenv("SHUFFLE_HANDED_OVER", "e" * 40)
    updater.prune_overlays()
    assert all(updater.overlays.directory(item).exists() for item, _ in artifacts)
    monkeypatch.delenv("SHUFFLE_HANDED_OVER")
    stale = updater.overlays.root / ".staging-interrupted"
    stale.mkdir()
    updater.prune_overlays()
    assert not updater.overlays.directory(artifacts[-1][0]).exists()
    assert not stale.exists()


def runtime_artifact(kind="node", version="24", *, executable=None, entries=None):
    """Tiny executable tarballs, never actual runtime downloads."""
    import tarfile
    from docker.application_updates import runtime_platform

    executable = executable or f"{kind}/bin/{kind}"
    content = f"#!/bin/sh\necho {kind}-{version}\n".encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, value in (entries or {executable: content}).items():
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(value), 0o755
            archive.addfile(member, io.BytesIO(value))
    data = output.getvalue()
    item = {"kind": kind, "capability": f"{kind}-{version}", "platform": runtime_platform(),
            "url": f"https://runtimes.example/{kind}-{version}.tar.gz", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "unpacked_size": sum(len(v) for v in (entries or {executable: content}).values()),
            "executable": executable}
    return item, data


def offer_runtimes(feed, artifacts, **kwargs):
    updater, publish, _, _, responses = feed
    updater.image["provides"] = ["base-1", "runtime-overlays-v1"]
    for item, data in artifacts:
        responses[httpx.URL(item["url"]).path] = data
    return publish(container={"requires": ["base-1", "runtime-overlays-v1", *(item["capability"] for item, _ in artifacts)],
                              "overlays": [item for item, _ in artifacts]}, **kwargs)


@pytest.mark.parametrize("change", [
    {"sha256": "bad"}, {"size": True}, {"unpacked_size": 2**40},
    {"url": "http://runtimes.example/runtime.tar.gz"}, {"url": "https://user:pass@runtimes.example/x"},
    {"executable": "../bin/python"}, {"platform": "windows-x86_64"},
    {"kind": "postgres", "capability": "postgres-17"}, {"capability": "base-2"},
])
def test_runtime_manifest_rejects_invalid_signed_descriptors(feed, change):
    updater = feed[0]
    item, data = runtime_artifact()
    item.update(change)
    signed = offer_runtimes(feed, [(item, data)])
    with pytest.raises(UpdateError, match="Invalid or unsigned"):
        verify_manifest(signed, updater.public_key)


def test_runtime_manifest_signature_covers_artifact_pins(feed):
    item, data = runtime_artifact()
    signed = offer_runtimes(feed, [(item, data)])
    assert verify_manifest(signed, feed[0].public_key)["container"]["overlays"] == [item]
    envelope = json.loads(signed)
    payload = json.loads(base64.b64decode(envelope["payload"]))
    payload["container"]["overlays"][0]["sha256"] = "f" * 64
    envelope["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(UpdateError, match="Invalid or unsigned"):
        verify_manifest(json.dumps(envelope), feed[0].public_key)


def test_runtime_selection_is_opt_in_platform_specific_and_image_first(feed, monkeypatch):
    from docker import application_updates as module
    updater = feed[0]
    item, data = runtime_artifact()
    offer_runtimes(feed, [(item, data)])
    updater.check(download=True)
    assert updater.state == {"pending": B}
    _, _, env = updater.launch({"PATH": "/usr/bin"}, activate=True)
    assert item["sha256"] in env["PATH"]
    assert updater.overlays.installed(item).is_dir()
    assert stat.S_IMODE(updater.overlays.root.stat().st_mode) == 0o755
    assert stat.S_IMODE(updater.overlays.directory(item).stat().st_mode) == 0o755
    container = updater.manifest(B)["container"]
    assert module.selected_overlays(container, {"provides": ["base-1", item["capability"]]}) == []
    monkeypatch.setattr(module, "runtime_platform", lambda: "linux-unsupported")
    assert module.unsatisfied_capabilities(container, updater.image) == [item["capability"]]
    assert module.selected_overlays({"requires": ["base-1"]}, updater.image) == []


@pytest.mark.parametrize("corrupt", ["checksum", "short", "oversized"])
def test_runtime_download_hash_and_size_refused_without_activation(feed, corrupt):
    updater, _, _, _, responses = feed
    item, data = runtime_artifact()
    offer_runtimes(feed, [(item, data)])
    responses[httpx.URL(item["url"]).path] = {
        "checksum": b"x" * len(data), "short": data[:-1], "oversized": data + b"x"}[corrupt]
    with pytest.raises(UpdateError, match="checksum|signed size"):
        updater.check(download=True)
    assert updater.state == {}
    assert list(updater.overlays.root.iterdir()) == []


@pytest.mark.parametrize("bound", ["free", "store", "expanded"])
def test_runtime_disk_bounds_preserve_current_version(feed, monkeypatch, bound):
    from docker import application_updates as module
    updater = feed[0]
    item, data = runtime_artifact()
    if bound == "expanded":
        item["unpacked_size"] = 1
    elif bound == "store":
        monkeypatch.setattr(module, "MAX_RUNTIME_STORE", 1)
    else:
        monkeypatch.setattr(module.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 1})())
    offer_runtimes(feed, [(item, data)])
    with pytest.raises(UpdateError, match="disk space|expanded size"):
        updater.check(download=True)
    assert updater.state == {}
    assert list(updater.overlays.root.iterdir()) == []


@pytest.mark.parametrize("mutation", ["file", "archive", "extra"])
def test_cached_runtime_reverified_before_launch(feed, mutation):
    updater = feed[0]
    item, data = runtime_artifact()
    offer_runtimes(feed, [(item, data)])
    updater.check(download=True)
    directory = updater.overlays.directory(item)
    path = {"file": directory / "payload" / item["executable"],
            "archive": directory / "runtime.tar", "extra": directory / "payload/extra.py"}[mutation]
    path.write_bytes(b"tampered")
    with pytest.raises(UpdateError, match="signed archive|checksum"):
        updater.launch({}, activate=True)
    assert "booting" not in updater.state


def test_runtime_build_uses_selected_python_node_chromium_and_libraries(feed, monkeypatch):
    updater = feed[0]
    artifacts = [runtime_artifact(kind) for kind in ("python", "node", "chromium")]
    tool, data = runtime_artifact("tools", entries={"tools/bin/tool": b"tool", "tools/lib/libtest.so": b"lib"},
                                 executable="tools/bin/tool")
    tool["library_path"] = "tools/lib"
    artifacts.append((tool, data))
    offer_runtimes(feed, artifacts, entries={f"ai-shuffle-{B}/requirements.lock": "# pinned"})
    monkeypatch.setattr(updater, "build", ContainerUpdates.build.__get__(updater))
    commands = []
    def run(command, cwd, env):
        commands.append((command, dict(env)))
        if "venv" in command:
            stub_venv(Path(command[-1]).parent, command[0])
        if command == ["npm", "run", "build"] and cwd.name == "frontend":
            (cwd / "build").mkdir(parents=True)
            (cwd / "build/index.html").write_text("built")
    monkeypatch.setattr(updater, "run_build", run)
    updater.check(download=True)
    assert artifacts[0][0]["sha256"] in commands[0][0][0]
    build_env = commands[0][1]
    assert artifacts[1][0]["sha256"] in build_env["PATH"]
    assert artifacts[2][0]["sha256"] in build_env["CHROME_EXECUTABLE"]
    assert tool["sha256"] in build_env["LD_LIBRARY_PATH"]
    assert "SHUFFLE_DESKTOP_LIFECYCLE_TOKEN" not in build_env
    assert all("x-shuffle-lease" not in request.headers for request in feed[3]
               if request.url.host == "runtimes.example")


def test_runtime_rollback_restores_prior_selection_offline_and_prunes_unused(feed):
    updater = feed[0]
    updater.database = FakeDatabase()
    first = runtime_artifact("node", "22")
    second = runtime_artifact("node", "24")
    offer_runtimes(feed, [first])
    updater.check(download=True)
    updater.launch({"PATH": "/image/bin"}, activate=True)
    updater.activating()
    updater.healthy()
    original = updater.launch({"PATH": "/image/bin"})[2]
    offer_runtimes(feed, [second], revision=C, sequence=3)
    updater.check(download=True)
    trial = updater.launch({"PATH": "/image/bin"}, activate=True)[2]
    assert second[0]["sha256"] in trial["PATH"]
    updater.activating()
    updater.database.databases["ai_shuffle"] = "migrated"
    assert updater.failed()
    assert updater.launch({"PATH": "/image/bin"})[2] == original
    assert updater.database.databases["ai_shuffle"] == "before"
    restored = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: False, lambda _: {},
                                database=updater.database)
    assert restored.launch({"PATH": "/image/bin"})[2] == original
    updater.prune_overlays()
    assert updater.overlays.directory(first[0]).exists()
    assert updater.overlays.directory(second[0]).exists(), "failed builds retained for diagnosis keep their interpreter"
    updater.state.pop("failed")
    updater.prune_overlays()
    assert not updater.overlays.directory(second[0]).exists()


def test_runtime_unsupported_postgres_and_architecture_do_not_download(feed):
    updater, publish, _, requests, _ = feed
    updater.image["provides"] = ["base-1"]
    publish(container={"requires": ["base-1", "postgres-17"]})
    updater.check(download=True)
    status = json.loads((updater.root / "status.json").read_text())
    assert status["phase"] == "image-required" and "postgres-17" in status["message"]
    assert "tested backup and migration plan" in status["message"]
    assert len(requests) == 1 and updater.state == {}



def test_manifest_trust_key_rotation():
    keys = [Ed25519PrivateKey.generate() for _ in range(3)]
    public = [key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo) for key in keys]
    manifest = {"format": 2, "revision": A, "sha256": "b" * 64, "size": 1, "sequence": 1,
                "container": {"sequence": 1, "runtime": RUNTIME}}
    payload = json.dumps(manifest).encode()
    garbage = b"-----BEGIN PUBLIC KEY-----\ninvalid\n-----END PUBLIC KEY-----\n"
    for signer, key in enumerate(keys):
        raw = json.dumps({"payload": base64.b64encode(payload).decode(),
                          "signature": base64.b64encode(key.sign(payload)).decode()}).encode()
        for trusted in ((0,), (1,), (0, 1), (1, 0)):
            bundle = b"".join(public[i] for i in trusted)
            if signer in trusted:
                assert verify_manifest(raw, bundle) == manifest
                assert verify_manifest(raw, garbage + bundle) == manifest
            else:
                with pytest.raises(UpdateError, match="Invalid or unsigned"):
                    verify_manifest(raw, bundle)
        for invalid in (b"", b"garbage", garbage):
            with pytest.raises(UpdateError, match="No valid Ed25519"):
                verify_manifest(raw, invalid)


def test_manifest_rejects_non_ed25519_trust_keys(feed):
    from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key

    updater, publish, *_ = feed
    wrong_type = generate_private_key(SECP256R1()).public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    raw = publish()
    with pytest.raises(UpdateError, match="No valid Ed25519"):
        verify_manifest(raw, wrong_type)
    assert verify_manifest(raw, wrong_type + updater.public_key)["revision"] == B


class FakeDatabase:
    """The container_runtime.Database rollback-copy surface, in memory.

    ``databases`` maps a name to its content; the updater only ever names
    ai_shuffle_rollback_* and ai_shuffle_failed_* copies."""

    def __init__(self, free=10**12, need=1000):
        self.databases = {"ai_shuffle": "before"}
        self.calls = []
        self.free, self.need = free, need
        self.fail = set()

    def _call(self, *call):
        self.calls.append(call)
        if call[0] in self.fail:
            raise RuntimeError(f"synthetic {call[0]} failure")

    def snapshot_space(self):
        self._call("space")
        return self.free, self.need

    def update_databases(self):
        self._call("list")
        return sorted(name for name in self.databases if name != "ai_shuffle")

    def snapshot(self, name):
        self._call("snapshot", name)
        self.databases[name] = self.databases["ai_shuffle"]

    def restore_snapshot(self, name, discard):
        self._call("restore", name, discard)
        if name not in self.databases:
            if discard in self.databases:
                return
            raise RuntimeError("copy missing")
        self.databases[discard] = self.databases["ai_shuffle"]
        self.databases["ai_shuffle"] = self.databases.pop(name)

    def drop_update_database(self, name):
        self._call("drop", name)
        self.databases.pop(name, None)


@pytest.fixture(autouse=True)
def _no_inherited_update_settings(monkeypatch):
    """Start every test from a customer container's defaults.

    A worker that follows edge exports UPDATE_CHANNEL=edge and its own
    SHUFFLE_CONTAINER_UPDATES directory; shells and agents on it inherit
    both, which made the supervisor follow edge instead of stable here.
    Tests that need one of these set it themselves.
    """
    for name in ("UPDATE_CHANNEL", "SHUFFLE_CONTAINER_UPDATES", "SHUFFLE_UPDATE_POLICY_FILE", "GIT_SHA"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def feed(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    responses, requests = {}, []
    def publish(revision=B, sequence=2, runtime=RUNTIME, entries=None, channel="stable", tree=None, container=None,
                **fields):
        output = io.BytesIO()
        entries = entries or {f"ai-shuffle-{revision}/source.txt": "content"}
        with zipfile.ZipFile(output, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        archive = output.getvalue()
        files = [(name.split("/", 1)[1], hashlib.sha256(value.encode()).hexdigest())
                 for name, value in entries.items() if not name.endswith("/")]
        release = {"format": 2, "revision": revision, "sequence": sequence,
                   "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive),
                   "container": {"sequence": sequence, "runtime": runtime, **(container or {})},
                   "channel": channel, "tree": tree or hashlib.sha256(tree_lines(files)).hexdigest(), **fields}
        payload = json.dumps({key: value for key, value in release.items() if value is not None}).encode()
        envelope = json.dumps({"payload": base64.b64encode(payload).decode(),
                               "signature": base64.b64encode(key.sign(payload)).decode()}).encode()
        responses["/v1/desktop/manifest" + ("" if channel == "stable" else "/nightly")] = envelope
        responses["/v1/desktop/sources/" + revision] = archive
        return envelope
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=responses[request.url.path])
    client = httpx.Client
    monkeypatch.setattr("docker.application_updates.httpx.Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    enabled = [True]
    image = {"revision": A, "sequence": 1, "runtime": RUNTIME}
    updater = ContainerUpdates(tmp_path / "updates", image, public, lambda: enabled[0], lambda action: {})
    def build(directory, revision, version=None):
        assert (directory / "source/source.txt").read_text() == "content"
        stub_venv(directory)
        for name in ("source/app/static/index.html", "complete"):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(revision)
    monkeypatch.setattr(updater, "build", build)
    return updater, publish, enabled, requests, responses


def test_signed_download_stages_then_explicit_restart_and_health_commit(feed):
    updater, publish, enabled, requests, _ = feed
    publish()
    updater.check()
    assert len(requests) == 1 and not updater.state.get("pending")
    updater.check(download=True)
    assert updater.state == {"pending": B}
    assert updater.launch({})[1] is None, "ordinary container restart must not activate staging"
    command, cwd, env = updater.launch({"PYTHONPATH": "wrong", "PATH": "/usr/bin"}, activate=True)
    assert Path(command[0]).parts[-5:] == ("versions", B, "venv", "bin", "python")
    assert "PYTHONPATH" not in env and env["GIT_SHA"] == B
    assert not updater.state.get("booting"), "validation must not mark old worker as new"
    updater.activating()
    updater.healthy()
    assert updater.state == {"active": B}
    # #1623: the idle status after an install must not read as work in progress.
    status = json.loads((updater.root / "status.json").read_text())
    assert status["phase"] == "idle" and not policy.IN_PROGRESS.search(status["message"])
    offline =ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: False, lambda _: {})
    assert offline.launch({})[1] == cwd
    assert all("authorization" not in request.headers for request in requests)


def test_fanned_out_download_stages_on_a_container_that_never_checked(feed, monkeypatch):
    # #1623 live test: the container checks on its own once a day, so a peer
    # usually has not seen the new revision when another worker fans out the
    # download. The app must queue it; the supervisor checks, then downloads.
    from app.core import update_workers
    updater, publish, _, _, _ = feed
    publish()
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(updater.root))
    monkeypatch.delenv("SHUFFLE_UPDATE_POLICY_FILE", raising=False)
    monkeypatch.setattr(policy.database, "get_system_setting", lambda key: None)
    update_workers._receipts.clear()
    assert policy.status()["phase"] == "idle"
    result = update_workers.local_action("download", "initiator:request")
    assert result["accepted"], result
    assert policy.status()["request_pending"] is True
    updater.service()
    assert updater.state == {"pending": B}
    status = policy.status()
    assert status["phase"] == "ready" and status["request_pending"] is False


def test_supervisor_publishes_when_it_last_read_the_feed(feed, monkeypatch):
    # #1623: other workers compare this with their own to tell "checked and
    # found nothing" from "has not checked since an update was found".
    updater, publish, _, _, _ = feed
    publish()
    monkeypatch.setattr("docker.application_updates.time.time", lambda: 1234.5)
    updater.check()
    assert json.loads((updater.root / "status.json").read_text())["checked_at"] == 1234.5
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(updater.root))
    monkeypatch.setattr(policy.database, "get_system_setting", lambda key: None)
    assert policy.status()["checked_at"] == 1234.5


def test_lease_goes_to_hub_manifest_and_archive_requests_only(feed):
    updater, publish, _, requests, _ = feed
    publish()
    calls = []
    def lease():
        calls.append(1)
        return "eyJsZWFzZSI6MX0"
    updater.lease = lease
    updater.check(download=True)
    assert [request.url.path for request in requests] == ["/v1/desktop/manifest", "/v1/desktop/sources/" + B]
    for request in requests:
        assert request.url.scheme == "https" and request.url.host == "hobbycoders.com"
        assert request.headers["x-shuffle-lease"] == "eyJsZWFzZSI6MX0"
    assert len(calls) == 2


@pytest.mark.parametrize("value", [None, "", "not a lease!", "x" * 4097, 42, RuntimeError("app not ready")])
def test_missing_or_bad_lease_sends_no_header(feed, value):
    updater, publish, _, requests, _ = feed
    publish()
    def lease():
        if isinstance(value, Exception):
            raise value
        return value
    updater.lease = lease
    updater.check(download=True)
    assert updater.state == {"pending": B}
    assert requests and all("x-shuffle-lease" not in request.headers for request in requests)


def test_no_lease_provider_sends_no_header(feed):
    updater, publish, _, requests, _ = feed
    publish()
    updater.check()
    assert requests and "x-shuffle-lease" not in requests[0].headers


def test_disabled_before_download_and_after_staging_blocks_activation(feed):
    updater, publish, enabled, requests, _ = feed
    publish()
    enabled[0] = False
    with pytest.raises(UpdateError, match="disabled"):
        updater.check(download=True)
    assert requests == []
    enabled[0] = True
    updater.check(download=True)
    enabled[0] = False
    with pytest.raises(UpdateError, match="disabled"):
        updater.launch({}, activate=True)
    assert not updater.state.get("booting")


def test_policy_disabled_during_build_never_stages(feed, monkeypatch):
    updater, publish, enabled, _, _ = feed
    publish()
    monkeypatch.setattr(updater, "build", lambda *_: enabled.__setitem__(0, False))
    with pytest.raises(UpdateError, match="disabled"):
        updater.check(download=True)
    assert updater.state == {} and not updater.directory(B).exists()


def test_runtime_mismatch_requires_image_and_older_source_is_ignored(feed):
    updater, publish, _, requests, _ = feed
    publish(runtime="e" * 64)
    updater.check(download=True)
    assert json.loads((updater.root / "status.json").read_text())["phase"] == "image-required"
    assert len(requests) == 1
    publish(sequence=1)
    updater.check(download=True)
    assert updater.state == {}


def test_withdrawn_release_fallback_is_never_applied_backwards(feed):
    # When the hub withdraws a stable release it serves the previous one
    # (android/hub/src/desktop-updates.ts servedRelease). A container already
    # running the withdrawn release must read that as up to date, not as an
    # update, and must not download it.
    updater, publish, _, requests, _ = feed
    publish(revision=C, sequence=3)
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.activating()
    updater.healthy()
    assert updater.state == {"active": C}
    publish(revision=B, sequence=2)
    requests.clear()
    updater.check(download=True)
    assert updater.state == {"active": C} and not updater.directory(B).exists()
    assert [request.url.path for request in requests] == ["/v1/desktop/manifest"]
    status = json.loads((updater.root / "status.json").read_text())
    assert status["phase"] == "idle" and status["message"] == "You are up to date."


def test_cleanup_retains_active_previous_staged_and_failed_revisions(feed):
    updater, _, _, _, _ = feed
    for revision in (A, B, C, "e" * 40, "f" * 40):
        updater.directory(revision).mkdir(parents=True)
    updater.state = {"active": A, "previous": B, "pending": C, "failed": ["e" * 40]}
    updater.prune()
    assert updater.directory(A).exists() and updater.directory(B).exists()
    assert updater.directory(C).exists() and updater.directory("e" * 40).exists()
    assert not updater.directory("f" * 40).exists()


def test_new_image_discards_obsolete_pending_and_never_downgrades(feed):
    updater, publish, _, _, _ = feed
    publish()
    updater.check(download=True)
    newer = ContainerUpdates(updater.root, dict(updater.image, revision=C, sequence=3), updater.public_key, lambda: True, lambda _: {})
    assert newer.state == {} and newer.launch({})[1] is None
    with pytest.raises(UpdateError, match="No application update"):
        newer.launch({}, activate=True)


def test_image_behind_the_installed_update_names_both_sides_and_the_way_out(feed):
    updater, publish, _, _, _ = feed
    publish(version="1.2.3")
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.activating()
    updater.healthy()
    # A rebuilt or older image with another runtime contract, not newer on main.
    image = dict(updater.image, runtime="e" * 64)
    behind = ContainerUpdates(updater.root, image, updater.public_key, lambda: True, lambda _: {})
    assert behind.state == {"active": B}, "the installed update is never rolled back silently"
    with pytest.raises(UpdateError, match=r"update 1\.2\.3 \(stable channel, main revision 2\).*"
                       r"This image \(aaaaaaaaaaaa, main revision 1\).*main revision 2 or later.*state\.json"):
        behind.launch({})
    # The documented way out: discard the update record and run the image's own code.
    (updater.root / "state.json").unlink()
    recovered = ContainerUpdates(updater.root, image, updater.public_key, lambda: True, lambda _: {})
    assert recovered.state == {} and recovered.launch({})[1] is None


def test_failed_first_boot_without_a_database_copy_blocks_and_newer_milestone_can_recover(feed):
    # A first start recorded without a copy (an older supervisor, or no
    # managed database) is never rolled back over possibly migrated data.
    updater, publish, _, _, _ = feed
    publish()
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.activating()
    with pytest.raises(UpdateError, match="failed first boot"):
        ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {})
    with pytest.raises(UpdateError, match="failed first boot"):
        ContainerUpdates(updater.root, dict(updater.image, revision=B, sequence=2), updater.public_key, lambda: True, lambda _: {})
    newer = ContainerUpdates(updater.root, dict(updater.image, revision=C, sequence=3), updater.public_key, lambda: True, lambda _: {})
    assert newer.state == {} and newer.launch({})[1] is None


def staged_first_start(updater, database, publish, **fields):
    """Stage B, copy the database and record its first start, as restart does."""
    publish(**fields)
    updater.database = database
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.snapshot_room()
    updater.activating()
    database.databases["ai_shuffle"] = "migrated"  # what the new code did
    return "ai_shuffle_rollback_" + B[:12]


def test_failed_first_start_puts_the_database_copy_back_and_runs_the_previous_version(feed):
    updater, publish, _, _, _ = feed
    database = FakeDatabase()
    snapshot = staged_first_start(updater, database, publish, version="1.2.3")
    assert updater.state == {"pending": B, "booting": B, "snapshot": snapshot}
    assert database.databases[snapshot] == "before"
    assert updater.failed() is True
    assert database.databases == {"ai_shuffle": "before"}, "the copy is back and the migrated database removed"
    assert updater.state == {"failed": [B]}
    status = status_of(updater)
    assert status["phase"] == "error" and status["version"] == B[:12]
    assert "1.2.3 failed to start" in status["error"] and "data is as it was before" in status["error"]
    # The version that ran before: here the image's own code.
    assert updater.launch({})[1] is None and updater.running is None
    assert updater.failed() is False, "nothing left to roll back"


def test_rolled_back_release_is_not_offered_again_unless_asked_for(feed):
    updater, publish, _, requests, _ = feed
    database = FakeDatabase()
    staged_first_start(updater, database, publish, version="1.2.3")
    updater.failed()
    updater.check()
    status = status_of(updater)
    assert status["phase"] == "error" and status["version"] == B[:12] and "Choose Update to try it again" in status["error"]
    assert updater.state == {"failed": [B]}
    # The Update button asks for a download: one more try, from a fresh build.
    updater.check(download=True)
    assert updater.state == {"failed": [B], "pending": B}
    updater.launch({}, activate=True)
    updater.activating()
    updater.healthy()
    assert updater.state == {"active": B}, "a successful retry clears the failure"
    assert database.databases == {"ai_shuffle": "before"}, "the copy is dropped once the new version is ready"


def test_power_loss_during_a_first_start_rolls_back_at_the_next_container_start(feed):
    updater, publish, _, _, _ = feed
    database = FakeDatabase()
    staged_first_start(updater, database, publish)
    restarted = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {},
                                 database=database)
    assert restarted.state == {"failed": [B]} and database.databases == {"ai_shuffle": "before"}
    assert status_of(restarted)["phase"] == "error", "the rollback notice is not replaced by an idle status"
    assert restarted.launch({})[1] is None


def test_interrupted_rollback_is_finished_at_the_next_start(feed):
    updater, publish, _, _, _ = feed
    database = FakeDatabase()
    snapshot = staged_first_start(updater, database, publish)
    # Power loss after the swap committed, before the state was saved.
    discard = snapshot.replace("rollback", "failed")
    database.databases.update({discard: "migrated", "ai_shuffle": database.databases.pop(snapshot)})
    restarted = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {},
                                 database=database)
    assert restarted.state == {"failed": [B]} and database.databases == {"ai_shuffle": "before"}


def test_rollback_that_cannot_restore_blocks_and_keeps_every_copy(feed):
    updater, publish, _, _, _ = feed
    database = FakeDatabase()
    snapshot = staged_first_start(updater, database, publish)
    database.fail.add("restore")
    assert updater.failed() is False
    assert updater.state["blocked"] is True and updater.state["snapshot"] == snapshot
    with pytest.raises(UpdateError, match="failed first boot"):
        ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {}, database=database)
    assert database.databases == {"ai_shuffle": "migrated", snapshot: "before"}, "nothing is dropped on a blocked worker"


def test_first_start_recorded_without_a_copy_still_blocks_with_a_database(feed):
    updater, publish, _, _, _ = feed
    publish()
    updater.check(download=True)
    updater.launch({}, activate=True)
    updater.activating()  # an older supervisor: no copy
    with pytest.raises(UpdateError, match="failed first boot"):
        ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {},
                         database=FakeDatabase())


def test_a_copy_that_fails_never_records_a_first_start(feed):
    updater, publish, _, _, _ = feed
    publish()
    database = FakeDatabase()
    database.fail.add("snapshot")
    updater.database = database
    updater.check(download=True)
    with pytest.raises(RuntimeError, match="snapshot"):
        updater.activating()
    assert updater.state == {"pending": B}, "the previous version is started again over unchanged data"


def test_too_little_disk_for_the_copy_refuses_before_anything_stops(feed):
    updater, publish, _, _, _ = feed
    publish()
    updater.database = FakeDatabase(free=2 * 1024**3, need=int(3.3 * 1024**3))
    updater.check(download=True)
    with pytest.raises(UpdateError, match=r"Not enough free disk space.*2\.0 GB free, 3\.3 GB needed.*keeps running"):
        updater.snapshot_room()
    updater.database.fail.add("space")
    with pytest.raises(UpdateError, match="Could not check free disk space"):
        updater.snapshot_room()


def test_left_over_copies_are_removed_at_start(feed):
    updater, _, _, _, _ = feed
    database = FakeDatabase()
    database.databases.update({"ai_shuffle_rollback_" + C[:12]: "old", "ai_shuffle_failed_" + C[:12]: "old"})
    ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {}, database=database)
    assert database.databases == {"ai_shuffle": "before"}
    database.fail.add("list")
    ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {}, database=database)


def test_state_file_survives_power_loss_as_a_complete_record(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(policy.os, "fsync", lambda fd: synced.append(stat.S_ISDIR(os.fstat(fd).st_mode)) or real(fd))
    policy.atomic_json(tmp_path / "state.json", {"active": A})
    assert json.loads((tmp_path / "state.json").read_text()) == {"active": A}
    assert synced == [False, True], "the file is flushed to disk before the rename, then the directory"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_bad_signature_and_checksum_rejected_before_build(feed):
    updater, publish, _, _, responses = feed
    signed = publish()
    envelope = json.loads(signed)
    envelope["signature"] = base64.b64encode(b"bad").decode()
    with pytest.raises(UpdateError, match="unsigned"):
        verify_manifest(json.dumps(envelope).encode(), updater.public_key)
    responses["/v1/desktop/sources/" + B] = b"corrupt"
    with pytest.raises(UpdateError, match="checksum"):
        updater.check(download=True)
    assert updater.state == {} and not updater.directory(B).exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", f"ai-shuffle-{B}/../escape", f"ai-shuffle-{B}/a\\b", f"ai-shuffle-{B}/C:drive"])
def test_archive_rejects_traversal_before_writes(tmp_path, monkeypatch, name):
    class ContainerZipInfo(zipfile.ZipInfo):
        def __init__(self, filename="NoName", *args, **kwargs):
            super().__init__(filename, *args, **kwargs)
            # Model the Linux container parser: Windows ZipInfo normalizes
            # backslashes on both write AND read, before our guard sees them.
            self.filename = self.orig_filename

    monkeypatch.setattr(zipfile, "ZipInfo", ContainerZipInfo)
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(name, "bad")
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.namelist() == [name]
    with pytest.raises(UpdateError):
        extract_source(archive, tmp_path / "source", B)
    assert not (tmp_path / "source").exists()


def test_archive_rejects_links(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        info = zipfile.ZipInfo(f"ai-shuffle-{B}/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(info, "/etc/passwd")
    with pytest.raises(UpdateError):
        extract_source(archive, tmp_path / "source", B)


def test_build_environment_never_inherits_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "secret")
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("NODE_OPTIONS", "secret")
    env = build_environment(tmp_path)
    assert all("secret" not in value for value in env.values())
    assert env["PIP_CONFIG_FILE"] == os.devnull


@pytest.mark.parametrize("value, expected", [(None, True), ("true", True), ("false", False), ("invalid", False)])
def test_default_enabled_policy_and_desktop_mirror(tmp_path, monkeypatch, value, expected):
    monkeypatch.setattr(policy.database, "get_system_setting", lambda key: value)
    mirror = tmp_path / "allow-updates.json"
    monkeypatch.setenv("SHUFFLE_UPDATE_POLICY_FILE", str(mirror))
    assert policy.mirror_policy() is expected
    assert json.loads(mirror.read_text()) == {"allow_updates": expected}


def test_unchanged_policy_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(policy.database, "get_system_setting", lambda key: "true")
    mirror = tmp_path / "allow-updates.json"
    mirror.write_text(json.dumps({"allow_updates": True}))
    monkeypatch.setenv("SHUFFLE_UPDATE_POLICY_FILE", str(mirror))
    writes = []
    monkeypatch.setattr(policy, "atomic_json", lambda *a: writes.append(a))
    assert policy.mirror_policy() is True
    assert writes == []
    mirror.write_text("{not json")
    policy.mirror_policy()
    assert writes == [(str(mirror), {"allow_updates": True})]


def test_policy_write_waits_out_a_windows_sharing_violation(tmp_path, monkeypatch):
    target = tmp_path / "allow-updates.json"
    real_replace = os.replace
    calls = []

    def flaky_replace(source, destination):
        calls.append(destination)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(source, destination)

    monkeypatch.setattr(policy, "_WINDOWS", True)
    monkeypatch.setattr(policy.os, "replace", flaky_replace)
    monkeypatch.setattr(policy.time, "sleep", lambda seconds: None)
    policy.atomic_json(target, {"allow_updates": False})
    assert len(calls) == 3
    assert json.loads(target.read_text()) == {"allow_updates": False}
    assert [p.name for p in tmp_path.iterdir()] == ["allow-updates.json"]


def test_policy_write_gives_up_after_repeated_denials(tmp_path, monkeypatch):
    def denied(source, destination):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(policy, "_WINDOWS", True)
    monkeypatch.setattr(policy.os, "replace", denied)
    monkeypatch.setattr(policy.time, "sleep", lambda seconds: None)
    with pytest.raises(PermissionError):
        policy.atomic_json(tmp_path / "allow-updates.json", {"allow_updates": True})
    assert list(tmp_path.iterdir()) == []


def test_account_policy_is_shared():
    from app.network.policy import is_shared_setting_key
    assert is_shared_setting_key(policy.KEY)

@pytest.mark.parametrize("failure", [
    "busy", "policy", "files", "preflight-timeout", "flush", "stop-timeout",
    "stop-response", "stop-group", "activating", "popen", "none",
])
def test_supervisor_restart_failure_boundary(feed, monkeypatch, tmp_path, failure):
    """Drive run -> watch -> restart and the real shutdown-latch producer.

    Only transport, process/DB boundaries and the flush failure are injected;
    staging validation, lifecycle authorization, shutdown and update state are real.
    """
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from starlette.requests import Request
    from app.api import internal
    from app.core import session_runtime, startup, task_persistence
    from app.core.config import Settings
    from app.core.task_registry import task_registry
    pytest.importorskip("fcntl", reason="The container supervisor requires POSIX file locking")
    from docker import container_runtime as supervisor
    from docker import application_updates as update_module

    updater, publish, enabled, _, _ = feed
    publish()
    updater.check(download=True)
    if failure == "files":
        (updater.directory(B) / "complete").unlink()
    if failure == "policy":
        enabled[0] = False

    # run loads image provenance and the updater beside its own source file.
    packaging = tmp_path / "packaging"
    packaging.mkdir()
    (packaging / "image.json").write_text(json.dumps(updater.image))
    (packaging / "application-update-key.pem").write_bytes(updater.public_key)
    (packaging / "application_updates.py").write_text(
        "from docker.application_updates import ContainerUpdates, UpdateError\n"
    )
    monkeypatch.setattr(supervisor, "__file__", str(packaging / "container_runtime.py"))
    database = FakeDatabase()
    def factory(root, image, public_key, allowed, lifecycle, channel, lease, database=None):
        assert channel == "stable", "customers follow signed releases unless UPDATE_CHANNEL says otherwise"
        assert database is db, "the supervisor hands the updater its managed database"
        updater.allowed, updater.lifecycle, updater.lease = allowed, lifecycle, lease
        updater.database = fake_database
        return updater
    fake_database = database
    monkeypatch.setattr(update_module, "ContainerUpdates", factory)
    monkeypatch.setattr(startup, "is_shutting_down", False)
    monkeypatch.setattr(internal, "_shutdown_task", None)
    monkeypatch.setattr(session_runtime, "_executions", {})
    monkeypatch.setattr(task_registry, "active_session_ids", lambda: ["busy"] if failure == "busy" else [])
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "DATA_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("AI_SHUFFLE_ENV_FILE", "/dev/null")
    monkeypatch.setenv("START_CHROME", "0")

    events, actions = [], []
    release_flush = asyncio.Event()
    async def flush():
        assert startup.is_shutting_down, "the real producer must set the latch first"
        events.append("flush")
        if failure == "flush":
            raise OSError("synthetic flush failure")
        if failure == "stop-timeout":
            await release_flush.wait()
    flush_mock = AsyncMock(side_effect=flush)
    monkeypatch.setattr(task_persistence, "flush", flush_mock)

    with asyncio.Runner() as runner:
        def handler(request):
            action = request.url.path.rsplit("/", 1)[-1]
            actions.append(action)
            local_request = Request({"type": "http", "shuffle.ingress": "local",
                                     "headers": [(k.encode(), v.encode()) for k, v in request.headers.items()]})
            if action == "update-policy":
                internal.require_desktop(local_request)
                return httpx.Response(200, json={"allow_updates": enabled[0]})
            if action == "source-lease":
                internal.require_desktop(local_request)
                return httpx.Response(200, json={"lease": "bGVhc2U"})
            if action == "active-sessions":
                if failure == "preflight-timeout":
                    raise httpx.ReadTimeout("synthetic preflight timeout", request=request)
                result = runner.run(internal.desktop_active_sessions(local_request))
            else:
                assert action == "stop-sessions"
                async def stop():
                    return await asyncio.wait_for(internal.desktop_stop_sessions(local_request), 0.01)
                try:
                    result = runner.run(stop())
                except TimeoutError as exc:
                    assert startup.is_shutting_down
                    assert not internal._shutdown_task.done(), "HTTP cancellation must not cancel cleanup"
                    raise httpx.ReadTimeout("synthetic stop timeout", request=request) from exc
                except OSError:
                    return httpx.Response(500)
                if failure == "stop-response":
                    result = {"stopped": False}
            return httpx.Response(200, json=result)

        # feed patches the shared httpx module: use a captured real client type.
        with httpx.Client() as client:
            client_type = type(client)
        monkeypatch.setattr(httpx, "Client", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
        db = Mock(process=None)
        db.close.side_effect = lambda: events.append("db-close")
        monkeypatch.setattr(supervisor, "Database", lambda settings: db)
        old, new, recovered = Mock(name="old"), Mock(name="new"), Mock(name="recovered")
        old.poll.return_value = new.poll.return_value = recovered.poll.return_value = None
        def popen(*args, **kwargs):
            if "initial-start" not in events:
                events.append("initial-start")
                monkeypatch.setenv("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", kwargs["env"]["SHUFFLE_DESKTOP_LIFECYCLE_TOKEN"])
                return old
            if not updater.state.get("booting"):
                # The version that ran before, over the database it had.
                events.append("recovered-start")
                assert kwargs["cwd"] is None and database.databases["ai_shuffle"] == "before"
                return recovered
            events.append("replacement-start")
            assert updater.state["booting"] == B and updater.state["snapshot"] in database.databases, \
                "the database is copied before the update's code first runs"
            if failure == "popen":
                raise OSError("synthetic spawn failure")
            database.databases["ai_shuffle"] = "migrated"
            return new
        spawn = Mock(side_effect=popen)
        monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
        def stop_group(process, timeout):
            if process is None:
                return
            events.append({id(old): "stop-old", id(new): "stop-new"}.get(id(process), "stop-recovered"))
            if failure == "stop-group" and events.count("stop-old") == 1:
                raise OSError("synthetic process stop failure")
            process.poll.return_value = 0
            if failure == "stop-timeout" and events.count("stop-old") == 1 and process is old:
                assert startup.is_shutting_down
                assert not internal._shutdown_task.done()
                release_flush.set()
                async def finish_stop():
                    await internal._shutdown_task
                runner.run(finish_stop())
        monkeypatch.setattr(supervisor, "stop_group", stop_group)
        if failure == "activating":
            monkeypatch.setattr(updater, "activating", Mock(side_effect=OSError("synthetic state write failure")))

        # Deterministic watch clock, with a guard against silently continuing.
        stop_event = Mock(wraps=threading.Event())
        stop_event.is_set.side_effect = [False, False, False, True]
        monkeypatch.setattr(supervisor, "STOP", stop_event)
        clock = iter(range(100))
        monkeypatch.setattr(supervisor, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        monkeypatch.setattr(supervisor, "READY_PROBE_INTERVAL", 1000)
        monitor_type = supervisor.ReadinessMonitor
        monkeypatch.setattr(supervisor, "ReadinessMonitor", lambda url: monitor_type(url, probe=lambda _: False))
        safe = failure in {"busy", "policy", "files", "preflight-timeout"}
        def still_supervising(_):
            if safe:
                assert not startup.is_shutting_down
                assert old.poll() is None
                assert "stop-old" not in events
        stop_event.wait.side_effect = still_supervising
        updater.install_requested.set()
        tick = Mock()
        monkeypatch.setattr(updater, "tick", tick)
        status = Mock(wraps=updater.publish)
        monkeypatch.setattr(updater, "publish", status)
        result = supervisor.run(Settings(_env_file=None), "run")
        # The supervisor hands the updater the app's lease for the hub feed,
        # read with the lifecycle token from /internal/desktop/source-lease.
        assert updater.lease() == "bGVhc2U" and actions[-1] == "source-lease"

        # The worker is never left down: every outcome keeps supervising an
        # application until the external stop.
        assert result == 0
        assert events[-1] == "db-close"
        if safe:
            assert not startup.is_shutting_down
            flush_mock.assert_not_awaited()
            assert "stop-sessions" not in actions
            assert status.call_args.args == ("ready",)
            assert stop_event.wait.call_count == 2, "old worker remains supervised until external shutdown"
            assert "stop-old" in events
            spawn.assert_called_once()
            assert updater.state == {"pending": B} and database.calls == []
            return
        assert startup.is_shutting_down, "the quiesced old process is never advertised as usable"
        flush_mock.assert_awaited_once()
        assert "stop-old" in events
        if failure == "none":
            assert spawn.call_count == 2
            assert events[-2] == "stop-new", "finally owns the replacement process"
            status.assert_not_called()
            snapshot = "ai_shuffle_rollback_" + B[:12]
            assert updater.state == {"pending": B, "booting": B, "snapshot": snapshot}
            # The next start (power loss, kill) during that first boot puts the
            # copy back instead of blocking or running older code over migrated data.
            restarted = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: False, lambda _: {},
                                         database=database)
            assert restarted.state == {"failed": [B]} and restarted.launch({})[1] is None
            assert database.databases == {"ai_shuffle": "before"}
            assert status_of(restarted)["phase"] == "error" and status_of(restarted)["version"] == B[:12]
            return
        # A failure after the old process was quiesced: it is replaced by a
        # fresh process of the version that ran before, over unchanged data.
        assert events.count("recovered-start") == 1 and events[-2] == "stop-recovered"
        assert database.databases == {"ai_shuffle": "before"}, "no copy is left behind"
        if failure == "popen":
            assert spawn.call_count == 3
            assert updater.state == {"failed": [B]}
            assert status.call_args.args == ("error",) and status.call_args.kwargs["version"] == B
            assert "went back to the previous version" in status.call_args.kwargs["error"]
        else:
            assert spawn.call_count == 2
            assert updater.state == {"pending": B}
            assert status.call_args.args == ("ready",)
            assert "went back to the version it was running" in status.call_args.kwargs["error"]


@pytest.mark.parametrize("configured", ["edge", "nightly"])
@pytest.mark.parametrize("signed_channel", ["edge", "nightly"])
def test_only_a_signed_release_for_the_configured_channel_is_offered(feed, configured, signed_channel):
    updater, publish, _, requests, responses = feed
    # A hub serving an edge build at the stable address cannot relabel it.
    responses["/v1/desktop/manifest"] = publish(channel=signed_channel, version="0.8.18-dev.43+g3667297")
    with pytest.raises(UpdateError, match="signed stable release"):
        updater.check(download=True)
    assert updater.state == {} and [r.url.path for r in requests] == ["/v1/desktop/manifest"]
    edge = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {}, channel=configured)
    edge.build = updater.build
    edge.check(download=True)
    assert requests[-2].url.path == "/v1/desktop/manifest/nightly" and edge.state == {"pending": B}
    # Switching the worker back to stable never activates the staged edge build.
    stable = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {})
    assert stable.state == {}
    with pytest.raises(UpdateError, match="Unknown update channel"):
        ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {}, channel="beta")


@pytest.mark.parametrize("field, value", [("channel", "beta"), ("tree", "short"),
                                          ("version", "latest; rm"), ("notes", "x" * 4001)])
def test_signed_manifest_with_malformed_release_fields_is_refused(feed, field, value):
    updater, publish, _, _, _ = feed
    with pytest.raises(UpdateError, match="Invalid or unsigned"):
        verify_manifest(publish(**{field: value}), updater.public_key)


def test_desktop_launcher_field_is_ignored_by_the_container_updater(feed):
    # #1821 D5: the shared signed manifest gains desktop.launcher (the minimum
    # Windows launcher). The container verifies the same envelope; the field
    # must neither break the signature check nor stop the release staging.
    updater, publish, _, _, _ = feed
    envelope = publish(desktop={"launcher": 3})
    assert verify_manifest(envelope, updater.public_key)["desktop"] == {"launcher": 3}
    updater.check(download=True)
    assert updater.state == {"pending": B}


def test_archive_that_differs_from_the_signed_tree_is_never_built(feed, monkeypatch):
    updater, publish, _, _, _ = feed
    publish(tree="0" * 64)
    built = []
    monkeypatch.setattr(updater, "build", lambda *args: built.append(args))
    with pytest.raises(UpdateError, match="do not match the signed release"):
        updater.check(download=True)
    assert built == [] and updater.state == {} and not updater.directory(B).exists()


def test_staged_tree_is_reverified_before_every_launch(feed):
    updater, publish, _, _, _ = feed
    publish(version="1.2.3", notes="Faster startup.")
    updater.check(download=True)
    _command, _cwd, env = updater.launch({}, activate=True)
    assert env["APP_VERSION"] == "1.2.3"
    source = updater.directory(B) / "source/source.txt"
    source.write_text("backdoor")
    with pytest.raises(UpdateError, match="changed after download"):
        updater.launch({}, activate=True)
    source.write_text("content")
    listing = updater.directory(B) / "tree.txt"
    listing.write_bytes(listing.read_bytes().replace(b"source.txt", b"sourcx.txt"))
    with pytest.raises(UpdateError, match="listing does not match"):
        updater.launch({}, activate=True)
    listing.unlink()
    with pytest.raises(UpdateError, match="listing is missing"):
        updater.launch({}, activate=True)


def test_release_name_and_notes_reach_the_status(feed):
    updater, publish, _, _, _ = feed
    publish(version="1.2.3", notes="Faster startup.")
    updater.check()
    status = json.loads((updater.root / "status.json").read_text())
    assert status["phase"] == "available" and status["release"] == "1.2.3"
    assert status["notes"] == "Faster startup." and status["channel"] == "stable"
    updater.check(download=True)
    reopened = ContainerUpdates(updater.root, updater.image, updater.public_key, lambda: True, lambda _: {})
    status = json.loads((reopened.root / "status.json").read_text())
    assert status["phase"] == "ready" and status["notes"] == "Faster startup."


def test_published_tree_digest_matches_what_the_updater_extracts(tmp_path):
    import subprocess
    from scripts import source_archive
    repo = tmp_path / "repo"
    for name, content in {"app/main.py": "print(1)", "fluid/fluid/__init__.py": "", "frontend/package.json": "{}",
                          "requirements.lock": "x", "requirements-native.lock": "x",
                          "docs/reviews/secret.md": "internal"}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)

    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x")
    revision = run("rev-parse", "HEAD")
    result = source_archive.build(revision, tmp_path / "source.zip", repo)
    files = extract_source(tmp_path / "source.zip", tmp_path / "source", revision)
    assert hashlib.sha256(tree_lines(files)).hexdigest() == result["tree"]
    assert not (tmp_path / "source/docs/reviews").exists()


# --- The image/source contract (#1821) ---------------------------------------

from docker import update_contract as contract  # noqa: E402
from docker.application_updates import (HANDOVER_FILES, IMAGE_NOTICE, IMAGE_REQUIRED,  # noqa: E402
                                        handover_target, missing_capabilities)

ROOT = Path(__file__).resolve().parents[1]
LEGACY = contract.LEGACY_RUNTIME
# /app/docker/image.json of the images installed today (format 1, ce7c7fde7).
TODAYS_IMAGE = {"format": 1, "revision": A, "sequence": 1, "runtime": LEGACY}
NEW_IMAGE = contract.image_record(A, 1)
NEEDS_NEW_BASE = {"runtime": contract.runtime(("base-1", "python-3.13")), "requires": ["base-1", "python-3.13"],
                  "recommends": []}


def on_image(updater, image, module=None):
    """The fixture's updater state, read by an updater running on ``image``."""
    factory = module.ContainerUpdates if module else ContainerUpdates
    other = factory(updater.root, image, updater.public_key, lambda: True, lambda _: {})
    other.build = updater.build
    return other


def status_of(updater):
    return json.loads((updater.root / "status.json").read_text())


@pytest.fixture
def baked_updater(tmp_path):
    """docker/application_updates.py as baked into the images installed today."""
    shown = subprocess.run(["git", "show", "ce7c7fde7:docker/application_updates.py"], cwd=ROOT, capture_output=True)
    if shown.returncode:
        pytest.skip("the installed image's revision ce7c7fde7 is not in this clone")
    path = tmp_path / "baked_application_updates.py"
    path.write_bytes(shown.stdout)
    spec = importlib.util.spec_from_file_location("baked_application_updates", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_todays_image_takes_a_release_published_under_the_new_contract(feed, baked_updater):
    # The updater baked into today's images compares container.runtime and
    # nothing else; base-1 releases publish the frozen string it carries.
    updater, publish, _, _, _ = feed
    publish(container=contract.manifest_fields())
    old = on_image(updater, TODAYS_IMAGE, baked_updater)
    old.check(download=True)
    assert old.state == {"pending": B}


def test_todays_image_refuses_a_release_that_needs_a_new_base(feed, baked_updater):
    updater, publish, _, _, _ = feed
    publish(container=NEEDS_NEW_BASE)
    old = on_image(updater, TODAYS_IMAGE, baked_updater)
    old.check(download=True)
    assert old.state == {} and status_of(old)["phase"] == "image-required"


def test_new_updater_on_todays_image_takes_base_1_releases(feed):
    updater, publish, _, _, _ = feed
    publish(container=contract.manifest_fields())
    current = on_image(updater, TODAYS_IMAGE)
    current.check(download=True)
    assert current.state == {"pending": B}


@pytest.mark.parametrize("image", [TODAYS_IMAGE, NEW_IMAGE])
def test_release_needing_a_new_base_is_refused_in_plain_words_naming_it(feed, image, monkeypatch):
    updater, publish, _, _, _ = feed
    publish(revision=C, sequence=3, container=NEEDS_NEW_BASE)
    current = on_image(updater, image)
    current.check(download=True)
    assert current.state == {}
    status = status_of(current)
    # The offered revision, not the running one, or it reads as up to date (#1901).
    missing = "python-3.13" if "provides" in image else "runtime"
    assert (status["phase"], status["message"], status["version"]) == (
        "image-required", IMAGE_REQUIRED + " No compatible runtime overlay for: " + missing, C[:12])
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(updater.root))
    monkeypatch.setattr(policy.database, "get_system_setting", lambda key: None)
    assert policy.status()["message"] == status["message"]


def test_releases_published_before_capabilities_still_install_on_new_images(feed):
    updater, publish, _, _, _ = feed
    publish(runtime=LEGACY)
    current = on_image(updater, NEW_IMAGE)
    current.check(download=True)
    assert current.state == {"pending": B}
    publish(revision=C, sequence=3, runtime="e" * 64)
    other = on_image(updater, dict(NEW_IMAGE, sequence=2))
    other.check()
    assert status_of(other)["phase"] == "image-required"


def test_a_coming_requirement_is_announced_ahead_and_this_update_still_installs(feed, monkeypatch):
    updater, publish, _, _, _ = feed
    publish(container={**contract.manifest_fields(), "recommends": ["python-3.13"]})
    current = on_image(updater, NEW_IMAGE)
    current.check(download=True)
    assert current.state == {"pending": B}
    assert status_of(current)["notice"] == IMAGE_NOTICE
    monkeypatch.setenv("SHUFFLE_CONTAINER_UPDATES", str(updater.root))
    monkeypatch.setattr(policy.database, "get_system_setting", lambda key: None)
    assert policy.status()["notice"] == IMAGE_NOTICE
    # An image that already provides it is told nothing.
    publish(container={**contract.manifest_fields(), "recommends": ["source-supervisor"]})
    on_image(updater, NEW_IMAGE).check()
    assert status_of(updater)["notice"] is None


@pytest.mark.parametrize("requires", ["base-1", ["Base 1"], [1], ["x"] * 33, {"base-1": True}])
def test_manifest_with_malformed_capabilities_is_rejected(feed, requires):
    updater, publish, _, _, _ = feed
    envelope = publish(container={"requires": requires})
    with pytest.raises((UpdateError, ValueError)):
        verify_manifest(envelope, updater.public_key)


@pytest.mark.parametrize(("container", "image", "missing"), [
    ({"runtime": LEGACY}, TODAYS_IMAGE, []),
    ({"runtime": LEGACY, "requires": ["base-1"]}, TODAYS_IMAGE, []),
    (NEEDS_NEW_BASE, TODAYS_IMAGE, ["runtime"]),
    (NEEDS_NEW_BASE, NEW_IMAGE, ["python-3.13"]),
    ({"runtime": LEGACY, "requires": ["base-1"]}, NEW_IMAGE, []),
    ({"runtime": "e" * 64}, NEW_IMAGE, ["runtime"]),
])
def test_missing_capabilities(container, image, missing):
    assert missing_capabilities(container, image) == missing


def installed(updater, publish, image=NEW_IMAGE, files=HANDOVER_FILES):
    """An active, healthy update whose source carries ``files``."""
    publish(container=contract.manifest_fields(),
            entries={**{f"ai-shuffle-{B}/source.txt": "content"},
                     **{f"ai-shuffle-{B}/{name}": "# " + name for name in files}})
    current = on_image(updater, image)
    current.check(download=True)
    current.launch({}, activate=True)
    current.activating()
    current.healthy()
    assert current.state == {"active": B}
    return current


def test_handover_target_is_the_verified_healthy_active_update(feed):
    updater, publish, _, _, _ = feed
    current = installed(updater, publish)
    directory = current.directory(B)
    assert handover_target(current.root, NEW_IMAGE, current.public_key) == (
        B, directory / "venv/bin/python", directory / "source/docker/container_runtime.py")


def test_no_handover_without_an_active_update_or_while_it_is_unproven(feed):
    updater, publish, _, _, _ = feed
    assert handover_target(updater.root, NEW_IMAGE, updater.public_key) is None
    current = installed(updater, publish)
    state = current.root / "state.json"
    for extra in ({"booting": B}, {"blocked": B}, {"failed": [B]}):
        state.write_text(json.dumps({"active": B, **extra}))
        assert handover_target(current.root, NEW_IMAGE, current.public_key) is None, extra
    state.write_text(json.dumps({"active": B}))
    # A newer image runs its own code (ContainerUpdates then resets the state).
    assert handover_target(current.root, dict(NEW_IMAGE, sequence=3), current.public_key) is None
    # An image without what the update requires.
    container = dict(NEEDS_NEW_BASE)
    assert missing_capabilities(container, {"provides": ["base-1"], "runtime": LEGACY})
    assert handover_target(current.root, dict(NEW_IMAGE, provides=[]), current.public_key) is None


def test_a_supervisor_that_never_reached_ready_is_not_handed_over_to_again(feed):
    updater, publish, _, _, _ = feed
    current = installed(updater, publish)
    (current.root / "handover.json").write_text(json.dumps({"revision": B}))
    with pytest.raises(UpdateError, match="did not reach a healthy start"):
        handover_target(current.root, NEW_IMAGE, current.public_key)
    # A marker for another revision does not block this one.
    (current.root / "handover.json").write_text(json.dumps({"revision": C}))
    assert handover_target(current.root, NEW_IMAGE, current.public_key)[0] == B


def test_handover_refuses_changed_or_unsigned_supervisor_code(feed):
    updater, publish, _, _, _ = feed
    current = installed(updater, publish)
    script = current.directory(B) / "source/docker/container_runtime.py"
    script.write_text("import os; os.system('evil')")
    with pytest.raises(UpdateError, match="changed after download"):
        handover_target(current.root, NEW_IMAGE, current.public_key)


def test_handover_needs_the_supervisor_files_in_the_signed_listing(feed):
    updater, publish, _, _, _ = feed
    current = installed(updater, publish, files=("docker/application_updates.py",))
    # Present on disk but not signed: never run.
    script = current.directory(B) / "source/docker/container_runtime.py"
    script.write_text("planted")
    for name in ("chrome-supervisor.sh", "start-chrome.sh"):
        (script.parent / name).write_text("planted")
    assert handover_target(current.root, NEW_IMAGE, current.public_key) is None
