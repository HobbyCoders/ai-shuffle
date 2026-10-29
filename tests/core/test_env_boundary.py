"""Prevent env configuration from growing outside the reviewed boundary."""
import json
from pathlib import Path

from app.core.config import Settings
from scripts.ci.env_boundary import basesettings_names, discovered, own_names

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_environment_boundary():
    allowed = json.loads((ROOT / "scripts/ci/env_allowlist.json").read_text())
    failures = []
    for path, name in discovered(ROOT):
        if name not in allowed:
            failures.append(f"{path}: {name} needs an explicit infra/internal reason")
    for name, entry in allowed.items():
        assert entry["kind"] in {"infra", "internal"}, name
        assert entry["reason"].strip(), name
        if "target_pr" in entry:
            assert entry["target_pr"] in range(1, 7), name
    # Runtime introspection also catches Pydantic aliases and inherited fields.
    for name, field in Settings.model_fields.items():
        names = {name.upper()}
        alias = field.validation_alias
        if hasattr(alias, "choices"):
            names.update(a.upper() for a in alias.choices if isinstance(a, str))
        elif isinstance(alias, str):
            names.add(alias.upper())
        for env_name in names:
            if not allowed.get(env_name, {}).get("base_settings"):
                failures.append(f"BaseSettings field {name}: {env_name} is not approved")
    assert not failures, "\n".join(sorted(set(failures)))


def test_preference_publication_kill_switch_is_internal_only():
    allowed = json.loads((ROOT / "scripts/ci/env_allowlist.json").read_text())
    entry = allowed["AI_SHUFFLE_PERF_RESOURCE_WAKE"]
    assert entry["kind"] == "internal"
    assert entry["base_settings"] is False


def test_scanner_detects_new_fields_aliases_and_own_prefix_reads():
    source = '''class Config(BaseSettings):
    harmless_name: bool = Field(False, validation_alias="AI_SHUFFLE_NEW_BEHAVIOUR")
    policy: ClassVar[bool] = True
value = os.environ.get("FLUID_NEW_BEHAVIOUR")
'''
    assert basesettings_names(source) == {"HARMLESS_NAME", "AI_SHUFFLE_NEW_BEHAVIOUR"}
    assert basesettings_names("from pydantic_settings import BaseSettings as Env\nclass Deployment(Env):\n    port: int = 8000\nclass Extra(Deployment):\n    new_feature: bool = True") == {"PORT", "NEW_FEATURE"}
    for source in (source, 'process.env.FLUID_NEW_BEHAVIOUR', '${FLUID_NEW_BEHAVIOUR}',
                   '$.env.get("FLUID_NEW_BEHAVIOUR")', 'env["FLUID_NEW_BEHAVIOUR"]'):
        assert "FLUID_NEW_BEHAVIOUR" in own_names(source)
