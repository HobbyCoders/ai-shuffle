package com.hobbycoders.aishuffle.push

import org.junit.Assert.*
import org.junit.Test

class AttentionPolicyTest {
    private val data = mapOf("type" to "attention", "worker_id" to "dev_worker", "session_id" to "chat-123",
        "version" to "1", "episode" to "episode-one", "action" to "alert", "expires_at" to "1500",
        "body" to "A chat needs you: Plan")
    private fun command(changes: Map<String, String> = emptyMap()) = AttentionCommand.parse(data + changes, 1000)!!

    @Test fun initialEpisodeAlertsAndRetriesOrReconnectsCannotAlertAgain() {
        val first = AttentionPolicy.apply(AttentionReceipt(), command())!!
        assertTrue(first.show)
        assertTrue(first.alert)
        assertNull(AttentionPolicy.apply(first.receipt, command()))
        val update = AttentionPolicy.apply(first.receipt, command(mapOf("version" to "2", "action" to "update")))!!
        assertTrue(update.show)
        assertFalse(update.alert)
        assertNull(AttentionPolicy.apply(update.receipt, command()))
    }
    @Test fun aCollapsedInitialAlertStillAlertsExactlyOnce() {
        // FCM may collapse the initial alert into a later update for that episode.
        val first = AttentionPolicy.apply(AttentionReceipt(), command(mapOf("version" to "2", "action" to "update")))!!
        assertTrue(first.alert)
        assertNull(AttentionPolicy.apply(first.receipt, command()))
    }
    @Test fun clearWinsOverDelayedMessagesAndNextEpisodeMayAlert() {
        val clear = AttentionPolicy.apply(AttentionReceipt(1, "episode-one"),
            command(mapOf("version" to "3", "action" to "clear")))!!
        assertFalse(clear.show)
        assertFalse(clear.alert)
        assertNull(AttentionPolicy.apply(clear.receipt, command(mapOf("version" to "2"))))
        assertTrue(AttentionPolicy.apply(clear.receipt, command(mapOf("version" to "4", "episode" to "episode-two")))!!.alert)
    }
    @Test fun openingAppIsAlwaysSilentEvenWithoutAReceipt() {
        assertFalse(AttentionPolicy.apply(AttentionReceipt(), command(mapOf("silent" to "true")))!!.alert)
    }
    @Test fun malformedExpiredAndUnsafeDestinationsAreRejected() {
        for (bad in listOf(mapOf("worker_id" to "https://evil.invalid"), mapOf("session_id" to "../settings"),
            mapOf("session_id" to "chat?session=other"), mapOf("version" to "-1"), mapOf("expires_at" to "999"),
            mapOf("expires_at" to "99999"), mapOf("type" to "session_complete"), mapOf("action" to "progress"))) {
            assertNull(AttentionCommand.parse(data + bad, 1000))
        }
        assertEquals("A chat needs you", command(mapOf("body" to "private question text")).body)
    }
}
