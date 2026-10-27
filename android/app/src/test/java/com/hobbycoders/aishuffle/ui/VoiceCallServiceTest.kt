package com.hobbycoders.aishuffle.ui

import android.app.Application
import android.app.Notification
import android.content.Intent
import android.os.Looper
import androidx.core.app.NotificationCompat
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.time.Duration

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class VoiceCallServiceTest {
    private val app: Application = RuntimeEnvironment.getApplication()
    private val commands = mutableListOf<MobileUi.VoiceCallCommand>()
    private var visible = true
    private var microphone = true
    private val keeper = VoiceCallKeeper(app, "worker-1", "HomeLab", { visible }, { microphone }) { commands += it }
    private val live = MobileUi.ShellMessage.VoiceCallState("live", false, 1_700_000_000_000, "Planning")

    private val services = mutableListOf<org.robolectric.android.controller.ServiceController<VoiceCallService>>()

    @After fun tearDown() { keeper.ended(); services.forEach { it.destroy() }; shadowOf(app).clearStartedServices() }

    private fun startedService(): Intent? = shadowOf(app).nextStartedService
    private fun run(action: String?): VoiceCallService =
        Robolectric.buildService(VoiceCallService::class.java, Intent(app, VoiceCallService::class.java).setAction(action))
            .also { services += it }.create().startCommand(0, 1).get()
    private fun actions(notification: Notification) = (0 until NotificationCompat.getActionCount(notification))
        .map { NotificationCompat.getAction(notification, it)!!.title.toString() }

    @Test fun aCallStartsTheMicrophoneServiceOnlyWhileVisibleWithTheMicrophone() {
        visible = false
        keeper.started(document = 3)
        assertFalse(keeper.keptAlive)
        assertFalse("onStop pauses chat without a kept-alive call", keeper.keepsRunningInBackground())
        assertNull(startedService())

        visible = true; microphone = false
        assertFalse(keeper.keepAlive())
        assertNull(startedService())

        microphone = true
        assertTrue(keeper.keepAlive())
        assertTrue("onStop leaves the call's page running", keeper.keepsRunningInBackground())
        val intent = startedService()!!
        assertEquals(VoiceCallService::class.java.name, intent.component!!.className)
        assertEquals(VoiceCallService.ACTION_SHOW, intent.action)
        assertFalse("a running call never starts twice", keeper.keepAlive())
    }

    @Test fun theServiceGoesForegroundWithTheCallNotificationAndStopsWithTheCall() {
        keeper.started(3)
        keeper.update(3, live)
        val service = run(VoiceCallService.ACTION_SHOW)
        val notification = shadowOf(service).lastForegroundNotification
        assertNotNull(notification)
        assertEquals(VoiceCallService.NOTIFICATION_ID, shadowOf(service).lastForegroundNotificationId)
        assertTrue(VoiceCallService.running)
        assertEquals("Voice call · Planning", notification.extras.getCharSequence(Notification.EXTRA_TITLE).toString())
        assertEquals("On call", notification.extras.getCharSequence(Notification.EXTRA_TEXT).toString())
        assertTrue(notification.flags and Notification.FLAG_ONGOING_EVENT != 0)
        assertEquals(listOf("Mute", "Hang up"), actions(notification))
        assertEquals("Voice call", notification.publicVersion.extras.getCharSequence(Notification.EXTRA_TITLE).toString())

        keeper.ended()
        assertFalse(keeper.keepsRunningInBackground())
        assertEquals(VoiceCallService::class.java.name, shadowOf(app).nextStoppedService.component!!.className)
        assertNull(VoiceCallService.call)
        services.removeAt(0).destroy()
        assertFalse(VoiceCallService.running)
    }

    @Test fun notificationButtonsReachThePageAndFollowMuteState() {
        keeper.started(3)
        keeper.update(3, live.copy(muted = true))
        assertEquals(listOf("Unmute", "Hang up"), actions(VoiceCallService.notification(app, VoiceCallService.call!!)))
        assertEquals("On call · Muted", VoiceCallService.text(VoiceCallService.call!!))
        run(VoiceCallService.ACTION_UNMUTE)
        run(VoiceCallService.ACTION_MUTE)
        run(VoiceCallService.ACTION_HANGUP)
        assertEquals(listOf(MobileUi.VoiceCallCommand.UNMUTE, MobileUi.VoiceCallCommand.MUTE, MobileUi.VoiceCallCommand.HANGUP), commands)
    }

    @Test fun hangUpStopsTheServiceEvenIfThePageNeverAnswers() {
        keeper.started(3)
        run(VoiceCallService.ACTION_SHOW)
        run(VoiceCallService.ACTION_HANGUP)
        assertNotNull(VoiceCallService.call)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(VoiceCallService.HANGUP_FALLBACK_MS))
        assertNull(VoiceCallService.call)
        assertNotNull(shadowOf(app).nextStoppedService)
    }

    @Test fun allowingNotificationsMidCallPostsTheCallNotification() {
        keeper.started(3)
        keeper.update(3, live)
        run(VoiceCallService.ACTION_SHOW)
        val manager = shadowOf(app.getSystemService(android.app.NotificationManager::class.java))
        manager.cancelAll()
        shadowOf(app).grantPermissions(android.Manifest.permission.POST_NOTIFICATIONS)
        keeper.refresh()
        val posted = manager.getNotification(VoiceCallService.NOTIFICATION_ID)
        assertEquals("Voice call · Planning", posted.extras.getCharSequence(Notification.EXTRA_TITLE).toString())
    }

    @Test fun anOlderPageGetsANotificationWithoutControls() {
        keeper.started(3)
        val notification = VoiceCallService.notification(app, VoiceCallService.call!!)
        assertEquals("Voice call · HomeLab", notification.extras.getCharSequence(Notification.EXTRA_TITLE).toString())
        assertEquals(emptyList<String>(), actions(notification))
    }

    @Test fun updatesFromAnotherDocumentOrAfterTheCallAreIgnored() {
        keeper.started(3)
        keeper.update(4, live)
        assertNull(VoiceCallService.call!!.status)
        keeper.ended()
        keeper.update(3, live)
        assertNull(VoiceCallService.call)
        assertFalse(keeper.active)
        keeper.onCommand(MobileUi.VoiceCallCommand.HANGUP)
        assertEquals(emptyList<MobileUi.VoiceCallCommand>(), commands)
    }

    @Test fun aServiceStartedAfterTheCallEndedStopsItself() {
        val service = run(VoiceCallService.ACTION_SHOW)
        assertTrue(shadowOf(service).isStoppedBySelf)
    }

    @Test fun anotherOwnerCannotStopOrUpdateTheCall() {
        keeper.started(3)
        val other = VoiceCallKeeper(app, "worker-2", "GamingPC", { true }, { true }) { }
        VoiceCallService.stop(app, other)
        VoiceCallService.update(app, other, VoiceCallService.Call("worker-2", "GamingPC"))
        assertEquals("worker-1", VoiceCallService.call!!.serverId)
    }

    @Test fun theNotificationsOffNoticeIsSentOncePerKeptAliveCall() {
        keeper.notificationsOff()
        assertEquals(emptyList<MobileUi.VoiceCallCommand>(), commands)
        keeper.started(3)
        keeper.notificationsOff(); keeper.notificationsOff()
        assertEquals(listOf(MobileUi.VoiceCallCommand.NOTIFICATIONS_OFF), commands)
        keeper.ended(); keeper.started(5); keeper.notificationsOff()
        assertEquals(2, commands.size)
    }
}
