"""
Request queue manager for AI Hub.

Manages a priority queue for requests that exceed rate limits.
Higher priority users are processed first.
"""

import asyncio
import heapq
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class QueuedRequest:
    """A request waiting in the queue"""
    id: str
    user_id: Optional[str]
    api_key_id: Optional[str]
    priority: int  # Higher = processed first
    created_at: datetime
    request_data: Dict[str, Any]
    callback: Optional[Callable] = None
    # For heap comparison (lower = higher priority since heapq is min-heap)
    _sort_key: Tuple[int, datetime] = field(init=False, repr=False)

    def __post_init__(self):
        # Negative priority so higher priority is popped first
        self._sort_key = (-self.priority, self.created_at)

    def __lt__(self, other: "QueuedRequest") -> bool:
        return self._sort_key < other._sort_key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, QueuedRequest):
            return False
        return self.id == other.id


@dataclass
class QueueStatus:
    """Status of a user's position in the queue"""
    position: int
    estimated_wait_seconds: int
    total_queued: int
    is_queued: bool


class RequestQueue:
    """
    Priority queue for rate-limited requests.

    Features:
    - Priority-based ordering (higher priority users first)
    - FIFO within same priority level
    - Maximum queue size limit
    - Estimated wait time calculation
    """

    DEFAULT_MAX_SIZE = 100
    DEFAULT_ESTIMATED_PROCESS_TIME = 30  # seconds per request

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE):
        self._queue: List[QueuedRequest] = []  # heapq
        self._user_requests: Dict[str, str] = {}  # user_key -> request_id
        self._request_map: Dict[str, QueuedRequest] = {}  # request_id -> request
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._process_time_estimate = self.DEFAULT_ESTIMATED_PROCESS_TIME

    def _get_user_key(self, user_id: Optional[str], api_key_id: Optional[str]) -> str:
        """Generate a unique key for the user/API key"""
        if api_key_id:
            return f"api:{api_key_id}"
        elif user_id:
            return f"user:{user_id}"
        else:
            return f"anon:{uuid.uuid4()}"

    async def enqueue(
        self,
        user_id: Optional[str],
        api_key_id: Optional[str],
        priority: int,
        request_data: Dict[str, Any],
        callback: Optional[Callable] = None
    ) -> Optional[str]:
        """
        Add a request to the queue.

        Args:
            user_id: The user ID
            api_key_id: The API key ID
            priority: Priority level (higher = processed first)
            request_data: The request data to queue
            callback: Optional callback when request is processed

        Returns:
            Request ID if queued, None if queue is full
        """
        async with self._lock:
            # Check queue size limit
            if len(self._queue) >= self._max_size:
                logger.warning(f"Queue full ({self._max_size}), rejecting request")
                return None

            user_key = self._get_user_key(user_id, api_key_id)

            # Check if user already has a request queued
            if user_key in self._user_requests:
                existing_id = self._user_requests[user_key]
                logger.debug(f"User {user_key} already has request {existing_id} queued")
                return existing_id

            # Create queued request
            request_id = str(uuid.uuid4())
            request = QueuedRequest(
                id=request_id,
                user_id=user_id,
                api_key_id=api_key_id,
                priority=priority,
                created_at=datetime.utcnow(),
                request_data=request_data,
                callback=callback
            )

            # Add to heap
            heapq.heappush(self._queue, request)
            self._user_requests[user_key] = request_id
            self._request_map[request_id] = request

            logger.info(f"Queued request {request_id} for user {user_key} (priority={priority})")
            return request_id

    async def dequeue(self) -> Optional[QueuedRequest]:
        """
        Get the next request from the queue.

        Returns:
            The highest priority request, or None if queue is empty
        """
        async with self._lock:
            if not self._queue:
                return None

            request = heapq.heappop(self._queue)
            user_key = self._get_user_key(request.user_id, request.api_key_id)

            # Clean up tracking
            if user_key in self._user_requests:
                del self._user_requests[user_key]
            if request.id in self._request_map:
                del self._request_map[request.id]

            logger.info(f"Dequeued request {request.id} for user {user_key}")
            return request

    async def remove(self, request_id: str) -> bool:
        """
        Remove a specific request from the queue.

        Args:
            request_id: The request ID to remove

        Returns:
            True if removed, False if not found
        """
        async with self._lock:
            if request_id not in self._request_map:
                return False

            request = self._request_map[request_id]
            user_key = self._get_user_key(request.user_id, request.api_key_id)

            # Remove from heap (inefficient but necessary)
            self._queue = [r for r in self._queue if r.id != request_id]
            heapq.heapify(self._queue)

            # Clean up tracking
            if user_key in self._user_requests:
                del self._user_requests[user_key]
            del self._request_map[request_id]

            logger.info(f"Removed request {request_id} from queue")
            return True

    async def get_position(
        self,
        user_id: Optional[str],
        api_key_id: Optional[str]
    ) -> QueueStatus:
        """
        Get the queue position for a user.

        Args:
            user_id: The user ID
            api_key_id: The API key ID

        Returns:
            QueueStatus with position and estimated wait
        """
        async with self._lock:
            user_key = self._get_user_key(user_id, api_key_id)

            if user_key not in self._user_requests:
                return QueueStatus(
                    position=0,
                    estimated_wait_seconds=0,
                    total_queued=len(self._queue),
                    is_queued=False
                )

            request_id = self._user_requests[user_key]
            request = self._request_map.get(request_id)

            if not request:
                return QueueStatus(
                    position=0,
                    estimated_wait_seconds=0,
                    total_queued=len(self._queue),
                    is_queued=False
                )

            # Calculate position (count requests ahead)
            position = 1
            for queued in self._queue:
                if queued.id == request_id:
                    break
                if queued._sort_key < request._sort_key:
                    position += 1

            estimated_wait = position * self._process_time_estimate

            return QueueStatus(
                position=position,
                estimated_wait_seconds=estimated_wait,
                total_queued=len(self._queue),
                is_queued=True
            )

    async def get_queue_size(self) -> int:
        """Get the current queue size"""
        async with self._lock:
            return len(self._queue)

    async def is_user_queued(
        self,
        user_id: Optional[str],
        api_key_id: Optional[str]
    ) -> bool:
        """Check if a user has a request in the queue"""
        user_key = self._get_user_key(user_id, api_key_id)
        return user_key in self._user_requests

    async def clear(self) -> int:
        """Clear all requests from the queue. Returns count of cleared requests."""
        async with self._lock:
            count = len(self._queue)
            self._queue.clear()
            self._user_requests.clear()
            self._request_map.clear()
            logger.info(f"Cleared {count} requests from queue")
            return count

    def set_max_size(self, max_size: int) -> None:
        """Update the maximum queue size"""
        self._max_size = max_size

    def set_process_time_estimate(self, seconds: int) -> None:
        """Update the estimated processing time per request"""
        self._process_time_estimate = seconds


# Global queue manager instance
request_queue = RequestQueue()
