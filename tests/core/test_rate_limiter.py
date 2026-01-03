"""
Unit tests for rate limiter module.

Tests cover:
- Request window tracking
- Rate limit configuration
- Sliding window algorithm
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from app.core.rate_limiter import (
    RateLimitConfig,
    RateLimitResult,
    RateLimitStatus,
    RequestWindow,
)


class TestRateLimitConfig:
    """Test rate limit configuration."""

    def test_default_config_values(self):
        """Default config should have reasonable values."""
        config = RateLimitConfig()

        assert config.requests_per_minute == 20
        assert config.requests_per_hour == 200
        assert config.requests_per_day == 1000
        assert config.concurrent_requests == 3
        assert config.priority == 0
        assert config.is_unlimited is False

    def test_custom_config_values(self):
        """Should accept custom configuration."""
        config = RateLimitConfig(
            requests_per_minute=10,
            requests_per_hour=100,
            requests_per_day=500,
            concurrent_requests=5,
            priority=1,
            is_unlimited=True
        )

        assert config.requests_per_minute == 10
        assert config.requests_per_hour == 100
        assert config.requests_per_day == 500
        assert config.concurrent_requests == 5
        assert config.priority == 1
        assert config.is_unlimited is True


class TestRateLimitResult:
    """Test rate limit result enum."""

    def test_result_values(self):
        """Should have correct enum values."""
        assert RateLimitResult.ALLOWED.value == "allowed"
        assert RateLimitResult.DENIED.value == "denied"
        assert RateLimitResult.QUEUED.value == "queued"


class TestRateLimitStatus:
    """Test rate limit status dataclass."""

    def test_default_status(self):
        """Default status should be not limited."""
        status = RateLimitStatus()

        assert status.minute_count == 0
        assert status.hour_count == 0
        assert status.day_count == 0
        assert status.concurrent_count == 0
        assert status.is_limited is False
        assert status.retry_after == 0


class TestRequestWindow:
    """Test request window tracking."""

    def test_add_request(self):
        """Should track added requests."""
        window = RequestWindow()
        now = datetime.utcnow()

        window.add_request(now)

        assert len(window.timestamps) == 1
        assert window.concurrent_count == 1

    def test_add_multiple_requests(self):
        """Should track multiple requests."""
        window = RequestWindow()
        now = datetime.utcnow()

        for i in range(5):
            window.add_request(now + timedelta(seconds=i))

        assert len(window.timestamps) == 5
        assert window.concurrent_count == 5

    def test_complete_request(self):
        """Should decrease concurrent count on completion."""
        window = RequestWindow()
        now = datetime.utcnow()

        window.add_request(now)
        assert window.concurrent_count == 1

        window.complete_request()
        assert window.concurrent_count == 0

    def test_complete_request_no_negative(self):
        """Should not go below 0 concurrent count."""
        window = RequestWindow()

        window.complete_request()
        assert window.concurrent_count == 0

    def test_count_since(self):
        """Should count requests since a given time."""
        window = RequestWindow()
        now = datetime.utcnow()

        # Add requests at different times
        window.add_request(now - timedelta(seconds=30))
        window.add_request(now - timedelta(seconds=20))
        window.add_request(now - timedelta(seconds=10))
        window.add_request(now)

        # Count requests in last 25 seconds
        cutoff = now - timedelta(seconds=25)
        count = window.count_since(cutoff)

        assert count == 3

    def test_cleanup_old(self):
        """Should remove old timestamps."""
        window = RequestWindow()
        now = datetime.utcnow()

        # Add some old and new requests
        window.add_request(now - timedelta(minutes=10))
        window.add_request(now - timedelta(minutes=5))
        window.add_request(now - timedelta(minutes=1))
        window.add_request(now)

        # Cleanup requests older than 3 minutes
        cutoff = now - timedelta(minutes=3)
        window.cleanup_old(cutoff)

        assert len(window.timestamps) == 2

    def test_cleanup_preserves_concurrent_count(self):
        """Cleanup should not affect concurrent count."""
        window = RequestWindow()
        now = datetime.utcnow()

        # Add old requests
        window.add_request(now - timedelta(minutes=10))
        window.add_request(now - timedelta(minutes=5))

        # Cleanup old requests
        window.cleanup_old(now - timedelta(minutes=3))

        # Concurrent count should not change (it tracks active requests)
        assert window.concurrent_count == 2
