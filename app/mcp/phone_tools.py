"""
MCP Tools for phone capabilities.

Provides tools that Claude can call to control the user's phone, including:
- Calendar management
- Alarms and timers
- SMS messaging
- Notifications
- Contacts search
- System controls
- Battery status

Each tool uses the ActionQueue to queue actions and PushNotificationService
to notify the phone of pending actions.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.services.action_queue import (
    action_queue,
    ActionStatus,
    SecurityLevel as ActionSecurityLevel,
)
from app.services.push_notification import (
    push_notification_service,
    NotificationPriority,
    SecurityLevel as PushSecurityLevel,
)
from app.services.mobile_device_manager import mobile_device_manager

logger = logging.getLogger(__name__)


# ============================================================================
# Type Definitions for Tool Inputs/Outputs
# ============================================================================

class CalendarEvent(BaseModel):
    """A calendar event."""
    id: str
    title: str
    start_time: str
    end_time: str
    description: Optional[str] = None
    location: Optional[str] = None
    all_day: bool = False


class Contact(BaseModel):
    """A contact entry."""
    id: str
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None


class SmsMessage(BaseModel):
    """An SMS message."""
    id: str
    sender: str
    body: str
    timestamp: str
    is_read: bool = True


class BatteryStatus(BaseModel):
    """Battery status information."""
    level: int = Field(..., ge=0, le=100, description="Battery level 0-100")
    charging: bool


@dataclass
class ActionResult:
    """Result from a phone action execution."""
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ============================================================================
# Security Level Mapping
# ============================================================================

# Map our tool security levels to action queue security levels
SECURITY_LEVEL_MAP = {
    "safe": ActionSecurityLevel.LOW,
    "confirm": ActionSecurityLevel.MEDIUM,
    "sensitive": ActionSecurityLevel.HIGH,
}

# Map our levels to push notification security levels
PUSH_SECURITY_MAP = {
    "safe": PushSecurityLevel.LOW,
    "confirm": PushSecurityLevel.MEDIUM,
    "sensitive": PushSecurityLevel.HIGH,
}


# ============================================================================
# Helper Functions
# ============================================================================

async def _get_default_device() -> Optional[Dict[str, Any]]:
    """Get the default (most recently active) device."""
    devices = await mobile_device_manager.list_devices(active_only=True)
    if not devices:
        return None
    # Return most recently seen device (list is already sorted by last_seen)
    return devices[0]


async def _wait_for_action_completion(
    action_id: str,
    timeout_seconds: int = 120,
) -> ActionResult:
    """
    Wait for an action to be completed by the mobile device.

    Args:
        action_id: The action ID to wait for
        timeout_seconds: Maximum time to wait

    Returns:
        ActionResult with success/failure and data
    """
    start_time = asyncio.get_running_loop().time()

    while True:
        elapsed = asyncio.get_running_loop().time() - start_time
        if elapsed >= timeout_seconds:
            return ActionResult(
                success=False,
                error="Action timed out waiting for device response",
            )

        # Check action status
        action = await action_queue.get_action(action_id)
        if not action:
            return ActionResult(
                success=False,
                error="Action not found",
            )

        status = action.get("status")

        if status == ActionStatus.COMPLETED.value:
            return ActionResult(
                success=True,
                data=json.loads(action.get("executor_result") or "{}") if action.get("executor_result") else None,
            )
        elif status == ActionStatus.APPROVED.value:
            # Action approved but not yet completed - wait for completion
            await asyncio.sleep(0.5)
            continue
        elif status == ActionStatus.REJECTED.value:
            return ActionResult(
                success=False,
                error="Action rejected by user",
            )
        elif status == ActionStatus.EXPIRED.value:
            return ActionResult(
                success=False,
                error="Action expired",
            )
        elif status == ActionStatus.FAILED.value:
            return ActionResult(
                success=False,
                error=action.get("error_message") or "Action failed",
            )
        elif status == ActionStatus.PENDING.value:
            # Still waiting for user action
            await asyncio.sleep(0.5)
            continue
        else:
            await asyncio.sleep(0.5)


async def _execute_phone_action(
    action_type: str,
    params: Dict[str, Any],
    security_level: str = "confirm",
    description: Optional[str] = None,
) -> ActionResult:
    """
    Execute a phone action with the appropriate security level.

    Args:
        action_type: Type of action (e.g., "calendar_read")
        params: Action parameters
        security_level: Security level ("safe", "confirm", "sensitive")
        description: Human-readable description for notifications

    Returns:
        ActionResult with success/failure and data
    """
    device = await _get_default_device()
    if not device:
        return ActionResult(
            success=False,
            error="No mobile device connected. Please connect a device first.",
        )

    device_id = device["id"]
    fcm_token = device.get("fcm_token")

    # Map security level
    action_security = SECURITY_LEVEL_MAP.get(security_level, ActionSecurityLevel.MEDIUM)

    # Queue the action
    action_id = await action_queue.queue_action(
        device_id=device_id,
        action_type=action_type,
        params=params,
        security_level=action_security,
    )

    # Send push notification to alert device
    if fcm_token:
        push_security = PUSH_SECURITY_MAP.get(security_level, PushSecurityLevel.MEDIUM)

        if security_level != "safe":
            # For confirm/sensitive actions, send action request notification
            await push_notification_service.send_action_request(
                device_id=fcm_token,
                action_id=action_id,
                action_type=action_type,
                params=params,
                security_level=push_security,
            )
        else:
            # For safe actions, just notify the phone to execute
            await push_notification_service.send_notification(
                device_id=fcm_token,
                title="Action Request",
                body=description or f"Executing {action_type}",
                data={
                    "type": "action_execute",
                    "action_id": action_id,
                    "action_type": action_type,
                    "params": json.dumps(params),
                },
                priority=NotificationPriority.NORMAL,
            )

    # Wait for result
    timeout = 120 if security_level == "sensitive" else 60
    result = await _wait_for_action_completion(action_id, timeout_seconds=timeout)
    return result


# ============================================================================
# Phone MCP Tools
# ============================================================================

async def phone_calendar_read(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """
    Read calendar events from user's phone.

    Args:
        start_date: Optional start date filter (ISO format)
        end_date: Optional end date filter (ISO format)
        limit: Maximum number of events to return (default 10)

    Returns:
        Dict with success status and list of calendar events
    """
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "limit": limit,
    }

    result = await _execute_phone_action(
        action_type="calendar_read",
        params=params,
        security_level="safe",
        description="Reading calendar events",
    )

    if result.success:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "events": result.data.get("events", []) if result.data else [],
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to read calendar: {result.error}",
            }]
        }


async def phone_calendar_create(
    title: str,
    start_time: str,
    end_time: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a calendar event on user's phone.

    Requires user confirmation before creating the event.

    Args:
        title: Event title
        start_time: Start time in ISO format
        end_time: End time in ISO format
        description: Optional event description
        location: Optional event location

    Returns:
        Dict with success status and created event ID
    """
    params = {
        "title": title,
        "start_time": start_time,
        "end_time": end_time,
        "description": description,
        "location": location,
    }

    result = await _execute_phone_action(
        action_type="calendar_create",
        params=params,
        security_level="confirm",
        description=f"Create calendar event: {title}",
    )

    if result.success:
        event_id = result.data.get("event_id") if result.data else None
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "event_id": event_id,
                    "message": f"Calendar event '{title}' created successfully",
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to create calendar event: {result.error}",
            }]
        }


