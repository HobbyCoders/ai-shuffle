package com.hobbycoders.aishuffle.ui

import android.Manifest
import android.app.Activity
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Looper
import android.widget.TextView
import androidx.core.app.ActivityCompat
import com.hobbycoders.aishuffle.R
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.presence.WorkerPresenceProbe
import com.hobbycoders.aishuffle.push.PushPermission
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.android.controller.ActivityController
import org.robolectric.annotation.Config
import java.security.Security
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Issue 1067, direct chat: restoring into a worker that fails at once while the notification
 * prompt is up must not open the next worker and finish the activity that owns the prompt.
 * Drives the real chat startup, failover, probe and permission launcher over a local transport.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ChatFailoverPermissionTest {
    private val context: android.app.Application get() = RuntimeEnvironment.getApplication()
    private val failing = Server("failing", "Failing", "https://failing.example", "aim_key", "phone")
    private val reachable = Server("reachable", "Reachable", "https://reachable.example", "aim_key", "phone")
    private val probes = AtomicInteger()
    @Volatile private var holdProbe: CountDownLatch? = null
    private val probeStarted = CountDownLatch(1)
    private val transport = Interceptor { chain ->
        val request = chain.request()
        val body = if (request.url.host == reachable.url()) {
            probes.incrementAndGet()
            probeStarted.countDown()
            holdProbe?.await(20, TimeUnit.SECONDS)
            """{"status":"healthy","service":"AI Shuffle","version":"fixture","authenticated":false}"""
        } else null
        Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(if (body != null) 200 else 503).message("Fixture")
            .body((body ?: "{}").toResponseBody("application/json".toMediaType())).build()
    }
    private fun Server.url() = baseUrl.removePrefix("https://")
    private val apiClient = (ApiClient::class.java.getDeclaredField("client\$delegate").apply { isAccessible = true }.get(null) as Lazy<*>).value as OkHttpClient
    private val probeClient = WorkerPresenceProbe::class.java.getDeclaredField("client").apply { isAccessible = true }.get(null) as OkHttpClient
    private val clients = listOf(apiClient, probeClient)
    /** Both clients are static finals the JVM will not let a test replace, so the fixture goes in as their interceptor. */
    private val interceptors = OkHttpClient::class.java.getDeclaredField("interceptors").apply { isAccessible = true }
    private val originalInterceptors = clients.map { it.interceptors }

    @Before fun setUp() {
        Security.addProvider(ServerListAutoConnectTest.FakeAndroidKeyStore())
        ServerStore::class.java.getDeclaredField("instance").apply { isAccessible = true }.set(null, null)
        ServerStore.get(context).add(failing)
        ServerStore.get(context).add(reachable)
        PushPermission.enabled = true
        clients.forEach { interceptors.set(it, listOf(transport)) }
        notifications(enabled = false)
    }
    @After fun tearDown() {
        holdProbe?.countDown()
        PushPermission.enabled = com.hobbycoders.aishuffle.BuildConfig.PUSH_ENABLED
        clients.zip(originalInterceptors).forEach { (client, original) -> interceptors.set(client, original) }
        ActivityCompat.setPermissionCompatDelegate(null)
        Security.removeProvider(ServerListAutoConnectTest.FakeAndroidKeyStore.NAME)
        ServerListAutoConnectTest.FakeAndroidKeyStore.keys.clear()
    }

    private fun notifications(enabled: Boolean) =
        shadowOf(context.getSystemService(NotificationManager::class.java)).setNotificationsEnabled(enabled)
    private fun chat(): ActivityController<ChatActivity> = Robolectric.buildActivity(ChatActivity::class.java,
        Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, failing.id))
    private fun settle() {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20)
        do {
            shadowOf(Looper.getMainLooper()).idle()
            if (clients.all { it.dispatcher.runningCallsCount() + it.dispatcher.queuedCallsCount() == 0 }) break
            if (System.nanoTime() > deadline) throw AssertionError("The worker calls did not finish")
            Thread.sleep(2)
        } while (true)
        shadowOf(Looper.getMainLooper()).idle()
    }
    /** Failover runs in the same step that shows the failed start, so this is after it decided. */
    private fun awaitFailedStart(list: ActivityController<ChatActivity>) {
        val status = list.get().findViewById<TextView>(R.id.screenStatus)
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20)
        while (status.text.isNullOrEmpty() || status.text.toString() == "Opening a secure chat…") {
            if (System.nanoTime() > deadline) throw AssertionError("The chat did not fail to start: '${status.text}'")
            shadowOf(Looper.getMainLooper()).idle()
            Thread.sleep(2)
        }
        settle()
    }
    private fun openedChats(): List<String?> {
        settle()
        return generateSequence { shadowOf(context).nextStartedActivity }
            .filter { it.component?.className == ChatActivity::class.java.name }
            .map { it.getStringExtra(ChatActivity.EXTRA_SERVER_ID) }.toList()
    }
    private fun requestCode(chat: ActivityController<ChatActivity>): Int {
        val request = shadowOf(chat.get()).lastRequestedPermission
        assertNotNull("direct chat must make the real request", request)
        assertEquals(listOf(Manifest.permission.POST_NOTIFICATIONS), request.requestedPermissions.toList())
        return request.requestCode
    }
    private fun answer(chat: ActivityController<ChatActivity>, code: Int, granted: Boolean) = chat.get().onRequestPermissionsResult(
        code, arrayOf(Manifest.permission.POST_NOTIFICATIONS),
        intArrayOf(if (granted) PackageManager.PERMISSION_GRANTED else PackageManager.PERMISSION_DENIED))
    private fun assertStillOnThePrompt(chat: ActivityController<ChatActivity>) {
        assertEquals("failover opened a worker over the permission prompt", emptyList<String?>(), openedChats())
        assertFalse("failover finished the activity that owns the prompt", chat.get().isFinishing)
    }
    private fun assertFailedOver(chat: ActivityController<ChatActivity>) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20)
        while (!chat.get().isFinishing) {
            if (System.nanoTime() > deadline) throw AssertionError("The chat did not fail over")
            shadowOf(Looper.getMainLooper()).idle()
            Thread.sleep(2)
        }
        assertEquals(listOf<String?>(reachable.id), openedChats())
    }

    @Test fun withNoRequestAFailedStartStillFailsOver() {
        notifications(enabled = true)
        val chat = chat().setup()
        awaitFailedStart(chat)
        assertFailedOver(chat)
    }

    @Test fun failoverWaitsForAnAnswerGivenWhilePaused() {
        val chat = chat().setup()
        val code = requestCode(chat)
        awaitFailedStart(chat)
        assertStillOnThePrompt(chat)
        assertEquals("a held failover probes nothing", 0, probes.get())
        chat.pause()
        answer(chat, code, granted = true)
        chat.resume()
        assertFailedOver(chat)
    }

    /** "Don't ask again" can answer while the chat never left RESUMED, with no resume to retry. */
    @Test fun anImmediateAnswerRetriesFailoverWithNoResumeToFollow() {
        val chat = chat().setup()
        val code = requestCode(chat)
        awaitFailedStart(chat)
        assertStillOnThePrompt(chat)
        answer(chat, code, granted = false)
        assertFailedOver(chat)
    }

    @Test fun aChatRecreatedUnderThePromptKeepsWaitingAndThenFailsOver() {
        val first = chat().setup()
        val code = requestCode(first)
        awaitFailedStart(first)
        val state = Bundle()
        first.pause().saveInstanceState(state).stop().destroy()
        // Even if the "asked" flag did not reach disk: a second request would cancel the open one.
        context.getSharedPreferences("needs_you_permission", 0).edit().clear().commit()
        val second = chat().setup(state)
        awaitFailedStart(second)
        assertStillOnThePrompt(second)
        assertNull("the request is still open: never ask twice", shadowOf(second.get()).lastRequestedPermission)
        second.pause()
        answer(second, code, granted = true)
        second.resume()
        assertFailedOver(second)
    }

    @Test fun aRequestThatCannotStartDoesNotBlockFailover() {
        ActivityCompat.setPermissionCompatDelegate(object : ActivityCompat.PermissionCompatDelegate {
            override fun requestPermissions(activity: Activity, permissions: Array<String>, requestCode: Int): Boolean =
                throw IllegalStateException("no permission controller")
            override fun onActivityResult(activity: Activity, requestCode: Int, resultCode: Int, data: Intent?) = false
        })
        val chat = chat().setup()
        awaitFailedStart(chat)
        assertFailedOver(chat)
    }

    @Test fun aProbeAlreadyRunningWhenTheRequestStartsCannotOpenOverIt() {
        val hold = CountDownLatch(1).also { holdProbe = it }
        notifications(enabled = true)
        val chat = chat().setup()
        val status = chat.get().findViewById<TextView>(R.id.screenStatus)
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20)
        while (probeStarted.count > 0) {
            if (System.nanoTime() > deadline) throw AssertionError("Failover did not probe: '${status.text}'")
            shadowOf(Looper.getMainLooper()).idle()
            Thread.sleep(2)
        }
        notifications(enabled = false)
        chat.pause().resume()
        val code = requestCode(chat)
        hold.countDown()
        assertStillOnThePrompt(chat)
        answer(chat, code, granted = true)
        assertFailedOver(chat)
    }
}
