"""
Unit tests for queue manager module.

Tests cover:
- QueuedRequest dataclass behavior and comparison
- QueueStatus dataclass
- RequestQueue operations (enqueue, dequeue, remove)
- Priority handling (higher priority first, FIFO within priority)
- Queue size limits and full queue rejection
- User deduplication (one request per user)
- Position tracking and wait time estimation
- Concurrent access with asyncio locks
- Queue clearing and configuration
- Error handling edge cases
"""

import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from typing import Callable

from app.core.queue_manager import (
    QueuedRequest,
    QueueStatus,
    RequestQueue,
    request_queue,
)


class TestQueuedRequest:
    """Test QueuedRequest dataclass."""

    def test_create_queued_request(self):
        """Should create a queued request with all fields."""
        now = datetime.utcnow()
        request = QueuedRequest(
            id="req-123",
            user_id="user-456",
            api_key_id=None,
            priority=5,
            created_at=now,
            request_data={"model": "claude-3", "prompt": "test"},
        )

        assert request.id == "req-123"
        assert request.user_id == "user-456"
        assert request.api_key_id is None
        assert request.priority == 5
        assert request.created_at == now
        assert request.request_data == {"model": "claude-3", "prompt": "test"}
        assert request.callback is None

    def test_create_queued_request_with_callback(self):
        """Should accept optional callback."""
        def my_callback():
            pass

        request = QueuedRequest(
            id="req-123",
            user_id="user-456",
            api_key_id=None,
            priority=5,
            created_at=datetime.utcnow(),
            request_data={},
            callback=my_callback,
        )

        assert request.callback == my_callback

    def test_sort_key_computed_in_post_init(self):
        """Sort key should be computed automatically."""
        now = datetime.utcnow()
        request = QueuedRequest(
            id="req-123",
            user_id="user-456",
            api_key_id=None,
            priority=5,
            created_at=now,
            request_data={},
        )

        # Negative priority for min-heap, plus created_at for FIFO
        assert request._sort_key == (-5, now)

    def test_lt_comparison_higher_priority_first(self):
        """Higher priority requests should compare as 'less than'."""
        now = datetime.utcnow()
        high_priority = QueuedRequest(
            id="high", user_id="u1", api_key_id=None,
            priority=10, created_at=now, request_data={}
        )
        low_priority = QueuedRequest(
            id="low", user_id="u2", api_key_id=None,
            priority=1, created_at=now, request_data={}
        )

        # High priority should be "less than" low priority (for min-heap)
        assert high_priority < low_priority
        assert not (low_priority < high_priority)

    def test_lt_comparison_fifo_for_same_priority(self):
        """Earlier requests should compare as 'less than' for same priority."""
        earlier = datetime.utcnow()
        later = earlier + timedelta(seconds=10)

        first_request = QueuedRequest(
            id="first", user_id="u1", api_key_id=None,
            priority=5, created_at=earlier, request_data={}
        )
        second_request = QueuedRequest(
            id="second", user_id="u2", api_key_id=None,
            priority=5, created_at=later, request_data={}
        )

        assert first_request < second_request
        assert not (second_request < first_request)

    def test_eq_comparison_by_id(self):
        """Equality should be based on request ID."""
        request1 = QueuedRequest(
            id="same-id", user_id="u1", api_key_id=None,
            priority=5, created_at=datetime.utcnow(), request_data={}
        )
        request2 = QueuedRequest(
            id="same-id", user_id="u2", api_key_id=None,
            priority=10, created_at=datetime.utcnow(), request_data={}
        )
        request3 = QueuedRequest(
            id="different-id", user_id="u1", api_key_id=None,
            priority=5, created_at=datetime.utcnow(), request_data={}
        )

        assert request1 == request2
        assert request1 != request3

    def test_eq_comparison_with_non_request(self):
        """Equality with non-QueuedRequest should return False."""
        request = QueuedRequest(
            id="req-123", user_id="u1", api_key_id=None,
            priority=5, created_at=datetime.utcnow(), request_data={}
        )

        assert request != "not a request"
        assert request != 123
        assert request != None


