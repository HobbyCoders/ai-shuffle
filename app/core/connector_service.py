"""
Connector service — business logic for CRUD, credential encryption, and API user linking.
"""

import hashlib
import logging
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core import encryption
from app.core.models import VALID_CONNECTOR_EVENTS
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sensitive-field mapping (shared with ConnectorManager)
# ---------------------------------------------------------------------------

def get_sensitive_fields(connector_type: str) -> List[str]:
    """Return the config keys that contain secrets for a given connector type."""
    mapping = {
        "telegram": ["bot_token"],
        "discord": ["bot_token"],
        "whatsapp": ["access_token", "app_secret"],
        "webhook": ["secret", "inbound_token"],
        "slack": ["bot_token", "app_token"],
        "line": ["channel_access_token", "channel_secret"],
        "signal": [],
        "imessage": [],
        "msteams": ["app_password"],
        "bluebubbles": ["password"],
        "spotify": ["client_secret", "refresh_token"],
        "hue": ["api_key"],
        "homeassistant": ["access_token"],
        "gmail": ["client_secret", "refresh_token"],
        "plaid": ["client_secret", "access_token"],
    }
    return mapping.get(connector_type, [])


# ---------------------------------------------------------------------------
# Config encryption / decryption / masking
# ---------------------------------------------------------------------------

def _get_list_fields(connector_type: str) -> List[str]:
    """Return config keys that should be stored as lists for a given connector type."""
    type_meta = get_connector_type_info()
    for t in type_meta:
        if t["type"] == connector_type:
            return [f["key"] for f in t.get("config_fields", []) if f.get("type") == "list"]
    return []


def normalize_config(config: Dict, connector_type: str) -> Dict:
    """Ensure list-type config fields are stored as actual lists.

    The frontend may submit a bare string (e.g. ``"12345"``) for fields
    declared as ``type: "list"``.  This normalizes them so downstream code
    can always iterate safely.
    """
    list_fields = _get_list_fields(connector_type)
    if not list_fields:
        return config

    normalized = dict(config)
    for key in list_fields:
        value = normalized.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, str):
            # Split on commas or whitespace to support "id1, id2" input
            normalized[key] = [v.strip() for v in value.replace(",", " ").split() if v.strip()]
    return normalized


def encrypt_config(config: Dict, connector_type: str) -> Dict:
    """Encrypt sensitive fields in a connector config dict.

    Refuses to write secrets when server key storage is unavailable.
    """
    sensitive = get_sensitive_fields(connector_type)
    if not sensitive:
        return config

    encrypted = dict(config)
    for key in sensitive:
        value = encrypted.get(key)
        if value and isinstance(value, str) and not encryption.is_encrypted(value):
            encrypted[key] = encryption.encrypt_value(value)
    return encrypted


def decrypt_config(config: Dict, connector_type: str) -> Dict:
    """Decrypt sensitive fields in a connector config dict."""
    sensitive = get_sensitive_fields(connector_type)
    if not sensitive:
        return config

    decrypted = dict(config)
    for key in sensitive:
        value = decrypted.get(key)
        if value and isinstance(value, str):
            if encryption.is_encrypted(value):
                if not encryption.is_encryption_ready():
                    logger.warning(
                        f"Cannot decrypt connector config field '{key}' — "
                        "encryption key not loaded"
                    )
                    continue
                try:
                    decrypted[key] = encryption.decrypt_value(value)
                except Exception as e:
                    logger.error(f"Failed to decrypt connector field '{key}': {e}")
    return decrypted


def mask_config(config: Dict, connector_type: str) -> Dict:
    """Return a copy of *config* with sensitive fields masked.

    Non-sensitive fields are passed through unchanged. Stored secrets are
    encrypted, and an encrypted value masks to a bare ``****`` (nothing of the
    secret is decrypted just to show it). A plaintext value, which only occurs
    before encryption, shows its last four characters after ``****``.
    """
    sensitive = get_sensitive_fields(connector_type)
    if not sensitive:
        return config

    masked = dict(config)
    for key in sensitive:
        value = masked.get(key)
        if value and isinstance(value, str):
            # If the value is encrypted, we can't show any real chars
            if encryption.is_encrypted(value):
                masked[key] = "****"
            elif len(value) > 4:
                masked[key] = "****" + value[-4:]
            else:
                masked[key] = "****"
    return masked


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

