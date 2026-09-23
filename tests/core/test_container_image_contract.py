"""Cheap packaging guards; real container acceptance runs separately in CI."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_docker_bundles_one_supported_postgres_major():
    image = (ROOT / "Dockerfile").read_text()
    assert "FROM python:3.12-slim-trixie" in image
    assert "postgresql-16" in image
    assert "postgresql-client-16" in image
    assert "signed-by=/etc/apt/keyrings/postgresql.asc" in image
    assert "create_main_cluster = false" in image
    assert "/usr/lib/postgresql/16/bin" in image
    assert "pg_trgm.control" in image


def test_docker_supervisor_owns_signals_and_persistent_storage():
    image = (ROOT / "Dockerfile").read_text()
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]' in image
    assert "COPY docker/container_runtime.py docker/application_updates.py docker/update_contract.py ./docker/" in image
    assert "COPY desktop/build/application-update-key.pem ./docker/application-update-key.pem" in image
    assert "ARG APPLICATION_SEQUENCE=0" in image
    assert 'VOLUME ["/data"]' in image
    assert "ENV DATA_DIR=/data" in image
    assert "EXPOSE 5432" not in image
    assert "/health/ready" in image


def test_image_startup_does_not_require_network_package_installation():
    entrypoint = (ROOT / "entrypoint.sh").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    # Updates arrive by pulling a new image. Compose no longer offers the
    # legacy AUTO_UPDATE switch; while the entrypoint still carries it, it
    # must stay opt-in and time-bounded.
    assert "AUTO_UPDATE" not in compose
    if "AUTO_UPDATE" in entrypoint:
        assert "AUTO_UPDATE=${AUTO_UPDATE:-false}" in entrypoint
        assert "timeout 60s" in entrypoint
    assert "DATABASE_URL=${DATABASE_URL:-}" not in compose
    assert "DATABASE_MODE=${DATABASE_MODE:-auto}" not in compose