class TestQueueStatus:
    """Test QueueStatus dataclass."""

    def test_create_queue_status(self):
        """Should create queue status with all fields."""
        status = QueueStatus(
            position=3,
            estimated_wait_seconds=90,
            total_queued=10,
            is_queued=True,
        )

        assert status.position == 3
        assert status.estimated_wait_seconds == 90
        assert status.total_queued == 10
        assert status.is_queued is True

    def test_not_queued_status(self):
        """Should represent not-queued state."""
        status = QueueStatus(
            position=0,
            estimated_wait_seconds=0,
            total_queued=5,
            is_queued=False,
        )

        assert status.is_queued is False
        assert status.position == 0


class TestRequestQueueBasics:
    """Test basic RequestQueue initialization and configuration."""

    def test_default_initialization(self):
        """Should initialize with default values."""
        queue = RequestQueue()

        assert queue._max_size == RequestQueue.DEFAULT_MAX_SIZE
        assert queue._process_time_estimate == RequestQueue.DEFAULT_ESTIMATED_PROCESS_TIME
        assert queue._queue == []
        assert queue._user_requests == {}
        assert queue._request_map == {}

    def test_custom_max_size(self):
        """Should accept custom max size."""
        queue = RequestQueue(max_size=50)

        assert queue._max_size == 50

    def test_set_max_size(self):
        """Should update max size."""
        queue = RequestQueue()
        queue.set_max_size(200)

        assert queue._max_size == 200

    def test_set_process_time_estimate(self):
        """Should update process time estimate."""
        queue = RequestQueue()
        queue.set_process_time_estimate(60)

        assert queue._process_time_estimate == 60


class TestUserKeyGeneration:
    """Test user key generation for tracking."""

    def test_get_user_key_with_api_key(self):
        """API key should take precedence."""
        queue = RequestQueue()
        key = queue._get_user_key(user_id="user-123", api_key_id="api-456")

        assert key == "api:api-456"

    def test_get_user_key_with_user_id_only(self):
        """Should use user ID when no API key."""
        queue = RequestQueue()
        key = queue._get_user_key(user_id="user-123", api_key_id=None)

        assert key == "user:user-123"

    def test_get_user_key_anonymous(self):
        """Should generate unique key for anonymous users."""
        queue = RequestQueue()
        key1 = queue._get_user_key(user_id=None, api_key_id=None)
        key2 = queue._get_user_key(user_id=None, api_key_id=None)

        assert key1.startswith("anon:")
        assert key2.startswith("anon:")
        # Anonymous keys should be unique
        assert key1 != key2


