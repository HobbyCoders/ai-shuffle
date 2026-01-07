"""
Rate limiter service for AI Hub.

Provides per-user rate limiting using a sliding window algorithm.
Uses in-memory tracking for speed with database persistence for configuration.
"""

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple

from app.db import database

logger = logging.getLogger(__name__)


class RateLimitResult(Enum):
    """Result of a rate limit check"""
    ALLOWED = "allowed"
    DENIED = "denied"
    QUEUED = "queued"


@dataclass
class RateLimitConfig:
    """Rate limit configuration for a user or API key"""
    requests_per_minute: int = 20
    requests_per_hour: int = 200
    requests_per_day: int = 1000
    concurrent_requests: int = 3
    priority: int = 0
    is_unlimited: bool = False


@dataclass
class RateLimitStatus:
    """Current rate limit status for a user"""
    minute_count: int = 0
    hour_count: int = 0
    day_count: int = 0
    concurrent_count: int = 0
    minute_remaining: int = 0
    hour_remaining: int = 0
    day_remaining: int = 0
    concurrent_remaining: int = 0
    minute_reset: datetime = field(default_factory=datetime.utcnow)
    hour_reset: datetime = field(default_factory=datetime.utcnow)
    day_reset: datetime = field(default_factory=datetime.utcnow)
    is_limited: bool = False
    retry_after: int = 0  # seconds


@dataclass
class RequestWindow:
    """Sliding window for tracking requests"""
    timestamps: List[datetime] = field(default_factory=list)
    concurrent_count: int = 0

    def add_request(self, timestamp: datetime) -> None:
        """Add a request timestamp"""
        self.timestamps.append(timestamp)
        self.concurrent_count += 1

    def complete_request(self) -> None:
        """Mark a request as complete (decrease concurrent count)"""
        if self.concurrent_count > 0:
            self.concurrent_count -= 1

    def count_since(self, since: datetime) -> int:
        """Count requests since a given time"""
        return sum(1 for ts in self.timestamps if ts >= since)

    def cleanup_old(self, cutoff: datetime) -> None:
        """Remove timestamps older than the cutoff"""
        self.timestamps = [ts for ts in self.timestamps if ts >= cutoff]