async def create_connector(
    name: str,
    connector_type: str,
    config: Dict[str, Any],
    events: List[str],
    api_user_id: Optional[str] = None,
    project_id: Optional[str] = None,
    profile_ids: Optional[List[str]] = None,
) -> Dict:
    """Create a new connector.

    If *api_user_id* is provided, the connector is linked to an existing API
    user.  Otherwise a dedicated API user is auto-created (legacy behaviour).

    Returns the connector dict with masked config.
    """
    # 1. Validate events
    invalid = [e for e in events if e not in VALID_CONNECTOR_EVENTS]
    if invalid:
        raise ValueError(f"Invalid event types: {', '.join(invalid)}")

    # 2. Generate connector ID
    connector_id = f"conn-{secrets.token_hex(4)}"

    def _create_blocking() -> Dict:
        """Steps 3–5 in ONE thread hop, and as ONE atomic block.

        The API-user resolve/insert, the connector INSERT and the compensating
        ``delete_api_user`` on failure are contiguous (no awaits between them),
        and the rollback in particular MUST stay in the same block as the write
        that it undoes. Same calls, same order; ``ValueError``/``RuntimeError``
        propagate out of the ``await`` exactly as before.
        """
        nonlocal api_user_id, config

        # 3. Resolve API user
        auto_created = False
        if api_user_id:
            # Use the provided (existing) API user
            existing_user = database.get_api_user(api_user_id)
            if not existing_user:
                raise ValueError(f"API user not found: {api_user_id}")
        else:
            # Auto-create a dedicated API user for this connector
            auto_created = True
            api_user_id = secrets.token_hex(4)
            api_key = f"aih_{secrets.token_urlsafe(32)}"
            api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

            database.create_api_user(
                user_id=api_user_id,
                name=f"connector:{connector_type}:{name}",
                api_key_hash=api_key_hash,
                project_id=project_id,
                profile_ids=profile_ids,
                web_login_allowed=False,
                description=f"Auto-created API user for connector {connector_id}",
            )

        # 4. Normalize list-type fields, then encrypt sensitive config fields
        config = normalize_config(config, connector_type)
        encrypted_config = encrypt_config(config, connector_type)

        # 5. Persist the connector, pinned to this install so that — when two
        # installs share one Postgres — only the install that created it starts
        # the live client (see ConnectorManager.start_all).
        from app.core.config import settings

        connector = database.create_connector(
            connector_id=connector_id,
            connector_type=connector_type,
            name=name,
            config=encrypted_config,
            events=events,
            api_user_id=api_user_id,
            runner_id=settings.effective_instance_id,
        )

        if not connector:
            if auto_created:
                database.delete_api_user(api_user_id)
            raise RuntimeError("Failed to create connector in database")
        return connector

    def _create_with_package():
        from app.core.connector_packages import lifecycle_lock, require_installed
        with lifecycle_lock():
            config["runtime_plugin_id"] = require_installed(connector_type)
            return _create_blocking()

    connector = await run_sync(_create_with_package)

    # 6. Return with masked config
    connector["config"] = mask_config(connector.get("config", {}), connector_type)
    return connector


