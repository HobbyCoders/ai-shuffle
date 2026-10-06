"""The "Cloud provider or gateway" Claude Code sign-in method (issue #1862).

The stock Claude Code CLI can also run on Amazon Bedrock, Google Vertex AI,
Microsoft Foundry, or an LLM gateway (a base URL plus a bearer token). AI
Shuffle offers these as a third Claude auth mode, ``cloud``
(``app.core.claude_auth_mode.MODE_CLOUD``), which is off by default and only
the owner turns on.

Rules this module keeps:

- Nothing is detected. The values come only from what the owner saved in
  Settings: the non-secret ones (region, project, resource, base URL) in the
  ``claude_cloud_config`` system setting, the secret ones encrypted with the
  app's credential storage (``app.core.encryption``, the same as the Anthropic
  API key) in ``claude_cloud_secrets``. Host, profile, project and plugin
  environment values are never used.
- One named method at a time. :func:`env_for_cli` returns exactly the chosen
  method's allowlisted variables. Everything else that could pick an account
  or an endpoint is removed from what the CLI would otherwise inherit: every
  name with a :data:`CLOUD_MODE_UNSET_PREFIXES` prefix (``AWS_``, ``GOOGLE_``,
  ``AZURE_``, ``ANTHROPIC_``, ...) and the names in :data:`CLOUD_MODE_UNSET`.
  Proxy and CA variables (``HTTPS_PROXY``, ``NODE_EXTRA_CA_CERTS``, ...) are the
  host owner's network setting and still apply;
  ``NODE_TLS_REJECT_UNAUTHORIZED`` (turning TLS checks off) never does.
- Local to this worker. Both settings are kept out of network sync
  (``app.network.policy``), like the auth mode itself.
- Secrets are write-only: :func:`public_config` reports only whether each one
  is set. Nothing here logs a value.

The variable names follow Claude Code's documentation (checked 2026-10-06):

- Bedrock: https://code.claude.com/docs/en/amazon-bedrock
- Vertex AI: https://code.claude.com/docs/en/google-vertex-ai
- Foundry: https://code.claude.com/docs/en/microsoft-foundry
- Gateway: https://code.claude.com/docs/en/llm-gateway-connect
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from app.db import database

logger = logging.getLogger(__name__)

CONFIG_SETTING = "claude_cloud_config"
SECRETS_SETTING = "claude_cloud_secrets"

WARNING_TEXT = (
    "Agents on this worker can run shell commands and can read these credentials, "
    "including the Vertex AI key file on this worker's disk, with their own tools and in "
    "terminals. A project's or your own .claude/settings.json can still change where Claude "
    "connects, and this worker's proxy and certificate settings still apply. Use an account "
    "with the smallest permissions that work, and only on a worker you trust."
)
GATEWAY_WARNING_TEXT = (
    "All Claude traffic from this worker goes to the address you enter. The browser tool "
    "also receives this address and token."
)


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    env: str
    secret: bool
    kind: str = "text"  # text | region | url | resource | json
    required: bool = True
    choice: Optional[str] = None  # alternative this field belongs to


@dataclass(frozen=True)
class Method:
    id: str
    label: str
    selector: Tuple[Tuple[str, str], ...]
    fields: Tuple[Field, ...]
    # Alternatives where exactly one must be filled: (choice id, label).
    choices: Tuple[Tuple[str, str], ...] = ()
    docs: str = ""

    def field(self, name: str) -> Optional[Field]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    @property
    def allowlist(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys([k for k, _ in self.selector] + [f.env for f in self.fields]))


BEDROCK = Method(
    id="bedrock",
    label="Amazon Bedrock",
    selector=(("CLAUDE_CODE_USE_BEDROCK", "1"),),
    fields=(
        Field("region", "AWS region", "AWS_REGION", secret=False, kind="region"),
        Field("access_key_id", "Access key ID", "AWS_ACCESS_KEY_ID", secret=True, choice="keys"),
        Field("secret_access_key", "Secret access key", "AWS_SECRET_ACCESS_KEY", secret=True, choice="keys"),
        Field("session_token", "Session token", "AWS_SESSION_TOKEN", secret=True,
              required=False, choice="keys"),
        Field("bearer_token", "Bedrock API key", "AWS_BEARER_TOKEN_BEDROCK", secret=True, choice="bearer"),
    ),
    choices=(("keys", "AWS access keys"), ("bearer", "Bedrock API key")),
    docs="https://code.claude.com/docs/en/amazon-bedrock",
)
VERTEX = Method(
    id="vertex",
    label="Google Vertex AI",
    selector=(("CLAUDE_CODE_USE_VERTEX", "1"),),
    fields=(
        Field("region", "Region", "CLOUD_ML_REGION", secret=False, kind="region"),
        Field("project_id", "Project ID", "ANTHROPIC_VERTEX_PROJECT_ID", secret=False),
        # The CLI reads a file path; the owner pastes the service account JSON,
        # which is stored encrypted and written to a 0600 file for the CLI.
        Field("credentials_json", "Service account key (JSON)", "GOOGLE_APPLICATION_CREDENTIALS",
              secret=True, kind="json"),
    ),
    docs="https://code.claude.com/docs/en/google-vertex-ai",
)
FOUNDRY = Method(
    id="foundry",
    label="Microsoft Foundry",
    selector=(("CLAUDE_CODE_USE_FOUNDRY", "1"),),
    fields=(
        Field("resource", "Resource name", "ANTHROPIC_FOUNDRY_RESOURCE", secret=False,
              kind="resource", choice="resource"),
        Field("base_url", "Base URL", "ANTHROPIC_FOUNDRY_BASE_URL", secret=False,
              kind="url", choice="base_url"),
        Field("api_key", "API key", "ANTHROPIC_FOUNDRY_API_KEY", secret=True),
    ),
    choices=(("resource", "Resource name"), ("base_url", "Base URL")),
    docs="https://code.claude.com/docs/en/microsoft-foundry",
)
GATEWAY = Method(
    id="gateway",
    label="LLM gateway",
    selector=(),
    fields=(
        Field("base_url", "Base URL", "ANTHROPIC_BASE_URL", secret=False, kind="url"),
        Field("auth_token", "Auth token", "ANTHROPIC_AUTH_TOKEN", secret=True),
    ),
    docs="https://code.claude.com/docs/en/llm-gateway-connect",
)

METHODS: Dict[str, Method] = {m.id: m for m in (BEDROCK, VERTEX, FOUNDRY, GATEWAY)}

# Every variable any method may set, the other Claude credentials, and the
# variables that would let a host or profile value pick a cloud account or an
# endpoint on its own. In cloud mode all of them, and every name with one of
# CLOUD_MODE_UNSET_PREFIXES, are removed from what the CLI inherits; then only
# the chosen method's stored values are put back.
CLOUD_MODE_UNSET: Tuple[str, ...] = tuple(dict.fromkeys(
    [name for m in METHODS.values() for name in m.allowlist]
    + [
        # Other Claude credentials: never a fallback in this mode.
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
        # Other provider switches and endpoint overrides.
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH", "CLAUDE_CODE_SKIP_MANTLE_AUTH",
        "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
        # AWS credential chain inputs (AWS_PROFILE also disables env keys).
        "AWS_PROFILE", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME", "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        # Google project and credential inputs.
        "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "CLOUDSDK_CONFIG",
        # Azure default credential chain inputs.
        "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
        "AZURE_USERNAME", "AZURE_PASSWORD", "AZURE_FEDERATED_TOKEN_FILE",
        # Turning TLS certificate checks off is never inherited in this mode.
        # (Proxy and CA variables are the host owner's network setting and stay.)
        "NODE_TLS_REJECT_UNAUTHORIZED",
    ]
))
# Removed by prefix: every AWS, Google, Azure and Anthropic variable (endpoint
# overrides, custom headers, model overrides, credential chains), the provider
# switches, and the per-model Vertex region overrides (VERTEX_REGION_CLAUDE_...).
CLOUD_MODE_UNSET_PREFIXES: Tuple[str, ...] = (
    "AWS_", "GOOGLE_", "AZURE_", "ANTHROPIC_", "CLOUD_ML_", "CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_",
    "VERTEX_REGION_",
)


def is_cloud_unset_name(name: str) -> bool:
    """True for a variable cloud mode removes from what the CLI inherits."""
    upper = name.upper()
    return upper in CLOUD_MODE_UNSET or upper.startswith(CLOUD_MODE_UNSET_PREFIXES)

# Variables whose change means a pooled CLI talks to another account.
CLOUD_IDENTITY_ENV_KEYS: Tuple[str, ...] = tuple(dict.fromkeys(
    name for m in METHODS.values() for name in m.allowlist
))


class CloudConfigError(ValueError):
    """The owner's input cannot be saved; the message says why (no values)."""


