"""
Unit tests for MCP phone tools.

Tests cover:
- Pydantic model definitions
- Security level mappings
- ActionResult dataclass
- Tool definitions structure
"""

import pytest
from pydantic import ValidationError

from app.mcp.phone_tools import (
    CalendarEvent,
    Contact,
    SmsMessage,
    BatteryStatus,
    ActionResult,
    SECURITY_LEVEL_MAP,
    PUSH_SECURITY_MAP,
)


# =============================================================================
# CalendarEvent Model Tests
# =============================================================================

class TestCalendarEvent:
    """Test CalendarEvent Pydantic model."""

    def test_create_valid_calendar_event(self):
        """Should create valid calendar event."""
        event = CalendarEvent(
            id="event-1",
            title="Team Meeting",
            start_time="2026-02-05T10:00:00",
            end_time="2026-02-05T11:00:00",
        )
        assert event.id == "event-1"
        assert event.title == "Team Meeting"
        assert event.all_day is False

    def test_calendar_event_with_all_fields(self):
        """Should create event with all optional fields."""
        event = CalendarEvent(
            id="event-2",
            title="Conference",
            start_time="2026-02-06T09:00:00",
            end_time="2026-02-06T17:00:00",
            description="Annual company conference",
            location="Main Hall",
            all_day=True,
        )
        assert event.description == "Annual company conference"
        assert event.location == "Main Hall"
        assert event.all_day is True

    def test_calendar_event_default_values(self):
        """Should have correct default values."""
        event = CalendarEvent(
            id="event-3",
            title="Quick Call",
            start_time="2026-02-05T14:00:00",
            end_time="2026-02-05T14:30:00",
        )
        assert event.description is None
        assert event.location is None
        assert event.all_day is False

    def test_calendar_event_missing_required_fields(self):
        """Should raise error for missing required fields."""
        with pytest.raises(ValidationError):
            CalendarEvent(id="event-4", title="Missing times")


# =============================================================================
# Contact Model Tests
# =============================================================================

class TestContact:
    """Test Contact Pydantic model."""

    def test_create_valid_contact(self):
        """Should create valid contact."""
        contact = Contact(
            id="contact-1",
            name="John Doe",
            phone="+1234567890",
            email="john@example.com",
        )
        assert contact.id == "contact-1"
        assert contact.name == "John Doe"
        assert contact.phone == "+1234567890"
        assert contact.email == "john@example.com"

    def test_contact_minimal(self):
        """Should create contact with only required fields."""
        contact = Contact(
            id="contact-2",
            name="Jane Smith",
        )
        assert contact.name == "Jane Smith"
        assert contact.phone is None
        assert contact.email is None

    def test_contact_with_phone_only(self):
        """Should create contact with phone only."""
        contact = Contact(
            id="contact-3",
            name="Bob",
            phone="+0987654321",
        )
        assert contact.phone == "+0987654321"
        assert contact.email is None


# =============================================================================
# SmsMessage Model Tests
# =============================================================================

class TestSmsMessage:
    """Test SmsMessage Pydantic model."""

    def test_create_valid_sms(self):
        """Should create valid SMS message."""
        sms = SmsMessage(
            id="sms-1",
            sender="+1234567890",
            body="Hello, how are you?",
            timestamp="2026-02-05T10:30:00",
        )
        assert sms.id == "sms-1"
        assert sms.sender == "+1234567890"
        assert sms.body == "Hello, how are you?"
        assert sms.is_read is True

    def test_sms_unread(self):
        """Should create unread SMS."""
        sms = SmsMessage(
            id="sms-2",
            sender="+0987654321",
            body="New message",
            timestamp="2026-02-05T11:00:00",
            is_read=False,
        )
        assert sms.is_read is False

    def test_sms_default_is_read(self):
        """Should default to read."""
        sms = SmsMessage(
            id="sms-3",
            sender="Test",
            body="Test message",
            timestamp="2026-02-05T12:00:00",
        )
        assert sms.is_read is True


# =============================================================================
# BatteryStatus Model Tests
# =============================================================================

class TestBatteryStatus:
    """Test BatteryStatus Pydantic model."""

    def test_create_valid_battery_status(self):
        """Should create valid battery status."""
        status = BatteryStatus(level=85, charging=True)
        assert status.level == 85
        assert status.charging is True

    def test_battery_full(self):
        """Should allow 100% battery."""
        status = BatteryStatus(level=100, charging=False)
        assert status.level == 100

    def test_battery_empty(self):
        """Should allow 0% battery."""
        status = BatteryStatus(level=0, charging=True)
        assert status.level == 0

    def test_battery_level_too_high(self):
        """Should reject level over 100."""
        with pytest.raises(ValidationError):
            BatteryStatus(level=101, charging=False)

    def test_battery_level_too_low(self):
        """Should reject negative level."""
        with pytest.raises(ValidationError):
            BatteryStatus(level=-1, charging=False)

    def test_battery_charging_required(self):
        """Should require charging field."""
        with pytest.raises(ValidationError):
            BatteryStatus(level=50)


# =============================================================================
# ActionResult Tests
# =============================================================================

class TestActionResult:
    """Test ActionResult dataclass."""

    def test_create_success_result(self):
        """Should create success result."""
        result = ActionResult(success=True, data={"key": "value"})
        assert result.success is True
        assert result.data == {"key": "value"}
        assert result.error is None

    def test_create_error_result(self):
        """Should create error result."""
        result = ActionResult(success=False, error="Something went wrong")
        assert result.success is False
        assert result.data is None
        assert result.error == "Something went wrong"

    def test_create_minimal_result(self):
        """Should create result with only required field."""
        result = ActionResult(success=True)
        assert result.success is True
        assert result.data is None
        assert result.error is None


# =============================================================================
# Security Level Mapping Tests
# =============================================================================

class TestSecurityLevelMappings:
    """Test security level mappings."""

    def test_security_level_map_has_expected_keys(self):
        """SECURITY_LEVEL_MAP should have expected keys."""
        assert "safe" in SECURITY_LEVEL_MAP
        assert "confirm" in SECURITY_LEVEL_MAP
        assert "sensitive" in SECURITY_LEVEL_MAP

    def test_push_security_map_has_expected_keys(self):
        """PUSH_SECURITY_MAP should have expected keys."""
        assert "safe" in PUSH_SECURITY_MAP
        assert "confirm" in PUSH_SECURITY_MAP
        assert "sensitive" in PUSH_SECURITY_MAP

    def test_maps_have_same_keys(self):
        """Both maps should have same keys."""
        assert set(SECURITY_LEVEL_MAP.keys()) == set(PUSH_SECURITY_MAP.keys())


# =============================================================================
# Module Import Tests
# =============================================================================

class TestModuleImports:
    """Test module imports."""

    def test_phone_tools_module_imports(self):
        """Phone tools module should import without errors."""
        from app.mcp import phone_tools
        assert phone_tools is not None

    def test_model_classes_importable(self):
        """Model classes should be importable."""
        from app.mcp.phone_tools import (
            CalendarEvent,
            Contact,
            SmsMessage,
            BatteryStatus,
            ActionResult,
        )
        assert CalendarEvent is not None
        assert Contact is not None
        assert SmsMessage is not None
        assert BatteryStatus is not None
        assert ActionResult is not None

    def test_constants_importable(self):
        """Constants should be importable."""
        from app.mcp.phone_tools import (
            SECURITY_LEVEL_MAP,
            PUSH_SECURITY_MAP,
        )
        assert SECURITY_LEVEL_MAP is not None
        assert PUSH_SECURITY_MAP is not None
