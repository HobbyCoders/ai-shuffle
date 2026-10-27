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
    private val kept = mutableListOf<Boolean>()
    private val keeper = VoiceCallKeeper(app, "worker-1", "Studio worker", { visible }, { microphone },
        send = { commands += it }, keptAliveChanged = { kept += it })
    private val realForeground = VoiceCallService.goForeground
    private val live = MobileUi.ShellMessage.VoiceCallState("live", false, 1_700_000_000_000, "Planning")

    private val services = mutableListOf<org.robolectric.android.controller.ServiceController<VoiceCallService>>()

    @After fun tearDown() {
        keeper.ended(); services.forEach { it.destroy() }; shadowOf(app).clearStartedServices()
        VoiceCallService.goForeground = realForeground
    }

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
        assertTrue("onStop leaves the page running while the service answers", keeper.keepsRunningInBackground())
        assertFalse("not kept alive until the service is in the foreground", keeper.keptAlive)
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
        val notifications = app.getSystemService(android.app.NotificationManager::class.java)
        notifications.cancelAll()
        val manager = shadowOf(notifications)
        assertNull(manager.getNotification(VoiceCallService.NOTIFICATION_ID))
        shadowOf(app).grantPermissions(android.Manifest.permission.POST_NOTIFICATIONS)
        keeper.refresh()
        val posted = manager.getNotification(VoiceCallService.NOTIFICATION_ID)
        assertEquals("Voice call · Planning", posted.extras.getCharSequence(Notification.EXTRA_TITLE).toString())
    }

    @Test fun anOlderPageGetsANotificationWithoutControls() {
        keeper.started(3)
        val notification = VoiceCallService.notification(app, VoiceCallService.call!!)
        assertEquals("Voice call · Studio worker", notification.extras.getCharSequence(Notification.EXTRA_TITLE).toString())
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
        val other = VoiceCallKeeper(app, "worker-2", "Desk worker", { true }, { true }, send = { })
        VoiceCallService.stop(app, other)
        VoiceCallService.update(app, other, VoiceCallService.Call("worker-2", "Desk worker"))
        assertEquals("worker-1", VoiceCallService.call!!.serverId)
    }

    @Test fun theNotificationsOffNoticeIsSentOncePerKeptAliveCall() {
        keeper.notificationsOff()
        assertEquals(emptyList<MobileUi.VoiceCallCommand>(), commands)
        keeper.started(3)
        keeper.notificationsOff()
        assertEquals("nothing to tell before the service is foreground", emptyList<MobileUi.VoiceCallCommand>(), commands)
        run(VoiceCallService.ACTION_SHOW)
        keeper.notificationsOff(); keeper.notificationsOff()
        assertEquals(listOf(MobileUi.VoiceCallCommand.NOTIFICATIONS_OFF), commands)
        keeper.ended(); keeper.started(5); run(VoiceCallService.ACTION_SHOW); keeper.notificationsOff()
        assertEquals(2, commands.size)
    }

    // Review 1: kept alive only once the service is really in the foreground.
    @Test fun theCallIsKeptAliveOnlyOnceTheServiceIsForeground() {
        keeper.started(3)
        assertTrue(keeper.starting)
        assertFalse(keeper.keptAlive)
        assertFalse("no background microphone before the notification exists", keeper.allowsBackgroundMicrophone(3))
        run(VoiceCallService.ACTION_SHOW)
        assertTrue(keeper.keptAlive); assertFalse(keeper.starting)
        assertEquals(listOf(true), kept)
        assertTrue(keeper.allowsBackgroundMicrophone(3))
        assertFalse("another document never gets the background microphone", keeper.allowsBackgroundMicrophone(4))
    }

    @Test fun aRefusedPromotionClearsTheKeptAliveCallAndPausesChat() {
        VoiceCallService.goForeground = { _, _ -> throw SecurityException("microphone service refused") }
        keeper.started(3)
        val service = run(VoiceCallService.ACTION_SHOW)
        assertTrue(shadowOf(service).isStoppedBySelf)
        assertFalse(keeper.keptAlive); assertFalse(keeper.starting)
        assertFalse("onStop pauses chat again", keeper.keepsRunningInBackground())
        assertFalse(keeper.allowsBackgroundMicrophone(3))
        assertEquals("chat is told, so it restores the renderer and pauses if off screen", listOf(false), kept)
        assertNull("no call or owner (Activity) is held", VoiceCallService.call)
        assertFalse("not retried for the same call", keeper.keepAlive())
        assertTrue(keeper.active)
    }

    // Review 2: every stop the keeper did not ask for reaches it and releases the owner.
    @Test fun theHangUpFallbackTellsTheKeeper() {
        keeper.started(3)
        run(VoiceCallService.ACTION_SHOW)
        run(VoiceCallService.ACTION_HANGUP)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(VoiceCallService.HANGUP_FALLBACK_MS))
        assertFalse(keeper.keptAlive)
        assertFalse(keeper.keepsRunningInBackground())
        assertEquals(listOf(true, false), kept)
        assertNull(VoiceCallService.call)
    }

    @Test fun aServiceStoppedByAndroidOrTaskRemovalTellsTheKeeper() {
        keeper.started(3)
        val service = run(VoiceCallService.ACTION_SHOW)
        service.onTaskRemoved(null)
        assertTrue(shadowOf(service).isStoppedBySelf)
        services.removeAt(0).destroy()
        assertFalse(keeper.keptAlive)
        assertEquals(listOf(true, false), kept)
        assertNull(VoiceCallService.call)
        assertFalse(keeper.allowsBackgroundMicrophone(3))
    }

    @Test fun anOldServiceInstanceDoesNotEndANewerCall() {
        keeper.started(3)
        val old = services.size.let { run(VoiceCallService.ACTION_SHOW); services.removeAt(it) }
        keeper.ended()
        keeper.started(4)
        run(VoiceCallService.ACTION_SHOW)
        old.destroy()
        assertTrue(keeper.keptAlive)
        assertEquals(4, keeper.document)
        assertNotNull(VoiceCallService.call)
    }

    @Test fun anotherChatTakingTheServiceTellsThePreviousOwner() {
        keeper.started(3)
        run(VoiceCallService.ACTION_SHOW)
        val other = VoiceCallKeeper(app, "worker-2", "Desk worker", { true }, { true }, send = { })
        other.started(1)
        assertFalse(keeper.keptAlive)
        assertEquals(listOf(true, false), kept)
        assertEquals("worker-2", VoiceCallService.call!!.serverId)
        other.ended()
    }

    // Review 3: the notification permission is asked only on screen, and recorded only once launched.
    @Test fun notificationPermissionIsAskedOnlyOnScreenAndRecordedOnlyWhenLaunched() {
        val prefs = app.getSharedPreferences(CallNotificationAsk.PREFS, 0)
        val ask = CallNotificationAsk(prefs, sdk = 34)
        fun next(visible: Boolean = true, kept: Boolean = true, dialog: Boolean = false, shown: Boolean = false, granted: Boolean = false) =
            ask.next(kept, visible, dialog, shown, granted)
        assertEquals(CallNotificationAsk.Next.NOTHING, next(visible = false))
        assertFalse("a background attempt does not use up the one ask", prefs.getBoolean(CallNotificationAsk.ASKED, false))
        assertEquals(CallNotificationAsk.Next.NOTHING, next(kept = false))
        assertEquals(CallNotificationAsk.Next.NOTHING, next(dialog = true))
        assertEquals(CallNotificationAsk.Next.NOTHING, next(shown = true))
        assertEquals(CallNotificationAsk.Next.ASK, next())
        assertEquals("not launched yet, so still asked", CallNotificationAsk.Next.ASK, next())
        ask.launched()
        assertEquals(CallNotificationAsk.Next.TELL_PAGE, next())
        assertEquals(CallNotificationAsk.Next.TELL_PAGE, next(visible = false))
        assertEquals("allowed but the channel is off", CallNotificationAsk.Next.TELL_PAGE,
            CallNotificationAsk(app.getSharedPreferences("other", 0), sdk = 34).next(true, true, false, false, true))
        assertEquals("before Android 13 there is nothing to ask", CallNotificationAsk.Next.TELL_PAGE,
            CallNotificationAsk(app.getSharedPreferences("old", 0), sdk = 32).next(true, true, false, false, false))
    }

    // Follow-up 1: a call ending while its service is still starting lets chat pause again.
    @Test fun endingACallWhileItsServiceStartsReportsItIsNoLongerKeptAlive() {
        keeper.started(3)
        assertTrue(keeper.starting)
        keeper.ended()
        assertFalse(keeper.keepsRunningInBackground())
        assertEquals(listOf(false), kept)
        assertEquals(VoiceCallService::class.java.name, shadowOf(app).nextStoppedService.component!!.className)
        assertNull(VoiceCallService.call)
    }

    // Follow-up 2: a refused promotion on an instance already running for another
    // chat stops it, so no foreground notification is left without an owner.
    @Test fun aRefusedPromotionOnARunningServiceStopsIt() {
        keeper.started(3)
        val service = run(VoiceCallService.ACTION_SHOW)
        assertTrue(VoiceCallService.running)
        val otherKept = mutableListOf<Boolean>()
        val other = VoiceCallKeeper(app, "worker-2", "Desk worker", { true }, { true }, send = { }, keptAliveChanged = { otherKept += it })
        VoiceCallService.goForeground = { _, _ -> throw SecurityException("microphone service refused") }
        other.started(1)
        assertTrue(other.starting)
        assertEquals("the previous chat is told it lost the service", listOf(true, false), kept)
        services[0].startCommand(0, 2)
        assertTrue(shadowOf(service).isStoppedBySelf)
        assertNull(VoiceCallService.call)
        assertFalse(other.starting); assertFalse(other.keptAlive)
        assertEquals(listOf(false), otherKept)
        other.ended()
    }

    // Review 4: off screen, only the kept-alive call's own page may reopen the microphone.
    @Test fun theBackgroundMicrophoneEndsWithTheCall() {
        keeper.started(3)
        run(VoiceCallService.ACTION_SHOW)
        assertTrue(keeper.allowsBackgroundMicrophone(3))
        keeper.ended()
        assertFalse(keeper.allowsBackgroundMicrophone(3))
        assertFalse(keeper.keepsRunningInBackground())
        assertEquals(listOf(true, false), kept)
    }
}
