"""Cloudflare rate-limit namespace ids are per account: no two Workers may share one.

Every Worker declares its limiters twice, in android/<worker>/wrangler.toml
(local runs and tests) and in its deploy workflow (production), so both are
checked, and they must agree.
"""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKERS = {"hub": "deploy-hub.yml", "site": "deploy-site.yml", "license": "deploy-license.yml", "registry": "deploy-registry.yml"}


def wrangler_limiters(worker):
    config = tomllib.loads((ROOT / "android" / worker / "wrangler.toml").read_text())
    return {item["name"]: item["namespace_id"] for item in config.get("ratelimits", [])}


def workflow_limiters(workflow):
    text = (ROOT / ".github" / "workflows" / workflow).read_text()
    return dict(re.findall(r'name:\s*"([A-Z_]+)",\s*namespace_id:\s*"(\d+)"', text))


def test_every_worker_has_its_own_namespace_ids_everywhere():
    owners = {}
    for worker, workflow in WORKERS.items():
        local, deployed = wrangler_limiters(worker), workflow_limiters(workflow)
        assert local, worker
        assert local == deployed, f"{worker}: wrangler.toml and {workflow} disagree"
        for name, namespace in local.items():
            assert namespace not in owners, f"{worker} {name} reuses namespace {namespace} of {owners.get(namespace)}"
            owners[namespace] = f"{worker} {name}"


def test_the_site_and_license_limiters_no_longer_collide():
    assert wrangler_limiters("site")["WAITLIST_LIMITER"] != wrangler_limiters("license")["LICENSE_LIMITER"]
    for worker in ("site", "license", "registry"):
        text = (ROOT / "android" / worker / "wrangler.toml").read_text()
        assert "In use: hub 4201-4203, site 4301, registry 4401-4402, license 4501." in text, worker