class CloudStorageLocked(RuntimeError):
    """Credential storage is locked, so secrets cannot be read or written."""


# ─── validation ───────────────────────────────────────────────────────────

_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_RESOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
_TEXT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_SECRET = 8192
_MAX_JSON = 65536


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(value: str) -> str:
    """An HTTPS base URL (http only for loopback) without credentials, query or fragment."""
    url = (value or "").strip()
    if not url:
        raise CloudConfigError("Enter a base URL.")
    if _CONTROL_RE.search(url) or " " in url:
        raise CloudConfigError("The base URL must not contain spaces or control characters.")
    if "?" in url:
        raise CloudConfigError("The base URL must not have a query (?...).")
    if "#" in url:
        raise CloudConfigError("The base URL must not have a fragment (#...).")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 — raises on a bad port
    except ValueError:
        raise CloudConfigError("The base URL is not a valid URL.") from None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise CloudConfigError("The base URL must not contain a user name or password.")
    if not host:
        raise CloudConfigError("The base URL needs a host name.")
    scheme = parts.scheme.lower()
    if scheme == "https":
        return url
    if scheme == "http" and _is_loopback(host):
        return url
    if scheme == "http":
        raise CloudConfigError("The base URL must use https (http is allowed only for localhost).")
    raise CloudConfigError("The base URL must start with https://.")


