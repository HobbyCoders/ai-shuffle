package com.hobbycoders.aishuffle

import android.Manifest
import android.app.UiAutomation
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Rect
import android.os.SystemClock
import android.view.InputDevice
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.widget.LinearLayout
import androidx.core.app.NotificationManagerCompat
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.work.WorkManager
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.ui.ChatActivity
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.time.Instant
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Separate push-enabled debug APK, real startup path and Android permission controller.
 * Worker HTTP is synthetic; this never registers with Firebase or needs publisher secrets. */
@RunWith(AndroidJUnit4::class)
class NotificationPermissionSmokeTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val automation get() = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)

    private fun await(message: String, condition: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 12_000
        while (!condition()) {
            if (SystemClock.elapsedRealtime() >= deadline) throw AssertionError(message)
            SystemClock.sleep(50)
        }
    }

    private fun tap(text: String, permission: Boolean = false) {
        var target: AccessibilityNodeInfo? = null
        await("Missing ${if (permission) "system notification permission" else "chat"} control: $text") {
            val root = automation.rootInActiveWindow ?: return@await false
            try {
                // A native app rationale must not satisfy the system-dialog assertion.
                val system = root.packageName?.toString()?.endsWith(".permissioncontroller") == true
                if (permission && !system) return@await false
                if (!permission && root.packageName?.toString() != context.packageName) return@await false
                // Android transforms permission-button labels (including all caps).
                // Match the controller's semantic resource ID, not rendered English.
                val nodes = if (permission) root.findAccessibilityNodeInfosByViewId(
                    "${root.packageName}:id/permission_${if (text == "Allow") "allow" else "deny"}_button")
                    else root.findAccessibilityNodeInfosByText(text)
                val label = nodes.firstOrNull { it.isEnabled && it.isVisibleToUser &&
                    (!permission || it.isClickable) &&
                    (permission || it.text?.toString() == text || it.contentDescription?.toString() == text) }
                // PopupMenu exposes its label as a non-clickable TextView. Tap
                // the enabled, visible clickable row, not a fake menu callback.
                var found = label?.let { AccessibilityNodeInfo.obtain(it) }
                while (found != null && !found.isClickable) {
                    val parent = found.parent
                    found.recycle()
                    found = parent
                }
                if (found?.isEnabled != true || found?.isVisibleToUser != true) {
                    found?.recycle()
                    found = null
                }
                target = found
                nodes.forEach { it.recycle() }
                found != null
            } finally { root.recycle() }
        }
        val node = requireNotNull(target)
        val bounds = Rect()
        try { node.getBoundsInScreen(bounds) } finally { node.recycle() }
        val now = SystemClock.uptimeMillis()
        for ((action, time) in listOf(MotionEvent.ACTION_DOWN to now, MotionEvent.ACTION_UP to now + 50)) {
            val event = MotionEvent.obtain(now, time, action, bounds.exactCenterX(), bounds.exactCenterY(), 0)
            try {
                event.source = InputDevice.SOURCE_TOUCHSCREEN
                assertTrue("Android rejected local input", automation.injectInputEvent(event, true))
            } finally { event.recycle() }
        }
    }

    private fun web(activity: ChatActivity): WebView {
        val column = activity.findViewById<android.view.View>(R.id.screenToolbar).parent as LinearLayout
        return (0 until column.childCount).map { column.getChildAt(it) }.filterIsInstance<WebView>().single()
    }

    private fun pageReady(scenario: ActivityScenario<ChatActivity>, token: String): Boolean {
        val done = CountDownLatch(1)
        var ready = false
        scenario.onActivity { activity ->
            if (!activity.hasWindowFocus()) { done.countDown(); return@onActivity }
            web(activity).evaluateJavascript("document.readyState === 'complete' && window.__SHUFFLE_MOBILE__?.token === '$token'") {
                ready = it == "true"; done.countDown()
            }
        }
        assertTrue("WebView did not answer after the permission decision", done.await(2, TimeUnit.SECONDS))
        return ready
    }

    @Test fun allowingStartupNotificationsLeavesChatUsable() = exercise(allow = true)
    @Test fun denyingStartupNotificationsLeavesChatUsable() = exercise(allow = false)

    private fun exercise(allow: Boolean) {
        assertTrue("This check must use the push-enabled debug APK", BuildConfig.DEBUG && BuildConfig.PUSH_ENABLED)
        assertTrue(android.os.Build.VERSION.SDK_INT >= 33)
        assertEquals("The runner must not pre-grant notifications", PackageManager.PERMISSION_DENIED,
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS))
        assertFalse(NotificationManagerCompat.from(context).areNotificationsEnabled())
        automation.serviceInfo = automation.serviceInfo.apply {
            flags = flags or android.accessibilityservice.AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
        }
        context.getSharedPreferences("needs_you_permission", 0).edit().clear().commit()
        val server = Server.create("Notification fixture", "https://notification-fixture.example", "fixture-key")
        val sessions = AtomicInteger()
        val field = ApiClient::class.java.getDeclaredField("client\$delegate").apply { isAccessible = true }
        val previous = field.get(null)
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            check(request.url.host == "notification-fixture.example")
            val body = when (request.url.encodedPath) {
                "/api/v1/mobile/key/renew" -> JSONObject().put("device_id", server.deviceId).put("api_key", server.apiKey)
                    .put("rotated", false).put("expires_at", Instant.now().plusSeconds(3600).toString()).toString()
                "/api/v1/mobile/web-session/reservations" -> "{\"reservation\":\"wsm_${"a".repeat(43)}\",\"expires_in\":120,\"expires_at\":\"2099-01-01T00:00:00Z\"}"
                "/api/v1/mobile/web-session" -> "{\"session_token\":\"permission-${sessions.incrementAndGet()}\"}"
                else -> "{}" // Push-state sync is local-only too; no Firebase config is installed.
            }
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("Fixture")
                .header("Content-Type", "application/json").body(body.toResponseBody("application/json".toMediaType())).build()
        }.build()
        instrumentation.runOnMainSync { ServerStore.get(context).add(server) }
        field.set(null, lazyOf(client))
        // A worker page fixture, not a fake permission callback. The real shell
        // still reserves/mints a session, installs its bootstrap and handles taps.
        ChatActivity.uiFixture = {
            val html = "<html><head><meta name='viewport' content='width=device-width'></head>" +
                "<body><h1>Permission chat fixture</h1></body></html>"
            WebResourceResponse("text/html", "utf-8", ByteArrayInputStream(html.toByteArray()))
        }
        try {
            ActivityScenario.launch<ChatActivity>(Intent(context, ChatActivity::class.java)
                .putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)).use { scenario ->
                tap(if (allow) "Allow" else "Don’t allow", permission = true)
                await("Permission result did not reach Android") {
                    context.getSharedPreferences("needs_you_permission", 0).getBoolean("asked", false) &&
                        context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
                        (if (allow) PackageManager.PERMISSION_GRANTED else PackageManager.PERMISSION_DENIED) &&
                        automation.rootInActiveWindow?.packageName?.toString() == context.packageName
                }
                await("Chat did not initialize after the permission decision") { pageReady(scenario, "permission-${sessions.get()}") }
                val before = sessions.get()
                tap("More chat options")
                tap("Refresh chat")
                await("A real Refresh chat tap did not mint another session") { sessions.get() > before }
                await("Refreshed chat is not usable") { pageReady(scenario, "permission-${sessions.get()}") }
                assertEquals(allow, NotificationManagerCompat.from(context).areNotificationsEnabled())
                println("NOTIFICATION_PERMISSION_PROOF: decision=${if (allow) "allow" else "deny"}; real_system_prompt=true; chat_refresh=ok")
            }
        } finally {
            // Each branch runs in its own instrumentation process with cleared app data.
            WorkManager.getInstance(context).cancelAllWork().result.get(5, TimeUnit.SECONDS)
            field.set(null, previous); ChatActivity.uiFixture = null
            instrumentation.runOnMainSync { ServerStore.get(context).remove(server.id) }
            client.dispatcher.executorService.shutdown()
        }
    }
}
