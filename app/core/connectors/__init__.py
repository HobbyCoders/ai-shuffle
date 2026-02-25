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

# Register built-in connector types
from app.core.connectors.telegram import TelegramConnector
from app.core.connectors.discord_connector import DiscordConnector
from app.core.connectors.whatsapp import WhatsAppConnector
from app.core.connectors.webhook_connector import WebhookConnector
from app.core.connectors.slack import SlackConnector
from app.core.connectors.line import LINEConnector
from app.core.connectors.signal_connector import SignalConnector
from app.core.connectors.imessage import IMessageConnector
from app.core.connectors.msteams import MSTeamsConnector
from app.core.connectors.bluebubbles import BlueBubblesConnector
from app.core.connectors.spotify import SpotifyConnector
from app.core.connectors.hue import HueConnector
from app.core.connectors.homeassistant import HomeAssistantConnector
from app.core.connectors.gmail import GmailConnector

register_connector("telegram", TelegramConnector)
register_connector("discord", DiscordConnector)
register_connector("whatsapp", WhatsAppConnector)
register_connector("webhook", WebhookConnector)
register_connector("slack", SlackConnector)
register_connector("line", LINEConnector)
register_connector("signal", SignalConnector)
register_connector("imessage", IMessageConnector)
register_connector("msteams", MSTeamsConnector)
register_connector("bluebubbles", BlueBubblesConnector)
register_connector("spotify", SpotifyConnector)
register_connector("hue", HueConnector)
register_connector("homeassistant", HomeAssistantConnector)
register_connector("gmail", GmailConnector)

# Global singleton — imported by other modules
from app.core.connectors.manager import connector_manager  # noqa: F401, E402
