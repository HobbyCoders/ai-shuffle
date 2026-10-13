"""Only disposable generated keys are used; never inspect an install's secrets."""
import errno
import json
import stat
import os
import subprocess
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.core import credential_keys, managed_credentials, preview_access, private_storage
from app.network import identity


def assert_private(path, *, directory=False):
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == (0o700 if directory else 0o600)
        return
    # Query ACL metadata, not file bytes. Pass paths as data, never script text.
    script = (
        '$ErrorActionPreference = "Stop"; '
        '$acl = Get-Acl -LiteralPath $env:SHUFFLE_TEST_PATH; '
        '$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; '
        '$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])); '
        '@{protected=$acl.AreAccessRulesProtected; count=$rules.Count; '
        'onlyUser=(@($rules | Where-Object {$_.IdentityReference.Value -ne $sid}).Count -eq 0); '
        'fullControl=(@($rules | Where-Object {$_.FileSystemRights -ne "FullControl"}).Count -eq 0); '
        'inherited=(@($rules | Where-Object {$_.IsInherited}).Count -gt 0)} | ConvertTo-Json -Compress'
    )
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    env["SHUFFLE_TEST_PATH"] = str(path)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout) == {
        "protected": True, "count": 1, "onlyUser": True, "fullControl": True, "inherited": False,
    }


def test_private_permissions_and_repeated_protection(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir()
    private_storage.protect_private_path(directory, directory=True)
    assert_private(directory, directory=True)
    path = directory / "test.key"
    path.write_text("not-a-real-secret", encoding="utf-8")
    private_storage.protect_private_path(path)
    private_storage.protect_private_path(path)
    assert_private(path)
    assert path.read_text(encoding="utf-8") == "not-a-real-secret"


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse point metadata")
def test_rejects_reparse_point(tmp_path, monkeypatch):
    import stat

    monkeypatch.setattr(type(tmp_path), "lstat", lambda self: SimpleNamespace(
        st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT))
    with pytest.raises(OSError, match="regular local"):
        private_storage.protect_private_path(tmp_path / "test.key")


def test_rejects_wrong_object_type(tmp_path):
    with pytest.raises(OSError, match="regular local"):
        private_storage.protect_private_path(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and ownership checks")
def test_rejects_links_and_foreign_owner(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_text("fixture", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError, match="regular local"):
        private_storage.protect_private_path(link)
    monkeypatch.setattr(private_storage.os, "geteuid", lambda: target.stat().st_uid + 1)
    with pytest.raises(OSError, match="another owner"):
        private_storage.protect_private_path(target)


@pytest.mark.parametrize("kind", ["managed", "credential", "identity", "preview"])
def test_real_key_producers_publish_private_files(tmp_path, monkeypatch, kind):
    settings = SimpleNamespace(effective_data_dir=tmp_path, effective_instance_id="test-install")
    for module in (managed_credentials, credential_keys, identity):
        monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(preview_access, "_key_path", lambda: tmp_path / "preview-access.key")
    if kind == "managed":
        generated = managed_credentials._key(create=True)
        path = managed_credentials._key_path()
        assert managed_credentials._key() == generated
    elif kind == "credential":
        database_id = "11111111-1111-1111-1111-111111111111"
        generated = Fernet.generate_key()
        credential_keys._publish(database_id, generated)
        path = credential_keys.key_path(database_id)
        assert managed_credentials._read_key(path) == generated
    elif kind == "identity":
        generated = identity.DeviceIdentity(b"s" * 32, b"e" * 32, 1, "test")
        path = identity._path()
        identity._write_new(path, generated)
        assert identity._read(path) == generated
    else:
        generated = preview_access._key()
        path = preview_access._key_path()
        assert preview_access._key() == generated
    assert_private(path)
    if kind != "preview":
        assert_private(path.parent, directory=True)
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list(tmp_path.rglob(".pending-*"))


@pytest.mark.skipif(os.name != "nt", reason="Native Windows pre-write handle check")
def test_preview_temp_is_private_before_any_read_handle_can_open(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.CreateRestrictedToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(SidAndAttributes),
                                           ctypes.POINTER(wintypes.HANDLE)]
    advapi.ImpersonateLoggedOnUser.argtypes = [wintypes.HANDLE]
    advapi.RevertToSelf.argtypes = []
    token, restricted = wintypes.HANDLE(), wintypes.HANDLE()
    everyone = ctypes.c_void_p()
    retained = []
    # Only the disposable fixture directory gets this deliberately broad ACL.
    subprocess.run(["icacls.exe", str(tmp_path), "/grant", "*S-1-1-0:(OI)(CI)(R)"],
                   check=True, capture_output=True, timeout=30)

    def open_as_everyone(path):
        # A restricting Everyone SID makes an owner-only ACE insufficient.
        # No other account or persistent token/account changes are needed.
        assert advapi.ImpersonateLoggedOnUser(restricted)
        try:
            handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
            error = ctypes.get_last_error()
        finally:
            assert advapi.RevertToSelf()
        if handle == wintypes.HANDLE(-1).value:
            return None, error
        retained.append(handle)
        return handle, 0

    try:
        assert advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x000A, ctypes.byref(token))
        assert advapi.ConvertStringSidToSidW("S-1-1-0", ctypes.byref(everyone))
        sid = SidAndAttributes(everyone, 0)
        assert advapi.CreateRestrictedToken(token, 1, 0, None, 0, None, 1,
                                            ctypes.byref(sid), ctypes.byref(restricted))
        control = tmp_path / "inherited-control.txt"
        control.write_text("public-fixture", encoding="utf-8")
        handle, _ = open_as_everyone(control)
        assert handle is not None  # proves the hostile-parent setup is effective

        path = tmp_path / "preview-access.key"
        monkeypatch.setattr(preview_access, "_key_path", lambda: path)
        original = private_storage.private_mkstemp
        before_write = []

        def inspect_creation(**kwargs):
            fd, name = original(**kwargs)
            handle, error = open_as_everyone(name)
            before_write.append((handle, error))
            return fd, name

        monkeypatch.setattr(private_storage, "private_mkstemp", inspect_creation)
        preview_access._key()
        assert len(before_write) == 1
        handle, error = before_write[0]
        # If a handle was exposed, inspect it after writing/rename as the
        # original attack does; changing a DACL cannot revoke that handle.
        leaked_bytes = wintypes.DWORD()
        if handle is not None:
            buffer = ctypes.create_string_buffer(128)
            assert kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(leaked_bytes), None)
        assert leaked_bytes.value == 0
        assert handle is None and error == 5  # ERROR_ACCESS_DENIED before writing
        assert_private(path)
    finally:
        for handle in retained:
            kernel.CloseHandle(handle)
        if restricted.value:
            kernel.CloseHandle(restricted)
        if token.value:
            kernel.CloseHandle(token)
        if everyone.value:
            kernel.LocalFree(everyone)


@pytest.mark.parametrize("mode,owner", [(0o644, 123), (0o640, 123), (0o600, 124)])
def test_unsafe_creation_mount_writes_no_preview_key_bytes(tmp_path, monkeypatch, mode, owner):
    import tempfile

    path = tmp_path / "preview-access.key"
    monkeypatch.setattr(preview_access, "_key_path", lambda: path)
    original = tempfile.mkstemp
    created = []
    closed_sizes = []
    writes = []
    original_fdopen = os.fdopen

    def unsafe_mount(**kwargs):
        fd, name = original(**kwargs)
        created.append((fd, name))
        return fd, name

    def inspect_before_close(fd):
        closed_sizes.append(os.fstat(fd).st_size)
        assert os.read(fd, 128) == b""
        os.close(fd)

    def observe_writer(*args, **kwargs):
        writes.append(True)
        return original_fdopen(*args, **kwargs)

    # Simulate mode/owner returned by a mount ignoring creation permissions.
    # Only this module's OS view changes; real descriptor I/O stays available.
    monkeypatch.setattr(private_storage, "os", SimpleNamespace(
        name="posix", geteuid=lambda: 123, close=inspect_before_close,
        fstat=lambda fd: SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=owner)))
    monkeypatch.setattr(tempfile, "mkstemp", unsafe_mount)
    monkeypatch.setattr(preview_access.os, "fdopen", observe_writer)
    with pytest.raises(OSError, match="temporary file permissions"):
        preview_access._key()
    assert writes == []
    assert len(created) == 1 and closed_sizes == [0]
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(OSError) as exc:
        os.fstat(created[0][0])
    assert exc.value.errno == errno.EBADF