async def phone_alarm_set(
    hour: int,
    minute: int,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Set an alarm on user's phone.

    Requires user confirmation before setting the alarm.

    Args:
        hour: Hour in 24-hour format (0-23)
        minute: Minute (0-59)
        label: Optional alarm label

    Returns:
        Dict with success status
    """
    if not (0 <= hour <= 23):
        return {
            "content": [{
                "type": "text",
                "text": "Invalid hour. Must be between 0 and 23.",
            }]
        }

    if not (0 <= minute <= 59):
        return {
            "content": [{
                "type": "text",
                "text": "Invalid minute. Must be between 0 and 59.",
            }]
        }

    params = {
        "hour": hour,
        "minute": minute,
        "label": label,
    }

    time_str = f"{hour:02d}:{minute:02d}"
    result = await _execute_phone_action(
        action_type="alarm_set",
        params=params,
        security_level="confirm",
        description=f"Set alarm for {time_str}",
    )

    if result.success:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "message": f"Alarm set for {time_str}" + (f" ({label})" if label else ""),
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to set alarm: {result.error}",
            }]
        }


async def phone_timer_set(
    seconds: int,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Set a countdown timer on user's phone.

    Requires user confirmation before setting the timer.

    Args:
        seconds: Timer duration in seconds
        label: Optional timer label

    Returns:
        Dict with success status
    """
    if seconds <= 0:
        return {
            "content": [{
                "type": "text",
                "text": "Invalid duration. Seconds must be positive.",
            }]
        }

    params = {
        "seconds": seconds,
        "label": label,
    }

    # Format duration for display
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    duration_str = ""
    if hours > 0:
        duration_str = f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        duration_str = f"{minutes}m {secs}s"
    else:
        duration_str = f"{secs}s"

    result = await _execute_phone_action(
        action_type="timer_set",
        params=params,
        security_level="confirm",
        description=f"Set timer for {duration_str}",
    )

    if result.success:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "message": f"Timer set for {duration_str}" + (f" ({label})" if label else ""),
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to set timer: {result.error}",
            }]
        }


