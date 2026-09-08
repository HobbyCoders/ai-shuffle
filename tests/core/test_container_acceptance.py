"""Guards for the actual-image acceptance harness (Docker runs separately in CI)."""
import json

from scripts.verify_container_postgres import Check


def test_backup_helper_uses_a_real_isolated_compose_service(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://never-forward.invalid/production")
    monkeypatch.setenv("POSTGRES_PASSWORD", "never-forward-this-password")
    check = Check("local-acceptance:tested", tmp_path)
    check.workspaces["owned-data"] = "owned-workspace"
    calls = []
    monkeypatch.setattr(check, "docker", lambda *args, **kwargs: calls.append(args))

    container, compose_file = check.launch_compose("owned-data")

    config = json.loads(compose_file.read_text())
    service = config["services"]["ai-shuffle"]
    assert service["image"] == "local-acceptance:tested"
    assert service["container_name"] == container
    assert service["network_mode"] == "none"
    assert service["environment"] == {"START_CHROME": "0"}
    assert "env_file" not in service and "labels" not in service
    assert config["volumes"] == {
        "data": {"external": True, "name": "owned-data"},
        "workspace": {"external": True, "name": "owned-workspace"},
    }
    assert service["volumes"] == ["data:/data", "workspace:/workspace"]
    assert calls == [("compose", "--project-name", check.prefix, "--file", str(compose_file),
                      "up", "--detach", "--no-build", "--pull", "never")]
    assert check.containers == [container]  # Standard evidence/cleanup owns it.
