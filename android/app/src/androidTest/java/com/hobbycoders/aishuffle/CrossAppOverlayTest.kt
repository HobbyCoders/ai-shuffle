package com.hobbycoders.aishuffle

import android.app.UiAutomation
import android.content.ComponentName
import android.content.Intent
import android.provider.Settings
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.tools.*
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.ui.ToolSettingsActivity
import kotlinx.coroutines.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Acceptance test against real API34 accessibility windows, not mocked roots or a production bypass. */
@RunWith(AndroidJUnit4::class)
class CrossAppOverlayTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private fun awaitCondition(message: String, predicate: () -> Boolean) = runBlocking {
        try {
            withTimeout(8_000) { while (!predicate()) delay(100) }
        } catch (e: kotlinx.coroutines.TimeoutCancellationException) {
            throw AssertionError(message, e)
        }
        assertTrue(message, predicate())
    }
    private var screenshotNumber = 0
    private var lastResult: AtomicReference<JSONObject>? = null
    private fun localPress(automation: UiAutomation, text: String) {
        var target: AccessibilityNodeInfo? = null
        awaitCondition("Consent button $text was not visible") {
            target = automation.windows.asSequence().mapNotNull { it.root }.flatMap { it.findAccessibilityNodeInfosByText(text).asSequence() }.firstOrNull { it.text?.toString() == text && it.isClickable }
            if (target == null && (text == "Allow once" || text == "Deny")) {
                lastResult?.get()?.let { result ->
                    val windows = automation.windows.map { "${it.id}:${it.type}:active=${it.isActive}:focused=${it.isFocused}:pkg=${it.root?.packageName}" }
                    fail("Request completed before $text was visible: $result; windows=$windows")
                }
            }
            target != null
        }
        if (text == "Allow once" || text == "Deny") {
            val bitmap = requireNotNull(automation.takeScreenshot())
            try { TestScreenshots.save(bitmap, "cross-app-consent-${screenshotNumber++}") }
            finally { bitmap.recycle() }
        }
        // Inject a real local touch through Android's input dispatcher, not ToolApprovals.resolve.
        val bounds = android.graphics.Rect(); target!!.getBoundsInScreen(bounds)
        val now = android.os.SystemClock.uptimeMillis()
        val down = android.view.MotionEvent.obtain(now, now, android.view.MotionEvent.ACTION_DOWN, bounds.exactCenterX(), bounds.exactCenterY(), 0)
        val up = android.view.MotionEvent.obtain(now, now + 50, android.view.MotionEvent.ACTION_UP, bounds.exactCenterX(), bounds.exactCenterY(), 0)
        down.source = android.view.InputDevice.SOURCE_TOUCHSCREEN; up.source = android.view.InputDevice.SOURCE_TOUCHSCREEN
        assertTrue(automation.injectInputEvent(down, true)); assertTrue(automation.injectInputEvent(up, true)); down.recycle(); up.recycle()
    }
    private fun request(name: String, args: JSONObject, ttl: Long = 20_000,
                        trace: AtomicReference<JSONObject>? = null): Pair<CountDownLatch, AtomicReference<JSONObject>> {
        val result = AtomicReference<JSONObject>(); val done = CountDownLatch(1)
        lastResult = result
        CoroutineScope(Dispatchers.Main).launch {
            try {
                // Read-only diagnostic of the real cancellation fence; never resolve
                // consent or alter service state to make the expiry case pass.
                val service = ShuffleAccessibilityService.current
                val revision = if (trace != null) ShuffleAccessibilityService::class.java
                    .getDeclaredField("revision").apply { isAccessible = true } else null
                val before = service?.let { revision?.getLong(it) }
                val started = System.currentTimeMillis()
                val elapsedStarted = android.os.SystemClock.elapsedRealtime()
                val deadline = started + ttl
                val reply = DeviceToolExecutor(context).execute(name, args, deadline)
                val finished = System.currentTimeMillis()
                val elapsed = android.os.SystemClock.elapsedRealtime() - elapsedStarted
                trace?.set(JSONObject().put("deadline_ms", deadline).put("finished_ms", finished)
                    .put("wall_elapsed_ms", finished - started)
                    .put("monotonic_elapsed_ms", elapsed)
                    .put("revision_before", before ?: JSONObject.NULL)
                    .put("revision_after", service?.let { revision?.getLong(it) } ?: JSONObject.NULL)
                    .put("same_service", service === ShuffleAccessibilityService.current)
                    .put("cross_app_enabled", ToolsPreferences(context).crossAppEnabled)
                    .put("accessibility_enabled", ToolsPreferences(context).accessibilityEnabled))
                result.set(reply)
            } finally { done.countDown() }
        }
        return done to result
    }
    private fun result(request: Pair<CountDownLatch, AtomicReference<JSONObject>>): JSONObject {
        assertTrue("Cross-app request did not finish", request.first.await(7, TimeUnit.SECONDS))
        return requireNotNull(request.second.get())
    }
    @Test fun overlayPreservesTargetAndAuthorizesOnlyOneFreshAction() {
        val automation = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)
        val automationInfo = automation.serviceInfo
        val originalAutomationFlags = automationInfo.flags
        val resolver = context.contentResolver
        val oldServices = Settings.Secure.getString(resolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        val oldEnabled = Settings.Secure.getInt(resolver, Settings.Secure.ACCESSIBILITY_ENABLED, 0)
        val fixturePackage = instrumentation.context.packageName
        assertNotEquals("The target must be the separately installed test APK, never Shuffle", context.packageName, fixturePackage)
        val fixtureComponent = ComponentName(fixturePackage, ControlTargetActivity::class.java.name)
        assertEquals(fixturePackage, context.packageManager.getActivityInfo(fixtureComponent, 0).packageName)
        val server = Server.create("Overlay test server", "https://instrumentation.invalid", "test-only")
        try {
            // UiAutomation omits interactive windows unless explicitly requested. The fixture's
            // OS activity/window/process all belong to the external test APK, not targetContext.
            automation.serviceInfo = automationInfo.apply {
                flags = flags or android.accessibilityservice.AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
            }
            automation.adoptShellPermissionIdentity(android.Manifest.permission.WRITE_SECURE_SETTINGS)
            val service = ComponentName(context, ShuffleAccessibilityService::class.java).flattenToString()
            Settings.Secure.putString(resolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, listOfNotNull(oldServices?.takeIf { it.isNotBlank() }, service).joinToString(":"))
            Settings.Secure.putInt(resolver, Settings.Secure.ACCESSIBILITY_ENABLED, 1)
            automation.dropShellPermissionIdentity()
            awaitCondition("OS never connected ShuffleAccessibilityService") { ShuffleAccessibilityService.current != null }
            ActivityScenario.launch(ToolSettingsActivity::class.java).use { scenario ->
                scenario.onActivity {
                    val prefs = ToolsPreferences(it); ServerStore.get(it).add(server)
                    prefs.setEnabled(true); DeviceToolsRuntime.attach(it, server)
                    prefs.setAllowedApps(setOf(fixturePackage)); prefs.setCrossAppEnabled(true)
                    assertTrue(prefs.crossAppEnabled)
                }
                context.startActivity(Intent().setComponent(fixtureComponent).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                awaitCondition("Test launcher surface did not become the active application") {
                    automation.windows.any { it.isActive && it.root?.packageName?.toString() == fixturePackage }
                }
                // Active-window visibility can precede the service's final layout events.
                automation.waitForIdle(500, 5_000)
                val reading = request("read_screen", JSONObject())
                localPress(automation, "Allow once")
                val screen = result(reading)
                assertTrue(screen.toString(), screen.optBoolean("ok"))
                assertEquals(fixturePackage, screen.getString("package"))
                val nodes = screen.getJSONArray("nodes")
                val targets = (0 until nodes.length()).map { nodes.getJSONObject(it) }.filter { it.optString("text") == "Play local track" }
                assertEquals("Expected one play button in captured nodes: $nodes", 1, targets.size)
                val play = targets.single()
                val tapping = request("tap_node", JSONObject().put("ref", play.getString("ref")))
                localPress(automation, "Allow once")
                assertEquals("performed", result(tapping).getString("status"))
                awaitCondition("Target button did not run") { automation.rootInActiveWindow?.findAccessibilityNodeInfosByText("Local track playing")?.isNotEmpty() == true }
                automation.waitForIdle(500, 5_000)
                assertEquals("stale_ref", result(request("tap_node", JSONObject().put("ref", play.getString("ref")))).getString("status"))
                val denied = request("read_screen", JSONObject()); localPress(automation, "Deny")
                assertEquals("denied", result(denied).getString("status"))
                val expiryTrace = AtomicReference<JSONObject>()
                val expiring = request("read_screen", JSONObject(), 2_000, expiryTrace)
                awaitCondition("Expiring cross-app consent was never presented") {
                    automation.windows.filter { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }
                        .any { it.root?.findAccessibilityNodeInfosByText("Allow once")?.any { node -> node.isVisibleToUser && node.isClickable } == true }
                }
                assertEquals(1L, expiring.first.count)
                assertEquals("denied", result(expiring).getString("status"))
                // Use the executor's actual deadline/completion, not the later
                // assertion time (diagnostic collection must not hide early denial).
                val completion = requireNotNull(expiryTrace.get())
                assertTrue("Cross-app request rejected before expiry: $completion",
                    completion.getLong("finished_ms") >= completion.getLong("deadline_ms"))
                localPress(automation, "Show protected content")
                awaitCondition("Protected screen fixture did not update") { automation.rootInActiveWindow?.findAccessibilityNodeInfosByText("Payment method")?.isNotEmpty() == true }
                // Let both accessibility consumers observe the changed target before
                // asserting its stable protected-screen classification.
                automation.waitForIdle(500, 5_000)
                assertEquals("protected_screen", result(request("read_screen", JSONObject())).getString("status"))
                instrumentation.runOnMainSync { ToolsPreferences(context).setCrossAppEnabled(false) }
                assertFalse(ToolsPreferences(context).crossAppEnabled)
                awaitCondition("Stop left an accessibility overlay attached") { automation.windows.none { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY } }
            }
        } finally {
            try {
                instrumentation.runOnMainSync {
                    ToolsPreferences(context).setEnabled(false); DeviceToolsRuntime.stop()
                    ServerStore.get(context).remove(server.id); ServerStore.get(context).selectedToolsServerId = null
                }
            } finally {
                try {
                    automation.adoptShellPermissionIdentity(android.Manifest.permission.WRITE_SECURE_SETTINGS)
                    // Disconnect ours first, including when it was already enabled before the test.
                    val ownService = ComponentName(context, ShuffleAccessibilityService::class.java).flattenToString()
                    Settings.Secure.putString(resolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
                        oldServices.orEmpty().split(":").filter { it != ownService }.joinToString(":"))
                    awaitCondition("Accessibility service did not disconnect during cleanup") { ShuffleAccessibilityService.current == null }
                    awaitCondition("Cleanup left an accessibility overlay attached") {
                        automation.windows.none { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }
                    }
                } finally {
                    try {
                        Settings.Secure.putString(resolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, oldServices)
                        Settings.Secure.putInt(resolver, Settings.Secure.ACCESSIBILITY_ENABLED, oldEnabled)
                    } finally {
                        try {
                            automation.dropShellPermissionIdentity()
                            android.os.ParcelFileDescriptor.AutoCloseInputStream(
                                automation.executeShellCommand("am force-stop $fixturePackage")
                            ).use { it.readBytes() }
                        } finally {
                            automation.serviceInfo = automation.serviceInfo.apply { flags = originalAutomationFlags }
                        }
                    }
                }
            }
        }
    }
}