class TestEnqueue:
    """Test enqueue operation."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=10)

    @pytest.mark.asyncio
    async def test_enqueue_returns_request_id(self, queue):
        """Enqueue should return the request ID."""
        request_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={"prompt": "test"},
        )

        assert request_id is not None
        assert isinstance(request_id, str)

    @pytest.mark.asyncio
    async def test_enqueue_adds_to_queue(self, queue):
        """Enqueue should add request to internal queue."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={"prompt": "test"},
        )

        assert len(queue._queue) == 1
        assert len(queue._request_map) == 1
        assert "user:user-123" in queue._user_requests

    @pytest.mark.asyncio
    async def test_enqueue_stores_request_data(self, queue):
        """Enqueue should store all request data."""
        request_data = {"model": "claude-3", "prompt": "hello"}
        request_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data=request_data,
        )

        stored_request = queue._request_map[request_id]
        assert stored_request.request_data == request_data
        assert stored_request.user_id == "user-123"
        assert stored_request.priority == 5

    @pytest.mark.asyncio
    async def test_enqueue_with_callback(self, queue):
        """Enqueue should store callback function."""
        async def my_callback():
            pass

        request_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
            callback=my_callback,
        )

        stored_request = queue._request_map[request_id]
        assert stored_request.callback == my_callback

    @pytest.mark.asyncio
    async def test_enqueue_duplicate_user_returns_existing_id(self, queue):
        """Second enqueue for same user should return existing request ID."""
        first_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={"first": True},
        )
        second_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=10,  # Different priority
            request_data={"second": True},  # Different data
        )

        assert second_id == first_id
        assert len(queue._queue) == 1  # Still only one in queue

    @pytest.mark.asyncio
    async def test_enqueue_full_queue_returns_none(self, queue):
        """Enqueue should return None when queue is full."""
        # Fill the queue
        for i in range(10):
            await queue.enqueue(
                user_id=f"user-{i}",
                api_key_id=None,
                priority=5,
                request_data={},
            )

        # Try to add one more
        result = await queue.enqueue(
            user_id="user-overflow",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        assert result is None
        assert len(queue._queue) == 10

    @pytest.mark.asyncio
    async def test_enqueue_with_api_key(self, queue):
        """Enqueue should track by API key when provided."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id="api-456",
            priority=5,
            request_data={},
        )

        assert "api:api-456" in queue._user_requests
        assert "user:user-123" not in queue._user_requests


class TestDequeue:
    """Test dequeue operation."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=10)

    @pytest.mark.asyncio
    async def test_dequeue_empty_queue_returns_none(self, queue):
        """Dequeue from empty queue should return None."""
        result = await queue.dequeue()

        assert result is None

    @pytest.mark.asyncio
    async def test_dequeue_returns_request(self, queue):
        """Dequeue should return the request."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={"prompt": "test"},
        )

        result = await queue.dequeue()

        assert result is not None
        assert result.user_id == "user-123"
        assert result.request_data == {"prompt": "test"}

    @pytest.mark.asyncio
    async def test_dequeue_removes_from_queue(self, queue):
        """Dequeue should remove request from internal structures."""
        request_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        await queue.dequeue()

        assert len(queue._queue) == 0
        assert request_id not in queue._request_map
        assert "user:user-123" not in queue._user_requests

    @pytest.mark.asyncio
    async def test_dequeue_respects_priority_order(self, queue):
        """Higher priority requests should be dequeued first."""
        await queue.enqueue(user_id="low", api_key_id=None, priority=1, request_data={})
        await queue.enqueue(user_id="high", api_key_id=None, priority=10, request_data={})
        await queue.enqueue(user_id="medium", api_key_id=None, priority=5, request_data={})

        first = await queue.dequeue()
        second = await queue.dequeue()
        third = await queue.dequeue()

        assert first.user_id == "high"
        assert second.user_id == "medium"
        assert third.user_id == "low"

    @pytest.mark.asyncio
    async def test_dequeue_fifo_within_same_priority(self, queue):
        """Requests with same priority should be FIFO."""
        await queue.enqueue(user_id="first", api_key_id=None, priority=5, request_data={})
        await asyncio.sleep(0.01)  # Ensure different timestamps
        await queue.enqueue(user_id="second", api_key_id=None, priority=5, request_data={})
        await asyncio.sleep(0.01)
        await queue.enqueue(user_id="third", api_key_id=None, priority=5, request_data={})

        first = await queue.dequeue()
        second = await queue.dequeue()
        third = await queue.dequeue()

        assert first.user_id == "first"
        assert second.user_id == "second"
        assert third.user_id == "third"


class TestRemove:
    """Test remove operation."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=10)

    @pytest.mark.asyncio
    async def test_remove_existing_request(self, queue):
        """Remove should return True for existing request."""
        request_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        result = await queue.remove(request_id)

        assert result is True
        assert len(queue._queue) == 0
        assert request_id not in queue._request_map

    @pytest.mark.asyncio
    async def test_remove_nonexistent_request(self, queue):
        """Remove should return False for non-existent request."""
        result = await queue.remove("nonexistent-id")

        assert result is False

    @pytest.mark.asyncio
    async def test_remove_cleans_up_user_tracking(self, queue):
        """Remove should clean up user tracking."""
        request_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        await queue.remove(request_id)

        assert "user:user-123" not in queue._user_requests

    @pytest.mark.asyncio
    async def test_remove_middle_request_maintains_heap(self, queue):
        """Removing from middle should maintain heap property."""
        await queue.enqueue(user_id="u1", api_key_id=None, priority=1, request_data={})
        middle_id = await queue.enqueue(user_id="u2", api_key_id=None, priority=5, request_data={})
        await queue.enqueue(user_id="u3", api_key_id=None, priority=10, request_data={})

        await queue.remove(middle_id)

        # Should still dequeue in priority order
        first = await queue.dequeue()
        second = await queue.dequeue()

        assert first.user_id == "u3"  # priority 10
        assert second.user_id == "u1"  # priority 1


class TestGetPosition:
    """Test position tracking."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=10)

    @pytest.mark.asyncio
    async def test_get_position_not_queued(self, queue):
        """Should return not-queued status for users not in queue."""
        status = await queue.get_position(user_id="user-123", api_key_id=None)

        assert status.is_queued is False
        assert status.position == 0
        assert status.estimated_wait_seconds == 0

    @pytest.mark.asyncio
    async def test_get_position_single_request(self, queue):
        """Single request should be in position 1."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        status = await queue.get_position(user_id="user-123", api_key_id=None)

        assert status.is_queued is True
        assert status.position == 1
        assert status.total_queued == 1

    @pytest.mark.asyncio
    async def test_get_position_estimated_wait(self, queue):
        """Estimated wait should be position * process time."""
        queue.set_process_time_estimate(30)

        await queue.enqueue(user_id="u1", api_key_id=None, priority=10, request_data={})
        await queue.enqueue(user_id="u2", api_key_id=None, priority=5, request_data={})

        status = await queue.get_position(user_id="u2", api_key_id=None)

        # u2 is behind u1 (lower priority), so position 2
        assert status.position == 2
        assert status.estimated_wait_seconds == 60  # 2 * 30

    @pytest.mark.asyncio
    async def test_get_position_respects_priority(self, queue):
        """Position should account for priority ordering."""
        # Add lower priority first
        await queue.enqueue(user_id="low", api_key_id=None, priority=1, request_data={})
        # Add higher priority second
        await queue.enqueue(user_id="high", api_key_id=None, priority=10, request_data={})

        low_status = await queue.get_position(user_id="low", api_key_id=None)
        high_status = await queue.get_position(user_id="high", api_key_id=None)

        # High priority should be ahead despite being added later
        assert high_status.position == 1
        assert low_status.position == 2

    @pytest.mark.asyncio
    async def test_get_position_with_missing_request_map(self, queue):
        """Should handle edge case where request is in user_requests but not request_map."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        # Simulate inconsistent state by clearing request_map
        request_id = queue._user_requests["user:user-123"]
        del queue._request_map[request_id]

        status = await queue.get_position(user_id="user-123", api_key_id=None)

        assert status.is_queued is False


class TestQueueSize:
    """Test queue size operations."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=10)

    @pytest.mark.asyncio
    async def test_get_queue_size_empty(self, queue):
        """Empty queue should return size 0."""
        size = await queue.get_queue_size()

        assert size == 0

    @pytest.mark.asyncio
    async def test_get_queue_size_with_items(self, queue):
        """Should return correct count of items."""
        await queue.enqueue(user_id="u1", api_key_id=None, priority=5, request_data={})
        await queue.enqueue(user_id="u2", api_key_id=None, priority=5, request_data={})
        await queue.enqueue(user_id="u3", api_key_id=None, priority=5, request_data={})

        size = await queue.get_queue_size()

        assert size == 3


class TestIsUserQueued:
    """Test user queue status check."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=10)

    @pytest.mark.asyncio
    async def test_is_user_queued_false_initially(self, queue):
        """User not in queue should return False."""
        result = await queue.is_user_queued(user_id="user-123", api_key_id=None)

        assert result is False

    @pytest.mark.asyncio
    async def test_is_user_queued_true_after_enqueue(self, queue):
        """User in queue should return True."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        result = await queue.is_user_queued(user_id="user-123", api_key_id=None)

        assert result is True

    @pytest.mark.asyncio
    async def test_is_user_queued_false_after_dequeue(self, queue):
        """User should not be queued after their request is dequeued."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )
        await queue.dequeue()

        result = await queue.is_user_queued(user_id="user-123", api_key_id=None)

        assert result is False


class TestClear:
    """Test queue clearing."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=10)

    @pytest.mark.asyncio
    async def test_clear_empty_queue(self, queue):
        """Clearing empty queue should return 0."""
        count = await queue.clear()

        assert count == 0

    @pytest.mark.asyncio
    async def test_clear_returns_count(self, queue):
        """Clear should return count of removed items."""
        await queue.enqueue(user_id="u1", api_key_id=None, priority=5, request_data={})
        await queue.enqueue(user_id="u2", api_key_id=None, priority=5, request_data={})
        await queue.enqueue(user_id="u3", api_key_id=None, priority=5, request_data={})

        count = await queue.clear()

        assert count == 3

    @pytest.mark.asyncio
    async def test_clear_removes_all_tracking(self, queue):
        """Clear should remove all internal tracking."""
        await queue.enqueue(user_id="u1", api_key_id=None, priority=5, request_data={})
        await queue.enqueue(user_id="u2", api_key_id=None, priority=5, request_data={})

        await queue.clear()

        assert len(queue._queue) == 0
        assert len(queue._user_requests) == 0
        assert len(queue._request_map) == 0


class TestConcurrentAccess:
    """Test concurrent access to the queue."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=100)

    @pytest.mark.asyncio
    async def test_concurrent_enqueue(self, queue):
        """Multiple concurrent enqueues should be safe."""
        async def enqueue_task(user_id: str):
            return await queue.enqueue(
                user_id=user_id,
                api_key_id=None,
                priority=5,
                request_data={"user": user_id},
            )

        # Enqueue 20 requests concurrently
        tasks = [enqueue_task(f"user-{i}") for i in range(20)]
        results = await asyncio.gather(*tasks)

        # All should succeed
        assert all(r is not None for r in results)
        assert len(queue._queue) == 20

    @pytest.mark.asyncio
    async def test_concurrent_dequeue(self, queue):
        """Multiple concurrent dequeues should be safe."""
        # Enqueue some requests first
        for i in range(10):
            await queue.enqueue(
                user_id=f"user-{i}",
                api_key_id=None,
                priority=5,
                request_data={},
            )

        # Dequeue all concurrently
        tasks = [queue.dequeue() for _ in range(15)]  # More than available
        results = await asyncio.gather(*tasks)

        # 10 should return requests, 5 should return None
        non_none = [r for r in results if r is not None]
        assert len(non_none) == 10
        assert len(queue._queue) == 0

    @pytest.mark.asyncio
    async def test_concurrent_mixed_operations(self, queue):
        """Mixed concurrent operations should be safe."""
        async def enqueue_task(user_id: str):
            return await queue.enqueue(
                user_id=user_id,
                api_key_id=None,
                priority=5,
                request_data={},
            )

        async def dequeue_task():
            return await queue.dequeue()

        # Mix of enqueue and dequeue operations
        tasks = []
        for i in range(10):
            tasks.append(enqueue_task(f"user-{i}"))
            tasks.append(dequeue_task())

        # Should complete without errors
        await asyncio.gather(*tasks)


class TestGlobalQueueInstance:
    """Test the global request_queue instance."""

    def test_global_instance_exists(self):
        """Global request_queue should exist."""
        assert request_queue is not None
        assert isinstance(request_queue, RequestQueue)

    def test_global_instance_has_default_config(self):
        """Global instance should have default configuration."""
        assert request_queue._max_size == RequestQueue.DEFAULT_MAX_SIZE


class TestLogging:
    """Test logging behavior."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=10)

    @pytest.mark.asyncio
    async def test_enqueue_logs_info(self, queue):
        """Enqueue should log info message."""
        with patch("app.core.queue_manager.logger") as mock_logger:
            await queue.enqueue(
                user_id="user-123",
                api_key_id=None,
                priority=5,
                request_data={},
            )

            mock_logger.info.assert_called()
            call_args = str(mock_logger.info.call_args)
            assert "Queued request" in call_args
            assert "user:user-123" in call_args

    @pytest.mark.asyncio
    async def test_enqueue_full_queue_logs_warning(self, queue):
        """Full queue rejection should log warning."""
        # Fill the queue
        for i in range(10):
            await queue.enqueue(
                user_id=f"user-{i}",
                api_key_id=None,
                priority=5,
                request_data={},
            )

        with patch("app.core.queue_manager.logger") as mock_logger:
            await queue.enqueue(
                user_id="overflow",
                api_key_id=None,
                priority=5,
                request_data={},
            )

            mock_logger.warning.assert_called()
            call_args = str(mock_logger.warning.call_args)
            assert "Queue full" in call_args

    @pytest.mark.asyncio
    async def test_dequeue_logs_info(self, queue):
        """Dequeue should log info message."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        with patch("app.core.queue_manager.logger") as mock_logger:
            await queue.dequeue()

            mock_logger.info.assert_called()
            call_args = str(mock_logger.info.call_args)
            assert "Dequeued request" in call_args

    @pytest.mark.asyncio
    async def test_remove_logs_info(self, queue):
        """Remove should log info message."""
        request_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        with patch("app.core.queue_manager.logger") as mock_logger:
            await queue.remove(request_id)

            mock_logger.info.assert_called()
            call_args = str(mock_logger.info.call_args)
            assert "Removed request" in call_args

    @pytest.mark.asyncio
    async def test_clear_logs_info(self, queue):
        """Clear should log info message."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        with patch("app.core.queue_manager.logger") as mock_logger:
            await queue.clear()

            mock_logger.info.assert_called()
            call_args = str(mock_logger.info.call_args)
            assert "Cleared" in call_args

    @pytest.mark.asyncio
    async def test_duplicate_enqueue_logs_debug(self, queue):
        """Duplicate enqueue should log debug message."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        with patch("app.core.queue_manager.logger") as mock_logger:
            await queue.enqueue(
                user_id="user-123",
                api_key_id=None,
                priority=5,
                request_data={},
            )

            mock_logger.debug.assert_called()
            call_args = str(mock_logger.debug.call_args)
            assert "already has request" in call_args


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.fixture
    def queue(self):
        """Create a fresh queue for each test."""
        return RequestQueue(max_size=10)

    @pytest.mark.asyncio
    async def test_dequeue_cleans_up_even_if_not_in_user_requests(self, queue):
        """Dequeue should handle case where user_key not in user_requests."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        # Manually remove from user_requests to simulate edge case
        del queue._user_requests["user:user-123"]

        # Should not raise error
        result = await queue.dequeue()
        assert result is not None

    @pytest.mark.asyncio
    async def test_dequeue_cleans_up_even_if_not_in_request_map(self, queue):
        """Dequeue should handle case where request_id not in request_map."""
        await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        # Get the request from queue but remove from request_map
        request = queue._queue[0]
        del queue._request_map[request.id]

        # Should not raise error
        result = await queue.dequeue()
        assert result is not None

    @pytest.mark.asyncio
    async def test_empty_request_data(self, queue):
        """Should handle empty request data."""
        request_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        result = await queue.dequeue()
        assert result.request_data == {}

    @pytest.mark.asyncio
    async def test_large_request_data(self, queue):
        """Should handle large request data."""
        large_data = {"prompt": "x" * 10000, "items": list(range(1000))}
        request_id = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data=large_data,
        )

        result = await queue.dequeue()
        assert result.request_data == large_data

    @pytest.mark.asyncio
    async def test_negative_priority(self, queue):
        """Should handle negative priority values."""
        await queue.enqueue(user_id="u1", api_key_id=None, priority=-5, request_data={})
        await queue.enqueue(user_id="u2", api_key_id=None, priority=-10, request_data={})
        await queue.enqueue(user_id="u3", api_key_id=None, priority=0, request_data={})

        first = await queue.dequeue()
        second = await queue.dequeue()
        third = await queue.dequeue()

        assert first.user_id == "u3"  # priority 0 (highest)
        assert second.user_id == "u1"  # priority -5
        assert third.user_id == "u2"  # priority -10 (lowest)

    @pytest.mark.asyncio
    async def test_very_high_priority(self, queue):
        """Should handle very high priority values."""
        await queue.enqueue(user_id="normal", api_key_id=None, priority=5, request_data={})
        await queue.enqueue(user_id="vip", api_key_id=None, priority=1000000, request_data={})

        first = await queue.dequeue()
        assert first.user_id == "vip"

    @pytest.mark.asyncio
    async def test_queue_size_zero(self):
        """Should handle queue with max size 0."""
        queue = RequestQueue(max_size=0)

        result = await queue.enqueue(
            user_id="user-123",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_special_characters_in_user_id(self, queue):
        """Should handle special characters in user ID."""
        request_id = await queue.enqueue(
            user_id="user@example.com:with/special#chars",
            api_key_id=None,
            priority=5,
            request_data={},
        )

        assert request_id is not None
        result = await queue.is_user_queued(
            user_id="user@example.com:with/special#chars",
            api_key_id=None,
        )
        assert result is True
