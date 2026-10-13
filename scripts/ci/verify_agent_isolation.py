#!/usr/bin/env python3
"""Docker agent isolation (#1957) on an actual built image, with disposable
containers and volumes only.

An install starts with the mode off, is switched on, restarted, and switched
off again. With it on, the agent account (PUID 99) must get "Permission
denied" on every protected DATA_DIR entry, directly and through the spawn
runner the app uses, while the app stays ready, PostgreSQL runs as the service
account and a dump still works. Off again, everything is the agent account's
as before and the cluster is the same one.

Run with --image ai-shuffle:local --artifacts /tmp/agent-isolation-results.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_container_postgres import RUNTIME, Check  # noqa: E402

AGENT = "99:100"
SERVICE_UID = 64000
FILES = ("/data/managed-postgres.json", "/data/database-mode.json", "/data/preview-access.key")
DIRS = ("/data/.credential-keys", "/data/.managed-connection", "/data/postgres")
OWNERS = (
    "import json, os; from pathlib import Path; "
    "paths=[Path('/data')]+[p for n in ('managed-postgres.json','database-mode.json','.credential-keys',"
    "'.managed-connection','preview-access.key','postgres') for p in [Path('/data')/n] if p.exists()]; "
    "print(json.dumps({str(p): [p.lstat().st_uid, p.lstat().st_gid, oct(p.lstat().st_mode & 0o7777)] "
    "for p in paths}))"
)
RUNNER = "import fluid.tools.agent_runner as a; print(a.RUNNER)"


class IsolationCheck(Check):
    def owners(self, volume):
        return json.loads(self.inspect_volume(volume, OWNERS).stdout)

    def denied(self, container, argv):
        result = self.docker("exec", "--user", AGENT, container, *argv, check=False)
        output = (result.stdout + result.stderr).decode(errors="replace")
        assert result.returncode != 0 and "Permission denied" in output, f"{argv[-1]} was readable: {output[:200]}"
        assert "postgresql://" not in output and "password" not in output.lower()

    def process_ids(self, container, pattern):
        code = ("import os, re, sys\n"
                "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
                "    if int(pid) == os.getpid():\n"
                "        continue\n"
                "    try:\n"
                "        cmd = open(f'/proc/{pid}/cmdline','rb').read().replace(b'\\0', b' ').decode()\n"
                "        uids = [l.split()[1:] for l in open(f'/proc/{pid}/status') if l.startswith('Uid:')][0]\n"
                "    except OSError:\n"
                "        continue\n"
                "    if re.search(sys.argv[1], cmd):\n"
                "        print(' '.join(uids))\n")
        return self.docker("exec", container, "python", "-c", code, pattern).stdout.decode().split("\n")

    def dump(self, container):
        # Root, as tools/backup-db.sh runs it; the CLI drops to the split itself.
        return self.docker("exec", container, "python", RUNTIME, "dump", "--file", "-", timeout=180).stdout

    def round_trip(self):
        volume = self.volume()
        app = self.launch(volume)
        self.ready(app)
        identity = self.system_id(app)
        self.sql(app, "CREATE TABLE agent_isolation_check (value text); INSERT INTO agent_isolation_check VALUES ('kept');")
        # The preview and managed-connection keys are created on first use;
        # make them the way the app does.
        self.docker("exec", "--user", AGENT, "--workdir", "/app", app, "python", "-c",
                    "from app.core import managed_credentials, preview_access; "
                    "preview_access._key(); managed_credentials._key(create=True)")
        self.stop(app)
        before = self.owners(volume)
        # managed-postgres.json exists only while PostgreSQL runs.
        assert set(before) == {"/data", *FILES, *DIRS} - {FILES[0]}, sorted(before)
        assert all(uid == 99 for uid, _gid, _mode in before.values()), before
        self.passed("mode off: an install's protected entries belong to PUID, as before")

        on = self.launch(volume, env={"AGENT_ISOLATION": "on"})
        self.ready(on)
        for path in FILES:
            self.denied(on, ["cat", path])
        for path in DIRS:
            self.denied(on, ["ls", path])
        runner = self.docker("exec", "--user", AGENT, on, "python", "-c", RUNNER).stdout.decode().strip()
        for path in FILES:
            # As the app spawns it: agent ids, no supplementary groups, a
            # filtered environment, an approved working directory.
            result = self.docker(
                "exec", "--workdir", "/tmp", on, "env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin",
                "setpriv", "--reuid=99", "--regid=100", "--clear-groups",
                "python", "-I", "-S", runner, "--root", "/workspace", "--root", "/tmp", "--", "cat", path,
                check=False)
            output = (result.stdout + result.stderr).decode(errors="replace")
            assert result.returncode != 0 and "Permission denied" in output, f"runner read {path}: {output[:200]}"
        writable = self.docker("exec", "--user", AGENT, on, "python", "-c",
                               "import os, sys; print(os.access(sys.argv[1], os.W_OK), "
                               "os.access('/app/app/main.py', os.W_OK))", runner).stdout.decode().split()
        assert writable == ["False", "False"], "the agent account can modify code the app runs"
        app_ids = [ids for ids in self.process_ids(on, r"-c import os, runpy") if ids]
        assert app_ids and all(ids.split()[:2] == ["99", "99"] and ids.split()[2] == str(SERVICE_UID)
                               for ids in app_ids), app_ids
        pg_ids = [ids for ids in self.process_ids(on, r"^(\S*/)?postgres[ :]") if ids]
        assert pg_ids and all(set(ids.split()) == {str(SERVICE_UID)} for ids in pg_ids), pg_ids
        assert b"agent_isolation_check" in self.dump(on)
        self.passed("mode on: PUID gets Permission denied on every protected entry, directly and through the "
                    "spawn runner; the app holds the service account only as its saved uid; PostgreSQL runs as "
                    "it; a root dump works")

        self.docker("restart", "--time", "90", on, timeout=100)
        self.ready(on)
        self.denied(on, ["cat", FILES[0]])
        self.stop(on)
        record = self.inspect_volume(volume, "import os; r=os.lstat('/data/agent-isolation.json'); "
                                             "d=os.lstat('/data'); print(r.st_uid, d.st_uid, d.st_mode & 0o1770 == 0o1770)")
        assert record.stdout.decode().split() == ["0", str(SERVICE_UID), "True"], record.stdout
        self.passed("mode on survives a restart; the root-owned migration record is in place")

        off = self.launch(volume)
        self.ready(off)
        assert self.system_id(off) == identity
        assert self.sql(off, "SELECT value FROM agent_isolation_check;") == "kept"
        self.stop(off)
        after = self.owners(volume)
        assert after == before, (before, after)
        self.inspect_volume(volume, "from pathlib import Path; assert not Path('/data/agent-isolation.json').exists()")
        self.passed("mode off again: ownership and modes are back exactly as before, same cluster and data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    check = IsolationCheck(args.image, args.artifacts)
    check.prefix = check.prefix.replace("pg-check", "isolation-check")
    try:
        check.docker("image", "inspect", args.image)
        check.round_trip()
    finally:
        check.cleanup()


if __name__ == "__main__":
    main()
