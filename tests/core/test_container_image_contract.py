"""Cheap packaging guards; real container acceptance runs separately in CI."""
import importlib.util
import json
from pathlib import Path

import pytest

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
    installer = (ROOT / "docker" / "install-claude-cli.sh").read_text()
    # Updates arrive by pulling a new image: no AUTO_UPDATE switch anywhere and
    # no package-manager upgrades at boot.
    assert "AUTO_UPDATE" not in compose
    assert "AUTO_UPDATE" not in entrypoint
    assert "apt-get" not in entrypoint
    # The Claude CLI install is bounded and never fails the container.
    assert "install-claude-cli.sh || true" in entrypoint
    assert "timeout 600s" in installer
    assert "https://claude.ai/install.sh" in installer
    assert "local-home:/home/appuser/.local" in compose
    assert "DATABASE_URL=${DATABASE_URL:-}" not in compose
    assert "DATABASE_MODE=${DATABASE_MODE:-auto}" not in compose


# --- The image/source contract (docker/update_contract.py, #1821) ------------

_spec = importlib.util.spec_from_file_location("shuffle_update_contract", ROOT / "docker" / "update_contract.py")
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)


def test_base_1_releases_publish_the_string_installed_images_compare():
    # The fingerprint today's images carry (#1821); never change it.
    assert contract.LEGACY_RUNTIME == "dd0de098e53ad234da2d74984419505c0eee7be6a2effde68e06cdc199f95d0c"
    assert contract.runtime(("base-1",)) == contract.LEGACY_RUNTIME
    assert contract.runtime(()) == contract.LEGACY_RUNTIME
    assert contract.manifest_fields()["runtime"] == contract.LEGACY_RUNTIME


def test_a_new_requirement_publishes_a_string_no_older_image_matches():
    more = contract.runtime(("base-1", "python-3.13"))
    assert more != contract.LEGACY_RUNTIME
    assert more == contract.runtime(("python-3.13", "base-1", "base-1"))
    assert more != contract.runtime(("base-1", "node-24"))


def test_declarations_are_consistent_and_the_pin_is_current(capsys):
    assert contract.validate() == []
    assert contract.main(["--check"]) == 0, capsys.readouterr().err
    assert contract.image_inputs_digest(contract._reader(ROOT)) == contract.IMAGE_INPUTS_PIN


def test_the_image_records_what_it_provides():
    record = contract.image_record("a" * 40, "7")
    assert record == {"format": 2, "revision": "a" * 40, "sequence": 7, "runtime": contract.LEGACY_RUNTIME,
                      "provides": sorted(contract.PROVIDES)}
    assert set(contract.REQUIRES) <= set(record["provides"])
    assert json.loads(json.dumps(contract.manifest_fields())) == {
        "runtime": contract.LEGACY_RUNTIME, "requires": sorted(contract.REQUIRES),
        "recommends": sorted(contract.RECOMMENDS)}


def test_comments_and_blank_lines_never_need_a_new_pin():
    files = {name: (ROOT / name).read_bytes() for name in contract.IMAGE_INPUTS}
    pinned = contract.image_inputs_digest(files.get)
    # Notes anywhere below the first line (the shebang stays first).
    commented = {name: data.replace(b"\n", b"\n# a note\n\n   # indented note\n", 3) + b"\n# end\n"
                 for name, data in files.items()}
    assert contract.image_inputs_digest(commented.get) == pinned
    changed = dict(files, Dockerfile=files["Dockerfile"] + b"RUN true\n")
    assert contract.image_inputs_digest(changed.get) != pinned
    shebang = dict(files, **{"entrypoint.sh": files["entrypoint.sh"].replace(b"#!/bin/bash", b"#!/bin/sh", 1)})
    if shebang != files:
        assert contract.image_inputs_digest(shebang.get) != pinned


@pytest.mark.parametrize(("names", "message"), [
    ({"REQUIRES": ("base-1", "python-3.13")}, "CAPABILITIES does not describe"),
    ({"RECOMMENDS": ("Python 3.13",)}, "CAPABILITIES does not describe"),
])
def test_undeclared_capabilities_are_errors(monkeypatch, names, message):
    for name, value in names.items():
        monkeypatch.setattr(contract, name, value)
    assert any(message in error for error in contract.validate())


def test_requiring_what_new_images_would_not_provide_is_an_error(monkeypatch):
    monkeypatch.setitem(contract.CAPABILITIES, "python-3.13", "Python 3.13")
    monkeypatch.setattr(contract, "REQUIRES", ("base-1", "python-3.13"))
    assert any("would not provide" in error for error in contract.validate())
    monkeypatch.setattr(contract, "PROVIDES", (*contract.PROVIDES, "python-3.13"))
    assert contract.validate() == []


def test_the_image_builds_its_record_from_the_contract_and_carries_the_launch_scripts():
    image = (ROOT / "Dockerfile").read_text()
    assert 'RUN python docker/update_contract.py /app "$GIT_SHA" "$APPLICATION_SEQUENCE" > /app/docker/image.json' in image
    # The runtime starts these from the active update when it can, else from here.
    assert "COPY docker/start-chrome.sh docker/chrome-supervisor.sh" in image


def test_updates_carry_the_supervisor_files_they_hand_over_to():
    from scripts import source_archive
    assert "docker" in source_archive.RUNTIME_PATHS