def _validate_value(method: Method, f: Field, value: str) -> str:
    if f.kind == "json":
        if len(value) > _MAX_JSON:
            raise CloudConfigError(f"{f.label} is too long.")
        try:
            parsed = json.loads(value)
        except ValueError:
            raise CloudConfigError(f"{f.label} is not valid JSON.") from None
        # Only a service account key. Other Google credential types can make
        # the CLI read local files or call any URL (external_account), or act
        # as a person (authorized_user). Never echo what was pasted.
        if not isinstance(parsed, dict) or parsed.get("type") != "service_account":
            raise CloudConfigError(f"{f.label} must be a service account key: a JSON file whose "
                                   "\"type\" is \"service_account\". Other Google credential types "
                                   "are not accepted.")
        return value
    value = value.strip()
    if f.kind == "url":
        return validate_base_url(value)
    if _CONTROL_RE.search(value):
        raise CloudConfigError(f"{f.label} must be one line.")
    if f.secret:
        if len(value) > _MAX_SECRET:
            raise CloudConfigError(f"{f.label} is too long.")
        return value
    if f.kind == "region" and not _REGION_RE.match(value):
        raise CloudConfigError(f"{f.label} must look like us-east-1 or global.")
    if f.kind == "resource" and not _RESOURCE_RE.match(value):
        raise CloudConfigError(f"{f.label} must be the resource name only, not a URL.")
    if f.kind == "text" and not _TEXT_RE.match(value):
        raise CloudConfigError(f"{f.label} may use only letters, digits, dots, dashes, colons and underscores.")
    return value


# ─── storage ──────────────────────────────────────────────────────────────


def _read_config() -> Optional[dict]:
    raw = database.get_system_setting(CONFIG_SETTING)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("The saved cloud provider settings are unreadable")
        return None
    if not isinstance(data, dict) or data.get("method") not in METHODS:
        return None
    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    return {"method": data["method"], "values": {str(k): str(v) for k, v in values.items()},
            "save_id": str(data.get("save_id") or "")}


def _read_secrets(save_id: str) -> Dict[str, str]:
    """The decrypted secrets saved together with the config ``save_id``.

    {} when none are stored, storage is locked, or the secrets belong to
    another save (a save interrupted between its two writes): the method then
    reports its secrets missing instead of running on another save's values.
    """
    from app.core import encryption

    raw = database.get_system_setting(SECRETS_SETTING)
    if not raw:
        return {}
    if not encryption.is_encrypted(raw):
        # Never written in plaintext; treat a plaintext row as absent.
        logger.warning("Ignoring unencrypted cloud provider secrets")
        return {}
    if not encryption.is_encryption_ready():
        logger.warning("The cloud provider secrets cannot be decrypted: credential storage is locked")
        return {}
    try:
        data = json.loads(encryption.decrypt_value(raw) or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.error("The cloud provider secrets could not be decrypted: %s", type(exc).__name__)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict):
        return {}
    if not save_id or data.get("save_id") != save_id:
        logger.warning("The saved cloud provider settings and secrets are out of step; save them again")
        return {}
    return {str(k): str(v) for k, v in data["values"].items() if v}


