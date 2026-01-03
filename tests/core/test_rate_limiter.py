"""
Unit tests for rate limiter module.

Tests cover:
- Request window tracking
- Rate limit configuration
- Sliding window algorithm
- Rate limit checking and recording
- Cache management
- Cleanup operations
- Edge cases and error handling
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

from app.core.rate_limiter import (
    RateLimitConfig,
    RateLimitResult,
    RateLimitStatus,
    RequestWindow,
    RateLimiter,
    rate_limiter,
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

    def test_custom_status(self):
        """Should accept custom status values."""
        now = datetime.utcnow()
        status = RateLimitStatus(
            minute_count=5,
            hour_count=50,
            day_count=500,
            concurrent_count=2,
            minute_remaining=15,
            hour_remaining=150,
            day_remaining=500,
            concurrent_remaining=1,
            minute_reset=now,
            hour_reset=now,
            day_reset=now,
            is_limited=True,
            retry_after=60
        )

        assert status.minute_count == 5
        assert status.hour_count == 50
        assert status.day_count == 500
        assert status.concurrent_count == 2
        assert status.is_limited is True
        assert status.retry_after == 60


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

    def test_complete_multiple_requests(self):
        """Should handle multiple completions correctly."""
        window = RequestWindow()
        now = datetime.utcnow()

        for i in range(3):
            window.add_request(now + timedelta(seconds=i))

        assert window.concurrent_count == 3

        window.complete_request()
        window.complete_request()
        assert window.concurrent_count == 1

        window.complete_request()
        assert window.concurrent_count == 0

        window.complete_request()
        assert window.concurrent_count == 0

    def test_count_since(self):
        """Should count requests since a given time."""
        window = RequestWindow()
        now = datetime.utcnow()

        window.add_request(now - timedelta(seconds=30))
        window.add_request(now - timedelta(seconds=20))
        window.add_request(now - timedelta(seconds=10))
        window.add_request(now)

        cutoff = now - timedelta(seconds=25)
        count = window.count_since(cutoff)

        assert count == 3

    def test_count_since_all_old(self):
        """Should return 0 when all requests are old."""
        window = RequestWindow()
        now = datetime.utcnow()

        window.add_request(now - timedelta(minutes=10))
        window.add_request(now - timedelta(minutes=5))

        count = window.count_since(now - timedelta(minutes=1))
        assert count == 0

    def test_count_since_all_new(self):
        """Should count all requests when all are recent."""
        window = RequestWindow()
        now = datetime.utcnow()

        window.add_request(now - timedelta(seconds=5))
        window.add_request(now - timedelta(seconds=3))
        window.add_request(now)

        count = window.count_since(now - timedelta(minutes=1))
        assert count == 3

    def test_count_since_empty(self):
        """Should return 0 for empty window."""
        window = RequestWindow()
        count = window.count_since(datetime.utcnow())
        assert count == 0

    def test_cleanup_old(self):
        """Should remove old timestamps."""
        window = RequestWindow()
        now = datetime.utcnow()

        window.add_request(now - timedelta(minutes=10))
        window.add_request(now - timedelta(minutes=5))
        window.add_request(now - timedelta(minutes=1))
        window.add_request(now)

        cutoff = now - timedelta(minutes=3)
        window.cleanup_old(cutoff)

        assert len(window.timestamps) == 2

    def test_cleanup_preserves_concurrent_count(self):
        """Cleanup should not affect concurrent count."""
        window = RequestWindow()
        now = datetime.utcnow()

        window.add_request(now - timedelta(minutes=10))
        window.add_request(now - timedelta(minutes=5))

        window.cleanup_old(now - timedelta(minutes=3))

        assert window.concurrent_count == 2

    def test_cleanup_removes_all(self):
        """Cleanup should remove all timestamps if all are old."""
        window = RequestWindow()
        now = datetime.utcnow()

        window.add_request(now - timedelta(hours=2))
        window.add_request(now - timedelta(hours=1))

        window.cleanup_old(now)
        assert len(window.timestamps) == 0


class TestRateLimiter:
    """Test the RateLimiter class."""

    @pytest.fixture
    def limiter(self):
        """Create a fresh rate limiter for each test."""
        return RateLimiter()

    @pytest.fixture
    def mock_database(self):
        """Mock database calls."""
        with patch("app.core.rate_limiter.database") as mock_db:
            mock_db.get_rate_limit_for_user.return_value = None
            mock_db.log_request.return_value = None
            mock_db.cleanup_old_request_logs.return_value = 0
            yield mock_db

    def test_get_key_with_api_key(self, limiter):
        """Should generate API key-based key."""
        key = limiter._get_key(user_id="user123", api_key_id="api456")
        assert key == "api:api456"

    def test_get_key_with_user_id(self, limiter):
        """Should generate user-based key."""
        key = limiter._get_key(user_id="user123", api_key_id=None)
        assert key == "user:user123"

    def test_get_key_with_neither(self, limiter):
        """Should generate default admin key."""
        key = limiter._get_key(user_id=None, api_key_id=None)
        assert key == "admin:default"

    def test_get_key_api_key_takes_precedence(self, limiter):
        """API key should take precedence over user ID."""
        key = limiter._get_key(user_id="user123", api_key_id="api456")
        assert key == "api:api456"

    def test_cache_config(self, limiter):
        """Should cache configuration."""
        config = RateLimitConfig(requests_per_minute=50)
        limiter._cache_config("test:key", config)

        cached = limiter._get_cached_config("test:key")
        assert cached is not None
        assert cached.requests_per_minute == 50

    def test_cache_expires(self, limiter):
        """Should expire cached configuration."""
        config = RateLimitConfig(requests_per_minute=50)
        limiter._cache_config("test:key", config)

        limiter._config_cache["test:key"] = (
            config, datetime.utcnow() - timedelta(minutes=10)
        )

        cached = limiter._get_cached_config("test:key")
        assert cached is None

    def test_get_cached_config_miss(self, limiter):
        """Should return None for uncached key."""
        cached = limiter._get_cached_config("nonexistent:key")
        assert cached is None

    def test_clear_cache(self, limiter):
        """Should clear the configuration cache."""
        config = RateLimitConfig()
        limiter._cache_config("test:key1", config)
        limiter._cache_config("test:key2", config)

        assert len(limiter._config_cache) == 2

        limiter.clear_cache()
        assert len(limiter._config_cache) == 0

    def test_get_limit_config_from_cache(self, limiter, mock_database):
        """Should return cached config without database call."""
        cached_config = RateLimitConfig(requests_per_minute=100)
        limiter._cache_config("user:user123", cached_config)

        config = limiter.get_limit_config(user_id="user123", api_key_id=None)

        assert config.requests_per_minute == 100
        mock_database.get_rate_limit_for_user.assert_not_called()

    def test_get_limit_config_from_database(self, limiter, mock_database):
        """Should load config from database when not cached."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 30,
            "requests_per_hour": 300,
            "requests_per_day": 3000,
            "concurrent_requests": 5,
            "priority": 2,
            "is_unlimited": False
        }

        config = limiter.get_limit_config(user_id="user123", api_key_id=None)

        assert config.requests_per_minute == 30
        assert config.requests_per_hour == 300
        assert config.requests_per_day == 3000
        assert config.concurrent_requests == 5
        assert config.priority == 2
        mock_database.get_rate_limit_for_user.assert_called_once()

    def test_get_limit_config_default(self, limiter, mock_database):
        """Should return default config when none in database."""
        mock_database.get_rate_limit_for_user.return_value = None

        config = limiter.get_limit_config(user_id="user123", api_key_id=None)

        assert config == limiter.DEFAULT_CONFIG

    def test_get_limit_config_partial_db_config(self, limiter, mock_database):
        """Should use defaults for missing database fields."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 50
        }

        config = limiter.get_limit_config(user_id="user123", api_key_id=None)

        assert config.requests_per_minute == 50
        assert config.requests_per_hour == 200
        assert config.requests_per_day == 1000

    @pytest.mark.asyncio
    async def test_check_rate_limit_admin_bypass(self, limiter, mock_database):
        """Admin users should bypass rate limits."""
        result, status = await limiter.check_rate_limit(
            user_id="admin", api_key_id=None, is_admin=True
        )

        assert result == RateLimitResult.ALLOWED
        assert status.is_limited is False

    @pytest.mark.asyncio
    async def test_check_rate_limit_admin_with_api_key(self, limiter, mock_database):
        """Admin with API key should still be rate limited."""
        result, status = await limiter.check_rate_limit(
            user_id="admin", api_key_id="api123", is_admin=True
        )

        assert result == RateLimitResult.ALLOWED

    @pytest.mark.asyncio
    async def test_check_rate_limit_unlimited_user(self, limiter, mock_database):
        """Unlimited users should bypass all limits."""
        mock_database.get_rate_limit_for_user.return_value = {
            "is_unlimited": True
        }

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert result == RateLimitResult.ALLOWED
        assert status.is_limited is False

    @pytest.mark.asyncio
    async def test_check_rate_limit_allowed(self, limiter, mock_database):
        """Should allow requests under the limit."""
        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert result == RateLimitResult.ALLOWED
        assert status.is_limited is False
        assert status.minute_remaining > 0

    @pytest.mark.asyncio
    async def test_check_rate_limit_minute_exceeded(self, limiter, mock_database):
        """Should deny when minute limit is exceeded."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 2,
            "requests_per_hour": 200,
            "requests_per_day": 1000,
            "concurrent_requests": 10
        }

        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        async with limiter._lock:
            limiter._windows[key].add_request(now - timedelta(seconds=30))
            limiter._windows[key].add_request(now - timedelta(seconds=20))

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert result == RateLimitResult.DENIED
        assert status.is_limited is True
        assert status.retry_after == 60

    @pytest.mark.asyncio
    async def test_check_rate_limit_hour_exceeded(self, limiter, mock_database):
        """Should deny when hour limit is exceeded."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 100,
            "requests_per_hour": 2,
            "requests_per_day": 1000,
            "concurrent_requests": 10
        }

        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        async with limiter._lock:
            limiter._windows[key].add_request(now - timedelta(minutes=30))
            limiter._windows[key].add_request(now - timedelta(minutes=20))

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert result == RateLimitResult.DENIED
        assert status.is_limited is True
        assert status.retry_after == 3600

    @pytest.mark.asyncio
    async def test_check_rate_limit_day_exceeded(self, limiter, mock_database):
        """Should deny when day limit is exceeded."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 100,
            "requests_per_hour": 100,
            "requests_per_day": 2,
            "concurrent_requests": 10
        }

        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        async with limiter._lock:
            limiter._windows[key].add_request(now - timedelta(hours=10))
            limiter._windows[key].add_request(now - timedelta(hours=5))

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert result == RateLimitResult.DENIED
        assert status.is_limited is True
        assert status.retry_after == 86400

    @pytest.mark.asyncio
    async def test_check_rate_limit_concurrent_exceeded(self, limiter, mock_database):
        """Should deny when concurrent limit is exceeded."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 100,
            "requests_per_hour": 100,
            "requests_per_day": 100,
            "concurrent_requests": 2
        }

        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        async with limiter._lock:
            limiter._windows[key].add_request(now)
            limiter._windows[key].add_request(now)

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert result == RateLimitResult.DENIED
        assert status.is_limited is True
        assert status.retry_after == 5

    @pytest.mark.asyncio
    async def test_check_rate_limit_cleans_old_timestamps(self, limiter, mock_database):
        """Should clean up old timestamps during check."""
        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        async with limiter._lock:
            limiter._windows[key].timestamps = [
                now - timedelta(hours=48),
                now - timedelta(hours=25),
                now - timedelta(hours=10),
            ]
            limiter._windows[key].concurrent_count = 0

        await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert len(limiter._windows[key].timestamps) == 1

    @pytest.mark.asyncio
    async def test_record_request(self, limiter, mock_database):
        """Should record a request and return request ID."""
        request_id = await limiter.record_request(
            user_id="user123",
            api_key_id=None,
            endpoint="/api/test",
            is_admin=False
        )

        assert request_id is not None
        assert len(request_id) == 36

        key = limiter._get_key("user123", None)
        assert len(limiter._windows[key].timestamps) == 1
        assert limiter._windows[key].concurrent_count == 1

    @pytest.mark.asyncio
    async def test_record_request_logs_to_database(self, limiter, mock_database):
        """Should log request to database."""
        await limiter.record_request(
            user_id="user123",
            api_key_id="api456",
            endpoint="/api/test",
            is_admin=False
        )

        mock_database.log_request.assert_called_once()
        call_kwargs = mock_database.log_request.call_args
        assert call_kwargs[1]["user_id"] == "user123"
        assert call_kwargs[1]["api_key_id"] == "api456"
        assert call_kwargs[1]["endpoint"] == "/api/test"
        assert call_kwargs[1]["status"] == "success"

    @pytest.mark.asyncio
    async def test_record_request_handles_database_error(self, limiter, mock_database):
        """Should handle database logging errors gracefully."""
        mock_database.log_request.side_effect = Exception("Database error")

        request_id = await limiter.record_request(
            user_id="user123",
            api_key_id=None,
            endpoint="/api/test",
            is_admin=False
        )

        assert request_id is not None

    @pytest.mark.asyncio
    async def test_complete_request(self, limiter, mock_database):
        """Should decrease concurrent count on completion."""
        request_id = await limiter.record_request(
            user_id="user123",
            api_key_id=None,
            endpoint="/api/test",
            is_admin=False
        )

        key = limiter._get_key("user123", None)
        assert limiter._windows[key].concurrent_count == 1

        await limiter.complete_request(
            user_id="user123",
            api_key_id=None,
            request_id=request_id,
            duration_ms=100
        )

        assert limiter._windows[key].concurrent_count == 0

    @pytest.mark.asyncio
    async def test_complete_request_without_record(self, limiter, mock_database):
        """Should handle completion without prior record."""
        await limiter.complete_request(
            user_id="user123",
            api_key_id=None,
            request_id="fake-id",
            duration_ms=None
        )

        key = limiter._get_key("user123", None)
        assert limiter._windows[key].concurrent_count == 0

    def test_get_rate_limit_status_admin(self, limiter, mock_database):
        """Admin status check should show not limited."""
        status = limiter.get_rate_limit_status(
            user_id="admin", api_key_id=None, is_admin=True
        )

        assert status.is_limited is False

    def test_get_rate_limit_status_admin_with_api_key(self, limiter, mock_database):
        """Admin with API key should get normal status."""
        status = limiter.get_rate_limit_status(
            user_id="admin", api_key_id="api123", is_admin=True
        )

        assert isinstance(status, RateLimitStatus)

    def test_get_rate_limit_status_unlimited(self, limiter, mock_database):
        """Unlimited user status should show not limited."""
        mock_database.get_rate_limit_for_user.return_value = {
            "is_unlimited": True
        }

        status = limiter.get_rate_limit_status(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert status.is_limited is False

    def test_get_rate_limit_status_normal_user(self, limiter, mock_database):
        """Normal user should get accurate status."""
        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        limiter._windows[key].add_request(now - timedelta(seconds=30))
        limiter._windows[key].add_request(now - timedelta(seconds=20))

        status = limiter.get_rate_limit_status(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert status.minute_count == 2
        assert status.hour_count == 2
        assert status.day_count == 2
        assert status.concurrent_count == 2

    def test_get_rate_limit_status_no_window(self, limiter, mock_database):
        """Should handle missing window gracefully."""
        status = limiter.get_rate_limit_status(
            user_id="newuser", api_key_id=None, is_admin=False
        )

        assert status.minute_count == 0
        assert status.hour_count == 0
        assert status.day_count == 0

    def test_cleanup(self, limiter, mock_database):
        """Should clean up old data from all windows."""
        now = datetime.utcnow()

        limiter._windows["user:user1"].timestamps = [
            now - timedelta(hours=48),
            now - timedelta(hours=10),
        ]
        limiter._windows["user:user2"].timestamps = [
            now - timedelta(hours=30),
            now - timedelta(hours=5),
        ]

        limiter.cleanup()

        assert len(limiter._windows["user:user1"].timestamps) == 1
        assert len(limiter._windows["user:user2"].timestamps) == 1

    def test_cleanup_calls_database(self, limiter, mock_database):
        """Should call database cleanup."""
        mock_database.cleanup_old_request_logs.return_value = 10

        limiter.cleanup()

        mock_database.cleanup_old_request_logs.assert_called_once()

    def test_cleanup_handles_database_error(self, limiter, mock_database):
        """Should handle database cleanup errors gracefully."""
        mock_database.cleanup_old_request_logs.side_effect = Exception("DB Error")

        limiter.cleanup()

    def test_cleanup_logs_deleted_count(self, limiter, mock_database, caplog):
        """Should log when records are deleted."""
        mock_database.cleanup_old_request_logs.return_value = 100

        import logging
        with caplog.at_level(logging.INFO):
            limiter.cleanup()

        assert "100" in caplog.text


class TestRateLimiterConcurrency:
    """Test concurrent access to rate limiter."""

    @pytest.fixture
    def limiter(self):
        """Create a fresh rate limiter for each test."""
        return RateLimiter()

    @pytest.fixture
    def mock_database(self):
        """Mock database calls."""
        with patch("app.core.rate_limiter.database") as mock_db:
            mock_db.get_rate_limit_for_user.return_value = None
            mock_db.log_request.return_value = None
            yield mock_db

    @pytest.mark.asyncio
    async def test_concurrent_checks(self, limiter, mock_database):
        """Should handle concurrent rate limit checks."""
        async def check():
            return await limiter.check_rate_limit(
                user_id="user123", api_key_id=None, is_admin=False
            )

        results = await asyncio.gather(*[check() for _ in range(10)])

        for result, status in results:
            assert result == RateLimitResult.ALLOWED

    @pytest.mark.asyncio
    async def test_concurrent_records(self, limiter, mock_database):
        """Should handle concurrent request recording."""
        async def record():
            return await limiter.record_request(
                user_id="user123", api_key_id=None,
                endpoint="/test", is_admin=False
            )

        request_ids = await asyncio.gather(*[record() for _ in range(5)])

        assert len(set(request_ids)) == 5

        key = limiter._get_key("user123", None)
        assert limiter._windows[key].concurrent_count == 5


class TestGlobalRateLimiter:
    """Test the global rate_limiter instance."""

    def test_global_instance_exists(self):
        """Should have a global rate limiter instance."""
        assert rate_limiter is not None
        assert isinstance(rate_limiter, RateLimiter)

    def test_global_instance_has_default_config(self):
        """Global instance should have default configuration."""
        assert rate_limiter.DEFAULT_CONFIG.requests_per_minute == 20
        assert rate_limiter.DEFAULT_CONFIG.requests_per_hour == 200
        assert rate_limiter.DEFAULT_CONFIG.requests_per_day == 1000


class TestRateLimitStatusCalculations:
    """Test rate limit status remaining calculations."""

    @pytest.fixture
    def limiter(self):
        """Create a fresh rate limiter for each test."""
        return RateLimiter()

    @pytest.fixture
    def mock_database(self):
        """Mock database calls."""
        with patch("app.core.rate_limiter.database") as mock_db:
            mock_db.get_rate_limit_for_user.return_value = {
                "requests_per_minute": 10,
                "requests_per_hour": 100,
                "requests_per_day": 500,
                "concurrent_requests": 5
            }
            mock_db.log_request.return_value = None
            yield mock_db

    @pytest.mark.asyncio
    async def test_remaining_calculations(self, limiter, mock_database):
        """Should calculate remaining requests correctly."""
        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        async with limiter._lock:
            for i in range(3):
                limiter._windows[key].add_request(now - timedelta(seconds=i*10))

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert status.minute_count == 3
        assert status.minute_remaining == 7
        assert status.hour_remaining == 97
        assert status.day_remaining == 497
        assert status.concurrent_remaining == 2

    @pytest.mark.asyncio
    async def test_remaining_never_negative(self, limiter, mock_database):
        """Remaining count should never be negative."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 2,
            "requests_per_hour": 100,
            "requests_per_day": 500,
            "concurrent_requests": 5
        }

        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        async with limiter._lock:
            for i in range(5):
                limiter._windows[key].add_request(now - timedelta(seconds=i*10))

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert status.minute_remaining == 0
        assert status.minute_count == 5


class TestRateLimiterEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.fixture
    def limiter(self):
        """Create a fresh rate limiter for each test."""
        return RateLimiter()

    @pytest.fixture
    def mock_database(self):
        """Mock database calls."""
        with patch("app.core.rate_limiter.database") as mock_db:
            mock_db.get_rate_limit_for_user.return_value = None
            mock_db.log_request.return_value = None
            mock_db.cleanup_old_request_logs.return_value = 0
            yield mock_db

    @pytest.mark.asyncio
    async def test_zero_limit_config(self, limiter, mock_database):
        """Should handle zero limits in config."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 0,
            "requests_per_hour": 0,
            "requests_per_day": 0,
            "concurrent_requests": 0
        }

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert result == RateLimitResult.DENIED
        assert status.is_limited is True

    def test_empty_windows_dict(self, limiter, mock_database):
        """Should handle empty windows dictionary."""
        limiter.cleanup()

    @pytest.mark.asyncio
    async def test_request_exactly_at_limit(self, limiter, mock_database):
        """Should deny when exactly at the limit."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 3,
            "requests_per_hour": 100,
            "requests_per_day": 1000,
            "concurrent_requests": 10
        }

        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        async with limiter._lock:
            for i in range(3):
                limiter._windows[key].add_request(now - timedelta(seconds=i*10))

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert result == RateLimitResult.DENIED
        assert status.minute_remaining == 0

    @pytest.mark.asyncio
    async def test_request_one_below_limit(self, limiter, mock_database):
        """Should allow when one below the limit."""
        mock_database.get_rate_limit_for_user.return_value = {
            "requests_per_minute": 3,
            "requests_per_hour": 100,
            "requests_per_day": 1000,
            "concurrent_requests": 10
        }

        key = limiter._get_key("user123", None)
        now = datetime.utcnow()

        async with limiter._lock:
            for i in range(2):
                limiter._windows[key].add_request(now - timedelta(seconds=i*10))

        result, status = await limiter.check_rate_limit(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert result == RateLimitResult.ALLOWED
        assert status.minute_remaining == 1

    def test_get_rate_limit_status_calculates_reset_times(self, limiter, mock_database):
        """Should calculate appropriate reset times."""
        now = datetime.utcnow()

        status = limiter.get_rate_limit_status(
            user_id="user123", api_key_id=None, is_admin=False
        )

        assert status.minute_reset > now
        assert status.hour_reset > now
        assert status.day_reset > now

        assert (status.minute_reset - now).total_seconds() < 65
        assert (status.hour_reset - now).total_seconds() < 3605
        assert (status.day_reset - now).total_seconds() < 86405
