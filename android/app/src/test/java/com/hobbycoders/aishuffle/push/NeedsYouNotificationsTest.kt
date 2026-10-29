package com.hobbycoders.aishuffle.push

import android.app.Application
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.ui.ChatActivity
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class NeedsYouNotificationsTest {
    private val app: Application = RuntimeEnvironment.getApplication()
    private val manager = app.getSystemService(NotificationManager::class.java)
    private val server = Server("saved-worker", "Worker", "https://worker.invalid", "not-a-real-key", "phone-device")
    private fun command(version: Long, action: String = "alert", episode: String = "episode-one", silent: Boolean = false) =
        AttentionCommand("dev_worker", "chat-123", version, episode, action, "A chat needs you: Plan",
            System.currentTimeMillis() / 1000 + 600, silent)

    @After fun cleanup() {
        manager.cancelAll()
        app.getSharedPreferences("needs_you_push", Context.MODE_PRIVATE).edit().clear().commit()
    }
    @Test fun highChannelGenericTextStableIdAndTapDestination() {
        NeedsYouNotifications.display(app, command(1), server)
        assertEquals(NotificationManager.IMPORTANCE_HIGH, manager.getNotificationChannel("needs-you").importance)
        val posted = manager.activeNotifications.single()
        val notification = posted.notification
        assertEquals("needs-you:chat-123", posted.tag)
        assertEquals("AI Shuffle", notification.extras.getCharSequence(Notification.EXTRA_TITLE).toString())
        assertEquals("A chat needs you: Plan", notification.extras.getCharSequence(Notification.EXTRA_TEXT).toString())
        assertTrue(notification.flags and Notification.FLAG_ONLY_ALERT_ONCE != 0)
        assertEquals(Notification.VISIBILITY_PRIVATE, notification.visibility)
        val tap = shadowOf(notification.contentIntent).savedIntent
        assertEquals("saved-worker", tap.getStringExtra(ChatActivity.EXTRA_SERVER_ID))
        assertEquals("dev_worker", tap.getStringExtra("push_worker_id"))
        assertEquals("chat-123", tap.getStringExtra("push_session_id"))
        NeedsYouNotifications.display(app, command(2, "update"), server)
        assertEquals(1, manager.activeNotifications.size)
        assertEquals(posted.id, manager.activeNotifications.single().id)
    }
    @Test fun persistedReceiptsBlockReplayAfterClearAndMissingSyncCancels() {
        NeedsYouNotifications.display(app, command(1), server)
        NeedsYouNotifications.display(app, command(2, "clear"), server)
        assertEquals(0, manager.activeNotifications.size)
        NeedsYouNotifications.display(app, command(1), server)
        assertEquals(0, manager.activeNotifications.size)
        NeedsYouNotifications.display(app, command(3, episode = "episode-two"), server)
        assertEquals(1, manager.activeNotifications.size)
        NeedsYouNotifications.cancelMissing(app, "dev_other", emptySet())
        assertEquals(1, manager.activeNotifications.size)
        NeedsYouNotifications.cancelMissing(app, "dev_worker", emptySet())
        assertEquals(0, manager.activeNotifications.size)
    }
    @Test fun unboundOrMalformedPushCannotDisplay() {
        NeedsYouNotifications.receive(app, mapOf("type" to "attention", "worker_id" to "../../bad"))
        assertNull(NeedsYouNotifications.server(app, "https://worker.invalid"))
        assertEquals(0, manager.activeNotifications.size)
        assertFalse(NeedsYouNotifications.authorized(app, server.copy(apiKey = "")))
    }
}
