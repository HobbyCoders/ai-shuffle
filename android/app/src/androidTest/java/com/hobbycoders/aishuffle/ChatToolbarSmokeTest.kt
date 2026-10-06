package com.hobbycoders.aishuffle

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
import com.google.android.material.button.MaterialButton
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.tools.ToolsPreferences
import com.hobbycoders.aishuffle.ui.ChatActivity
import com.hobbycoders.aishuffle.ui.MobileUi
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.atomic.AtomicInteger

/** Uses the real native shell and the phone UI (served by PhoneUiFixture as the worker's /mobile/) in the WebView, with local HTTP fixtures for the worker API. */
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
        val api = OkHttpClient.Builder().addInterceptor { chain ->
            if (chain.request().url.encodedPath == "/api/v1/mobile/key/renew")
                return@addInterceptor PresenceFixture.renewDeviceKey(chain.request(), server)
            if (chain.request().url.encodedPath == "/api/v1/mobile/web-session/reservations")
                return@addInterceptor Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(404).message("Legacy fixture")
                    .header("Content-Type", "application/json").body("{\"detail\":\"Not Found\"}".toResponseBody("application/json".toMediaType())).build()
            val request = chain.request()
            check(request.url.toString() == "${server.baseUrl}/api/v1/mobile/web-session")
            check(request.header("Authorization") == "Bearer ${server.apiKey}")
            sessions.incrementAndGet()
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("Fixture")
                .body("{\"session_token\":\"fixture-session\"}".toResponseBody("application/json".toMediaType())).build()
        }.build()
        apiField.set(null, lazyOf(api)); PhoneUiFixture.install(context)
        try {
            ActivityScenario.launch<ChatActivity>(Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)).use { scenario ->
                fun loaded(orientation: Int) = waitFor("Chat did not load in the expected viewport") {
                    var ready = false
                    scenario.onActivity { activity ->
                        val status = activity.findViewById<TextView>(R.id.screenStatus)
                        val shell = activity.findViewById<LinearLayout>(R.id.screenToolbar).parent as LinearLayout
                        val web = (0 until shell.childCount).map { shell.getChildAt(it) }.filterIsInstance<WebView>().single()
                        ready = activity.hasWindowFocus() && activity.resources.configuration.orientation == orientation &&
                            status.visibility == View.GONE && MobileUi.isUiUrl(server.baseUrl, web.url) && web.height > 0
                    }
                    ready
                }
                fun page(run: (WebView) -> Unit) = scenario.onActivity { activity ->
                    val shell = activity.findViewById<LinearLayout>(R.id.screenToolbar).parent as LinearLayout
                    run((0 until shell.childCount).map { shell.getChildAt(it) }.filterIsInstance<WebView>().single())
                }
                // The page tells the shell over `shuffleShell` whether it draws its own
                // "Connected to" bar; post that from the page itself, as the bar does.
                fun announce(shown: Boolean) {
                    page { it.evaluateJavascript("window.${MobileUi.SHELL_BRIDGE}.postMessage(JSON.stringify({type: 'worker-bar', shown: $shown}))", null) }
                    waitFor("The page's worker-bar message did not reach the shell") {
                        var done = false
                        scenario.onActivity { done = it.pageRendersWorkerBar == shown }
                        done
                    }
                    instrumentation.waitForIdleSync()
                }
                fun checkViewport() {
                    scenario.onActivity { activity ->
                        val density = activity.resources.displayMetrics.density
                        val toolbar = activity.findViewById<LinearLayout>(R.id.screenToolbar)
                        val status = activity.findViewById<TextView>(R.id.screenStatus)
                        val shell = toolbar.parent as LinearLayout
                        val children = (0 until shell.childCount).map { shell.getChildAt(it) }
                        val web = children.filterIsInstance<WebView>().single()
                        val repair = children.filterIsInstance<MaterialButton>().single()
                        assertEquals("Reconnect this worker", repair.text.toString())
                        assertEquals(View.GONE, repair.visibility)
                        assertEquals(View.GONE, status.visibility)
                        assertEquals(shell.height, web.bottom)
                        if (activity.pageRendersWorkerBar) {
                            // The page's "Connected to" bar replaces the native one: the WebView gets it all.
                            assertEquals(View.GONE, toolbar.visibility)
                            assertEquals(listOf(web), children.filter { it.visibility == View.VISIBLE })
                            assertEquals(0, web.top)
                            assertEquals(shell.height, web.height)
                        } else {
                            assertEquals(listOf(toolbar, web), children.filter { it.visibility == View.VISIBLE })
                            assertEquals((56 * density).toInt(), toolbar.height)
                            assertEquals(shell.height - toolbar.height, web.height)
                            assertEquals(toolbar.bottom, web.top)
                            val title = (0 until toolbar.childCount).map { toolbar.getChildAt(it) }.filterIsInstance<TextView>().single()
                            assertEquals(name, title.text.toString())
                            assertEquals(1, title.lineCount)
                            assertTrue("Long worker name must ellipsize", title.layout.getEllipsisCount(0) > 0)
                            for (label in listOf("Back", "More chat options")) {
                                val action = (0 until toolbar.childCount).map { toolbar.getChildAt(it) }.single { it.contentDescription == label }
                                assertTrue(action.isClickable && action.isFocusable)
                                assertTrue(action.width >= (48 * density).toInt() && action.height >= (48 * density).toInt())
                            }
                        }
                        assertEquals("Chat must allow user screenshots", 0, activity.window.attributes.flags and WindowManager.LayoutParams.FLAG_SECURE)
                        assertEquals(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE,
                            activity.window.attributes.softInputMode and WindowManager.LayoutParams.SOFT_INPUT_MASK_ADJUST)
                        assertFalse(web.settings.allowFileAccess)
                        assertFalse(web.settings.allowContentAccess)
                        assertEquals(android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW, web.settings.mixedContentMode)
                    }
                }
                loaded(Configuration.ORIENTATION_PORTRAIT)
                // The page draws its own bar: the native one goes. Until told, or once the page
                // says it no longer does, the native bar stays (worker UIs without that bar).
                announce(true); checkViewport()
                TestScreenshots.capture("chat-page-worker-bar-portrait")
                announce(false); checkViewport()
                TestScreenshots.capture("chat-toolbar-portrait")
                // A switch to a worker this phone is not paired with is refused natively: chat stays.
                page { it.evaluateJavascript("location.assign('/mobile/?connect=' + encodeURIComponent('https://unpaired.example'))", null) }
                Thread.sleep(1_000); instrumentation.waitForIdleSync()
                assertEquals(androidx.lifecycle.Lifecycle.State.RESUMED, scenario.state)
                page { web ->
                    assertTrue(MobileUi.isUiUrl(server.baseUrl, web.url))
                    assertFalse(web.url.orEmpty().contains("connect="))
                }
                onView(withText("Refresh chat")).check(doesNotExist())
                onView(withText("Voice")).check(doesNotExist())
                onView(withText(server.host)).check(doesNotExist())
                onView(withText("account server")).check(doesNotExist())
                // Errors are still announced, over the chat: showing or clearing one moves nothing (#1946).
                var webBounds = listOf<Int>()
                page { webBounds = listOf(it.top, it.height) }
                scenario.onActivity { it.findViewById<TextView>(R.id.screenStatus).text = "Connection interrupted" }
                instrumentation.waitForIdleSync()
                scenario.onActivity { assertEquals(View.VISIBLE, it.findViewById<TextView>(R.id.screenStatus).visibility) }
                page { assertEquals(webBounds, listOf(it.top, it.height)) }
                // Tapping dismisses it.
                scenario.onActivity { activity ->
                    val status = activity.findViewById<TextView>(R.id.screenStatus)
                    status.performClick()
                    assertEquals("", status.text.toString())
                }
                instrumentation.waitForIdleSync(); checkViewport()
                page { assertEquals(webBounds, listOf(it.top, it.height)) }
                val beforeRefresh = sessions.get()
                announce(false)
                onView(withContentDescription("More chat options")).perform(click())
                onView(withText("Refresh chat")).perform(click())
                waitFor("Overflow refresh did not renew the session") { sessions.get() == beforeRefresh + 1 }
                loaded(Configuration.ORIENTATION_PORTRAIT)
                // Voice is the page's own; the shell adds no native call button.
                onView(withContentDescription("Start voice call")).check(doesNotExist())
                scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE }
                loaded(Configuration.ORIENTATION_LANDSCAPE); checkViewport()
                TestScreenshots.capture("chat-toolbar-landscape")
                scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT }
                loaded(Configuration.ORIENTATION_PORTRAIT); checkViewport()
                announce(false)
                onView(withContentDescription("Back")).perform(click())
                waitFor("Back did not finish the chat activity") {
                    scenario.state == androidx.lifecycle.Lifecycle.State.DESTROYED
                }
            }
        } finally {
            apiField.set(null, oldApi); PhoneUiFixture.clear()
            instrumentation.runOnMainSync { DeviceToolsRuntime.stop(); ServerStore.get(context).remove(server.id) }
        }
    }
}