async def phone_sms_send(
    to: str,
    message: str,
) -> Dict[str, Any]:
    """
    Send an SMS from user's phone.

    Requires biometric/PIN verification due to sensitive nature.

    Args:
        to: Recipient phone number or contact name
        message: SMS message content

    Returns:
        Dict with success status
    """
    if not to:
        return {
            "content": [{
                "type": "text",
                "text": "Recipient is required.",
            }]
        }

    if not message:
        return {
            "content": [{
                "type": "text",
                "text": "Message content is required.",
            }]
        }

    params = {
        "to": to,
        "message": message,
    }

    result = await _execute_phone_action(
        action_type="sms_send",
        params=params,
        security_level="sensitive",
        description=f"Send SMS to {to}",
    )

    if result.success:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "message": f"SMS sent to {to}",
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to send SMS: {result.error}",
            }]
        }


async def phone_sms_read(
    limit: int = 10,
    contact: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read recent SMS messages from user's phone.

    Args:
        limit: Maximum number of messages to return (default 10)
        contact: Optional contact name/number to filter messages

    Returns:
        Dict with success status and list of messages
    """
    params = {
        "limit": limit,
        "contact": contact,
    }

    result = await _execute_phone_action(
        action_type="sms_read",
        params=params,
        security_level="safe",
        description="Reading SMS messages",
    )

    if result.success:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "messages": result.data.get("messages", []) if result.data else [],
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to read SMS: {result.error}",
            }]
        }


async def phone_notify(
    title: str,
    body: str,
    priority: str = "default",
) -> Dict[str, Any]:
    """
    Send a notification to user's phone.

    Args:
        title: Notification title
        body: Notification body text
        priority: Priority level (low, default, high)

    Returns:
        Dict with success status and notification ID
    """
    if priority not in ("low", "default", "high"):
        priority = "default"

    device = await _get_default_device()
    if not device:
        return {
            "content": [{
                "type": "text",
                "text": "No mobile device connected. Please connect a device first.",
            }]
        }

    fcm_token = device.get("fcm_token")
    if not fcm_token:
        return {
            "content": [{
                "type": "text",
                "text": "Device does not have push notifications enabled.",
            }]
        }

    notification_priority = NotificationPriority.HIGH if priority == "high" else NotificationPriority.NORMAL

    result = await push_notification_service.send_notification(
        device_id=fcm_token,
        title=title,
        body=body,
        data={"type": "user_notification", "priority": priority},
        priority=notification_priority,
    )

    if result.success:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "notification_id": result.message_id,
                    "message": f"Notification sent: {title}",
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to send notification: {result.error}",
            }]
        }


async def phone_contacts_search(
    query: str,
) -> Dict[str, Any]:
    """
    Search contacts on user's phone.

    Args:
        query: Name or phone number to search for

    Returns:
        Dict with success status and list of matching contacts
    """
    if not query:
        return {
            "content": [{
                "type": "text",
                "text": "Search query is required.",
            }]
        }

    params = {
        "query": query,
    }

    result = await _execute_phone_action(
        action_type="contacts_search",
        params=params,
        security_level="safe",
        description=f"Searching contacts for: {query}",
    )

    if result.success:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "contacts": result.data.get("contacts", []) if result.data else [],
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to search contacts: {result.error}",
            }]
        }


async def phone_system_control(
    action: str,
) -> Dict[str, Any]:
    """
    Control phone system settings.

    Requires biometric/PIN verification due to sensitive nature.

    Args:
        action: System action to perform. Valid values:
            - wifi_on, wifi_off
            - bluetooth_on, bluetooth_off
            - flashlight_on, flashlight_off
            - volume_up, volume_down
            - silent_mode

    Returns:
        Dict with success status
    """
    valid_actions = [
        "wifi_on", "wifi_off",
        "bluetooth_on", "bluetooth_off",
        "flashlight_on", "flashlight_off",
        "volume_up", "volume_down",
        "silent_mode",
    ]

    if action not in valid_actions:
        return {
            "content": [{
                "type": "text",
                "text": f"Invalid action. Valid actions: {', '.join(valid_actions)}",
            }]
        }

    params = {
        "action": action,
    }

    result = await _execute_phone_action(
        action_type="system_control",
        params=params,
        security_level="sensitive",
        description=f"System control: {action.replace('_', ' ')}",
    )

    if result.success:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "message": f"System control '{action}' executed successfully",
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to execute system control: {result.error}",
            }]
        }


async def phone_battery_status() -> Dict[str, Any]:
    """
    Get phone battery level and charging status.

    Returns:
        Dict with battery level (0-100) and charging status
    """
    result = await _execute_phone_action(
        action_type="battery_status",
        params={},
        security_level="safe",
        description="Checking battery status",
    )

    if result.success:
        data = result.data or {}
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "success": True,
                    "level": data.get("level", 0),
                    "charging": data.get("charging", False),
                }, indent=2),
            }]
        }
    else:
        return {
            "content": [{
                "type": "text",
                "text": f"Failed to get battery status: {result.error}",
            }]
        }


# ============================================================================
# Tool Registration for Claude Agent SDK
# ============================================================================

# Tool definitions following Claude Agent SDK pattern
PHONE_TOOLS = [
    {
        "name": "phone_calendar_read",
        "description": "Read calendar events from user's phone",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Optional start date filter (ISO format, e.g., 2024-01-15)",
                },
                "end_date": {
                    "type": "string",
                    "description": "Optional end date filter (ISO format)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events to return",
                    "default": 10,
                },
            },
        },
        "handler": phone_calendar_read,
        "security_level": "safe",
    },
    {
        "name": "phone_calendar_create",
        "description": "Create a calendar event on user's phone. Requires confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Event title",
                },
                "start_time": {
                    "type": "string",
                    "description": "Start time in ISO format (e.g., 2024-01-15T14:00:00)",
                },
                "end_time": {
                    "type": "string",
                    "description": "End time in ISO format",
                },
                "description": {
                    "type": "string",
                    "description": "Optional event description",
                },
                "location": {
                    "type": "string",
                    "description": "Optional event location",
                },
            },
            "required": ["title", "start_time", "end_time"],
        },
        "handler": phone_calendar_create,
        "security_level": "confirm",
    },
    {
        "name": "phone_alarm_set",
        "description": "Set an alarm on user's phone. Requires confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hour": {
                    "type": "integer",
                    "description": "Hour in 24-hour format (0-23)",
                    "minimum": 0,
                    "maximum": 23,
                },
                "minute": {
                    "type": "integer",
                    "description": "Minute (0-59)",
                    "minimum": 0,
                    "maximum": 59,
                },
                "label": {
                    "type": "string",
                    "description": "Optional alarm label",
                },
            },
            "required": ["hour", "minute"],
        },
        "handler": phone_alarm_set,
        "security_level": "confirm",
    },
    {
        "name": "phone_timer_set",
        "description": "Set a countdown timer on user's phone. Requires confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "integer",
                    "description": "Timer duration in seconds",
                    "minimum": 1,
                },
                "label": {
                    "type": "string",
                    "description": "Optional timer label",
                },
            },
            "required": ["seconds"],
        },
        "handler": phone_timer_set,
        "security_level": "confirm",
    },
    {
        "name": "phone_sms_send",
        "description": "Send an SMS from user's phone. Requires biometric verification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient phone number or contact name",
                },
                "message": {
                    "type": "string",
                    "description": "SMS message content",
                },
            },
            "required": ["to", "message"],
        },
        "handler": phone_sms_send,
        "security_level": "sensitive",
    },
    {
        "name": "phone_sms_read",
        "description": "Read recent SMS messages from user's phone",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return",
                    "default": 10,
                },
                "contact": {
                    "type": "string",
                    "description": "Optional contact name/number to filter messages",
                },
            },
        },
        "handler": phone_sms_read,
        "security_level": "safe",
    },
    {
        "name": "phone_notify",
        "description": "Send a notification to user's phone",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Notification title",
                },
                "body": {
                    "type": "string",
                    "description": "Notification body text",
                },
                "priority": {
                    "type": "string",
                    "description": "Priority level: low, default, or high",
                    "enum": ["low", "default", "high"],
                    "default": "default",
                },
            },
            "required": ["title", "body"],
        },
        "handler": phone_notify,
        "security_level": "safe",
    },
    {
        "name": "phone_contacts_search",
        "description": "Search contacts on user's phone",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name or phone number to search for",
                },
            },
            "required": ["query"],
        },
        "handler": phone_contacts_search,
        "security_level": "safe",
    },
    {
        "name": "phone_system_control",
        "description": "Control phone system settings. Requires biometric verification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "System action to perform",
                    "enum": [
                        "wifi_on", "wifi_off",
                        "bluetooth_on", "bluetooth_off",
                        "flashlight_on", "flashlight_off",
                        "volume_up", "volume_down",
                        "silent_mode",
                    ],
                },
            },
            "required": ["action"],
        },
        "handler": phone_system_control,
        "security_level": "sensitive",
    },
    {
        "name": "phone_battery_status",
        "description": "Get phone battery level and charging status",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": phone_battery_status,
        "security_level": "safe",
    },
]


def get_phone_tools() -> List[Dict[str, Any]]:
    """Get all phone tool definitions for MCP server registration."""
    return PHONE_TOOLS


async def execute_phone_tool(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a phone tool by name.

    Args:
        tool_name: Name of the tool to execute
        args: Tool arguments

    Returns:
        Tool result dict
    """
    for tool in PHONE_TOOLS:
        if tool["name"] == tool_name:
            handler = tool["handler"]
            return await handler(**args)

    return {
        "content": [{
            "type": "text",
            "text": f"Unknown phone tool: {tool_name}",
        }]
    }
