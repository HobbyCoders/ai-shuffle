package com.hobbycoders.aishuffle

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.os.SystemClock
import android.view.TextureView
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import androidx.test.core.app.ActivityScenario
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Black-box tests: no app class references or reflective test injection. R8
 * gets the production keep rules only, not keeps for test-visible internals. */
@RunWith(AndroidJUnit4::class)
class MinifiedReleaseSmokeTest {
    @get:Rule val camera: GrantPermissionRule = GrantPermissionRule.grant(Manifest.permission.CAMERA)
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val context get() = instrumentation.targetContext
    private fun intent(activity: String) = Intent().setClassName(context.packageName, "${context.packageName}.$activity")
    private fun await(message: String, condition: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 12_000
        while (!condition()) {
            if (SystemClock.elapsedRealtime() >= deadline) throw AssertionError(message)
            Thread.sleep(50)
        }
    }
    private fun views(root: View): List<View> = listOf(root) +
        if (root is ViewGroup) (0 until root.childCount).flatMap { views(root.getChildAt(it)) } else emptyList()
    private fun javascript(web: WebView, source: String): String? {
        val value = AtomicReference<String>()
        val done = CountDownLatch(1)
        instrumentation.runOnMainSync { web.evaluateJavascript(source) { value.set(it); done.countDown() } }
        assertTrue("JavaScript callback did not return", done.await(5, TimeUnit.SECONDS))
        return value.get()
    }

    @Test fun realChatInstallsBothBridgesAndProcessesMessagesAfterMinification() {
        assertEquals("Smoke APK must not be debuggable", 0, context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE)
        assertTrue("Only the disposable smoke variant is allowed", context.applicationInfo.flags and ApplicationInfo.FLAG_TEST_ONLY != 0)
        ActivityScenario.launch<Activity>(intent("ci.ReleaseSmokeFixtureActivity")).use {
            onView(withText("Open minified chat")).perform(click())
            var chat: Activity? = null
            var web: WebView? = null
            var toolbar: View? = null
            try {
                await("Real ChatActivity never installed its WebView") {
                    instrumentation.runOnMainSync {
                        chat = ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED)
                            .singleOrNull { activity -> activity.javaClass.name == "${context.packageName}.ui.ChatActivity" }
                        chat?.let { activity ->
                            web = views(activity.window.decorView).filterIsInstance<WebView>().singleOrNull()
                            toolbar = activity.findViewById(context.resources.getIdentifier("screenToolbar", "id", context.packageName))
                        }
                    }
                    web != null && toolbar != null
                }
                val page = requireNotNull(web)
                // The production onCreate installs the real listeners. Only the
                // HTTPS document is synthetic; no listener/bootstrap is seeded.
                instrumentation.runOnMainSync {
                    page.loadDataWithBaseURL("https://release-smoke.invalid/mobile/", "<html><body>Release bridge smoke</body></html>", "text/html", "UTF-8", null)
                }
                await("Production message bridges missing") {
                    javascript(page, "typeof shuffleShell === 'object' && typeof shuffleFileSave === 'object'") == "true"
                }
                for (shown in listOf(true, false)) {
                    javascript(page, "shuffleShell.postMessage(JSON.stringify({type:'worker-bar',shown:$shown}))")
                    await("Native toolbar did not react to the real JS message") {
                        var changed = false
                        instrumentation.runOnMainSync { changed = toolbar!!.visibility == if (shown) View.GONE else View.VISIBLE }
                        changed
                    }
                }
                javascript(page, """
                    window.releaseReply = false;
                    shuffleFileSave.onmessage = e => { const r = JSON.parse(e.data); window.releaseReply = r.id === 'release-smoke' && r.ok === false; };
                    shuffleFileSave.postMessage(JSON.stringify({id:'release-smoke',type:'finish',sequence:0}));
                """.trimIndent())
                await("File bridge did not reply through native JSON/coroutine code") { javascript(page, "window.releaseReply") == "true" }
                instrumentation.runOnMainSync {
                    page.loadDataWithBaseURL("https://untrusted.invalid/mobile/", "<html><body>Wrong origin</body></html>", "text/html", "UTF-8", null)
                }
                await("Untrusted document did not load without privileged bridges") {
                    javascript(page, "location.hostname === 'untrusted.invalid' && typeof shuffleShell === 'undefined' && typeof shuffleFileSave === 'undefined'") == "true"
                }
            } finally {
                instrumentation.runOnMainSync { chat?.finish() }
            }
        }
    }

    @Test fun minifiedScannerHasALiveCameraPreview() {
        val scan = intent("ui.QrCaptureActivity").setAction("com.google.zxing.client.android.SCAN")
            .putExtra("SCAN_FORMATS", "QR_CODE").putExtra("BEEP_ENABLED", false).putExtra("ORIENTATION_LOCKED", false)
        ActivityScenario.launch<Activity>(scan).use { scenario ->
            await("Minified ZXing camera preview never rendered a frame") {
                var active = false
                scenario.onActivity { activity ->
                    val texture = views(activity.window.decorView).filterIsInstance<TextureView>().singleOrNull()
                    val bitmap = texture?.takeIf { it.isAvailable }?.bitmap
                    if (bitmap != null) {
                        active = activity.hasWindowFocus() && (1..9).any { x ->
                            (1..9).any { y -> (bitmap.getPixel(bitmap.width * x / 10, bitmap.height * y / 10) and 0x00ffffff) != 0 }
                        }
                        bitmap.recycle()
                    }
                }
                active
            }
        }
    }
}
