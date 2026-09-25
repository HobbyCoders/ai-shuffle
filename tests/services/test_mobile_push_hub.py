"""
Unit tests for the in-process mobile push hub.

The hub fans notification events out to every connected SSE stream (the Android
app's embedded foreground service), so it must:
- deliver a published event to all current subscribers
- never block on a slow/full consumer (drop-oldest behaviour)
- stop delivering after unsubscribe
"""


from app.services.mobile_push_hub import MobilePushHub, mobile_push_hub


class TestMobilePushHub:
    """Behavioural tests for MobilePushHub."""

    async def test_publish_delivers_to_all_subscribers(self):
        hub = MobilePushHub()
        q1 = hub.subscribe()
        q2 = hub.subscribe()
        assert hub.subscriber_count == 2

        event = {"type": "session_done", "title": "T", "body": "B"}
        await hub.publish(event)

        assert q1.get_nowait() == event
        assert q2.get_nowait() == event

    async def test_unsubscribe_stops_delivery(self):
        hub = MobilePushHub()
        q = hub.subscribe()
        hub.unsubscribe(q)
        assert hub.subscriber_count == 0

        await hub.publish({"type": "x"})
        assert q.empty()

    async def test_unsubscribe_is_idempotent(self):
        hub = MobilePushHub()
        q = hub.subscribe()
        hub.unsubscribe(q)
        # A second call must not raise.
        hub.unsubscribe(q)
        assert hub.subscriber_count == 0

    async def test_full_queue_drops_oldest(self):
        # A tiny queue makes the slow-consumer path deterministic.
        hub = MobilePushHub(max_queue=1)
        q = hub.subscribe()

        await hub.publish({"n": 1})  # fills the queue
        await hub.publish({"n": 2})  # queue full -> drop oldest, keep newest

        assert q.get_nowait() == {"n": 2}
        assert q.empty()

    async def test_publish_with_no_subscribers_is_noop(self):
        hub = MobilePushHub()
        # Must not raise even with nobody listening.
        await hub.publish({"type": "x"})
        assert hub.subscriber_count == 0

    def test_module_singleton_exists(self):
        assert isinstance(mobile_push_hub, MobilePushHub)
