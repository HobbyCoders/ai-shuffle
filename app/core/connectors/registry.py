"""
Registry mapping connector types to their implementation classes.
"""

from typing import Dict, List, Type

from app.core.connectors.base import BaseConnector

_CONNECTOR_CLASSES: Dict[str, Type[BaseConnector]] = {}


def register_connector(connector_type: str, cls: Type[BaseConnector]) -> None:
    """Register a connector class for a given type string."""
    _CONNECTOR_CLASSES[connector_type] = cls


def get_connector_class(connector_type: str) -> Type[BaseConnector]:
    """Look up the connector class for a given type. Raises ValueError if unknown."""
    if connector_type not in _CONNECTOR_CLASSES:
        raise ValueError(f"Unknown connector type: {connector_type}")
    return _CONNECTOR_CLASSES[connector_type]


def get_available_types() -> List[str]:
    """Return list of registered connector type strings."""
    return list(_CONNECTOR_CLASSES.keys())