@pytest.mark.parametrize("error", [errno.EROFS, errno.EPERM])
@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_private_preview_read_needs_no_chmod(tmp_path, monkeypatch, error, mode):
    path = tmp_path / "preview-access.key"
    path.write_text("ab" * 32, encoding="utf-8")
    # Simulate POSIX mount behavior on every host, without altering pathlib's OS.
    monkeypatch.setattr(private_storage, "os", SimpleNamespace(name="posix", geteuid=lambda: 123))
    monkeypatch.setattr(type(path), "lstat", lambda self: SimpleNamespace(
        st_mode=stat.S_IFREG | mode, st_uid=123))

    def reject_chmod(*args, **kwargs):
        raise OSError(error, "fixture mount rejects chmod")

    monkeypatch.setattr(type(path), "chmod", reject_chmod)
    assert preview_access._read_key(path) == bytes.fromhex("ab" * 32)


@pytest.mark.parametrize("error", [errno.EROFS, errno.EPERM])
def test_unsafe_mount_fails_when_required_chmod_is_rejected(tmp_path, monkeypatch, error):
    path = tmp_path / "test.key"
    monkeypatch.setattr(private_storage, "os", SimpleNamespace(name="posix", geteuid=lambda: 123))
    monkeypatch.setattr(type(path), "lstat", lambda self: SimpleNamespace(
        st_mode=stat.S_IFREG | 0o644, st_uid=123))

    def reject_chmod(*args, **kwargs):
        raise OSError(error, "fixture mount rejects chmod")

    monkeypatch.setattr(type(path), "chmod", reject_chmod)
    with pytest.raises(OSError) as exc:
        private_storage.protect_private_path(path)
    assert exc.value.errno == error


def test_required_chmod_must_actually_protect_path(tmp_path, monkeypatch):
    path = tmp_path / "test.key"
    monkeypatch.setattr(private_storage, "os", SimpleNamespace(name="posix", geteuid=lambda: 123))
    monkeypatch.setattr(type(path), "lstat", lambda self: SimpleNamespace(
        st_mode=stat.S_IFREG | 0o644, st_uid=123))
    monkeypatch.setattr(type(path), "chmod", lambda *args: None)
    with pytest.raises(OSError, match="not enforced"):
        private_storage.protect_private_path(path)


@pytest.mark.skipif(os.name != "nt", reason="Win32 ACL failure path")
def test_acl_failure_leaves_no_published_preview_key(tmp_path, monkeypatch):
    path = tmp_path / "preview-access.key"
    monkeypatch.setattr(preview_access, "_key_path", lambda: path)

    def fail(*args, **kwargs):
        raise OSError("Private storage ACL protection is unavailable")

    monkeypatch.setattr(private_storage, "_windows_private_acl", fail)
    with pytest.raises(OSError, match="ACL protection"):
        preview_access._key()
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp"))