class RateLimiter:
    """
    In-memory rate limiter with sliding window algorithm.

    Tracks requests per user/API key in memory for fast access.
    Configuration is stored in the database.
    """

    # Default limits when no configuration exists
    DEFAULT_CONFIG = RateLimitConfig(
        requests_per_minute=20,
        requests_per_hour=200,
        requests_per_day=1000,
        concurrent_requests=3,
        priority=0,
        is_unlimited=False
    )

    def __init__(self):
        # In-memory request windows: key -> RequestWindow
        # Key format: "user:{user_id}" or "api:{api_key_id}"
        self._windows: Dict[str, RequestWindow] = defaultdict(RequestWindow)

        # Cache for rate limit configurations
        self._config_cache: Dict[str, Tuple[RateLimitConfig, datetime]] = {}
        self._config_cache_ttl = timedelta(minutes=5)

        # Lock for thread safety
        self._lock = asyncio.Lock()

    def _get_key(self, user_id: Optional[str], api_key_id: Optional[str]) -> str:
        """Generate a unique key for the user/API key"""
        if api_key_id:
            return f"api:{api_key_id}"
        elif user_id:
            return f"user:{user_id}"
        else:
            return "admin:default"

    def _get_cached_config(self, key: str) -> Optional[RateLimitConfig]:
        """Get cached configuration if still valid"""
        if key in self._config_cache:
            config, cached_at = self._config_cache[key]
            if datetime.utcnow() - cached_at < self._config_cache_ttl:
                return config
        return None

    def _cache_config(self, key: str, config: RateLimitConfig) -> None:
        """Cache a configuration"""
        self._config_cache[key] = (config, datetime.utcnow())

    def get_limit_config(
        self,
        user_id: Optional[str],
        api_key_id: Optional[str]
    ) -> RateLimitConfig:
        """
        Get the rate limit configuration for a user/API key.

        Priority:
        1. API key specific config
        2. User specific config
        3. Default config from database
        4. Built-in defaults
        """
        key = self._get_key(user_id, api_key_id)

        # Check cache first
        cached = self._get_cached_config(key)
        if cached:
            return cached

        # Load from database
        db_config = database.get_rate_limit_for_user(user_id, api_key_id)

        if db_config:
            config = RateLimitConfig(
                requests_per_minute=db_config.get("requests_per_minute", 20),
                requests_per_hour=db_config.get("requests_per_hour", 200),
                requests_per_day=db_config.get("requests_per_day", 1000),
                concurrent_requests=db_config.get("concurrent_requests", 3),
                priority=db_config.get("priority", 0),
                is_unlimited=db_config.get("is_unlimited", False)
            )
        else:
            config = self.DEFAULT_CONFIG

        self._cache_config(key, config)
        return config

    async def check_rate_limit(
        self,
        user_id: Optional[str],
        api_key_id: Optional[str],
        is_admin: bool = False
    ) -> Tuple[RateLimitResult, RateLimitStatus]:
        """
        Check if a request is allowed under rate limits.

        Args:
            user_id: The user ID (None for anonymous)
            api_key_id: The API key ID if using API key auth
            is_admin: True if the user is an admin (bypasses limits by default)

        Returns:
            Tuple of (result, status)
        """
        # Admin users bypass rate limits by default
        if is_admin and not api_key_id:
            return (RateLimitResult.ALLOWED, RateLimitStatus(is_limited=False))

        key = self._get_key(user_id, api_key_id)
        config = self.get_limit_config(user_id, api_key_id)

        # Unlimited users bypass all limits
        if config.is_unlimited:
            return (RateLimitResult.ALLOWED, RateLimitStatus(is_limited=False))

        async with self._lock:
            now = datetime.utcnow()
            window = self._windows[key]

            # Clean up old timestamps (keep last 24 hours)
            window.cleanup_old(now - timedelta(hours=24))

            # Calculate counts for each window
            minute_count = window.count_since(now - timedelta(minutes=1))
            hour_count = window.count_since(now - timedelta(hours=1))
            day_count = window.count_since(now - timedelta(hours=24))

            # Calculate reset times
            minute_reset = now + timedelta(minutes=1)
            hour_reset = now + timedelta(hours=1)
            day_reset = now + timedelta(hours=24)

            # Check concurrent limit
            concurrent_exceeded = window.concurrent_count >= config.concurrent_requests

            # Check rate limits
            minute_exceeded = minute_count >= config.requests_per_minute
            hour_exceeded = hour_count >= config.requests_per_hour
            day_exceeded = day_count >= config.requests_per_day

            is_limited = concurrent_exceeded or minute_exceeded or hour_exceeded or day_exceeded

            # Calculate retry after
            retry_after = 0
            if minute_exceeded:
                retry_after = 60
            elif hour_exceeded:
                retry_after = 3600
            elif day_exceeded:
                retry_after = 86400
            elif concurrent_exceeded:
                retry_after = 5  # Short retry for concurrent limits

            status = RateLimitStatus(
                minute_count=minute_count,
                hour_count=hour_count,
                day_count=day_count,
                concurrent_count=window.concurrent_count,
                minute_remaining=max(0, config.requests_per_minute - minute_count),
                hour_remaining=max(0, config.requests_per_hour - hour_count),
                day_remaining=max(0, config.requests_per_day - day_count),
                concurrent_remaining=max(0, config.concurrent_requests - window.concurrent_count),
                minute_reset=minute_reset,
                hour_reset=hour_reset,
                day_reset=day_reset,
                is_limited=is_limited,
                retry_after=retry_after
            )

            if is_limited:
                return (RateLimitResult.DENIED, status)

            return (RateLimitResult.ALLOWED, status)

    async def record_request(
        self,
        user_id: Optional[str],
        api_key_id: Optional[str],
        endpoint: str,
        is_admin: bool = False
    ) -> str:
        """
        Record a request for rate limiting.

        Args:
            user_id: The user ID
            api_key_id: The API key ID if using API key auth
            endpoint: The endpoint being called
            is_admin: True if the user is an admin

        Returns:
            Request ID for tracking
        """
        request_id = str(uuid.uuid4())
        key = self._get_key(user_id, api_key_id)

        async with self._lock:
            now = datetime.utcnow()
            self._windows[key].add_request(now)

        # Log to database for persistence (fire and forget)
        try:
            database.log_request(
                request_id=request_id,
                user_id=user_id,
                api_key_id=api_key_id,
                endpoint=endpoint,
                status="success"
            )
        except Exception as e:
            logger.warning(f"Failed to log request to database: {e}")

        return request_id

    async def complete_request(
        self,
        user_id: Optional[str],
        api_key_id: Optional[str],
        request_id: str,
        duration_ms: Optional[int] = None
    ) -> None:
        """
        Mark a request as complete (decreases concurrent count).

        Args:
            user_id: The user ID
            api_key_id: The API key ID
            request_id: The request ID from record_request
            duration_ms: Optional duration in milliseconds
        """
        key = self._get_key(user_id, api_key_id)

        async with self._lock:
            self._windows[key].complete_request()

    def get_rate_limit_status(
        self,
        user_id: Optional[str],
        api_key_id: Optional[str],
        is_admin: bool = False
    ) -> RateLimitStatus:
        """
        Get current rate limit status without checking/recording.

        Synchronous version for use in headers.
        """
        if is_admin and not api_key_id:
            return RateLimitStatus(is_limited=False)

        key = self._get_key(user_id, api_key_id)
        config = self.get_limit_config(user_id, api_key_id)

        if config.is_unlimited:
            return RateLimitStatus(is_limited=False)

        now = datetime.utcnow()
        window = self._windows.get(key, RequestWindow())

        minute_count = window.count_since(now - timedelta(minutes=1))
        hour_count = window.count_since(now - timedelta(hours=1))
        day_count = window.count_since(now - timedelta(hours=24))

        return RateLimitStatus(
            minute_count=minute_count,
            hour_count=hour_count,
            day_count=day_count,
            concurrent_count=window.concurrent_count,
            minute_remaining=max(0, config.requests_per_minute - minute_count),
            hour_remaining=max(0, config.requests_per_hour - hour_count),
            day_remaining=max(0, config.requests_per_day - day_count),
            concurrent_remaining=max(0, config.concurrent_requests - window.concurrent_count),
            minute_reset=now + timedelta(minutes=1),
            hour_reset=now + timedelta(hours=1),
            day_reset=now + timedelta(hours=24),
            is_limited=False
        )

    def clear_cache(self) -> None:
        """Clear the configuration cache"""
        self._config_cache.clear()

    def cleanup(self) -> None:
        """Clean up old data from memory"""
        cutoff = datetime.utcnow() - timedelta(hours=24)
        for window in self._windows.values():
            window.cleanup_old(cutoff)

        # Also clean up old request logs from database
        try:
            deleted = database.cleanup_old_request_logs(cutoff)
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} old request logs")
        except Exception as e:
            logger.warning(f"Failed to cleanup old request logs: {e}")


# Global rate limiter instance
rate_limiter = RateLimiter()
