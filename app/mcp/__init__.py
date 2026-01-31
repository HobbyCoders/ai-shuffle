"""
MCP (Model Context Protocol) Tools for AI Shuffle.

This module provides MCP tools that Claude can call to interact with
mobile devices and other services.
"""

from app.mcp.phone_tools import (
    PHONE_TOOLS,
    get_phone_tools,
    execute_phone_tool,
    # Individual tool functions
    phone_calendar_read,
    phone_calendar_create,
    phone_alarm_set,
    phone_timer_set,
    phone_sms_send,
    phone_sms_read,
    phone_notify,
    phone_contacts_search,
    phone_system_control,
    phone_battery_status,
)

__all__ = [
    # Tool registry
    "PHONE_TOOLS",
    "get_phone_tools",
    "execute_phone_tool",
    # Individual tools
    "phone_calendar_read",
    "phone_calendar_create",
    "phone_alarm_set",
    "phone_timer_set",
    "phone_sms_send",
    "phone_sms_read",
    "phone_notify",
    "phone_contacts_search",
    "phone_system_control",
    "phone_battery_status",
]