def stored() -> Optional[dict]:
    """``{"method", "values", "secrets"}`` as saved, or None. Secrets in clear: internal only."""
    config = _read_config()
    if config is None:
        return None
    method = METHODS[config["method"]]
    secrets = _read_secrets(config["save_id"])
    values = {f.name: config["values"][f.name] for f in method.fields
              if not f.secret and config["values"].get(f.name)}
    secrets = {f.name: secrets[f.name] for f in method.fields if f.secret and secrets.get(f.name)}
    return {"method": method.id, "values": values, "secrets": secrets}


def _filled(cfg: dict, f: Field) -> bool:
    return bool((cfg["secrets"] if f.secret else cfg["values"]).get(f.name))


def missing(cfg: Optional[dict]) -> List[str]:
    """Labels of what the chosen method still needs (empty when ready)."""
    if cfg is None:
        return ["a cloud provider or gateway"]
    method = METHODS[cfg["method"]]
    out: List[str] = []
    for f in method.fields:
        if f.choice is None and f.required and not _filled(cfg, f):
            out.append(f.label)
    if method.choices:
        satisfied = [c for c, _ in method.choices
                     if all(_filled(cfg, f) for f in method.fields if f.choice == c and f.required)
                     and any(_filled(cfg, f) for f in method.fields if f.choice == c)]
        if not satisfied:
            out.append(" or ".join(label for _, label in method.choices))
    return out


def status() -> dict:
    """Presence check for Settings and the credential gate. No values, no network."""
    cfg = stored()
    method = METHODS.get(cfg["method"]) if cfg else None
    need = missing(cfg)
    return {
        "method": method.id if method else None,
        "method_label": method.label if method else None,
        "configured": cfg is not None,
        "ready": cfg is not None and not need,
        "missing": need,
        "storage_locked": cfg is not None and bool(need) and _secrets_locked(),
    }


def _secrets_locked() -> bool:
    from app.core import encryption

    try:
        return bool(database.get_system_setting(SECRETS_SETTING)) and not encryption.is_encryption_ready()
    except Exception:  # noqa: BLE001
        return False


def public_config() -> dict:
    """What the owner's Settings page reads back: non-secret values, ``set`` flags for secrets."""
    cfg = stored()
    return {
        "method": cfg["method"] if cfg else None,
        "values": dict(cfg["values"]) if cfg else {},
        "secrets": ({f.name: {"set": bool(cfg["secrets"].get(f.name))}
                     for f in METHODS[cfg["method"]].fields if f.secret} if cfg else {}),
        "status": status(),
        "methods": [
            {
                "id": m.id,
                "label": m.label,
                "docs": m.docs,
                "allowlist": list(m.allowlist),
                "choices": [{"id": c, "label": label} for c, label in m.choices],
                "fields": [{"name": f.name, "label": f.label, "env": f.env, "secret": f.secret,
                            "kind": f.kind, "required": f.required, "choice": f.choice}
                           for f in m.fields],
            }
            for m in METHODS.values()
        ],
        "warning": WARNING_TEXT,
        "gateway_warning": GATEWAY_WARNING_TEXT,
    }


def save(method_id: str, values: Mapping[str, object]) -> dict:
    """Validate and store the owner's input; returns :func:`public_config`.

    Non-secret fields are replaced by what was sent. A secret left empty keeps
    its stored value when the method is unchanged, so the owner can edit a
    region without re-entering the key. Filling one alternative (Bedrock
    access keys or API key, Foundry resource or base URL) drops the other.
    Choosing another method drops every value of the previous one.
    """
    from app.core import encryption

    method = METHODS.get(method_id)
    if method is None:
        raise CloudConfigError(f"Unknown method. Use one of: {', '.join(METHODS)}.")
    unknown = [k for k in values if method.field(k) is None]
    if unknown:
        raise CloudConfigError(f"{method.label} does not use: {', '.join(sorted(unknown))}.")
    if not encryption.is_encryption_ready():
        raise CloudStorageLocked(encryption.credential_storage_error())

    previous = stored()
    keep_secrets = dict(previous["secrets"]) if previous and previous["method"] == method.id else {}
    new_values: Dict[str, str] = {}
    new_secrets: Dict[str, str] = {}
    sent_choices = set()
    for f in method.fields:
        raw = values.get(f.name)
        if raw is not None and not isinstance(raw, str):
            raise CloudConfigError(f"{f.label} must be text.")
        text = raw or ""
        if not text.strip():
            continue
        clean = _validate_value(method, f, text)
        (new_secrets if f.secret else new_values)[f.name] = clean
        if f.choice:
            sent_choices.add(f.choice)
    if len(sent_choices) > 1:
        raise CloudConfigError(
            f"Enter {' or '.join(label for _, label in method.choices)}, not both.")
    for f in method.fields:
        if not f.secret or f.name in new_secrets or f.name not in keep_secrets:
            continue
        if sent_choices and f.choice and f.choice not in sent_choices:
            continue  # the other alternative was chosen: drop this one
        new_secrets[f.name] = keep_secrets[f.name]

    cfg = {"method": method.id, "values": new_values, "secrets": new_secrets}
    need = missing(cfg)
    if need:
        raise CloudConfigError(f"{method.label} needs: {', '.join(need)}.")

    # Two rows, paired by a save id: the secrets count only for the config
    # they were saved with. Secrets go first; if the config write then fails,
    # the previous config no longer matches them, so nothing runs on a mix of
    # two saves (the gate reports the secrets missing until the owner saves).
    save_id = uuid.uuid4().hex
    database.set_system_setting(SECRETS_SETTING, encryption.encrypt_value(json.dumps(
        {"save_id": save_id, "values": new_secrets}, sort_keys=True)))
    database.set_system_setting(CONFIG_SETTING, json.dumps(
        {"method": method.id, "values": new_values, "save_id": save_id}, sort_keys=True))
    if method.id != "vertex":
        remove_materialized_files()
    logger.info("Claude cloud provider settings saved: method=%s", method.id)
    return public_config()


