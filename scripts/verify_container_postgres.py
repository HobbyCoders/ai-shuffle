#!/usr/bin/env python3
"""Exercise an actual built image using disposable Docker containers and volumes.

No host environment, production credentials, or existing volume is forwarded.
Run with --image ai-shuffle:local --artifacts /tmp/container-postgres-results.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = "/app/docker/container_runtime.py"
SQL_HELPER = """import sys
from docker.container_runtime import Database, load_settings
db = Database(load_settings())
try:
    db.open(purpose="dump")
    print(db.query(sys.stdin.read(), admin=sys.argv[1] == "admin"))
finally:
    db.close()
"""


class Check:
    def __init__(self, image, artifacts):
        self.image = image
        self.artifacts = artifacts
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.prefix = "shuffle-pg-check-" + uuid.uuid4().hex[:10]
        self.containers = []
        self.volumes = []
        self.networks = []
        self.results = []
        self.workspaces = {}

    def docker(self, *args, input=None, check=True, timeout=120):
        result = subprocess.run(["docker", *map(str, args)], input=input,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout)
        if check and result.returncode:
            # Do not include command arguments: they can contain test credentials.
            raise RuntimeError("Docker operation failed: " + result.stderr.decode(errors="replace")[-2500:])
        return result

    def volume(self):
        name = self.prefix + "-v" + str(len(self.volumes))
        self.docker("volume", "create", name)
        self.volumes.append(name)
        return name

    def launch(self, volume, *, env=None, command=(), user=None, network="none", chrome=False, mounts=()):
        name = self.prefix + "-c" + str(len(self.containers))
        if volume not in self.workspaces:
            self.workspaces[volume] = self.volume()
        args = ["run", "-d", "--name", name, "--network", network,
                "--stop-timeout", "90", "--shm-size", "256m",
                "--label", f"com.docker.compose.project={self.prefix}",
                "--label", "com.docker.compose.service=ai-shuffle",
                "--label", "com.docker.compose.container-number=1",
                "--label", "com.docker.compose.oneoff=False",
                "--mount", f"type=volume,src={volume},dst=/data",
                "--mount", f"type=volume,src={self.workspaces[volume]},dst=/workspace",
                "-e", "START_CHROME=" + ("1" if chrome else "0")]
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])
        for mount in mounts:
            args.extend(["--mount", mount])
        if user:
            args.extend(["--user", user])
        self.containers.append(name)
        self.docker(*args, self.image, *command)
        return name

    def state(self, container):
        return json.loads(self.docker("inspect", "--format", "{{json .State}}", container).stdout)

    def ready(self, container, timeout=180):
        deadline = time.monotonic() + timeout
        probe = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3).read().decode())"
        while time.monotonic() < deadline:
            assert self.state(container)["Running"], "Container exited before the application was ready"
            result = self.docker("exec", container, "python", "-c", probe, check=False, timeout=10)
            if result.returncode == 0:
                body = json.loads(result.stdout)
                assert body["status"] == "ready" and body["checks"]["database"] == "ok"
                return
            time.sleep(2)
        raise TimeoutError("Application did not become ready")

    def exited(self, container, *, success, timeout=120):
        result = self.docker("wait", container, timeout=timeout)
        code = int(result.stdout)
        assert (code == 0) == success, f"Unexpected container exit code {code}"
        assert not self.state(container)["OOMKilled"], "Container was OOM-killed"

    def stop(self, container):
        self.docker("stop", "--time", "90", container, timeout=100)
        self.exited(container, success=True)

    def sql(self, container, query, uid="99:100", *, admin=False):
        result = self.docker("exec", "-i", "--user", uid, container, "python", "-c", SQL_HELPER,
                             "admin" if admin else "app", input=query.encode())
        return result.stdout.decode().strip()

    def inspect_volume(self, volume, code, *, input=None):
        return self.docker("run", "--rm", "-i", "--network", "none", "--entrypoint", "python",
                           "--mount", f"type=volume,src={volume},dst=/data", self.image,
                           "-c", code, input=input)

    def system_id(self, container):
        result = self.docker("exec", "--user", "99:100", container, "pg_controldata", "/data/postgres/data")
        match = re.search(rb"Database system identifier:\s+(\d+)", result.stdout)
        assert match, "Cluster identity missing from pg_controldata"
        return match.group(1)

    def passed(self, name):
        self.results.append(name)
        print("PASS:", name, flush=True)

    def fresh_persistence_and_restore(self):
        volume = self.volume()
        app = self.launch(volume, chrome=True)
        self.ready(app)
        assert self.sql(app, "SHOW server_version;").startswith("16.")
        assert self.sql(app, "SELECT rolsuper FROM pg_roles WHERE rolname=current_user;") == "f"
        assert self.sql(app, "SELECT extname FROM pg_extension WHERE extname='pg_trgm';") == "pg_trgm"
        assert self.sql(app, "SHOW listen_addresses;") in ("127.0.0.1", "localhost", "127.0.0.1,::1")
        for binary in ("postgres", "initdb", "pg_ctl", "pg_dump", "pg_restore", "psql"):
            assert b" 16." in self.docker("exec", app, binary, "--version").stdout
        self.docker("exec", "--user", "99:100", app, "python", "-c",
                    "from pathlib import Path; import os,stat; s=Path('/data/postgres/data').stat(); assert s.st_uid==os.getuid()==99; assert stat.S_IMODE(s.st_mode)==0o700")
        identity = self.system_id(app)
        self.sql(app, "CREATE TABLE container_persistence_check (value text); INSERT INTO container_persistence_check VALUES ('retained');")
        self.passed("fresh one-container boot, PG16 clients/extension, non-superuser role and private storage")

        # Real local browser: render and operate the actual bundled UI/backend.
        self.docker("exec", "--user", "99:100", app, "python", "-c",
                    "from pathlib import Path; Path('/workspace/container-onboarding-check').mkdir(exist_ok=True)")
        script = ROOT / "tests/integration/container_ui_smoke.mjs"
        self.docker("cp", script, app + ":/tmp/container-ui-smoke.mjs")
        self.docker("exec", "--user", "99:100", app, "node", "/tmp/container-ui-smoke.mjs", timeout=120)
        self.docker("cp", app + ":/tmp/container-ui.png", self.artifacts / "container-ui.png")
        self.passed("bundled UI initialization and onboarding write through the real browser")

        self.docker("restart", "--time", "90", app, timeout=100)
        self.ready(app)
        assert self.system_id(app) == identity
        assert self.sql(app, "SELECT value FROM container_persistence_check;") == "retained"
        self.stop(app)
        recreated = self.launch(volume)
        self.ready(recreated)
        assert self.system_id(recreated) == identity
        assert self.sql(recreated, "SELECT value FROM container_persistence_check;") == "retained"
        self.docker("exec", "--user", "99:100", recreated, "python", "-c",
                    "import json,urllib.request; "
                    "data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/workspace/config')); "
                    "assert data['workspace_path']=='/workspace/container-onboarding-check'")
        self.passed("graceful restart and recreation preserve cluster identity, data and UI-authored workspace settings")

        # Dump stdout must be only the custom archive, not entrypoint diagnostics.
        archive = self.docker("exec", "--user", "99:100", recreated, "python", RUNTIME,
                              "dump", "--file", "-").stdout
        assert archive.startswith(b"PGDMP"), "Dump stream is not a PostgreSQL custom archive"
        host_backup = self.artifacts / "host-helper.dump"
        helper_env = dict(os.environ, COMPOSE_PROJECT_NAME=self.prefix,
                          COMPOSE_FILE=str(ROOT / "docker-compose.yml"))
        helper = subprocess.run(["bash", str(ROOT / "tools/backup-db.sh"), str(host_backup)], env=helper_env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        helper_log = re.sub(rb"postgres(?:ql)?://[^\s]+", b"[redacted-database-url]",
                            helper.stdout + helper.stderr)
        (self.artifacts / "host-helper.log").write_bytes(helper_log)
        assert helper.returncode == 0, "Host backup helper failed: " + helper_log.decode(errors="replace")
        with host_backup.open("rb") as stream:
            assert stream.read(5) == b"PGDMP", "Host backup helper corrupted the custom dump"
        host_backup.unlink()
        fixture = self.volume()
        self.inspect_volume(fixture, "from pathlib import Path; import sys; p=Path('/data/fixture.dump'); p.write_bytes(sys.stdin.buffer.read()); p.chmod(0o644)", input=archive)
        self.docker("exec", "-e", "BACKUP_DIR=/data/acceptance-backups", recreated,
                    "bash", "/app/scripts/backup.sh", timeout=180)
        self.docker("exec", "--user", "99:100", recreated, "python", "-c",
                    "from pathlib import Path; import stat,tarfile; "
                    "p=Path('/data/acceptance-backups'); files=list(p.iterdir()); "
                    "assert any('settings-' in f.name for f in files); "
                    "assert all(stat.S_IMODE(f.stat().st_mode)==0o600 for f in files if f.is_file()); "
                    "t=next(p.glob('ai-shuffle-data-*.tar.gz')); "
                    "assert not any(n.startswith('./postgres/data') for n in tarfile.open(t).getnames())")
        self.passed("online dump stream and packaged full-backup script work without exposing PostgreSQL")

        destination = self.volume()
        restored = self.launch(destination, command=("restore", "--file", "/fixture/fixture.dump"),
                               mounts=(f"type=volume,src={fixture},dst=/fixture,readonly",))
        self.exited(restored, success=True, timeout=180)
        # Restore matching app keys/identity from the real full-backup artifact,
        # preserving the NEW cluster's mode binding and database credentials.
        recover_files = (
            "from pathlib import Path; import subprocess; "
            "archives=list(Path('/source/acceptance-backups').glob('ai-shuffle-data-*.tar.gz')); "
            "assert len(archives)==1; "
            "subprocess.run(['tar','-xzf',str(archives[0]),'-C','/data',"
            "'--exclude=./postgres','--exclude=./database-mode.json'],check=True)"
        )
        self.docker("run", "--rm", "--network", "none", "--entrypoint", "python",
                    "--mount", f"type=volume,src={volume},dst=/source,readonly",
                    "--mount", f"type=volume,src={destination},dst=/data",
                    self.image, "-c", recover_files)
        restored_app = self.launch(destination)
        self.ready(restored_app)
        assert self.sql(restored_app, "SELECT value FROM container_persistence_check;") == "retained"
        assert self.system_id(restored_app) != identity
        self.stop(restored_app)
        duplicate_restore = self.launch(destination, command=("restore", "--file", "/fixture/fixture.dump"),
                                        mounts=(f"type=volume,src={fixture},dst=/fixture,readonly",))
        self.exited(duplicate_restore, success=False)
        self.passed("offline logical restore into fresh cluster succeeds; nonempty destination restore is refused")

        # A valid dump referencing a source-only role must fail transactionally
        # on a fresh destination (policy role references survive --no-privileges).
        self.sql(recreated, "CREATE ROLE container_missing_restore_role;", admin=True)
        self.sql(recreated, "CREATE TABLE container_restore_failure (value text); "
                 "CREATE POLICY source_only ON container_restore_failure TO container_missing_restore_role USING (true);")
        failed_archive = self.docker("exec", "--user", "99:100", recreated, "python", RUNTIME,
                                     "dump", "--file", "-").stdout
        self.failure_fixture = self.volume()
        self.inspect_volume(self.failure_fixture,
            "from pathlib import Path; import sys; p=Path('/data/failure.dump'); p.write_bytes(sys.stdin.buffer.read()); p.chmod(0o644)",
            input=failed_archive)

        # A real unexpected database death must fence the app, then recover WAL.
        self.docker("exec", "--user", "99:100", recreated, "python", "-c",
                    "from pathlib import Path; import os,signal; os.kill(int(Path('/data/postgres/data/postmaster.pid').read_text().splitlines()[0]),signal.SIGKILL)")
        self.exited(recreated, success=False)
        recovered = self.launch(volume)
        self.ready(recovered)
        assert self.sql(recovered, "SELECT value FROM container_persistence_check;") == "retained"
        assert self.system_id(recovered) == identity
        self.stop(recovered)
        self.passed("unexpected PostgreSQL death stops the app; normal PostgreSQL recovery preserves committed data")

        # Modify only our stopped disposable volume; never a user's data.
        self.inspect_volume(volume, r"from pathlib import Path; Path('/data/postgres/data/PG_VERSION').write_text('17\n')")
        incompatible = self.launch(volume)
        self.exited(incompatible, success=False)
        self.inspect_volume(volume, r"from pathlib import Path; assert Path('/data/postgres/data/PG_VERSION').read_text()=='17\n'; assert Path('/data/postgres/data/global/pg_control').is_file()")
        self.passed("wrong PostgreSQL major fails closed without rewriting the old cluster")

    def identity_and_failure_safety(self):
        volume = self.volume()
        mapped = self.launch(volume, env={"PUID": "12345", "PGID": "12346"})
        self.ready(mapped)
        self.docker("exec", "--user", "12345:12346", mapped, "python", "-c",
                    "from pathlib import Path; import os; assert Path('/data/postgres/data').stat().st_uid==os.getuid()==12345")
        self.stop(mapped)
        direct = self.launch(self.volume(), user="1000:1000", env={"PUID": "1000", "PGID": "1000"})
        self.ready(direct)
        self.stop(direct)
        self.passed("custom Unraid PUID/PGID and explicit non-root Docker user boot successfully")

        legacy = self.volume()
        self.inspect_volume(legacy, "from pathlib import Path; import os; p=Path('/data/instance_id'); p.write_text('retained-install'); os.chown(p,99,100); os.chown('/data',99,100)")
        ambiguous = self.launch(legacy)
        self.exited(ambiguous, success=False)
        self.inspect_volume(legacy, "from pathlib import Path; assert Path('/data/instance_id').read_text()=='retained-install'; assert not Path('/data/postgres/data/PG_VERSION').exists()")

        invalid_source = self.volume()
        self.inspect_volume(invalid_source, "from pathlib import Path; Path('/data/bad.dump').write_text('not a PostgreSQL archive')")
        destination = self.volume()
        bad_restore = self.launch(destination, command=("restore", "--file", "/fixture/bad.dump"),
                                  mounts=(f"type=volume,src={invalid_source},dst=/fixture,readonly",))
        self.exited(bad_restore, success=False)
        self.inspect_volume(destination, "from pathlib import Path; assert not Path('/data/postgres/data/PG_VERSION').exists()")
        failed_destination = self.volume()
        failed_restore = self.launch(failed_destination, command=("restore", "--file", "/fixture/failure.dump"),
                                     mounts=(f"type=volume,src={self.failure_fixture},dst=/fixture,readonly",))
        self.exited(failed_restore, success=False)
        blocked = self.launch(failed_destination)
        self.exited(blocked, success=False)
        self.passed("legacy data is protected; invalid archives leave no cluster; failed restore transactions fence normal startup")

    def external_compatibility(self):
        network = self.prefix + "-network"
        self.docker("network", "create", "--internal", network)
        self.networks.append(network)
        external = self.prefix + "-external"
        self.containers.append(external)
        password = uuid.uuid4().hex
        self.docker("run", "-d", "--name", external, "--network", network,
                    "--network-alias", "legacy-postgres", "-e", "POSTGRES_USER=legacy_worker",
                    "-e", "POSTGRES_DB=legacy_worker", "-e", "POSTGRES_PASSWORD=" + password,
                    "postgres:16-alpine")
        deadline = time.monotonic() + 60
        while self.docker("exec", external, "pg_isready", "-U", "legacy_worker", check=False).returncode:
            assert self.state(external)["Running"], "External fixture exited"
            if time.monotonic() >= deadline:
                raise TimeoutError("External PostgreSQL fixture did not become ready")
            time.sleep(1)
        volume = self.volume()
        # Exercise the same env-file selection as an existing install, not only -e.
        env_text = "DATABASE_URL=postgresql://legacy_worker:" + password + "@legacy-postgres:5432/legacy_worker\n"
        self.inspect_volume(volume, "from pathlib import Path; import os,sys; p=Path('/data/external.env'); p.write_bytes(sys.stdin.buffer.read()); p.chmod(0o600); os.chown(p,99,100); os.chown('/data',99,100)", input=env_text.encode())
        app = self.launch(volume, network=network, env={"AI_SHUFFLE_ENV_FILE": "/data/external.env"})
        self.ready(app)
        assert self.sql(app, "SELECT current_user;") == "legacy_worker"
        self.inspect_volume(volume, "from pathlib import Path; assert not Path('/data/postgres').exists()")
        self.stop(app)
        assert self.state(external)["Running"], "App stopped an externally managed database"
        missing_config = self.launch(volume)
        self.exited(missing_config, success=False)
        self.inspect_volume(volume, "from pathlib import Path; assert not Path('/data/postgres').exists()")
        broken = self.launch(self.volume(), env={"DATABASE_URL": "postgresql://REDACTED@127.0.0.1:1/absent"})
        self.exited(broken, success=False, timeout=180)
        self.passed("external env-file database survives app shutdown; missing/unreachable external configuration never falls back")

    def cleanup(self):
        for name in reversed(self.containers):
            captured = self.docker("logs", name, check=False)
            logs = captured.stdout + captured.stderr
            logs = re.sub(rb"postgres(?:ql)?://[^\s]+", b"[redacted-database-url]", logs)
            (self.artifacts / (name + ".log")).write_bytes(logs)
            state = self.docker("inspect", "--format", "{{json .State}}", name, check=False)
            (self.artifacts / (name + ".state.json")).write_bytes(state.stdout)
            if name != self.prefix + "-external":
                metadata = self.docker("run", "--rm", "--network", "none", "--volumes-from", name,
                    "--entrypoint", "python", self.image, "-c",
                    "from pathlib import Path; import json; "
                    "paths=[Path('/data'),Path('/workspace')]+list(Path('/data').glob('*'))[:12]; "
                    "print(json.dumps([{'path':str(p),'uid':p.lstat().st_uid,'gid':p.lstat().st_gid,"
                    "'mode':oct(p.lstat().st_mode & 0o777),'bytes':p.lstat().st_size} "
                    "for p in paths if p.exists()]))", check=False)
                (self.artifacts / (name + ".storage.json")).write_bytes(metadata.stdout)
            self.docker("rm", "--force", "--volumes", name, check=False)
        for name in self.volumes:
            self.docker("volume", "rm", name, check=False)
        for name in self.networks:
            self.docker("network", "rm", name, check=False)
        (self.artifacts / "results.json").write_text(json.dumps(self.results, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    check = Check(args.image, args.artifacts)
    try:
        check.docker("image", "inspect", args.image)
        # Resolve the isolated external fixture before putting containers on an
        # internal network; never grant the app outbound access to real services.
        check.docker("pull", "postgres:16-alpine", timeout=180)
        check.fresh_persistence_and_restore()
        check.identity_and_failure_safety()
        check.external_compatibility()
    finally:
        check.cleanup()


if __name__ == "__main__":
    main()
