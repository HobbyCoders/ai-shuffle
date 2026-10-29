"""
Connectors package — external messaging platform integrations.

Registers all built-in connector types and exposes the global ConnectorManager singleton.
"""

from app.core.connectors.base import (  # noqa: F401
    BaseConnector,
    ConnectorCapabilities,
    ConnectorStatus,
    IncomingMessage,
    OutgoingMessage,
    PermissionPrompt,
    TriggerDef,
    ActionDef,
    ToolDef,
    chunk_text,
)
from app.core.connectors.registry import (  # noqa: F401
    register_connector,
    get_connector_class,
    get_available_types,
)

def __getattr__(name):
    # Preserve explicit imports of the old package-level class exports without
    # resolving every other connector as a side effect.
    from app.core.connectors.registry import CATALOG, get_bundled_connector_class
    for kind, spec in CATALOG["packages"].items():
        if spec["class"] == name:
            return get_bundled_connector_class(kind)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Short-lived rollback switch: opt back into the original eager imports.
import os as _os
if _os.environ.get("AI_SHUFFLE_PERF_LAZY_CONNECTORS", "").strip().lower() in ("0", "false", "off"):
    from importlib import import_module as _import_module
    from app.core.connectors.registry import get_bundled_connector_class as _bundled
    for _kind in get_available_types():
        _bundled(_kind)
    for _sdk in ("aiogram", "discord", "slack_sdk", "google.oauth2.credentials", "googleapiclient.discovery"):
        try:
            _import_module(_sdk)
        except ImportError:
            pass  # Optional SDKs were also tolerated by the eager implementation.

# Global singleton — imported by other modules
from app.core.connectors.manager import connector_manager  # noqa: F401, E402
