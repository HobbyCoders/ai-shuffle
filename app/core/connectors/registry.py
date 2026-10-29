"""Declarative connector discovery; resolve implementations only on demand."""

from importlib import import_module
from typing import Dict, List, Type

from app.core.connector_packages import CATALOG
from app.core.connectors.base import BaseConnector

# Overrides and resolved bundled classes. The verified package catalog is also
# the source of truth for built-in type names and implementation entry points.
_CONNECTOR_CLASSES: Dict[str, Type[BaseConnector]] = {}


def register_connector(connector_type: str, cls: Type[BaseConnector]) -> None:
    """Register (or override) a connector without resolving other types."""
    _CONNECTOR_CLASSES[connector_type] = cls


def get_bundled_connector_class(connector_type: str) -> Type[BaseConnector]:
    """SDK-free definitions for discovery and package-proxy metadata.

    Implementations keep SDK imports inside connection/action methods, so their
    declarative capabilities, triggers and actions remain available offline.
    """
    if connector_type not in _CONNECTOR_CLASSES:
        spec = CATALOG["packages"].get(connector_type)
        if spec is None:
            raise ValueError(f"Unknown connector type: {connector_type}")
        module = import_module(f"app.core.connectors.{spec['module']}")
        _CONNECTOR_CLASSES[connector_type] = getattr(module, spec["class"])
    return _CONNECTOR_CLASSES[connector_type]


def get_connector_class(connector_type: str) -> Type[BaseConnector]:
    """Resolve one configured connector, honoring its installed package first."""
    if connector_type not in CATALOG["packages"] and connector_type not in _CONNECTOR_CLASSES:
        raise ValueError(f"Unknown connector type: {connector_type}")
    from app.core.connector_packages import runtime_class
    return runtime_class(connector_type) or get_bundled_connector_class(connector_type)


def get_available_types() -> List[str]:
    """List type names without importing any connector implementation or SDK."""
    return list(dict.fromkeys((*CATALOG["packages"], *_CONNECTOR_CLASSES)))
