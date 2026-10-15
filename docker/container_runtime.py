"""Single-container PostgreSQL lifecycle and database-only maintenance.

No app startup imports, shell credential parsing, automatic recovery edits, or
external restore. Run as appuser (root is dropped before opening a database).
The lifetime lock belongs to the supervisor, never to a daemonized child.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time


MAJOR = "16"
# Readiness supervision: `restart: unless-stopped` only restarts an exited
# container, so an application that is alive but no longer answers
# /health/ready is probed here and the supervisor exits non-zero after
# READY_PROBE_FAILURES consecutive failures (2 minutes at these values).
# Until the first successful probe a larger allowance covers first-boot
# migrations; the probe itself is one cheap loopback GET per interval.
READY_PROBE_INTERVAL = 30.0
READY_PROBE_TIMEOUT = 5.0
READY_PROBE_FAILURES = 4
READY_PROBE_STARTUP_FAILURES = 30
APP_USER = "ai_shuffle"
APP_DB = "ai_shuffle"
ADMIN = "shuffle_admin"
STOP = threading.Event()
# Hand-over (#1821). An image that provides "source-supervisor" starts the
# active update's own copy of this file, passing SHUFFLE_HANDED_OVER (the
# revision), SHUFFLE_IMAGE_DIR (its image.json and trust key) and
# SHUFFLE_HANDOVER (this protocol number). Every later copy of this file must
# keep accepting what protocol 1 images pass, because those images stay in the
# field. SHUFFLE_SOURCE_SUPERVISOR=0 in the container settings turns it off.
HANDOVER_PROTOCOL = "1"
HANDOVER_TRIAL = "handover.json"
_handover_confirmed = False
# Opt-in agent account split (#1957, agent_isolation.py). With it on, the
# supervisor itself runs with a PATH of root-owned directories only; the app
# and its children get the image's full PATH back from this.
_agent_path = None
_isolation = None
_ISOLATION_FILE = Path(__file__).with_name("agent_isolation.py")


# An application update copies the database before the new code first starts
# and puts the copy back if that start fails (docker/application_updates.py).
ROLLBACK_PATTERN = r"^ai_shuffle_(rollback|failed)_[0-9a-f]{12}$"
SNAPSHOT_MARGIN = 256 * 1024**2
SNAPSHOT_TIMEOUT = 1800
# An application that exits or stops answering readiness is restarted in place,
# without restarting the container, at most this often. Past that the
# supervisor exits and the container's restart policy takes over (#2112).
RELAUNCH_LIMIT = 3
RELAUNCH_WINDOW = 15 * 60.0


def _rollback_name(name, kind):
    if not re.fullmatch(ROLLBACK_PATTERN, name) or not name.startswith(f"ai_shuffle_{kind}_"):
        raise RuntimeFailure("invalid update database name")


class RuntimeFailure(Exception):
    """Deliberately secret-free operator diagnostic."""


class RestartRejected(RuntimeFailure):
    """Preflight failed before any request could quiesce the running worker.

    ``reason`` is shown to the user when it says what to do next."""

    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason


def log(message):
    print(f"container runtime: {message}", file=sys.stderr, flush=True)


def load_settings():
    # This uses Pydantic's sources and the app's normalization, including the
    # explicit-fields set (which includes env-file values even equal to defaults).
    from app.core.config import Settings, _resolve_env_file
    return Settings(_env_file=_resolve_env_file())


def safe_path(path):
    """Refuse symlinks in every existing path component; do not resolve them."""
    path = Path(os.path.abspath(path))
    for item in reversed((path, *path.parents)):
        if item.is_symlink():
            raise RuntimeFailure("symlinked database storage is not supported")
    return path


def check_owned(path, *, directory=False, private=True):
    safe_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise RuntimeFailure("retained database state is missing; operator recovery required") from None
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeFailure("database storage ownership/type mismatch; stop and repair ownership explicitly")
    if private and info.st_mode & 0o077:
        raise RuntimeFailure("database private state must have owner-only permissions")
    if not directory and info.st_nlink != 1:
        raise RuntimeFailure("hard-linked database state is not supported")


def write_private(path, text, *, replace=False):
    """Durable 0600 state; replacement is atomic and never follows a symlink."""
    safe_path(path)
    target = path.with_name(path.name + ".new-" + secrets.token_hex(6)) if replace else path
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(target, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if replace and target.exists():
            target.unlink()


def read_record(path):
    check_owned(path)
    try:
        result = json.loads(path.read_text())
        if not isinstance(result, dict) or result.get("version") != 1:
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise RuntimeFailure("unrecognized database lifecycle record; operator recovery required") from None


class ClusterLock:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def acquire(self):
        safe_path(self.path)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            check_owned(self.path)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.close()
            return False
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def kill_group(process, sig):
    if process is not None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass


def stop_group(process, timeout):
    if process is None:
        return
    kill_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The leader exiting is not evidence its grandchildren exited.
        kill_group(process, signal.SIGKILL)
        process.wait(timeout=5)


# What PostgreSQL and its client tools take from the supervisor's environment.
# Everything else stays out: the supervisor carries the app's secrets
# (SECRET_KEY, OWNER_PASSWORD, provider keys, ...), and a same-uid process can
# read an exec'd child's start-up environment through /proc/<pid>/environ.
# LD_LIBRARY_PATH is the operator's own setting, not a secret: a PostgreSQL
# whose libraries are not on the default search path (an unpacked or
# relocated install, as the local boot smoke tests use) cannot start without it.
_POSTGRES_ENV_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "LD_LIBRARY_PATH",
})


def postgres_base_env(environ=None):
    """The allow-listed part of ``environ`` (default os.environ) for PostgreSQL:
    path, library path, home, user, locale and time zone. No PG* is inherited
    either; the callers set the ones they mean."""
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items() if k in _POSTGRES_ENV_NAMES or k.startswith("LC_")}
    env.update(isolation().service_env())
    return env


def chrome_env(environ):
    """The environment for chrome-supervisor, start-chrome and Chromium.

    They need the display, profile, proxy and egress-policy settings (CHROME_*,
    BROWSER_*, HOME, PATH, the app's own non-secret settings, which
    ``python -m app.core.browser_egress`` reads to compute the policy). Host
    secrets go (the same list the hooks environment drops: SECRET_KEY,
    DATABASE_*, PG*, *_TOKEN including SHUFFLE_DESKTOP_LIFECYCLE_TOKEN,
    *_PASSWORD, ...), and so do the provider keys that list lets agent
    children keep: the browser needs no credential at all.
    """
    from fluid.tools.child_env import ALLOWED_CREDENTIAL_NAMES, is_host_secret
    return {k: v for k, v in environ.items()
            if not is_host_secret(k)
            and k.upper() not in ALLOWED_CREDENTIAL_NAMES
            and not k.upper().endswith("_KEY")}


PR_SET_DUMPABLE = 4


def harden_process_introspection(settings):
    """Mark the supervisor non-dumpable on Linux, as the app does itself.

    The supervisor's environment holds every .env/compose secret, and a same-uid
    process (a hook, the CLI's Bash, a kernel) could otherwise read it through
    /proc/<pid>/environ. A root start that drops privileges is already
    non-dumpable, a ``--user`` start is not. ``ALLOW_PROCESS_INTROSPECTION=true``
    keeps it dumpable. Best effort; children are dumpable again after execve.
    """
    if not sys.platform.startswith("linux") or getattr(settings, "allow_process_introspection", False):
        return False
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl failed")
    except Exception as error:
        log(f"could not mark the supervisor non-dumpable ({type(error).__name__})")
        return False
    return True


def client_env(url):
    # No inherited libpq service/password/options may silently redirect a
    # maintenance command. The normalized DSN is environment, never argv/logs.
    env = postgres_base_env()
    env.update(PGCONNECT_TIMEOUT="3", LC_ALL="C")
    if url:
        from urllib.parse import urlsplit, unquote, parse_qsl
        parts = urlsplit(url)
        if parts.scheme not in {"postgres", "postgresql"} or parts.hostname != "127.0.0.1":
            raise RuntimeFailure("managed database URL must identify the loopback host")
        env.update(PGHOST=parts.hostname, PGPORT=str(parts.port or 5432),
            PGUSER=unquote(parts.username or ""), PGPASSWORD=unquote(parts.password or ""),
            PGDATABASE=unquote(parts.path.lstrip("/")))
        for key, value in parse_qsl(parts.query):
            if key in {"sslmode", "sslcert", "sslkey", "sslrootcert", "sslcrl"}:
                env["PG" + key.upper()] = value
    return env


class Database:
    def __init__(self, settings, *, port=5432):
        self.settings = settings
        self.mode = "embedded"
        self.root = Path(settings.effective_data_dir).absolute()
        self.pg = self.root / "postgres"
        self.data = self.pg / "data"
        self.mode_file = self.root / "database-mode.json"
        self.port = port
        self.process = None
        self.lock = None
        self.socket = None
        self.private_runtime = None
        self.pg_log = None
        self.password = None
        self.identifier = None
        self.url = None

    def command(self, args, *, env=None, text=None, source=None, output=None, timeout=30, reject_stderr=False):
        process = subprocess.Popen(args, env=env or client_env(""),
            stdin=source if source is not None else subprocess.PIPE,
            stdout=output if output is not None else subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, **isolation().service_spawn())
        deadline = time.monotonic() + timeout
        first = True
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(input=text.encode() if first and text is not None else None, timeout=0.2)
                    if process.returncode or (reject_stderr and stderr):
                        raise RuntimeFailure(f"{Path(args[0]).name} failed; no credentials or subprocess output are logged")
                    return (stdout or b"").decode()
                except subprocess.TimeoutExpired:
                    first = False
                    if STOP.is_set() or time.monotonic() >= deadline:
                        raise RuntimeFailure("database command interrupted or timed out")
                    if self.process is not None and self.process.poll() is not None:
                        raise RuntimeFailure("managed PostgreSQL exited unexpectedly")
        finally:
            if process.poll() is None:
                kill_group(process, signal.SIGKILL)
                process.communicate(timeout=5)

    def admin_env(self):
        env = client_env("")
        env.update(PGHOST=str(self.socket), PGPORT=str(self.port), PGUSER=ADMIN, PGDATABASE="postgres")
        return env

    def query(self, sql="SELECT 1", *, admin=False):
        env = self.admin_env() if admin else client_env(self.url)
        return self.command(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1"], env=env, text=sql, timeout=10).strip()

    def admin_script(self, sql, *, timeout=10):
        """Statements run one at a time (psql autocommit) on the admin socket."""
        return self.command(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1"], env=self.admin_env(),
                            text=sql, timeout=timeout).strip()

    # Update rollback copies (docker/application_updates.py). Only these names
    # are ever created, renamed or dropped here; pg_hba admits the app role to
    # APP_DB alone, so nothing but the admin socket can reach a copy.
    def update_databases(self):
        listed = self.query(f"SELECT datname FROM pg_database WHERE datname ~ '{ROLLBACK_PATTERN}'", admin=True)
        return [name for name in listed.splitlines() if re.fullmatch(ROLLBACK_PATTERN, name)]

    def snapshot_space(self):
        """(free, needed) bytes on the data volume for one more copy of APP_DB."""
        size = int(self.query(f"SELECT pg_database_size('{APP_DB}')", admin=True))
        return shutil.disk_usage(self.data).free, size + max(size // 10, SNAPSHOT_MARGIN)

    def snapshot(self, name):
        """Copy APP_DB to ``name`` while the application is stopped."""
        _rollback_name(name, "rollback")
        self.admin_script(f"DROP DATABASE IF EXISTS {name}")
        for attempt in range(3):
            try:
                # CREATE DATABASE waits up to 5 s for terminated sessions to end.
                self.admin_script(f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                                  f"WHERE datname = '{APP_DB}' AND pid <> pg_backend_pid();\n"
                                  f"CREATE DATABASE {name} TEMPLATE {APP_DB} OWNER {APP_USER} STRATEGY FILE_COPY",
                                  timeout=SNAPSHOT_TIMEOUT)
                return
            except RuntimeFailure:
                if attempt == 2 or STOP.is_set():
                    raise

    def restore_snapshot(self, name, discard):
        """Put copy ``name`` back as APP_DB; the replaced database is renamed
        ``discard`` in the same transaction, so APP_DB always exists. Safe to
        repeat after an interruption: a finished swap leaves ``discard``."""
        _rollback_name(name, "rollback")
        _rollback_name(discard, "failed")
        present = set(self.update_databases())
        if name not in present:
            if discard in present:
                return
            raise RuntimeFailure("the database copy taken before the update is missing")
        if discard in present:
            self.admin_script(f"DROP DATABASE {discard}")
        for attempt in range(3):
            try:
                self.admin_script(f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                                  f"WHERE datname = '{APP_DB}' AND pid <> pg_backend_pid();\n"
                                  f"BEGIN;\nALTER DATABASE {APP_DB} RENAME TO {discard};\n"
                                  f"ALTER DATABASE {name} RENAME TO {APP_DB};\nCOMMIT", timeout=60)
                return
            except RuntimeFailure:
                if attempt == 2 or STOP.is_set():
                    raise

    def drop_update_database(self, name):
        if not re.fullmatch(ROLLBACK_PATTERN, name):
            raise RuntimeFailure("refusing to drop a database the updater did not create")
        self.admin_script(f"DROP DATABASE IF EXISTS {name}", timeout=120)

    def record_mode(self):
        write_private(self.mode_file, json.dumps({"version": 1, "mode": self.mode}), replace=True)

    def open(self, purpose="run"):
        safe_path(self.root)
        # Never rename/import a legacy source or replace it with an empty DB.
        if (self.root / "db.sqlite").exists() or (self.root / "db.sqlite").is_symlink():
            raise RuntimeFailure("legacy SQLite detected; explicit offline migration is required")
        if self.mode_file.exists():
            if read_record(self.mode_file).get("mode") != "embedded":
                raise RuntimeFailure("previous external database state detected; refusing a new database; explicit offline migration is required")
        safe_path(self.pg)
        if not self.pg.exists():
            if purpose == "dump":
                raise RuntimeFailure("dump requires a retained database; it never initializes one")
            if self.root.exists() and any(not isolation().own_record(p) for p in self.root.iterdir()):
                raise RuntimeFailure("unrecognized legacy data; refusing to initialize PostgreSQL")
            self.root.mkdir(parents=True, exist_ok=True)
            self.pg.mkdir(mode=0o700)
            fresh = True
        else:
            check_owned(self.pg, directory=True)
            fresh = False
        self.lock = ClusterLock(self.pg / "lifecycle.lock")
        if not self.lock.acquire():
            if purpose != "dump":
                raise RuntimeFailure("managed database is already owned by another supervisor")
            self.validate_cluster(online=True)
            record = read_record(self.pg / "runtime.json")
            if record.get("system_identifier") != self.identifier:
                raise RuntimeFailure("online database identity mismatch")
            self.socket = Path(record["socket"])
            check_owned(self.socket, directory=True)
            self.port = int(record["port"])
            self.set_url()
            if self.query("SELECT system_identifier FROM pg_control_system()", admin=True) != self.identifier:
                raise RuntimeFailure("online database identity mismatch")
            self.query()
            return
        if fresh:
            self.initialize()
        else:
            self.validate_cluster()
            self.start()
            self.wait_ready()
        self.record_mode()
        write_private(self.pg / "runtime.json", json.dumps({"version": 1, "socket": str(self.socket),
            "port": self.port, "system_identifier": self.identifier}), replace=True)

    def control_identifier(self):
        # pg_controldata reports an invalid CRC with exit 0, on stdout on
        # PG16. Reject warnings on either stream before trusting any identifier.
        text = self.command(["pg_controldata", str(self.data)], reject_stderr=True)
        match = re.search(r"^Database system identifier:\s*(\d+)\s*$", text, re.M)
        if (not match or not text.startswith("pg_control version number:")
                or not re.search(r"^pg_control version number:\s*1300$", text, re.M)
                or not re.search(r"^Catalog version number:\s*202307071$", text, re.M)
                or re.search(r"^(WARNING|ERROR|FATAL):", text, re.M)):
            raise RuntimeFailure("invalid PostgreSQL control data")
        return match.group(1)

    def validate_cluster(self, *, online=False):
        for marker in ("initializing", "restore-incomplete"):
            if (self.pg / marker).exists() or (self.pg / marker).is_symlink():
                raise RuntimeFailure(f"incomplete {marker} operation; operator recovery required")
        if not (self.pg / "cluster.json").exists():
            raise RuntimeFailure("partial or unrecognized PostgreSQL state; refusing to initialize")
        record = read_record(self.pg / "cluster.json")
        if record.get("mode") != "embedded" or record.get("state") != "ready":
            raise RuntimeFailure("partial or unrecognized PostgreSQL state")
        check_owned(self.data, directory=True)
        # PG permits symlinks (tablespaces/WAL), but this single-volume contract
        # does not. Refuse instead of following/chowning arbitrary host paths.
        for directory, dirs, files in os.walk(self.data, followlinks=False):
            for name in dirs + files:
                item = Path(directory) / name
                check_owned(item, directory=item.is_dir(), private=False)
        version = self.data / "PG_VERSION"
        if not version.is_file() or version.read_text().strip() != MAJOR:
            raise RuntimeFailure("unsupported or missing PostgreSQL major version; expected 16")
        # A retained postmaster.pid can be the result of SIGKILL, not corrupt
        # state. With the lifetime lock held for startup, leave it untouched:
        # PostgreSQL must reject a live PID or perform its own crash recovery.
        # Manually starting PostgreSQL outside our lock is unsupported.
        try:
            check_owned(self.pg / "password")
            self.password = (self.pg / "password").read_text()
        except (OSError, RuntimeFailure):
            raise RuntimeFailure("retained password missing or unsafe; credentials are never reset") from None
        if not re.fullmatch(r"[0-9a-f]{96}", self.password):
            raise RuntimeFailure("invalid retained password; credentials are never reset")
        self.identifier = self.control_identifier()
        if record.get("system_identifier") != self.identifier:
            raise RuntimeFailure("retained PostgreSQL cluster identity mismatch")
        self.set_url()

    def set_url(self):
        self.url = f"postgresql://{APP_USER}:{self.password}@127.0.0.1:{self.port}/{APP_DB}"

    def publish_descriptor(self):
        path = self.root / "managed-postgres.json"
        if path.exists() or path.is_symlink():
            check_owned(path)
        write_private(path, json.dumps({"version": 1, "host": "127.0.0.1",
            "port": self.port, "user": APP_USER, "password": self.password,
            "database": APP_DB}), replace=True)

    def initialize(self):
        if not re.search(r"\b16\.", self.command(["initdb", "--version"])):
            raise RuntimeFailure("managed PostgreSQL initdb major must be 16")
        write_private(self.pg / "initializing", "initialization in progress\n")
        self.password = secrets.token_hex(48)
        write_private(self.pg / "password", self.password)
        self.command(["initdb", "-D", str(self.data), "--username=" + ADMIN,
            "--auth-local=trust", "--auth-host=scram-sha-256", "--encoding=UTF8", "--locale=C"], timeout=60)
        self.identifier = self.control_identifier()
        self.set_url()
        self.start()
        self.wait_ready(admin=True)
        self.query(f"SET password_encryption = 'scram-sha-256'; CREATE ROLE {APP_USER} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '{self.password}'; CREATE DATABASE {APP_DB} OWNER {APP_USER};", admin=True)
        self.wait_ready()
        write_private(self.pg / "cluster.json", json.dumps({"version": 1, "mode": "embedded", "state": "ready", "system_identifier": self.identifier}))
        (self.pg / "initializing").unlink()

    def start(self):
        if not re.search(r"\b16\.", self.command(["postgres", "--version"])):
            raise RuntimeFailure("managed PostgreSQL server major must be 16")
        self.private_runtime = tempfile.TemporaryDirectory(prefix="shuffle-pg-")
        self.socket = Path(self.private_runtime.name)
        hba = self.socket / "pg_hba.conf"
        write_private(hba, f"local all {ADMIN} trust\nhost {APP_DB} {APP_USER} 127.0.0.1/32 scram-sha-256\n")
        # Never put server/SQL error text into container logs. Operators can
        # explicitly inspect this protected file; bootstrap SQL includes a secret.
        logpath = self.pg / "server.log"
        safe_path(logpath)
        if logpath.exists():
            check_owned(logpath)
        fd = os.open(logpath, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        self.pg_log = os.fdopen(fd, "ab", buffering=0)
        self.process = subprocess.Popen(["postgres", "-D", str(self.data), "-h", "127.0.0.1",
            "-p", str(self.port), "-k", str(self.socket), "-c", "unix_socket_permissions=0700",
            "-c", "hba_file=" + str(hba), "-c", "password_encryption=scram-sha-256",
            "-c", "log_statement=none", "-c", "log_min_error_statement=panic"],
            env=client_env(""), stdout=self.pg_log, stderr=self.pg_log, start_new_session=True,
            **isolation().service_spawn())

    def wait_ready(self, *, admin=False):
        deadline = time.monotonic() + 30
        while not STOP.is_set() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeFailure("managed PostgreSQL exited before authenticated readiness")
            try:
                if self.query(admin=admin) == "1":
                    return
            except RuntimeFailure:
                pass
            STOP.wait(0.2)
        raise RuntimeFailure("authenticated database readiness timed out or was interrupted")

    def dump(self, filename):
        args = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
        if filename == "-":
            self.command(args, env=client_env(self.url), output=sys.stdout.buffer, timeout=900)
        else:
            # Never truncate an existing backup, follow a symlink, or copy its input.
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                self.command(args, env=client_env(self.url), output=stream, timeout=900)
                stream.flush()
                os.fsync(stream.fileno())
        log("logical dump completed")

    def restore(self, source):
        # Check more than tables: sequences, views, custom functions/types,
        # schemas and extensions are state too. Fresh public is permitted.
        empty = self.query("""SELECT NOT (
            EXISTS (SELECT FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN ('public','information_schema')) OR
            EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') OR
            EXISTS (SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') OR
            EXISTS (SELECT FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema') OR
            EXISTS (SELECT FROM pg_extension WHERE extname <> 'plpgsql') OR
            EXISTS (SELECT FROM pg_largeobject_metadata))""")
        if empty != "t":
            raise RuntimeFailure("restore target is not verified empty")
        write_private(self.pg / "restore-incomplete", "restore in progress; retained on failure\n")
        self.command(["pg_restore", "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", "--dbname=" + APP_DB],
            env=client_env(self.url), source=source, timeout=900)
        (self.pg / "restore-incomplete").unlink()
        log("database-only restore completed; application was not started")

    def close(self):
        try:
            if self.process is not None:
                forced = False
                if self.process.poll() is None:
                    self.process.send_signal(signal.SIGINT)  # PostgreSQL FAST shutdown
                    try:
                        self.process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        forced = True
                kill_group(self.process, signal.SIGKILL)
                self.process.wait(timeout=5)
                if forced:
                    raise RuntimeFailure("PostgreSQL fast shutdown deadline exceeded; operator inspection required")
                for endpoint in (self.pg / "runtime.json", self.root / "managed-postgres.json"):
                    if endpoint.exists():
                        endpoint.unlink()
        finally:
            if self.pg_log is not None:
                self.pg_log.close()
            if self.private_runtime is not None:
                self.private_runtime.cleanup()
            if self.lock is not None:
                self.lock.close()


def image_dir():
    """The image's own /app/docker: image.json and the update trust key. Under
    a hand-over this file runs from an update, but those stay the image's."""
    if os.environ.get("SHUFFLE_HANDED_OVER") and os.environ.get("SHUFFLE_IMAGE_DIR"):
        return Path(os.environ["SHUFFLE_IMAGE_DIR"])
    return Path(__file__).resolve().parent


def is_production(image):
    """A provenance-free development image stays pinned to its own code."""
    return image.get("sequence", 0) > 0 and bool(re.fullmatch(r"[a-f0-9]{40}", image.get("revision", "")))


def isolation():
    """The agent isolation helpers beside this file (#1957). A copy of this
    file without them (packaging tests) behaves as with the mode off."""
    global _isolation
    if _isolation is None:
        import importlib.util
        import types
        path = _ISOLATION_FILE
        if path.is_file():
            spec = importlib.util.spec_from_file_location("shuffle_agent_isolation", path)
            _isolation = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_isolation)
        else:
            import contextlib
            _isolation = types.SimpleNamespace(ACTIVE=None, service_env=dict, service_spawn=dict,
                                               agent_spawn=dict, agent_files=contextlib.nullcontext,
                                               own_record=lambda path: False)
    return _isolation


def isolate(settings, account):
    """Root, before the drop: migrate for AGENT_ISOLATION and return the
    identity to drop to, or None for the usual single-account drop."""
    module = isolation()
    if not hasattr(module, "bootstrap"):
        return None
    try:
        ident = module.identity(account.pw_uid, account.pw_gid)
    except module.IsolationError as error:
        if module.setting() == "on":
            log(f"agent isolation could NOT be enabled ({error}); agents run unisolated")
        return None
    try:
        return module.bootstrap(Path(settings.effective_data_dir), ident,
                                app_dir=Path(__file__).resolve().parents[1], log=log)
    except module.IsolationError as error:
        raise RuntimeFailure(str(error)) from None


def start_app(command, cwd, env, updates_root=None):
    """The app process; through the isolation launcher when the split is on."""
    module = isolation()
    if module.ACTIVE is not None:
        if updates_root is not None:
            module.ensure_traversable(updates_root, log)
        command = module.app_command(command)
    return subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)


def load_updater():
    """The updater beside this file (the image's, or the update's after a hand-over)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("shuffle_container_updates", Path(__file__).with_name("application_updates.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def handover(settings, execve=os.execve):
    """Start the active update's own supervisor in place of this image's copy.

    Runs once at container start, as appuser, before PostgreSQL or anything
    else starts. The update qualifies only when its signed manifest and every
    source file verify against this image's key, it runs on this image, and it
    is healthy (no first boot pending or blocked). A trial marker names it until
    its supervisor reports a ready app; if that never happens, the next start
    runs this image's supervisor instead, over the same update. Returns False
    when this image's supervisor should run; on success it does not return.
    """
    if os.environ.get("SHUFFLE_HANDED_OVER") or os.environ.get("SHUFFLE_SOURCE_SUPERVISOR", "1").strip() == "0":
        return False
    if isolation().ACTIVE is not None:
        # An update's supervisor would start with the service account's ids
        # and code the agent could have written before the mode was on.
        log("agent isolation: running this image's supervisor; updates still apply to the app")
        return False
    here = Path(__file__).resolve().parent
    try:
        image = json.loads((here / "image.json").read_text())
    except (OSError, ValueError):
        return False
    if not is_production(image) or "source-supervisor" not in (image.get("provides") or []):
        return False
    root = safe_path(Path(settings.effective_data_dir) / "application-updates")
    try:
        module = load_updater()
        target = module.handover_target(root, image, (here / "application-update-key.pem").read_bytes())
    except Exception as exc:
        reason = str(exc) if type(exc).__name__ == "UpdateError" else f"{type(exc).__name__} while checking the update"
        log(f"running this image's supervisor: {reason}")
        return False
    if target is None:
        return False
    revision, python, script = target
    write_private(root / HANDOVER_TRIAL, json.dumps({"revision": revision}), replace=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(SHUFFLE_HANDED_OVER=revision, SHUFFLE_IMAGE_DIR=str(here),
               SHUFFLE_HANDOVER=HANDOVER_PROTOCOL, PYTHONNOUSERSITE="1")
    log(f"starting the supervisor of application update {revision[:12]}")
    sys.stdout.flush()
    sys.stderr.flush()
    execve(str(python), [str(python), str(script), "run"], env)
    return True


def confirm_handover(root):
    """This handed-over supervisor got the app ready: clear its trial marker."""
    global _handover_confirmed
    revision = os.environ.get("SHUFFLE_HANDED_OVER")
    if _handover_confirmed or not revision:
        return
    _handover_confirmed = True
    marker = Path(root) / HANDOVER_TRIAL
    try:
        if json.loads(marker.read_text()).get("revision") == revision:
            marker.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def chrome_command(env):
    """chrome-supervisor and start-chrome from beside this file, copied to a
    private directory for this container's life: an in-place update prunes old
    builds while Chromium keeps running and relaunching from these scripts.
    Returns (command, env, directory or None)."""
    here = Path(__file__).resolve().parent
    scripts = [here / "chrome-supervisor.sh", here / "start-chrome.sh"]
    if not all(script.is_file() for script in scripts):
        return ["chrome-supervisor"], env, None
    private = Path(tempfile.mkdtemp(prefix="ai-shuffle-chrome-"))
    for script in scripts:
        shutil.copyfile(script, private / script.name)
        (private / script.name).chmod(0o700)
    return ([str(private / "chrome-supervisor.sh")],
            dict(env, START_CHROME_COMMAND=str(private / "start-chrome.sh")), private)


def drop_privileges(settings):
    """Root may prepare mount roots, never recursively repair database files."""
    global _agent_path
    if os.geteuid() == 0:
        account = pwd.getpwnam("appuser")
        if not account.pw_uid or not account.pw_gid:
            raise RuntimeFailure("PUID/PGID must identify a non-root account")
        for path in (Path(settings.effective_data_dir), Path(settings.effective_workspace_dir)):
            safe_path(path)
            path.mkdir(parents=True, exist_ok=True)
            # Empty image-created mounts may still have the default UID when
            # PUID changes. Retained contents (especially PG) are never chowned.
            if not any(path.iterdir()):
                os.chown(path, account.pw_uid, account.pw_gid)
        split = isolate(settings, account)
        if split is not None:
            isolation().drop(split)
            _agent_path = os.environ.get("PATH", "")
            os.environ["PATH"] = isolation().trusted_path(_agent_path)
        else:
            os.initgroups(account.pw_name, account.pw_gid)
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
        os.environ["HOME"] = account.pw_dir
    elif getattr(isolation(), "setting", lambda: "off")() == "on":
        log("agent isolation could NOT be enabled: the container did not start as root "
            "(--user or a hand-over); agents run unisolated")
    # Real ids: with agent isolation the effective ids are the service account's.
    for name, actual in (("PUID", os.getuid()), ("PGID", os.getgid())):
        if name in os.environ and int(os.environ[name]) != actual:
            raise RuntimeFailure("direct --user identity must match PUID/PGID; ownership is never repaired recursively")
    if not os.geteuid():
        raise RuntimeFailure("managed runtime must not run as root")
    root = safe_path(Path(settings.effective_data_dir))
    if not root.exists() or not os.access(root, os.W_OK | os.X_OK):
        raise RuntimeFailure("DATA_DIR must exist and be writable by the runtime UID; prepare mount ownership explicitly")


def probe_ready(url, timeout=None):
    """True when the readiness endpoint answers 200; never logs response content."""
    from urllib.request import urlopen
    try:
        with urlopen(url, timeout=READY_PROBE_TIMEOUT if timeout is None else timeout) as response:
            return response.status == 200
    except Exception:
        return False


class ReadinessMonitor:
    """Counts consecutive readiness failures; `record` says when the app is considered dead."""

    def __init__(self, url, *, probe=probe_ready, failures=None, startup_failures=None):
        self.url = url
        self.probe = probe
        self.limit = READY_PROBE_FAILURES if failures is None else failures
        self.startup_limit = READY_PROBE_STARTUP_FAILURES if startup_failures is None else startup_failures
        self.failures = 0
        self.ever_ready = False

    def check(self):
        return self.record(self.probe(self.url))

    def record(self, ready):
        if ready:
            self.failures = 0
            self.ever_ready = True
            return False
        self.failures += 1
        return self.failures >= (self.limit if self.ever_ready else self.startup_limit)


def watch(app, db, chrome, monitor=None, updates=None, restart=None, relaunch=None, chrome_current=None):
    """Supervise until STOP (returns 0) or a failure the container must restart for (1).

    An application that exits or stops answering readiness is handed to
    ``relaunch(app)``, which stops it, rolls back an update's failed first
    start, and returns the new process (None when the worker cannot run).
    At most RELAUNCH_LIMIT relaunches per RELAUNCH_WINDOW; then, or without
    ``relaunch``, the supervisor exits and the restart policy takes over."""
    next_probe = time.monotonic() + READY_PROBE_INTERVAL
    next_update = time.monotonic() + 1
    relaunched = []
    while not STOP.is_set():
        chrome_process = chrome_current() if chrome_current is not None else chrome
        failure = update_failure = None
        if app.poll() is not None:
            failure = "application exited unexpectedly"
        elif db.process is not None and db.process.poll() is not None:
            log("managed PostgreSQL exited unexpectedly")
            return 1
        elif chrome_process is not None and chrome_process.poll() is not None:
            log("Chromium supervisor exited unexpectedly")
            return 1
        elif monitor is not None and time.monotonic() >= next_probe:
            next_probe = time.monotonic() + READY_PROBE_INTERVAL
            previous_failures = monitor.failures
            if monitor.check():
                failure = f"application readiness probe failed {monitor.failures} times in a row"
            else:
                if monitor.failures == 1:
                    log("application readiness degraded; waiting for subsequent probes")
                elif previous_failures and monitor.failures == 0:
                    log("application readiness recovered")
                if updates is not None and monitor.ever_ready and monitor.failures == 0:
                    updates.healthy()
                    confirm_handover(updates.root)
        if failure is None and updates is not None and time.monotonic() >= next_update:
            next_update = time.monotonic() + 1
            if updates.install_requested.is_set():
                updates.install_requested.clear()
                try:
                    app = restart(app)
                    monitor = ReadinessMonitor(monitor.url)
                    next_probe = time.monotonic() + 1
                except RestartRejected as exc:
                    updates.publish("ready", error=exc.reason or "Could not restart for the update. Finish active sessions, check Allow updates, then retry.")
                except Exception:
                    # A stop request may have latched shutdown even when its
                    # response failed or timed out, so this worker is never
                    # advertised as usable again: relaunch replaces it with a
                    # fresh process (rolling back a started first boot).
                    failure = "application update restart failed after preflight"
                    update_failure = ("The update could not be started, so this worker went back to the "
                                      "version it was running. Try again later.")
            elif monitor.ever_ready and monitor.failures == 0:
                updates.tick()
        if failure is not None:
            now = time.monotonic()
            relaunched = [moment for moment in relaunched if now - moment < RELAUNCH_WINDOW]
            if relaunch is None or len(relaunched) >= RELAUNCH_LIMIT:
                if relaunch is None and updates is not None:
                    updates.failed()
                log(f"{failure}; exiting so the container restarts")
                return 1
            log(f"{failure}; restarting the application")
            try:
                app = relaunch(app)
            except Exception as exc:
                log(f"could not restart the application ({type(exc).__name__}); exiting so the container restarts")
                return 1
            if app is None:
                log("the application cannot be started again; exiting so the container restarts")
                return 1
            relaunched.append(now)
            if updates is not None and update_failure and updates.state.get("pending"):
                updates.publish("ready", error=update_failure)
            if monitor is not None:
                monitor = ReadinessMonitor(monitor.url)
            next_probe = time.monotonic() + READY_PROBE_INTERVAL
            continue
        STOP.wait(0.2)
    return 0


def _update_error(exc):
    # UpdateError comes from the updater module loaded beside this file.
    return type(exc).__name__ == "UpdateError"


def restart_into_update(current, updates, env, before_start=None):
    """Replace the running app with the staged update; returns the new process.

    RestartRejected leaves ``current`` untouched and serving. Any other error
    comes after the worker was asked to quiesce, and watch replaces it with a
    fresh process (start_current)."""
    try:
        busy = updates.lifecycle("active-sessions").get("active_sessions") != 0
    except Exception as exc:
        raise RestartRejected("Application update preflight failed") from exc
    if busy:
        raise RestartRejected("Finish active sessions before restarting")
    try:
        # Validate staged files, policy and room for the database copy
        # before stopping the healthy app.
        command, cwd, child_env = updates.launch(env, activate=True)
        updates.snapshot_room()
    except Exception as exc:
        raise RestartRejected("Application update preflight failed", str(exc) if _update_error(exc) else None) from exc
    # Once sent, even a timeout cannot prove the worker is still usable.
    if updates.lifecycle("stop-sessions").get("stopped") is not True:
        raise RuntimeFailure("Could not quiesce the worker")
    stop_group(current, 30)
    updates.activating()  # copies the database, then records the first start
    if before_start is not None:
        before_start(child_env)
    return start_app(command, cwd, child_env, updates.root)


def start_current(current, updates, env, before_start=None):
    """Stop ``current`` and start the version that should run now: after a
    failed first start, the previous one over the restored database copy.
    None when the worker cannot run (blocked, or its build fails to verify)."""
    stop_group(current, 30)
    command, cwd, child_env = [sys.executable, "-m", "app.main"], None, env
    if updates is not None:
        updates.failed()
        if updates.state.get("blocked"):
            return None
        try:
            command, cwd, child_env = updates.launch(env)
        except Exception as exc:
            log(str(exc) if _update_error(exc) else f"cannot start the application ({type(exc).__name__})")
            return None
    if before_start is not None:
        before_start(child_env)
    return start_app(command, cwd, child_env, updates.root if updates is not None else None)


def shutdown(app, chrome, db):
    # tini forwards only to THIS supervisor. All three children have separate
    # process groups, so app cleanup can still use PostgreSQL and Chromium.
    try:
        stop_group(app, 30)
    finally:
        try:
            stop_group(chrome, 5)
        finally:
            db.close()


def run(settings, action, filename=None):
    db = Database(settings)
    app = chrome = chrome_log = chrome_scripts = source = updates = None
    try:
        if action == "restore":
            # pg_restore inherits the OS descriptor, not Python's buffered
            # position. Header validation must not leave native reads at the
            # buffer's read-ahead offset after seek(0).
            source = open(filename, "rb", buffering=0)
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode) or source.read(5) != b"PGDMP":
                raise RuntimeFailure("restore requires a custom-format PostgreSQL logical archive")
            source.seek(0)
            db.command(["pg_restore", "--list"], source=source, timeout=30)
            source.seek(0)
        db.open(purpose=action)
        if action == "dump":
            db.dump(filename)
            return 0
        if action == "restore":
            db.restore(source)
            return 0
        # The descriptor is published only after authenticated readiness and
        # before any child starts. Inherited retired DB settings cannot select
        # a different server, even in worktrees or child processes.
        db.publish_descriptor()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("DATABASE_", "POSTGRES_", "PG"))}
        env["DATA_DIR"] = str(settings.effective_data_dir)
        if isolation().ACTIVE is not None:
            env.update(isolation().app_environment(settings, _agent_path))
        # A provenance-free development image stays pinned. Production images
        # carry a main-history ordinal and what they provide (update_contract).
        image_file = image_dir() / "image.json"
        if image_file.is_file():
            image = json.loads(image_file.read_text())
            if is_production(image):
                import httpx
                module = load_updater()
                ContainerUpdates, UpdateError = module.ContainerUpdates, module.UpdateError
                token = secrets.token_hex(32)
                env["SHUFFLE_DESKTOP_LIFECYCLE_TOKEN"] = token
                root = safe_path(settings.effective_data_dir / "application-updates")
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                safe_path(root / "versions").mkdir(exist_ok=True, mode=0o700)
                env["SHUFFLE_CONTAINER_UPDATES"] = str(root)

                def lifecycle(action):
                    method = "POST" if action == "stop-sessions" else "GET"
                    with httpx.Client(trust_env=False, timeout=35) as client:
                        response = client.request(method, f"http://127.0.0.1:{settings.port}/api/v1/internal/desktop/{action}",
                                                  headers={"Authorization": f"Bearer {token}"})
                        response.raise_for_status()
                        return response.json()

                from app.core.update_channel import selection
                try:
                    channel, _ = selection(root)
                except (ValueError, KeyError, TypeError) as exc:
                    raise RuntimeFailure("Invalid update channel configuration") from exc
                # The API compares the saved choice with this running supervisor.
                env["SHUFFLE_UPDATE_CHANNEL"] = channel
                def lease():
                    # D8: the app's license lease for the hub's gated feed. The
                    # updater sends it to the hub's update endpoints only.
                    value = lifecycle("source-lease").get("lease")
                    return value if isinstance(value, str) and value else None

                try:
                    updates = ContainerUpdates(root, image, (image_dir() / "application-update-key.pem").read_bytes(),
                                               lambda: lifecycle("update-policy").get("allow_updates") is True, lifecycle,
                                               channel=channel, lease=lease, database=db)
                except UpdateError as exc:
                    raise RuntimeFailure(str(exc)) from exc
        chrome_selection = None

        def select_chrome(selected_env):
            # Re-select with the application revision, including rollback. Never
            # modify os.environ: PostgreSQL keeps the image's PATH/libraries.
            nonlocal chrome, chrome_log, chrome_scripts, chrome_selection
            if os.environ.get("START_CHROME", "1") != "1":
                return
            if selected_env.get("SHUFFLE_RUNTIME_OVERLAYS") != "1":
                selected_env = env  # preserve the image browser path for ordinary releases
            selection = tuple(selected_env.get(key) for key in ("PATH", "LD_LIBRARY_PATH", "CHROME_EXECUTABLE"))
            if chrome is not None and chrome.poll() is None and selection == chrome_selection:
                return
            stop_group(chrome, 5)
            if updates is not None and isolation().ACTIVE is not None:
                isolation().ensure_traversable(updates.root, log)
            with isolation().agent_files():
                if chrome_scripts is not None:
                    shutil.rmtree(chrome_scripts)
                if chrome_log is None:
                    logdir = Path.home() / ".config"
                    logdir.mkdir(parents=True, exist_ok=True)
                    chrome_log = open(logdir / "chrome.log", "ab")
                command, browser_env, chrome_scripts = chrome_command(chrome_env(selected_env))
            chrome = subprocess.Popen(command, env=browser_env, stdout=chrome_log, stderr=chrome_log,
                                      start_new_session=True, **isolation().agent_spawn())
            chrome_selection = selection

        if STOP.is_set():
            return 0
        try:
            command, cwd, child_env = updates.launch(env) if updates else ([sys.executable, "-m", "app.main"], None, env)
        except UpdateError as exc:
            raise RuntimeFailure(str(exc)) from exc
        select_chrome(child_env)
        app = start_app(command, cwd, child_env, updates and updates.root)

        def restart(current):
            nonlocal app
            app = restart_into_update(current, updates, env, before_start=select_chrome)
            return app

        def relaunch(current):
            nonlocal app
            app = start_current(current, updates, env, before_start=select_chrome)
            return app

        log("authenticated database ready; application started")
        return watch(app, db, chrome, ReadinessMonitor(f"http://127.0.0.1:{settings.port}/health/ready"), updates,
                     restart, relaunch, chrome_current=lambda: chrome)
    finally:
        try:
            if updates is not None:
                updates.close()
            shutdown(app, chrome, db)
        finally:
            if source is not None:
                source.close()
            if chrome_log is not None:
                chrome_log.close()
            if chrome_scripts is not None:
                with isolation().agent_files():
                    shutil.rmtree(chrome_scripts, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("run")
    for action in ("dump", "restore"):
        sub.add_parser(action).add_argument("--file", required=True)
    args = parser.parse_args(argv)
    STOP.clear()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    try:
        settings = load_settings()
        drop_privileges(settings)
        # After the drop: setuid() resets the flag to suid_dumpable.
        harden_process_introspection(settings)
        if (args.action or "run") == "run":
            handover(settings)
        return run(settings, args.action or "run", getattr(args, "file", None))
    except RuntimeFailure as error:
        log(str(error))
    except Exception as error:
        # Never include exception text, source lines or locals: those may hold a
        # DSN/password. The type and our own call site still make CI actionable.
        trace = error.__traceback__
        line = 0
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == __file__:
                line = trace.tb_lineno
            trace = trace.tb_next
        log(f"operation failed ({type(error).__name__} at container_runtime.py:{line}); "
            "check configuration, permissions, and retained state (details suppressed)")
    return 1


if __name__ == "__main__":
    # Entrypoint and maintenance commands execute this absolute script. Python
    # otherwise searches only /app/docker, not the sibling application package.
    # Anchor imports to the installed script, never to a caller's working folder.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
