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
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time


MAJOR = "16"
APP_USER = "ai_shuffle"
APP_DB = "ai_shuffle"
ADMIN = "shuffle_admin"
STOP = threading.Event()


class RuntimeFailure(Exception):
    """Deliberately secret-free operator diagnostic."""


def log(message):
    print(f"container runtime: {message}", file=sys.stderr, flush=True)


def load_settings():
    # This uses Pydantic's sources and the app's normalization, including the
    # explicit-fields set (which includes env-file values even equal to defaults).
    from app.core.config import Settings, _resolve_env_file
    return Settings(_env_file=_resolve_env_file())


def select_mode(settings):
    mode = settings.database_mode.strip().lower()
    if mode not in {"auto", "external", "embedded"}:
        raise RuntimeFailure("DATABASE_MODE must be auto, embedded, or external")
    explicit = bool((settings.database_url or "").strip()) or bool(
        settings.model_fields_set & {"postgres_host", "postgres_port", "postgres_user", "postgres_password", "postgres_db"}
    )
    if mode == "embedded" and explicit:
        raise RuntimeFailure("embedded mode conflicts with explicit external database configuration")
    return "external" if explicit or mode == "external" else "embedded"


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


def client_env(url):
    # No inherited libpq service/password/options may silently redirect a
    # maintenance command. The normalized DSN is environment, never argv/logs.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update(PGCONNECT_TIMEOUT="3", LC_ALL="C")
    if url:
        from urllib.parse import urlsplit, unquote, parse_qsl
        parts = urlsplit(url)
        if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
            raise RuntimeFailure("external database URL must identify a PostgreSQL host")
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
        self.mode = select_mode(settings)
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
            stderr=subprocess.PIPE, start_new_session=True)
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

    def record_mode(self):
        write_private(self.mode_file, json.dumps({"version": 1, "mode": self.mode}), replace=True)

    def open(self, purpose="run"):
        safe_path(self.root)
        # Guard this exact path even in external mode: app startup would rename
        # this source and can continue with an empty database after an import failure.
        if (self.root / "db.sqlite").exists() or (self.root / "db.sqlite").is_symlink():
            raise RuntimeFailure("legacy SQLite detected; explicit offline migration is required")
        if self.mode == "external":
            if purpose == "restore":
                raise RuntimeFailure("external restore is forbidden")
            if purpose == "run":
                # Fence even a failed external launch: removing unavailable
                # configuration must not look like a fresh embedded install.
                self.record_mode()
            try:
                self.url = self.settings.get_database_url()
            except Exception:
                raise RuntimeFailure("external database configuration is incomplete or invalid") from None
            self.query()  # authenticated SQL, no embedded fallback
            return
        if self.mode_file.exists():
            if read_record(self.mode_file).get("mode") != "embedded":
                raise RuntimeFailure("previous external configuration is missing; refusing a new database")
        safe_path(self.pg)
        if not self.pg.exists():
            if purpose == "dump":
                raise RuntimeFailure("dump requires a retained database; it never initializes one")
            if self.root.exists() and any(self.root.iterdir()):
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
            env=client_env(""), stdout=self.pg_log, stderr=self.pg_log, start_new_session=True)

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
                endpoint = self.pg / "runtime.json"
                if endpoint.exists():
                    endpoint.unlink()
        finally:
            if self.pg_log is not None:
                self.pg_log.close()
            if self.private_runtime is not None:
                self.private_runtime.cleanup()
            if self.lock is not None:
                self.lock.close()


def drop_privileges(settings):
    """Root may prepare mount roots, never recursively repair database files."""
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
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        os.environ["HOME"] = account.pw_dir
    for name, actual in (("PUID", os.geteuid()), ("PGID", os.getegid())):
        if name in os.environ and int(os.environ[name]) != actual:
            raise RuntimeFailure("direct --user identity must match PUID/PGID; ownership is never repaired recursively")
    if not os.geteuid():
        raise RuntimeFailure("managed runtime must not run as root")
    root = safe_path(Path(settings.effective_data_dir))
    if not root.exists() or not os.access(root, os.W_OK | os.X_OK):
        raise RuntimeFailure("DATA_DIR must exist and be writable by the runtime UID; prepare mount ownership explicitly")


def watch(app, db, chrome):
    while not STOP.is_set():
        if app.poll() is not None:
            log("application exited unexpectedly")
            return 1
        if db.process is not None and db.process.poll() is not None:
            log("managed PostgreSQL exited unexpectedly")
            return 1
        if chrome is not None and chrome.poll() is not None:
            log("Chromium supervisor exited unexpectedly")
            return 1
        STOP.wait(0.2)
    return 0


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
    app = chrome = chrome_log = source = None
    try:
        if action == "restore":
            if db.mode == "external":
                raise RuntimeFailure("external restore is forbidden")
            source = open(filename, "rb")
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
        env = os.environ.copy()
        # Export the effective URL even in external mode, so child cwd changes
        # cannot switch databases. Never place a credential in argv.
        env["DATABASE_URL"] = db.url
        env["DATA_DIR"] = str(settings.effective_data_dir)
        if os.environ.get("START_CHROME", "1") == "1":
            logdir = Path.home() / ".config"
            logdir.mkdir(parents=True, exist_ok=True)
            chrome_log = open(logdir / "chrome.log", "ab")
            chrome = subprocess.Popen(["chrome-supervisor"], env=env, stdout=chrome_log, stderr=chrome_log, start_new_session=True)
        if STOP.is_set():
            return 0
        app = subprocess.Popen([sys.executable, "-m", "app.main"], env=env, start_new_session=True)
        log("authenticated database ready; application started")
        return watch(app, db, chrome)
    finally:
        try:
            shutdown(app, chrome, db)
        finally:
            if source is not None:
                source.close()
            if chrome_log is not None:
                chrome_log.close()


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
    sys.exit(main())
