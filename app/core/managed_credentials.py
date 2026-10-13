"""Installation-owned hub credentials that work before administrator login.

The existing password-encrypted secret is retained for password rotation and
recovery. A second, separately encrypted copy uses a random key kept only on this
installation's persistent disk, never in the shared database. It authorizes only
the exact managed binding; it cannot unlock other application secrets.
"""
import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import threading

from cryptography.fernet import Fernet

from app.core import encryption
from app.core.config import settings
from app.core.private_storage import owner_uid, protect_private_path, service_files
from app.db import database

_PREFIX = 'installation:v1:'
_lock = threading.Lock()


def _identity(binding):
    return {key: binding[key] for key in ('hub_url', 'server_id', 'linked_by')}


def _setting_key():
    return 'mobile_managed_credential:' + settings.effective_instance_id


def _windows_protect(value, *, decrypt=False):
    """DPAPI binds a Windows key file to the current OS user, without UI."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError('Installation key protection is unavailable')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


@service_files()
def _key_path():
    directory = settings.effective_data_dir / '.managed-connection'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or
            getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
        raise ValueError('Installation key directory must be local storage')
    if os.name != 'nt':
        if info.st_uid != owner_uid():
            raise ValueError('Installation key directory has another owner')
        directory.chmod(0o700)
    else:
        protect_private_path(directory, directory=True)
    name = hashlib.sha256(settings.effective_instance_id.encode()).hexdigest() + '.key'
    return directory / name


def _sync_directory(path):
    if os.name == 'nt':
        return  # Windows publication uses MoveFileExW with WRITE_THROUGH.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@service_files()
def _publish_key(temporary, path):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # WRITE_THROUGH, deliberately without REPLACE_EXISTING.
        if not move(str(temporary), str(path), 8):
            error = ctypes.get_last_error()
            if error in (80, 183):
                raise FileExistsError('Installation key already exists')
            raise OSError('Installation key publication failed')
    else:
        # An exclusive hard link publishes only complete, fsynced bytes. A
        # process death may retain the private temporary link, which is harmless.
        os.link(temporary, path)


@service_files()
def _sync_key_location(path):
    # Always sync both, including retries where an earlier process created the
    # directory or key but died before its directory entries became durable.
    _sync_directory(path.parent)
    _sync_directory(path.parent.parent)


@service_files()
def _read_key(path):
    path_info = path.lstat()
    if (not stat.S_ISREG(path_info.st_mode) or
            getattr(path_info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
        raise ValueError('Invalid installation key file')
    if os.name == 'nt':
        protect_private_path(path)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or
                getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise ValueError('Invalid installation key file')
        if os.name != 'nt' and (info.st_uid != owner_uid() or info.st_mode & 0o077):
            raise ValueError('Installation key file permissions are unsafe')
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError('Invalid installation key file')
    if os.name == 'nt':
        if not raw.startswith(b'dpapi:'):
            raise ValueError('Installation key file is not protected')
        raw = _windows_protect(base64.b64decode(raw[6:], validate=True), decrypt=True)
    Fernet(raw)  # Validate before using it; never silently replace an existing key.
    return raw


@service_files()
def _key(*, create=False):
    path = _key_path()
    try:
        key = _read_key(path)
        if create:
            _sync_key_location(path)
        return key
    except FileNotFoundError:
        if not create:
            raise
    key = Fernet.generate_key()
    raw = b'dpapi:' + base64.b64encode(_windows_protect(key)) if os.name == 'nt' else key
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            protect_private_path(temporary)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _publish_key(temporary, path)
        except FileExistsError:
            key = _read_key(path)
    finally:
        # On Windows successful publication moved the temporary file already.
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    _sync_key_location(path)
    return key


def secret_for(binding):
    """Load an owned managed secret, migrating once while legacy encryption is unlocked.

    Missing or corrupt existing installation credentials fail closed. Direct
    pairings and other installations continue to use their original encryption.
    No token is issued here: every caller still obtains live hub authorization.
    """
    if binding.get('linked_by') != 'managed:' + settings.effective_instance_id:
        return encryption.decrypt_value(binding['encrypted_secret'])
    identity = _identity(binding)
    with _lock:
        stored = database.get_system_setting(_setting_key())
        if stored is not None:
            if not stored.startswith(_PREFIX):
                raise ValueError('Invalid installation credential')
            data = json.loads(Fernet(_key()).decrypt(stored[len(_PREFIX):].encode()))
            if data.get('binding') != identity:
                raise ValueError('Installation credential belongs to another binding')
            secret = data.get('secret')
        else:
            # This succeeds only after administrator unlock, or the existing
            # ADMIN_PASSWORD startup flow. Never replace the original ciphertext.
            secret = encryption.decrypt_value(binding['encrypted_secret'])
        if not isinstance(secret, str) or not re.fullmatch(r'[a-f0-9]{64}', secret):
            raise ValueError('Invalid managed credential')
        if stored is None:
            encrypted = Fernet(_key(create=True)).encrypt(json.dumps({'binding': identity, 'secret': secret}).encode())
            database.set_system_setting(_setting_key(), _PREFIX + encrypted.decode())
        return secret
