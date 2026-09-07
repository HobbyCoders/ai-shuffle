"""Safety-only tests. No Docker daemon, real PostgreSQL, or provider calls."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("pg_capacity", Path(__file__).with_name("pg_capacity.py"))
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


class SafetyTests(unittest.TestCase):
    def target(self):
        return {"token": "a" * 32, "name": "shuffle-capacity-" + "a" * 32,
                "container_id": "b" * 64,
                "url": "postgresql://capacity:" + "c" * 48 + "@127.0.0.1:49152/capacity"}

    def test_generated_docker_arguments_are_ephemeral_and_loopback(self):
        t = self.target()
        args = harness.docker_args(t["name"], t["token"], 80)
        self.assertEqual(args[:4], ["docker", "--context", "default", "run"])
        self.assertEqual(args[args.index("--publish") + 1], "127.0.0.1::5432")
        self.assertIn("--rm", args)
        self.assertIn(harness.IMAGE, args)
        self.assertIn("POSTGRES_PASSWORD", args)
        self.assertFalse(any(x.startswith("POSTGRES_PASSWORD=") for x in args))
        self.assertIn("/var/lib/postgresql/data:rw,nosuid,nodev,size=1g", args)
        for forbidden in ("--volume", "-v", "--mount", "--env-file", "--network", "--privileged"):
            self.assertNotIn(forbidden, args)

    def test_identity_and_cap_reject_untrusted_values(self):
        t = self.target()
        for name, token, cap in [("existing", t["token"], 10),
                                 (t["name"], "../x", 10),
                                 (t["name"], t["token"], 999)]:
            with self.subTest(name=name, cap=cap), self.assertRaises(ValueError):
                harness.docker_args(name, token, cap)

    def test_only_generated_target_shape_accepted(self):
        t = self.target()
        self.assertEqual(harness.validate_target(t).port, 49152)
        for url in ("postgresql://capacity:secret@production:5432/capacity",
                    t["url"].replace("127.0.0.1", "localhost"),
                    t["url"].replace("/capacity", "/user_data"),
                    t["url"] + "?host=production", t["url"] + "#x",
                    t["url"].replace(":49152", ":543")):
            with self.subTest(url=url.split("@")[-1]), self.assertRaises(ValueError):
                harness.validate_target(dict(t, url=url))

    def test_environment_drops_shared_database_and_provider_credentials(self):
        source = {"PATH": "tools", "DATABASE_URL": "shared-secret", "POSTGRES_HOST": "production",
                  "PGSERVICEFILE": "secrets", "ANTHROPIC_API_KEY": "paid-key",
                  "AI_SHUFFLE_ENV_FILE": "real.env", "PYTHONPATH": "untrusted",
                  "DOCKER_HOST": "tcp://remote", "DOCKER_CONTEXT": "remote"}
        self.assertEqual(harness.clean_env(source), {"PATH": "tools"})
        with patch.dict(os.environ, source, clear=True):
            env = harness.worker_environment(self.target(), "/disposable", 0)
        self.assertEqual(env["DATABASE_URL"], self.target()["url"])
        self.assertEqual(env["TESTING"], "1")
        self.assertEqual(env["ALLOW_REAL_PG"], "1")
        self.assertNotIn("POSTGRES_HOST", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotEqual(env["AI_SHUFFLE_ENV_FILE"], "real.env")

    def test_default_gate_never_calls_docker(self):
        with patch.dict(os.environ, {"ALLOW_REAL_PG": "0"}), patch.object(harness, "docker") as docker:
            with self.assertRaises(RuntimeError):
                harness.launch(harness.parse_args([]))
            docker.assert_not_called()

    def test_child_rejects_missing_overrides_before_attestation_or_app_import(self):
        for env in ({}, {"ALLOW_REAL_PG": "1", "TESTING": "1", "DATABASE_URL": "shared"}):
            with patch.dict(os.environ, env, clear=True), patch.object(harness, "attest") as attest:
                with self.assertRaises(ValueError):
                    harness.child({"target": self.target()})
                attest.assert_not_called()

    def test_attestation_requires_owned_container_no_persistent_mounts(self):
        t = self.target()
        info = {"Id": t["container_id"], "Name": "/" + t["name"],
                "Config": {"Image": harness.IMAGE, "Labels": {harness.LABEL: t["token"]}},
                "State": {"Running": True}, "Mounts": [{"Type": "tmpfs"}],
                "NetworkSettings": {"Ports": {"5432/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "49152"}]}}}
        with patch.object(harness, "docker", return_value=json.dumps([info])):
            harness.attest(t, {})
        variants = []
        for field, value in (("Name", "/existing"), ("Mounts", [{"Type": "volume"}]),
                             ("Mounts", [{"Type": "bind"}]),
                             ("Config", {"Image": harness.IMAGE, "Labels": {}}),
                             ("NetworkSettings", {"Ports": {"5432/tcp": [
                                 {"HostIp": "0.0.0.0", "HostPort": "49152"}]}})):
            variants.append(dict(info, **{field: value}))
        for variant in variants:
            with patch.object(harness, "docker", return_value=json.dumps([variant])):
                with self.assertRaises(ValueError):
                    harness.attest(t, {})

    def test_cleanup_refuses_foreign_container_and_verifies_removal(self):
        t = self.target()
        info = {"Id": t["container_id"], "Name": "/" + t["name"],
                "Config": {"Labels": {harness.LABEL: "foreign"}}}
        with patch.object(harness, "docker", return_value=json.dumps([info])) as docker:
            with self.assertRaises(ValueError):
                harness.cleanup_owned(t["container_id"], t["name"], t["token"], {})
            self.assertEqual(docker.call_count, 1)
            self.assertEqual(docker.call_args.args[0][0], "inspect")
        info["Config"]["Labels"][harness.LABEL] = t["token"]
        with patch.object(harness, "docker", side_effect=[json.dumps([info]), "", ""]) as docker:
            harness.cleanup_owned(t["container_id"], t["name"], t["token"], {})
            self.assertEqual(docker.call_args_list[1].args[0], ["rm", "--force", t["container_id"]])
            self.assertEqual(docker.call_args_list[2].args[0][0], "ps")
        with patch.object(harness, "docker", side_effect=[json.dumps([info]), "", t["container_id"]]):
            with self.assertRaises(RuntimeError):
                harness.cleanup_owned(t["container_id"], t["name"], t["token"], {})

    def test_cli_bounds(self):
        self.assertEqual(harness.parse_args([]).turns, 80)
        for args in (["--turns", "0"], ["--turns", "129"], ["--installs", "9"],
                     ["--pool-cap", "1"], ["--interval-ms", "0"], ["--stagger-ms", "-1"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                harness.parse_args(args)

    def test_driver_error_output_does_not_expose_credentials(self):
        fake = subprocess.CompletedProcess([], 1, "", "secret-password")
        with patch.object(harness.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError) as caught:
                harness.docker(["inspect", "owned-id"], {})
        self.assertNotIn("secret-password", str(caught.exception))

    def test_blocked_standalone_is_json_and_does_not_import_app(self):
        env = harness.clean_env(os.environ)
        env.update(ALLOW_REAL_PG="0", DATABASE_URL="DO_NOT_PRINT_SHARED_SECRET")
        result = subprocess.run([sys.executable, str(Path(harness.__file__))],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertNotIn("DO_NOT_PRINT_SHARED_SECRET", result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")


class WorkloadTests(unittest.TestCase):
    def test_reader_rechecks_after_completion_races_an_empty_snapshot(self):
        if "pytest" not in sys.modules or os.environ.get("ALLOW_REAL_PG", "0") != "0":
            self.skipTest("requires the normal ALLOW_REAL_PG=0 pytest shim")
        import asyncio
        import threading
        from types import SimpleNamespace
        from app.core.turn_recorder import TurnRecorder
        from app.db import session_events

        finished = threading.Event()
        first = True
        original_read = session_events.get_events
        original_close = TurnRecorder.close

        def delayed_empty_read(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                # An empty snapshot taken before the final commit can return
                # after the producer has signalled completion.
                if not finished.wait(5):
                    raise RuntimeError("producer did not finish")
                return []
            return original_read(*args, **kwargs)

        async def close_and_signal(recorder, *args, **kwargs):
            await original_close(recorder, *args, **kwargs)
            finished.set()

        pool = SimpleNamespace(_connect=lambda key=None: None, _physical=set())
        base = SimpleNamespace(_pg_pool=pool, pressure_stats=lambda: {})
        payload = dict(turns=1, index="reader-race", seconds=.1, launch="simultaneous",
                       stagger_ms=0, interval_ms=10, pool_cap=2, faults=False)
        with patch.object(session_events, "get_events", side_effect=delayed_empty_read), \
             patch.object(TurnRecorder, "close", new=close_and_signal):
            result = asyncio.run(harness.workload(payload, base))
        self.assertTrue(result["ok"], result)

    def test_synthetic_workload_logic_on_normal_pytest_shim(self):
        # Under pytest the parent conftest supplies SQLite, never real PostgreSQL.
        # Standalone unittest discovery skips this test instead of touching a DB.
        if "pytest" not in sys.modules or os.environ.get("ALLOW_REAL_PG", "0") != "0":
            self.skipTest("requires the normal ALLOW_REAL_PG=0 pytest shim")
        import asyncio
        from types import SimpleNamespace
        pool = SimpleNamespace(_connect=lambda key=None: None, _physical=set())
        base = SimpleNamespace(_pg_pool=pool, pressure_stats=lambda: {})
        payload = dict(turns=2, index="shim", seconds=.25, launch="simultaneous",
                       stagger_ms=0, interval_ms=10, pool_cap=2, faults=False)
        result = asyncio.run(harness.workload(payload, base))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["counts"]["verified_turns"], 2)
        self.assertGreater(result["counts"]["lease_renewals"], 0)
        self.assertEqual(result["counts"]["expected_text_bytes"],
                         result["counts"]["actual_text_bytes"])


if __name__ == "__main__":
    unittest.main()
