"""What is shared, what stays on this worker, and how each record is shaped.

This module is the authority the design document defers to for the
shared-versus-local decision. Every synced collection is described once:
its table, key columns, the columns that travel, the columns whose values
are secrets encrypted at rest with the local data key, and the sharing mode.

Modes
    shared          last writer wins on (Lamport clock, device id)
    owner_ordered   only the owner device appends; mirrors are read-only

Collections that are not listed here never leave the worker.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

MODE_SHARED = "shared"
MODE_OWNER_ORDERED = "owner_ordered"

# Columns never compared when deciding whether a row changed locally. They
# still travel so a mirror can show them.
VOLATILE_COLUMNS = frozenset({"updated_at", "created_at", "last_used_at", "last_triggered_at",
                              "last_synced_at", "revision"})

OWNER_PLACEHOLDER = "@owner"


@dataclass(frozen=True)
class Collection:
    name: str
    table: str
    keys: Tuple[str, ...]
    fields: Tuple[str, ...]
    mode: str = MODE_SHARED
    schema_version: int = 1
    encrypted: Tuple[str, ...] = ()
    json_fields: Tuple[str, ...] = ()
    resource: Optional[str] = None
    priority: int = 100
    # Optional per-row filter deciding whether a row is shared at all.
    shared_row: Optional[Callable[[Mapping[str, Any]], bool]] = None
    # Columns that name another device's local path or are otherwise
    # device-specific; they are dropped on serialize and never overwritten.
    local_fields: Tuple[str, ...] = ()
    # Values for NOT NULL local columns a mirror has to fill on insert.
    mirror_defaults: Mapping[str, Any] = field(default_factory=dict)
    # Optional hooks so a collection can adapt identity-bearing values.
    serialize_hook: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    materialize_hook: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    notes: str = ""

    def record_id(self, row: Mapping[str, Any]) -> str:
        return "|".join(str(row[k]) for k in self.keys)

    def key_values(self, record_id: str) -> Tuple[str, ...]:
        parts = record_id.split("|", len(self.keys) - 1)
        if len(parts) != len(self.keys):
            raise ValueError("Record id does not match collection keys")
        return tuple(parts)

    @property
    def hash_fields(self) -> Tuple[str, ...]:
        return tuple(f for f in self.fields if f not in VOLATILE_COLUMNS)


# ---------------------------------------------------------------------------
# system_settings key policy
# ---------------------------------------------------------------------------

_LOCAL_SETTING_PATTERNS = [
    re.compile(p) for p in (
        r"^workspace_path(:|$)",
        r"^cluster_",
        r"^mobile_",
        r"^credential_key_state",
        r"^chat_connection:",
        r"^claude_active_account(:|$)",
        r"^encryption_salt$",
        r"^owner_preference_identity$",
        r"^schema_migrations_fingerprint$",
        r"^migration:",
        r"^installation_configured$",
        r"^network_",
        r"^sync_",
        r"^instance_",
        r"^device_",
        r"^port$|^host$|^advertise_url",
        r"^tunnel_",
        r"^openai_billing_",
        r"^fal_billing_api_key$",
        r"^oauth_state",
    )
]


def is_shared_setting_key(key: str) -> bool:
    """Shared unless it names something bound to this worker or its host."""
    if not isinstance(key, str) or not key:
        return False
    if ":" in key and key.rsplit(":", 1)[1] and _looks_like_instance_suffix(key):
        return False
    return not any(p.search(key) for p in _LOCAL_SETTING_PATTERNS)


def _looks_like_instance_suffix(key: str) -> bool:
    prefix = key.split(":", 1)[0]
    return prefix in {"workspace_path", "claude_active_account", "cluster_advertise_url",
                      "mobile_account_hub_secret", "mobile_managed_pending", "mobile_managed_credential"}


# ---------------------------------------------------------------------------
# Identity-bearing value hooks
# ---------------------------------------------------------------------------

def _owner_identity() -> str:
    from app.core import owner_auth
    return owner_auth.preference_identity()


def _serialize_preference(record: Dict[str, Any]) -> Dict[str, Any]:
    if record.get("user_type") == "admin" and record.get("user_id") == _owner_identity():
        record["user_id"] = OWNER_PLACEHOLDER
    return record


def _materialize_preference(record: Dict[str, Any]) -> Dict[str, Any]:
    if record.get("user_type") == "admin" and record.get("user_id") == OWNER_PLACEHOLDER:
        record["user_id"] = _owner_identity()
    return record


def _shared_setting_row(row: Mapping[str, Any]) -> bool:
    return is_shared_setting_key(str(row.get("key", "")))


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

COLLECTIONS: Dict[str, Collection] = {}


def _register(collection: Collection) -> Collection:
    COLLECTIONS[collection.name] = collection
    return collection


_register(Collection(
    name="membership", table="network_membership_records", keys=("id",),
    fields=("id", "record", "created_at"), json_fields=("record",), priority=0,
    notes="Certificates, revocations and key rotations. Applied through membership.apply_record."))

_register(Collection(
    name="presence", table="network_presence", keys=("device_id",),
    fields=("device_id", "endpoints", "capabilities", "version", "platform", "incarnation", "updated_at"),
    json_fields=("endpoints", "capabilities"), priority=1,
    notes="Each device publishes its own reachability and capabilities; last writer wins per device."))

_register(Collection(
    name="member_names", table="network_member_names", keys=("device_id",),
    fields=("device_id", "name", "updated_at"), resource="network", priority=2,
    notes="Display names editable from any worker; last writer wins without changing membership certificates."))

_register(Collection(
    name="user_preferences", table="user_preferences", keys=("user_type", "user_id", "key"),
    fields=("user_type", "user_id", "key", "value", "updated_at"), json_fields=("value",),
    resource="preferences", priority=10,
    serialize_hook=_serialize_preference, materialize_hook=_materialize_preference,
    notes="UI state and preferences. The admin identity is normalized to the network owner."))

_register(Collection(
    name="system_settings", table="system_settings", keys=("key",),
    fields=("key", "value", "updated_at"), encrypted=("value",), resource="settings", priority=10,
    shared_row=_shared_setting_row,
    notes="Only keys is_shared_setting_key() accepts. Values that are enc: ciphertext travel decrypted."))

_register(Collection(
    name="agents", table="agents", keys=("id",),
    fields=("id", "name", "description", "is_builtin", "config", "mcp_tools", "created_at", "updated_at"),
    json_fields=("config", "mcp_tools"), resource="agents", priority=20,
    notes="Profiles and their MCP definitions."))

_register(Collection(
    name="model_providers", table="model_providers", keys=("id",),
    fields=("id", "name", "provider_type", "base_url", "api_dialect", "auth_token", "models_json",
            "default_model", "small_fast_model", "extra_env_json", "context_window", "max_output_tokens",
            "enabled", "auth_mode", "codex_access_token", "codex_refresh_token", "codex_account_id",
            "codex_id_token_expires_at", "codex_last_refresh", "needs_reauth", "created_at", "updated_at"),
    encrypted=("auth_token", "codex_access_token", "codex_refresh_token"),
    local_fields=("codex_refresh_lease_owner", "codex_refresh_lease_expires_at",
                  "openai_admin_key", "openai_billing_org_id", "openai_billing_instance_id"),
    resource="settings", priority=30,
    notes="API keys travel encrypted under the network key. Refresh leases and billing bindings stay local."))

_register(Collection(
    name="claude_accounts", table="claude_accounts", keys=("id",),
    fields=("id", "name", "email", "subscription_type", "credentials", "credential_fingerprint",
            "expires_at", "has_refresh_token", "sync_pending", "created_at", "updated_at"),
    encrypted=("credentials",), resource="settings", priority=30,
    notes="OAuth bundles. Whether a restored bundle is usable is decided at use time."))

_register(Collection(
    name="api_users", table="api_users", keys=("id",),
    fields=("id", "name", "username", "password_hash", "api_key_hash", "project_id", "profile_id",
            "is_active", "web_login_allowed", "description", "model", "effort", "created_at", "updated_at"),
    resource="api_users", priority=40,
    local_fields=("last_used_at",)))

_register(Collection(
    name="api_user_credentials", table="api_user_credentials", keys=("id",),
    fields=("id", "api_user_id", "credential_type", "encrypted_value", "created_at", "updated_at"),
    encrypted=("encrypted_value",), resource="api_users", priority=41))

_register(Collection(
    name="api_user_profiles", table="api_user_profiles", keys=("api_user_id", "profile_id"),
    fields=("api_user_id", "profile_id", "created_at"), resource="api_users", priority=42))

_register(Collection(
    name="credential_policies", table="credential_policies", keys=("id",),
    fields=("id", "policy", "description", "updated_at"), resource="api_users", priority=43))

_register(Collection(
    name="user_credential_policies", table="user_credential_policies", keys=("id",),
    fields=("id", "api_user_id", "credential_type", "policy", "created_at", "updated_at"),
    resource="api_users", priority=44))

_register(Collection(
    name="templates", table="templates", keys=("id",),
    fields=("id", "name", "description", "prompt", "profile_id", "icon", "category", "is_builtin",
            "created_at", "updated_at"),
    resource="templates", priority=50))

_register(Collection(
    name="tags", table="tags", keys=("id",),
    fields=("id", "name", "color", "created_at", "updated_at"), resource="tags", priority=50))

_register(Collection(
    name="permission_rules", table="permission_rules", keys=("id",),
    fields=("id", "profile_id", "tool_name", "tool_pattern", "decision", "created_at"),
    resource="settings", priority=51))

_register(Collection(
    name="project_hooks", table="project_hooks", keys=("id",),
    fields=("id", "project_id", "name", "event", "hook_type", "command", "timeout", "url", "secret",
            "enabled", "tool_matcher", "message_template", "created_at", "updated_at"),
    local_fields=("last_triggered_at", "failure_count"), resource="hooks", priority=52))

_register(Collection(
    name="webhooks", table="webhooks", keys=("id",),
    fields=("id", "url", "secret", "events", "is_active", "created_at"), json_fields=("events",),
    local_fields=("last_triggered_at", "failure_count"), resource="hooks", priority=52))

_register(Collection(
    name="projects_v2", table="projects_v2", keys=("id",),
    fields=("id", "origin", "name", "settings", "created_at", "updated_at"), json_fields=("settings",),
    resource="projects", priority=60,
    notes="Network project identity. Local paths live in checkouts, which never sync."))

_register(Collection(
    name="notes", table="notes", keys=("id",),
    fields=("id", "title", "content", "tags", "is_pinned", "created_at", "updated_at"),
    json_fields=("tags",), local_fields=("file_path",), resource="notes", priority=70))

_register(Collection(
    name="note_attachments", table="note_attachments", keys=("id",),
    fields=("id", "note_id", "filename", "content_type", "size_bytes", "blob_hash", "created_at"),
    local_fields=("file_path", "data"), mirror_defaults={"file_path": ""}, resource="notes", priority=71,
    notes="Bytes are content-addressed blobs fetched on demand."))

_register(Collection(
    name="automations", table="automations", keys=("id",),
    fields=("id", "name", "description", "trigger_type", "trigger_config", "prompt", "profile_id",
            "project_id", "output_config", "max_turns", "timeout_seconds", "max_cost_per_run",
            "max_cost_per_day", "max_cost_total", "max_runs_per_day", "is_enabled", "requires_approval",
            "action_steps", "triggers", "owner_device_id", "created_at", "updated_at"),
    json_fields=("trigger_config", "output_config"), encrypted=("triggers", "trigger_config"),
    local_fields=("status", "last_run_at", "last_error", "run_count", "failure_count", "total_cost_used"),
    resource="automations", priority=80,
    notes="Definition only. Runs, fires and approvals stay on the owner device."))

_register(Collection(
    name="connectors", table="connectors", keys=("id",),
    fields=("id", "type", "name", "api_user_id", "config", "events", "is_active", "owner_device_id",
            "created_at", "updated_at"),
    json_fields=("config", "events"), encrypted=("config",),
    local_fields=("status", "status_message", "last_connected_at", "last_error", "last_event_at",
                  "failure_count", "runner_id"),
    resource="connectors", priority=80,
    notes="Definition only. Connector sessions stay on the owner device."))

_register(Collection(
    name="plugins_desired", table="network_plugins_desired", keys=("id",),
    fields=("id", "version", "enabled", "config", "updated_at"), json_fields=("config",),
    resource="plugins", priority=85,
    notes="Desired plugin state; each worker installs it."))

_register(Collection(
    name="memory", table="network_memory_records", keys=("id",),
    fields=("id", "kind", "scope", "content", "content_hash", "deleted", "updated_at"),
    resource="memory", priority=86,
    notes="Facts, journal entries and skills as records keyed by fact id."))

_register(Collection(
    name="sessions", table="sessions", keys=("id",),
    fields=("id", "project_id", "profile_id", "api_user_id", "title", "status", "is_active",
            "total_cost_usd", "total_tokens_in", "total_tokens_out", "total_cache_read_tokens",
            "total_cache_creation_tokens", "turn_count", "total_model_calls", "created_at", "updated_at",
            "parent_session_id", "fork_point_message_index", "owner_device_id", "launched_model",
            "first_model", "project_v2_id", "branch", "placement", "provider_id", "model", "effort",
            "permission_mode", "context_window", "workspace_project_ids", "fast", "mode", "hidden",
            "is_worker", "pr_number", "pr_url", "pr_state"),
    json_fields=("placement", "workspace_project_ids"),
    mode=MODE_OWNER_ORDERED, resource="sessions", priority=90,
    local_fields=("instance_id", "sdk_session_id", "worktree_id", "worktree_branch", "worktree_path",
                  "cloud_branch", "working_set_ref", "context_usage_json", "v2_stream"),
    notes="Session catalog. Owner-ordered: only the owner device publishes."))

_register(Collection(
    name="session_runtime", table="network_session_runtime", keys=("session_id",),
    fields=("session_id", "owner_device_id", "state", "updated_at"),
    mode=MODE_OWNER_ORDERED, resource="sessions", priority=92,
    notes="Read-only owner runtime summary; never materialized into execution tables."))

_register(Collection(
    name="session_tags", table="session_tags", keys=("session_id", "tag_id"),
    fields=("session_id", "tag_id", "created_at"), mode=MODE_OWNER_ORDERED, resource="sessions", priority=91))

_register(Collection(
    name="session_events", table="session_events", keys=("session_id", "seq"),
    fields=("id", "session_id", "seq", "run_id", "turn_id", "event_type", "payload", "command_id", "created_at"),
    json_fields=("payload",), mode=MODE_OWNER_ORDERED, resource="sessions", priority=95,
    notes="Transcript mirror. Hydrated on demand; mirrors are read-only."))

_register(Collection(
    name="pending_interactions", table="pending_interactions", keys=("id",),
    fields=("id", "session_id", "run_id", "kind", "payload", "state", "answer", "answered_by_device",
            "created_at", "expires_at", "answered_at"),
    json_fields=("payload", "answer"), mode=MODE_OWNER_ORDERED, resource="sessions", priority=96,
    notes="Read view of the owner's prompts. Answers travel to the owner through the runner API."))

_register(Collection(
    name="artifacts", table="artifacts", keys=("hash",),
    fields=("hash", "size", "mime", "filename", "created_by_session", "owner_device_id", "created_at"),
    local_fields=("runner_id",), resource="artifacts", priority=96,
    notes="Content-addressed; bytes fetched from any holder."))


def collection(name: str) -> Collection:
    try:
        return COLLECTIONS[name]
    except KeyError:
        raise KeyError("Unknown collection: " + name) from None


def ordered_collections() -> List[Collection]:
    return sorted(COLLECTIONS.values(), key=lambda c: (c.priority, c.name))


def collections_for_resource(resource: str) -> List[Collection]:
    return [c for c in COLLECTIONS.values() if c.resource == resource]


# Collections whose scanner is only meaningful once the owner-ordered
# machinery (milestone 4) runs: they are published by explicit calls.
SCANNED_COLLECTIONS: Tuple[str, ...] = tuple(
    c.name for c in ordered_collections()
    if c.mode == MODE_SHARED and c.name not in ("membership", "plugins_desired", "memory"))

# These collections share one publisher: a session wake must reconcile its
# catalog, runtime, children and transcript together.
CATALOG_COLLECTIONS: tuple[str, ...] = tuple(
    c.name for c in ordered_collections()
    if c.mode == MODE_OWNER_ORDERED and c.resource == "sessions")


def settings_policy_table() -> List[Dict[str, Any]]:
    """Human-readable dump of the settings policy for the docs and the API."""
    return [{"pattern": p.pattern, "sharing": "local"} for p in _LOCAL_SETTING_PATTERNS] + [
        {"pattern": "(everything else)", "sharing": "shared"}]


def inventory() -> List[Dict[str, Any]]:
    return [{
        "collection": c.name, "table": c.table, "mode": c.mode, "keys": list(c.keys),
        "fields": list(c.fields), "encrypted": list(c.encrypted), "local_fields": list(c.local_fields),
        "schema_version": c.schema_version, "notes": c.notes,
    } for c in ordered_collections()]