async def update_connector(connector_id: str, **kwargs) -> Optional[Dict]:
    """Update an existing connector.

    Accepts keyword arguments matching ``ConnectorUpdate`` fields:
    ``name``, ``config``, ``events``, ``is_active``.

    ``config`` is a partial update: the provided keys are merged over the
    stored config, so a PATCH that sends only ``allowed_chat_ids`` keeps the
    stored ``bot_token``. If the merged config changes and the connector is
    active, it will be restarted (the restart reads the merged row).
    Returns the updated connector with masked config, or ``None`` if not found.
    """
    def _update_blocking() -> Tuple[Optional[Dict], bool, bool, Optional[str]]:
        """Read → validate → UPDATE → runner claim, in ONE thread hop.

        Every DB touch here is contiguous — the only thing that has to happen
        on the loop is the ``restart_connector`` await below, which needs the
        claim decision this block makes. Keeping the read, the write and the
        ``claim_connector_runner`` compare-and-set in one block also keeps the
        ownership decision atomic: splitting them would let two installs both
        conclude they own an unclaimed connector. Returns
        ``(updated, config_changed, owns_locally, connector_type)``; a missing
        row yields ``updated=None``, the same ``None`` the caller returned.
        """
        existing = database.get_connector(connector_id)
        if not existing:
            return None, False, False, None

        connector_type = existing["type"]
        if kwargs.get("is_active") is False:
            from app.core.connection_dependencies import require_unreferenced_connection
            require_unreferenced_connection(connector_id, enabled_only=True)

        # Encrypt sensitive config fields if config is being updated.
        # Preserve existing encrypted values when the frontend sends masked
        # placeholders (e.g. "****abcd") back — these must not overwrite the
        # real token stored in the database.
        config_changed = False
        if "config" in kwargs and kwargs["config"] is not None:
            existing_config = existing.get("config", {}) or {}
            sensitive = get_sensitive_fields(connector_type)
            # Partial update: provided keys win, every key the caller left out
            # (stored secrets, runtime ownership, managed OAuth credentials)
            # keeps its stored value.
            new_config = {
                **existing_config,
                **normalize_config(kwargs["config"], connector_type),
            }
            kwargs["config"] = new_config

            for key in sensitive:
                new_val = new_config.get(key, "")
                if isinstance(new_val, str) and new_val.startswith("****"):
                    # Masked value — keep the original encrypted value from DB
                    new_config[key] = existing_config.get(key, new_val)

            # Check if config actually changed vs what's in the DB
            if new_config != existing_config:
                kwargs["config"] = encrypt_config(new_config, connector_type)
                config_changed = True
            else:
                # No real change — drop config from the update
                del kwargs["config"]

        # Validate events if provided
        if "events" in kwargs and kwargs["events"] is not None:
            invalid = [e for e in kwargs["events"] if e not in VALID_CONNECTOR_EVENTS]
            if invalid:
                raise ValueError(f"Invalid event types: {', '.join(invalid)}")

        updated = database.update_connector(connector_id, **kwargs)
        if not updated:
            return None, config_changed, False, connector_type

        # Restart if the connector is active and config was changed — but only
        # when this install owns (or claims) the connector, otherwise a config
        # edit on a non-owning install would connect a duplicate live client
        # here.
        from app.core.config import settings
        me = settings.effective_instance_id
        runner_id = updated.get("runner_id")
        owns_locally = runner_id == me or (
            runner_id is None and database.claim_connector_runner(connector_id, me)
        )
        return updated, config_changed, owns_locally, connector_type

    updated, config_changed, owns_locally, connector_type = await run_sync(
        _update_blocking
    )
    if not updated:
        return None

    if config_changed and updated.get("is_active") and owns_locally:
        try:
            # Lazy import to avoid circular dependency
            from app.core.connectors.manager import connector_manager
            await connector_manager.restart_connector(connector_id)
        except Exception as e:
            logger.error(f"Failed to restart connector {connector_id} after update: {e}")
            updated["_restart_error"] = str(e)

    # Mask config before returning
    updated["config"] = mask_config(updated.get("config", {}), connector_type)
    return updated


async def delete_connector(connector_id: str) -> bool:
    """Delete a connector, its API user, and stop it if running."""
    connector = await run_sync(database.get_connector, connector_id)
    if not connector:
        return False

    from app.core.connection_dependencies import require_unreferenced_connection
    await run_sync(require_unreferenced_connection, connector_id)

    # Stop the connector if it is running
    try:
        from app.core.connectors.manager import connector_manager
        await connector_manager.stop_connector(connector_id)
    except Exception as e:
        logger.error(f"Error stopping connector {connector_id} during delete: {e}")

    def _delete_blocking() -> bool:
        """The deletion tail in ONE hop: the API-user check/delete and the
        connector DELETE are contiguous (the only await, the live-client stop,
        already happened above). Error handling is unchanged — the API-user
        failure is logged and swallowed exactly where it was, and the connector
        DELETE's result is what this returns."""
        # Only delete the API user if it was auto-created for this connector
        api_user_id = connector.get("api_user_id")
        if api_user_id:
            try:
                api_user = database.get_api_user(api_user_id)
                if api_user and api_user.get("name", "").startswith("connector:"):
                    database.delete_api_user(api_user_id)
            except Exception as e:
                logger.error(f"Failed to delete API user {api_user_id} for connector {connector_id}: {e}")

        # Delete the connector itself (cascades to event_log / sessions via FK)
        return database.delete_connector(connector_id)

    return await run_sync(_delete_blocking)