def clear() -> bool:
    """Delete the saved method, its secrets and any credential file written for the CLI."""
    removed = database.delete_system_setting(CONFIG_SETTING)
    removed = database.delete_system_setting(SECRETS_SETTING) or removed
    remove_materialized_files()
    logger.info("Claude cloud provider settings cleared")
    return removed


# ─── the CLI environment ──────────────────────────────────────────────────


def _files_dir() -> Path:
    from app.core.config import settings

    return Path(settings.data_dir) / "claude-cloud"


def remove_materialized_files() -> None:
    """Delete credential files written for the CLI (Vertex). Never raises."""
    try:
        entries = list(_files_dir().glob("vertex-*.json"))
    except Exception:  # noqa: BLE001 — no data dir, nothing written
        return
    for path in entries:
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not delete a Claude cloud credential file")


def _materialize_vertex(credentials_json: str) -> str:
    """Write the service account JSON to an owner-only file; returns its path.

    The name carries a hash of the content, so a changed key is a changed
    ``GOOGLE_APPLICATION_CREDENTIALS`` and a pooled CLI is respawned.

    The content goes to a fresh temp file (unique name, created exclusively,
    mode 0600) in the same directory, then is renamed over the final name:
    a link planted at either name is replaced, never followed, and two runs
    starting at once each write their own temp file and both succeed.
    Raises ``OSError`` when it cannot write; the caller fails the run.
    """
    directory = _files_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise OSError("the Claude cloud credential directory is a link")
    os.chmod(directory, 0o700)
    digest = hashlib.sha256(credentials_json.encode()).hexdigest()[:16]
    path = directory / f"vertex-{digest}.json"
    for old in directory.glob("vertex-*.json"):
        if old != path:
            try:
                old.unlink()
            except FileNotFoundError:
                pass
    if path.is_symlink() or not path.is_file():
        # mkstemp: O_CREAT | O_EXCL (| O_NOFOLLOW where the OS has it), mode 0600.
        fd, tmp = tempfile.mkstemp(prefix=".vertex-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(credentials_json)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return str(path)


def env_for_cli() -> Dict[str, str]:
    """Exactly the chosen method's variables from stored values ({} when not ready).

    Also the point every run passes: a Vertex key file left from an earlier
    config (cleared, changed to another method, or not complete) is removed.
    """
    cfg = stored()
    if cfg is None or cfg["method"] != VERTEX.id or missing(cfg):
        remove_materialized_files()
    if cfg is None or missing(cfg):
        return {}
    method = METHODS[cfg["method"]]
    env: Dict[str, str] = dict(method.selector)
    for f in method.fields:
        value = (cfg["secrets"] if f.secret else cfg["values"]).get(f.name)
        if not value:
            continue
        if f.kind == "json":
            value = _materialize_vertex(value)
        env[f.env] = value
    return env


def host_names_to_unset(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """:data:`CLOUD_MODE_UNSET` plus inherited names :func:`is_cloud_unset_name` matches."""
    source: Iterable[str] = (os.environ if environ is None else environ).keys()
    extra = [n for n in source if is_cloud_unset_name(n)]
    return list(dict.fromkeys(list(CLOUD_MODE_UNSET) + extra))
