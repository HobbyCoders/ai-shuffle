package com.hobbycoders.aishuffle

import android.content.ClipboardManager
import android.content.Intent
import android.os.SystemClock
import android.view.InputDevice
import android.view.MotionEvent
import android.view.ViewConfiguration
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.widget.LinearLayout
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
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
import com.hobbycoders.aishuffle.ui.ChatLinkMenu
import com.hobbycoders.aishuffle.ui.MobileUi
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.json.JSONArray
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** A real touch-and-hold on links in the chat WebView: the native link menu copies the address. */
@RunWith(AndroidJUnit4::class)
class ChatLinkMenuSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private fun waitFor(message: String, predicate: () -> Boolean) = kotlinx.coroutines.runBlocking {
        try { kotlinx.coroutines.withTimeout(8_000) { while (!predicate()) kotlinx.coroutines.delay(50) } }
        catch (e: kotlinx.coroutines.TimeoutCancellationException) { throw AssertionError(message, e) }
    }

    @Test fun holdingALinkOffersToCopyItsAddressAndPlainTextDoesNot() {
        val server = Server.create("Link worker", "https://worker.example", "test-only-key")
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false); AccountStore(context).clear()
            ServerStore.get(context).apply { getAll().forEach { remove(it.id) }; add(server) }
        }
        val apiField = ApiClient::class.java.getDeclaredField("client\$delegate").apply { isAccessible = true }
        val oldApi = apiField.get(null)
        apiField.set(null, lazyOf(OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            if (request.url.encodedPath == "/api/v1/mobile/key/renew")
                return@addInterceptor PresenceFixture.renewDeviceKey(request, server)
            if (request.url.encodedPath == "/api/v1/mobile/web-session/reservations")
                return@addInterceptor Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(404).message("Legacy fixture")
                    .header("Content-Type", "application/json").body("{\"detail\":\"Not Found\"}".toResponseBody("application/json".toMediaType())).build()
            check(request.url.toString() == "${server.baseUrl}/api/v1/mobile/web-session")
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("Fixture")
                .body("{\"session_token\":\"fixture-session\"}".toResponseBody("application/json".toMediaType())).build()
        }.build()))
        val textLink = "https://example.com/docs?page=2#top"
        val imageLink = "https://example.com/gallery/"
        val pixel = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='80'%3E%3Crect width='160' height='80' fill='%2338bdf8'/%3E%3C/svg%3E"
        val workerPage = """
            <!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
            <style>body { margin: 0; padding: 32px; font: 20px sans-serif; } p, a { display: block; margin: 0 0 48px; padding: 12px 0; }</style>
            </head><body><h1>Link menu fixture</h1>
            <a id="text-link" href="$textLink">Project docs</a>
            <a id="image-link" href="$imageLink"><img src="$pixel" width="160" height="80" alt="Gallery"></a>
            <p id="plain">Plain text that is not a link</p>
            <a id="script-link" href="javascript:void(0)">Script link</a>
            </body></html>
        """.trimIndent()
        ChatActivity.uiFixture = { WebResourceResponse("text/html", "utf-8", ByteArrayInputStream(workerPage.toByteArray(Charsets.UTF_8))) }
        try {
            ActivityScenario.launch<ChatActivity>(Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)).use { scenario ->
                fun web(activity: ChatActivity): WebView {
                    val shell = activity.findViewById<LinearLayout>(R.id.screenToolbar).parent as LinearLayout
                    return (0 until shell.childCount).map { shell.getChildAt(it) }.filterIsInstance<WebView>().single()
                }
                /** The element's centre in screen pixels, once the fixture page has laid it out. */
                fun centre(id: String): Pair<Float, Float> {
                    var point: Pair<Float, Float>? = null
                    waitFor("Fixture element #$id was not laid out") {
                        val done = CountDownLatch(1)
                        scenario.onActivity { activity ->
                            val view = web(activity)
                            if (!activity.hasWindowFocus() || !MobileUi.isUiUrl(server.baseUrl, view.url)) { done.countDown(); return@onActivity }
                            view.evaluateJavascript("""
                                (() => { const e = document.getElementById('$id'); if (!e || document.readyState !== 'complete') return null;
                                  const img = e.querySelector('img'); if (img && !img.complete) return null;
                                  const r = e.getBoundingClientRect(); return r.height > 0 ? [r.left + Math.min(r.width, 120) / 2, r.top + r.height / 2] : null; })()
                            """.trimIndent()) { result ->
                                if (result != null && result != "null") {
                                    val css = JSONArray(result)
                                    val origin = IntArray(2).also(view::getLocationOnScreen)
                                    val density = activity.resources.displayMetrics.density
                                    point = origin[0] + css.getDouble(0).toFloat() * density to origin[1] + css.getDouble(1).toFloat() * density
                                }
                                done.countDown()
                            }
                        }
                        assertTrue("Fixture layout callback timed out", done.await(5, TimeUnit.SECONDS))
                        point != null
                    }
                    return point!!
                }
                fun hold(id: String) {
                    val (x, y) = centre(id)
                    // Same UiAutomation flags as tapNative, so the two never reconnect it.
                    val automation = instrumentation.getUiAutomation(android.app.UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)
                    val now = SystemClock.uptimeMillis()
                    val down = MotionEvent.obtain(now, now, MotionEvent.ACTION_DOWN, x, y, 0).apply { source = InputDevice.SOURCE_TOUCHSCREEN }
                    assertTrue(automation.injectInputEvent(down, true)); down.recycle()
                    Thread.sleep(ViewConfiguration.getLongPressTimeout() * 2L)
                    val up = MotionEvent.obtain(now, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x, y, 0).apply { source = InputDevice.SOURCE_TOUCHSCREEN }
                    assertTrue(automation.injectInputEvent(up, true)); up.recycle()
                    instrumentation.waitForIdleSync()
                }
                fun clipboard(): String? {
                    var text: String? = null
                    scenario.onActivity { text = it.getSystemService(ClipboardManager::class.java)?.primaryClip?.getItemAt(0)?.text?.toString() }
                    return text
                }
                fun copiedBy(id: String, expected: String, screenshot: String? = null) {
                    hold(id)
                    onDialogView(withText(expected)).check(matches(isDisplayed()))
                    screenshot?.let(TestScreenshots::capture)
                    tapNative(onDialogView(withText(ChatLinkMenu.COPY_LABEL)))
                    waitFor("Copy link address did not put $expected on the clipboard") { clipboard() == expected }
                    onView(withText(ChatLinkMenu.COPY_LABEL)).check(doesNotExist())
                }

                copiedBy("text-link", textLink, screenshot = "chat-link-menu")
                copiedBy("image-link", imageLink)
                // Neither plain text nor a script link gets the menu; the chat document never navigates.
                for (id in listOf("plain", "script-link")) {
                    hold(id)
                    Thread.sleep(500); instrumentation.waitForIdleSync()
                    onView(withText(ChatLinkMenu.COPY_LABEL)).check(doesNotExist())
                }
                scenario.onActivity { assertTrue(MobileUi.isUiUrl(server.baseUrl, web(it).url)) }
                assertEquals(imageLink, clipboard())
            }
        } finally {
            apiField.set(null, oldApi); ChatActivity.uiFixture = null
            instrumentation.runOnMainSync { DeviceToolsRuntime.stop(); ServerStore.get(context).remove(server.id) }
        }
    }
}