async def test_connector(connector_id: str) -> Dict:
    """Instantiate a temporary connector and test its credentials.

    Returns ``{"success": bool, "message": str, "elapsed_ms": int}``.
    """
    from app.core.connectors.registry import get_connector_class

    # One isolated read; the credential test below is network work that stays
    # on the loop.
    connector = await run_sync(database.get_connector, connector_id)
    if not connector:
        return {"success": False, "message": "Connector not found", "elapsed_ms": 0}

    connector_type = connector["type"]
    config = connector.get("config", {}) or {}

    # Decrypt config for testing
    config = decrypt_config(config, connector_type)

    try:
        cls = get_connector_class(connector_type)
    except ValueError as e:
        return {"success": False, "message": str(e), "elapsed_ms": 0}

    # Create a disposable instance (callbacks are no-ops for testing)
    async def _noop_msg(_msg):
        pass

    async def _noop_perm(_rid, _sid, _approved):
        pass

    instance = cls(
        connector_id=connector_id,
        connector_type=connector_type,
        config=config,
        on_incoming_message=_noop_msg,
        on_permission_response=_noop_perm,
    )

    start = time.time()
    result = await instance.test_connection()
    elapsed_ms = int((time.time() - start) * 1000)

    return {
        "success": result.get("success", False),
        "message": result.get("message", ""),
        "response_time_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# Connector type metadata
# ---------------------------------------------------------------------------

# Chat connectors where anyone who finds the bot can message it. Each must have
# at least one of these allowlist fields filled before it may start. Discord
# and Slack only filter by server/channel, so a channel
# allowlist counts; LINE's group list does not cover direct messages, so only
# its user list counts.
SENDER_ALLOWLIST_KEYS: Dict[str, tuple] = {
    "telegram": ("allowed_chat_ids",),
    "discord": ("guild_ids", "allowed_channel_ids"),
    "whatsapp": ("allowed_phone_numbers",),
    "slack": ("allowed_channel_ids",),
    "line": ("allowed_user_ids",),
    "signal": ("allowed_numbers",),
    "imessage": ("allowed_handles",),
    "msteams": ("allowed_user_ids",),
    "bluebubbles": ("allowed_handles",),
}
ALLOWLIST_REQUIRED_MESSAGE = "Add at least one allowed sender before enabling this connector"


def missing_sender_allowlist(connector_type: str, config: Dict[str, Any]) -> bool:
    """True when a chat connector has no sender allowlist and must not start."""
    keys = SENDER_ALLOWLIST_KEYS.get(connector_type)
    if not keys:
        return False
    for key in keys:
        value = config.get(key)
        if isinstance(value, str):
            value = [v for v in value.replace(",", " ").split() if v]
        if value:
            return False
    return True


def _annotate_allowlist_fields(types: List[Dict]) -> List[Dict]:
    """Tell the connector form which fields gate starting the connector."""
    for entry in types:
        keys = SENDER_ALLOWLIST_KEYS.get(entry["type"])
        if not keys:
            continue
        either = " (or " + ", ".join(k for k in keys) + ")" if len(keys) > 1 else ""
        for field in entry["config_fields"]:
            if field["key"] in keys and not field.get("description"):
                field["description"] = (
                    "Required to enable this connector: only these senders can use "
                    f"the bot{either}. An empty list would let anyone who finds it "
                    "run the agent."
                )
    return types


def get_connector_type_info() -> List[Dict]:
    """Return metadata about each supported connector type."""
    types = [
        {
            "type": "telegram",
            "name": "Telegram",
            "description": "Telegram bot via Bot API",
            "config_fields": [
                {"key": "bot_token", "label": "Bot Token", "type": "password", "required": True},
                {"key": "allowed_chat_ids", "label": "Allowed Chat IDs", "type": "list", "required": False},
                {"key": "welcome_message", "label": "Welcome Message", "type": "text", "required": False},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): messages, reactions, pins, polls, media, chat, moderation"},
                {"key": "auto_approve_destructive", "label": "Auto-approve Destructive Actions", "type": "text", "required": False,
                 "placeholder": "false", "description": "Set to 'true' to skip the in-channel Approve/Deny prompt for destructive AI actions (message deletion, bans, unpinning, leaving a chat)"},
            ],
        },
        {
            "type": "discord",
            "name": "Discord Bot",
            "description": "Discord bot via discord.py",
            "config_fields": [
                {"key": "bot_token", "label": "Bot Token", "type": "password", "required": True},
                {"key": "guild_ids", "label": "Guild IDs", "type": "list", "required": False, "picker": "discord_guilds"},
                {"key": "allowed_channel_ids", "label": "Allowed Channel IDs", "type": "list", "required": False, "picker": "discord_channels"},
                {"key": "command_prefix", "label": "Command Prefix", "type": "text", "required": False, "presets": ["!", "?", "."]},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): messages, reactions, threads, channels, roles, members, moderation, webhooks, invites, emojis, events, server"},
                {"key": "auto_approve_destructive", "label": "Auto-approve Destructive Actions", "type": "text", "required": False,
                 "placeholder": "false", "description": "Set to 'true' to skip the in-channel Approve/Deny prompt for destructive AI actions (deletes, bans, purges)"},
            ],
        },
        {
            "type": "whatsapp",
            "name": "WhatsApp",
            "description": "WhatsApp via Cloud API",
            "config_fields": [
                {"key": "phone_number_id", "label": "Phone Number ID", "type": "text", "required": True},
                {"key": "access_token", "label": "Access Token", "type": "password", "required": True},
                {"key": "verify_token", "label": "Verify Token", "type": "text", "required": True},
                {"key": "app_secret", "label": "App Secret", "type": "password", "required": True,
                 "description": "Meta app secret (App settings > Basic). Every webhook must carry a valid X-Hub-Signature-256 made with it; unsigned webhooks are rejected."},
                {"key": "allowed_phone_numbers", "label": "Allowed Phone Numbers", "type": "list", "required": False},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): messages, reactions, templates, media, account"},
                {"key": "auto_approve_destructive", "label": "Auto-approve Destructive Actions", "type": "text", "required": False,
                 "placeholder": "false", "description": "Set to 'true' to skip the in-channel Approve/Deny prompt for destructive AI actions (this catalog currently has none)"},
            ],
        },
        {
            "type": "webhook",
            "name": "Webhook",
            "description": "HTTP webhook — outbound notifications and/or inbound triggers",
            "config_fields": [
                {"key": "mode", "label": "Mode", "type": "text", "required": False, "placeholder": "outbound", "description": "outbound, inbound, or bidirectional"},
                {"key": "url", "label": "Outbound URL", "type": "text", "required": False, "description": "Required for outbound/bidirectional mode"},
                {"key": "secret", "label": "Signing Secret", "type": "password", "required": False, "description": "HMAC-SHA256 secret for signing and verification"},
                {"key": "headers", "label": "Custom Headers", "type": "json", "required": False},
                {"key": "inbound_path", "label": "Inbound Path", "type": "text", "required": False, "placeholder": "my-webhook", "description": "URL slug for inbound: /api/v1/connectors/hooks/{path}"},
                {"key": "inbound_token", "label": "Inbound Auth Token", "type": "password", "required": False, "description": "Bearer token for incoming requests"},
                {"key": "prompt_template", "label": "Prompt Template", "type": "textarea", "required": False, "placeholder": "Process the above webhook payload.", "description": "Custom instructions appended after the payload"},
            ],
        },
        {
            "type": "slack",
            "name": "Slack",
            "description": "Slack bot via Socket Mode",
            "config_fields": [
                {"key": "bot_token", "label": "Bot Token (xoxb-...)", "type": "password", "required": True},
                {"key": "app_token", "label": "App Token (xapp-...)", "type": "password", "required": True},
                {"key": "allowed_channel_ids", "label": "Allowed Channel IDs", "type": "list", "required": False},
                {"key": "command_prefix", "label": "Command Prefix", "type": "text", "required": False},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): messages, reactions, pins, channels, users, files, workspace"},
                {"key": "auto_approve_destructive", "label": "Auto-approve Destructive Actions", "type": "text", "required": False,
                 "placeholder": "false", "description": "Set to 'true' to skip the in-channel Approve/Deny prompt for destructive AI actions (message deletion, channel archiving)"},
            ],
        },
        {
            "type": "line",
            "name": "LINE",
            "description": "LINE Messaging API via webhook",
            "config_fields": [
                {"key": "channel_access_token", "label": "Channel Access Token", "type": "password", "required": True},
                {"key": "channel_secret", "label": "Channel Secret", "type": "password", "required": True},
                {"key": "allowed_user_ids", "label": "Allowed User IDs", "type": "list", "required": False},
                {"key": "allowed_group_ids", "label": "Allowed Group IDs", "type": "list", "required": False},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): messages, users, groups, account"},
                {"key": "auto_approve_destructive", "label": "Auto-approve Destructive Actions", "type": "text", "required": False,
                 "placeholder": "false", "description": "Set to 'true' to skip the Approve/Deny prompt for destructive AI actions (leaving a group)"},
            ],
        },
        {
            "type": "signal",
            "name": "Signal",
            "description": "Signal via signal-cli REST API",
            "config_fields": [
                {"key": "account", "label": "Phone Number (E.164)", "type": "text", "required": True, "placeholder": "+1234567890"},
                {"key": "api_url", "label": "API URL", "type": "text", "required": False, "placeholder": "http://127.0.0.1:8080"},
                {"key": "allowed_numbers", "label": "Allowed Phone Numbers", "type": "list", "required": False},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): messages, presence, reactions, groups"},
                {"key": "auto_approve_destructive", "label": "Auto-approve Destructive Actions", "type": "text", "required": False,
                 "placeholder": "false", "description": "Set to 'true' to skip the text-based Approve/Deny prompt for destructive AI actions (leaving a group)"},
            ],
        },
        {
            "type": "imessage",
            "name": "iMessage",
            "description": "iMessage via imsg CLI (macOS only)",
            "config_fields": [
                {"key": "cli_path", "label": "CLI Path", "type": "text", "required": False, "placeholder": "imsg"},
                {"key": "service", "label": "Service", "type": "text", "required": False, "placeholder": "auto"},
                {"key": "allowed_handles", "label": "Allowed Handles", "type": "list", "required": False},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): messages, reactions, chats, presence"},
            ],
        },
        {
            "type": "msteams",
            "name": "Microsoft Teams",
            "description": "Microsoft Teams via Bot Framework",
            "config_fields": [
                {"key": "app_id", "label": "Application (Client) ID", "type": "text", "required": True},
                {"key": "app_password", "label": "Client Secret", "type": "password", "required": True},
                {"key": "tenant_id", "label": "Tenant ID", "type": "text", "required": False, "placeholder": "botframework.com"},
                {"key": "allowed_user_ids", "label": "Allowed User IDs", "type": "list", "required": False},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): messages, presence, members"},
                {"key": "auto_approve_destructive", "label": "Auto-approve Destructive Actions", "type": "text", "required": False,
                 "placeholder": "false", "description": "Set to 'true' to skip the Adaptive Card Approve/Deny prompt for destructive AI actions (message deletion)"},
            ],
        },
        {
            "type": "bluebubbles",
            "name": "BlueBubbles",
            "description": "iMessage via BlueBubbles server (macOS)",
            "config_fields": [
                {"key": "server_url", "label": "Server URL", "type": "text", "required": True, "placeholder": "http://192.168.1.10:1234"},
                {"key": "password", "label": "API Password", "type": "password", "required": True},
                {"key": "allowed_handles", "label": "Allowed Handles", "type": "list", "required": False},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): messages, reactions, chats"},
                {"key": "auto_approve_destructive", "label": "Auto-approve Destructive Actions", "type": "text", "required": False,
                 "placeholder": "false", "description": "Set to 'true' to skip the text Approve/Deny prompt for destructive AI actions (leaving a chat)"},
            ],
        },
        {
            "type": "spotify",
            "name": "Spotify",
            "description": "Spotify playback control via Web API",
            "config_fields": [
                {"key": "client_id", "label": "Client ID", "type": "text", "required": True, "description": "From Spotify Developer Dashboard"},
                {"key": "client_secret", "label": "Client Secret", "type": "password", "required": True},
                {"key": "refresh_token", "label": "Refresh Token", "type": "password", "required": True, "description": "OAuth2 refresh token from authorization flow"},
                {"key": "poll_interval_seconds", "label": "Poll Interval (seconds)", "type": "text", "required": False, "placeholder": "30"},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): playback, read, devices, search, queue, playlists, library"},
            ],
        },
        {
            "type": "hue",
            "name": "Philips Hue",
            "description": "Smart lighting via local Hue Bridge API",
            "config_fields": [
                {"key": "bridge_ip", "label": "Bridge IP Address", "type": "text", "required": True, "placeholder": "192.168.1.x"},
                {"key": "api_key", "label": "API Key (Username)", "type": "password", "required": True, "description": "Press bridge button, then generate via POST /api"},
                {"key": "poll_interval_seconds", "label": "Poll Interval (seconds)", "type": "text", "required": False, "placeholder": "60"},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): control, discovery"},
            ],
        },
        {
            "type": "homeassistant",
            "name": "Home Assistant",
            "description": "Smart home control via Home Assistant REST API",
            "config_fields": [
                {"key": "url", "label": "Home Assistant URL", "type": "text", "required": True, "placeholder": "http://homeassistant.local:8123"},
                {"key": "access_token", "label": "Long-Lived Access Token", "type": "password", "required": True, "description": "Generate in HA: Profile > Long-Lived Access Tokens"},
                {"key": "entity_filter", "label": "Entity Filter", "type": "text", "required": False, "placeholder": "light.,switch.,climate.", "description": "Comma-separated entity prefixes to monitor"},
                {"key": "poll_interval_seconds", "label": "Poll Interval (seconds)", "type": "text", "required": False, "placeholder": "60"},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): states, control, discovery, events"},
            ],
        },
        {
            "type": "gmail",
            "name": "Gmail",
            "description": "Connect your Google account for agent email tools, not a chat bot",
            "config_fields": [
                {"key": "client_id", "label": "OAuth Client ID", "type": "text", "required": True, "description": "From Google Cloud Console"},
                {"key": "client_secret", "label": "OAuth Client Secret", "type": "password", "required": True},
                {"key": "refresh_token", "label": "OAuth Refresh Token", "type": "password", "required": True, "description": "From OAuth2 authorization flow"},
                {"key": "watch_inbox", "label": "Watch Inbox for Automations", "type": "text", "required": False, "placeholder": "false", "description": "Set true to poll for email_received triggers. Never starts chats or auto-replies."},
                {"key": "labels", "label": "Gmail Labels", "type": "text", "required": False, "placeholder": "INBOX", "description": "Comma-separated labels to watch"},
                {"key": "max_results", "label": "Max Results per Poll", "type": "text", "required": False, "placeholder": "10"},
                {"key": "enabled_action_categories", "label": "Enabled AI Action Categories", "type": "list", "required": False,
                 "description": "Restrict AI actions to these categories (empty = all): compose, read, organize"},
                {"key": "poll_interval_seconds", "label": "Poll Interval (seconds)", "type": "text", "required": False, "placeholder": "60"},
            ],
        },
    ]

    types.append({
        "type": "plaid", "name": "Personal Finance (Plaid)",
        "description": "Connect bank and credit-card accounts through Plaid Link. Read-only accounts, balances, and purchases; no payments or transfers.",
        "config_fields": [],
    })

    # Enrich each type with capabilities from the registered connector class
    from dataclasses import asdict
    from app.core.connectors.registry import _CONNECTOR_CLASSES
    from app.core import connector_packages

    for type_info in types:
        state = connector_packages.record(type_info["type"])
        type_info["plugin_id"] = connector_packages.package_id(type_info["type"])
        type_info["plugin_installed"] = bool(state)
        type_info["plugin_enabled"] = bool(state and state.get("enabled"))
        type_info["plugin_version"] = (state or {}).get("release")
        type_info["plugin_update_available"] = bool(state and state.get("release") != connector_packages.CATALOG["release"])
        type_info["plugin_rollback_available"] = bool(state and state.get("previous"))
        try:
            cls = _CONNECTOR_CLASSES[type_info["type"]]
            type_info["capabilities"] = asdict(cls.CAPABILITIES)
            type_info["max_message_length"] = cls.MAX_MESSAGE_LENGTH
            type_info["triggers"] = [asdict(t) for t in cls.TRIGGERS]
            type_info["actions"] = [asdict(a) for a in cls.ACTIONS]
            type_info["tools"] = [asdict(t) for t in cls.TOOLS]
        except (ValueError, AttributeError) as e:
            logger.warning("Failed to enrich capabilities for connector type '%s': %s", type_info["type"], e)

    return _annotate_allowlist_fields(types)
