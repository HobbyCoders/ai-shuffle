"""Container acceptance must use the same identity as the app reader."""
from unittest.mock import Mock

from scripts.verify_container_postgres import Check


def test_removed_env_probe_runs_as_managed_app_user():
    harness = Mock()
    harness.sql.side_effect = ["ai_shuffle", "127.0.0.1"]
    Check.removed_environment_regression(harness)
    args = harness.docker.call_args.args
    assert args[:3] == ("exec", "--user", "99:100")
    assert args[3] is harness.launch.return_value
    assert args[4:6] == ("python", "-c")
