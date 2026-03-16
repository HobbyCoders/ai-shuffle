"""
MCP (Model Context Protocol) Tools for AI Shuffle.

This module provides MCP tools that Claude can call to interact with
mobile devices, web browsers, and other services.
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

from app.mcp.browser_tools import (
    BROWSER_TOOLS,
    get_browser_tools,
    execute_browser_tool,
    # Individual tool functions
    browser_navigate,
    browser_screenshot,
    browser_click,
    browser_type,
    browser_scroll,
    browser_get_content,
    browser_get_elements,
    browser_back,
    browser_forward,
    browser_evaluate,
    browser_fill,
    browser_select,
    browser_wait,
    browser_tab_new,
    browser_tab_list,
    browser_tab_switch,
    browser_close,
)

from app.mcp.chrome_session import chrome_session_manager as browser_session_manager

__all__ = [
    # Phone tool registry
    "PHONE_TOOLS",
    "get_phone_tools",
    "execute_phone_tool",
    # Phone individual tools
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
    # Browser tool registry
    "BROWSER_TOOLS",
    "get_browser_tools",
    "execute_browser_tool",
    "browser_session_manager",
    # Browser individual tools
    "browser_navigate",
    "browser_screenshot",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_get_content",
    "browser_get_elements",
    "browser_back",
    "browser_forward",
    "browser_evaluate",
    "browser_fill",
    "browser_select",
    "browser_wait",
    "browser_tab_new",
    "browser_tab_list",
    "browser_tab_switch",
    "browser_close",
]
