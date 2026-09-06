package com.hobbycoders.aishuffle

import android.app.Activity
import android.app.Instrumentation
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.res.Configuration
import android.view.View
import android.view.WindowManager
import android.webkit.WebView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.matcher.ViewMatchers.withContentDescription
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.tools.ToolsPreferences
import com.hobbycoders.aishuffle.ui.ChatActivity
import com.hobbycoders.aishuffle.ui.ContainedDocument
import com.hobbycoders.aishuffle.voice.VoiceCallActivity
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.atomic.AtomicInteger

/** Uses the real native shell and contained WebView with local HTTP fixtures. */
@RunWith(AndroidJUnit4::class)
class ChatToolbarSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private fun waitFor(message: String, predicate: () -> Boolean) = kotlinx.coroutines.runBlocking {
        try { kotlinx.coroutines.withTimeout(8_000) { while (!predicate()) kotlinx.coroutines.delay(50) } }
        catch (e: kotlinx.coroutines.TimeoutCancellationException) { throw AssertionError(message, e) }
    }

    @Test fun compactToolbarKeepsChatSpaceAndAccessibleActionsAcrossRotation() {
        val name = "My worker with a very long display name that stays recognizable across all of my projects and sessions without taking any extra chat space"
        val server = Server.create(name, "https://private-hostname.example", "test-only-key")
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false); AccountStore(context).clear()
            ServerStore.get(context).apply { getAll().forEach { remove(it.id) }; add(server) }
        }
        val sessions = AtomicInteger()
        val apiField = ApiClient::class.java.getDeclaredField("client\$delegate").apply { isAccessible = true }
        val oldApi = apiField.get(null)
        val documentField = ContainedDocument::class.java.getDeclaredField("http").apply { isAccessible = true }
        val oldDocument = documentField.get(null)
        val api = OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            check(request.url.toString() == "${server.baseUrl}/api/v1/mobile/web-session")
            check(request.header("Authorization") == "Bearer ${server.apiKey}")
            sessions.incrementAndGet()
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("Fixture")
                .body("{\"session_token\":\"fixture-session\"}".toResponseBody("application/json".toMediaType())).build()
        }.build()
        val document = OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            check(request.url.toString() == "${server.baseUrl}/")
            check(request.header("Cookie")?.contains("session=fixture-session") == true)
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("Fixture")
                .body("<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head><body>Chat fixture</body></html>"
                    .toResponseBody("text/html".toMediaType())).build()
        }.build()
        val voiceLaunches = AtomicInteger()
        val monitor = object : Instrumentation.ActivityMonitor() {
            override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? {
                if (intent.component?.className != VoiceCallActivity::class.java.name) return null
                assertEquals(server.id, intent.getStringExtra("server_id"))
                voiceLaunches.incrementAndGet()
                return Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null)
            }
        }
        apiField.set(null, lazyOf(api)); documentField.set(null, document); instrumentation.addMonitor(monitor)
        try {
            ActivityScenario.launch<ChatActivity>(Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)).use { scenario ->
                fun loaded(orientation: Int) = waitFor("Chat did not load in the expected viewport") {
                    var ready = false
                    scenario.onActivity { activity ->
                        val status = activity.findViewById<TextView>(R.id.screenStatus)
                        val shell = status.parent as LinearLayout
                        val web = shell.getChildAt(shell.childCount - 1) as WebView
                        ready = activity.hasWindowFocus() && activity.resources.configuration.orientation == orientation &&
                            status.visibility == View.GONE && web.url == server.baseUrl + "/" && web.height > 0
                    }
                    ready
                }
                fun checkViewport() {
                    scenario.onActivity { activity ->
                        val density = activity.resources.displayMetrics.density
                        val toolbar = activity.findViewById<LinearLayout>(R.id.screenToolbar)
                        val status = activity.findViewById<TextView>(R.id.screenStatus)
                        val shell = toolbar.parent as LinearLayout
                        val web = shell.getChildAt(2) as WebView
                        assertEquals(3, shell.childCount)
                        assertEquals((56 * density).toInt(), toolbar.height)
                        assertEquals(View.GONE, status.visibility)
                        assertEquals(shell.height - toolbar.height, web.height)
                        assertEquals(toolbar.bottom, web.top)
                        assertEquals(shell.height, web.bottom)
                        val title = (0 until toolbar.childCount).map { toolbar.getChildAt(it) }.filterIsInstance<TextView>().single()
                        assertEquals(name, title.text.toString())
                        assertEquals(1, title.lineCount)
                        assertTrue("Long worker name must ellipsize", title.layout.getEllipsisCount(0) > 0)
                        for (label in listOf("Back", "Start voice call", "More chat options")) {
                            val action = (0 until toolbar.childCount).map { toolbar.getChildAt(it) }.single { it.contentDescription == label }
                            assertTrue(action.isClickable && action.isFocusable)
                            assertTrue(action.width >= (48 * density).toInt() && action.height >= (48 * density).toInt())
                        }
                        assertTrue(activity.window.attributes.flags and WindowManager.LayoutParams.FLAG_SECURE != 0)
                        assertEquals(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE,
                            activity.window.attributes.softInputMode and WindowManager.LayoutParams.SOFT_INPUT_MASK_ADJUST)
                        assertFalse(web.settings.allowFileAccess)
                        assertFalse(web.settings.allowContentAccess)
                        assertEquals(android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW, web.settings.mixedContentMode)
                    }
                }
                loaded(Configuration.ORIENTATION_PORTRAIT); checkViewport()
                onView(withText("Refresh chat")).check(doesNotExist())
                onView(withText("Voice")).check(doesNotExist())
                onView(withText(server.host)).check(doesNotExist())
                onView(withText("account server")).check(doesNotExist())
                // Errors are still announced, but clearing them releases their entire row.
                scenario.onActivity { it.findViewById<TextView>(R.id.screenStatus).text = "Connection interrupted" }
                scenario.onActivity { assertEquals(View.VISIBLE, it.findViewById<TextView>(R.id.screenStatus).visibility) }
                scenario.onActivity { it.findViewById<TextView>(R.id.screenStatus).text = "" }
                instrumentation.waitForIdleSync(); checkViewport()
                val beforeRefresh = sessions.get()
                onView(withContentDescription("More chat options")).perform(click())
                onView(withText("Refresh chat")).perform(click())
                waitFor("Overflow refresh did not renew the session") { sessions.get() == beforeRefresh + 1 }
                loaded(Configuration.ORIENTATION_PORTRAIT)
                onView(withContentDescription("Start voice call")).perform(click())
                assertEquals(1, voiceLaunches.get())
                scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE }
                loaded(Configuration.ORIENTATION_LANDSCAPE); checkViewport()
                scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT }
                loaded(Configuration.ORIENTATION_PORTRAIT); checkViewport()
                onView(withContentDescription("Back")).perform(click())
                waitFor("Back did not finish the chat activity") {
                    scenario.state == androidx.lifecycle.Lifecycle.State.DESTROYED
                }
            }
        } finally {
            instrumentation.removeMonitor(monitor); apiField.set(null, oldApi); documentField.set(null, oldDocument)
            instrumentation.runOnMainSync { DeviceToolsRuntime.stop(); ServerStore.get(context).remove(server.id) }
        }
    }
}
