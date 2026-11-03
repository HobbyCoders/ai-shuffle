package com.hobbycoders.aishuffle

import android.app.UiAutomation
import android.content.ComponentName
import android.content.Intent
import android.graphics.Rect
import android.provider.Settings
import android.view.accessibility.AccessibilityWindowInfo
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.tools.*
import com.hobbycoders.aishuffle.ui.ToolSettingsActivity
import kotlinx.coroutines.*
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * The approval fence on real accessibility windows, with a target the panel really covers.
 *
 * A "blip" is a genuine move that undoes itself: it gives the fence the same shape as Android's
 * one bad read (bounds differ, then match again), on demand. Blips must survive; a move that lasts
 * or any content change must be refused, under the panel and after it has gone. So must a screen
 * that reads the same from different views, and one too slow to read in time.
 */
@RunWith(AndroidJUnit4::class)
class CrossAppFenceTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val fixturePackage get() = instrumentation.context.packageName
    private lateinit var automation: UiAutomation
    private lateinit var scenario: ActivityScenario<ToolSettingsActivity>
    private var originalFlags = 0
    private var oldServices: String? = null
    private var oldEnabled = 0
    private val server = Server.create("Fence test server", "https://instrumentation.invalid", "test-only")

    private fun await(message: String, timeoutMs: Long = 8_000, predicate: () -> Boolean) {
        val end = System.currentTimeMillis() + timeoutMs
        while (!predicate()) {
            if (System.currentTimeMillis() > end) throw AssertionError(message)
            Thread.sleep(50)
        }
    }
    private fun button(text: String) = automation.windows.asSequence().mapNotNull { it.root }
        .flatMap { it.findAccessibilityNodeInfosByText(text).asSequence() }.firstOrNull { it.text?.toString() == text && it.isClickable }
    private fun touch(text: String): Boolean {
        val bounds = Rect(); (button(text) ?: return false).getBoundsInScreen(bounds)
        val now = android.os.SystemClock.uptimeMillis()
        val down = android.view.MotionEvent.obtain(now, now, android.view.MotionEvent.ACTION_DOWN, bounds.exactCenterX(), bounds.exactCenterY(), 0)
        val up = android.view.MotionEvent.obtain(now, now + 50, android.view.MotionEvent.ACTION_UP, bounds.exactCenterX(), bounds.exactCenterY(), 0)
        down.source = android.view.InputDevice.SOURCE_TOUCHSCREEN; up.source = android.view.InputDevice.SOURCE_TOUCHSCREEN
        assertTrue(automation.injectInputEvent(down, true)); assertTrue(automation.injectInputEvent(up, true)); down.recycle(); up.recycle()
        return true
    }
    private fun panelUp() = await("Consent panel never appeared") { button("Allow once") != null }
    private fun allow() { panelUp(); assertTrue(touch("Allow once")) }
    /** Blocks until the fixture has laid out the change. Safe off the main thread only. */
    private fun fixture(command: String) {
        val replied = CountDownLatch(1)
        context.sendOrderedBroadcast(Intent(CoveredTargetActivity.COMMAND).setPackage(fixturePackage).putExtra("command", command), null,
            object : android.content.BroadcastReceiver() { override fun onReceive(c: android.content.Context, i: Intent) { replied.countDown() } }, null, 0, null, null)
        assertTrue("Fixture never applied $command", replied.await(5, TimeUnit.SECONDS))
    }
    private fun onceClosed(command: String) {
        ShuffleAccessibilityService.afterConsentClosed = { withContext(Dispatchers.IO) { fixture(command) } }
    }
    private fun shows(text: String) = automation.rootInActiveWindow?.findAccessibilityNodeInfosByText(text)?.any { it.text?.toString() == text } == true
    private fun request(name: String, args: JSONObject = JSONObject(), deadlineMs: Long = 20_000): Pair<CountDownLatch, AtomicReference<JSONObject>> {
        val result = AtomicReference<JSONObject>(); val done = CountDownLatch(1)
        CoroutineScope(Dispatchers.Main).launch {
            try { result.set(DeviceToolExecutor(context).execute(name, args, System.currentTimeMillis() + deadlineMs)) } finally { done.countDown() }
        }
        return done to result
    }
    /** Well inside the 20 s approval window, so a refusal here is the fence and not the expiry. */
    private fun status(request: Pair<CountDownLatch, AtomicReference<JSONObject>>, waitSeconds: Long = 7): JSONObject {
        assertTrue("Cross-app request did not finish", request.first.await(waitSeconds, TimeUnit.SECONDS))
        return requireNotNull(request.second.get())
    }
    private fun read(item: String = "Covered target", waitSeconds: Long = 7): String {
        val reading = request("read_screen"); allow()
        val screen = status(reading, waitSeconds)
        assertTrue(screen.toString(), screen.optBoolean("ok"))
        val nodes = screen.getJSONArray("nodes")
        return (0 until nodes.length()).map { nodes.getJSONObject(it) }.single { it.optString("text") == item }.getString("ref")
    }
    private fun tap(ref: String) = request("tap_node", JSONObject().put("ref", ref))
    private fun assertNotTapped() = assertTrue("The target must not have been tapped", shows(CoveredTargetActivity.RESTING))
    private fun assertTapped(count: Int) = await("Target did not record tap $count") { shows("Taps: $count") }

    @Before fun arm() {
        automation = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)
        val info = automation.serviceInfo
        originalFlags = info.flags
        automation.serviceInfo = info.apply { flags = flags or android.accessibilityservice.AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS }
        val resolver = context.contentResolver
        oldServices = Settings.Secure.getString(resolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        oldEnabled = Settings.Secure.getInt(resolver, Settings.Secure.ACCESSIBILITY_ENABLED, 0)
        automation.adoptShellPermissionIdentity(android.Manifest.permission.WRITE_SECURE_SETTINGS)
        val service = ComponentName(context, ShuffleAccessibilityService::class.java).flattenToString()
        Settings.Secure.putString(resolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, listOfNotNull(oldServices?.takeIf { it.isNotBlank() }, service).joinToString(":"))
        Settings.Secure.putInt(resolver, Settings.Secure.ACCESSIBILITY_ENABLED, 1)
        automation.dropShellPermissionIdentity()
        await("OS never connected ShuffleAccessibilityService") { ShuffleAccessibilityService.current != null }
        scenario = ActivityScenario.launch(ToolSettingsActivity::class.java)
        scenario.onActivity {
            val prefs = ToolsPreferences(it); ServerStore.get(it).add(server)
            prefs.setEnabled(true); DeviceToolsRuntime.attach(it, server)
            prefs.setAllowedApps(setOf(fixturePackage)); prefs.setCrossAppEnabled(true)
        }
        context.startActivity(Intent().setComponent(ComponentName(fixturePackage, CoveredTargetActivity::class.java.name))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
        await("Fixture did not become the active application") { automation.windows.any { it.isActive && it.root?.packageName?.toString() == fixturePackage } }
        // A real input round trip crosses the starting-window exit before the fence takes its baseline.
        await("Fixture was not ready for input") { touch("Get ready") }
        await("Shuffle did not see the ready fixture") {
            val windows = ShuffleAccessibilityService.current?.windows.orEmpty()
            val root = windows.singleOrNull { it.type == AccessibilityWindowInfo.TYPE_APPLICATION && it.isActive }?.root
            root?.findAccessibilityNodeInfosByText(CoveredTargetActivity.RESTING)?.isNotEmpty() == true
        }
    }
    @After fun disarm() {
        ShuffleAccessibilityService.afterConsentClosed = null
        instrumentation.runOnMainSync {
            ToolsPreferences(context).setEnabled(false); DeviceToolsRuntime.stop()
            ServerStore.get(context).remove(server.id); ServerStore.get(context).selectedToolsServerId = null
        }
        scenario.close()
        automation.adoptShellPermissionIdentity(android.Manifest.permission.WRITE_SECURE_SETTINGS)
        val own = ComponentName(context, ShuffleAccessibilityService::class.java).flattenToString()
        val resolver = context.contentResolver
        Settings.Secure.putString(resolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, oldServices.orEmpty().split(":").filter { it != own }.joinToString(":"))
        await("Accessibility service did not disconnect") { ShuffleAccessibilityService.current == null }
        Settings.Secure.putString(resolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, oldServices)
        Settings.Secure.putInt(resolver, Settings.Secure.ACCESSIBILITY_ENABLED, oldEnabled)
        automation.dropShellPermissionIdentity()
        android.os.ParcelFileDescriptor.AutoCloseInputStream(automation.executeShellCommand("am force-stop $fixturePackage")).use { it.readBytes() }
        automation.serviceInfo = automation.serviceInfo.apply { flags = originalFlags }
    }

    @Test fun coveredTargetCanBeApprovedRoundAfterRound() {
        repeat(5) { round ->
            val ref = read()
            val tapping = tap(ref)
            panelUp()
            if (round == 0) {
                val panel = Rect(); val target = Rect()
                val windows = automation.windows
                windows.single { it.type == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }.getBoundsInScreen(panel)
                windows.single { it.isActive && it.root?.packageName?.toString() == fixturePackage }.root
                    .findAccessibilityNodeInfosByText("Covered target").single().getBoundsInScreen(target)
                assertTrue("The panel $panel must really cover the target $target", panel.contains(target))
                val bitmap = requireNotNull(automation.takeScreenshot())
                try { TestScreenshots.save(bitmap, "cross-app-fence-covered") } finally { bitmap.recycle() }
            }
            allow()
            assertEquals("performed", status(tapping).getString("status"))
            assertTapped(round + 1)
            await("Shuffle did not see the tap") {
                ShuffleAccessibilityService.current?.windows.orEmpty().singleOrNull { it.type == AccessibilityWindowInfo.TYPE_APPLICATION && it.isActive }
                    ?.root?.findAccessibilityNodeInfosByText("Taps: ${round + 1}")?.isNotEmpty() == true
            }
        }
    }
    @Test fun blipUnderThePanelDoesNotCancelApproval() {
        val tapping = tap(read())
        panelUp(); fixture("blip"); allow()
        assertEquals("performed", status(tapping).getString("status"))
        assertTapped(1)
    }
    @Test fun blipAsThePanelClosesDoesNotRefuseTheTap() {
        val ref = read()
        onceClosed("blip")
        val tapping = tap(ref); allow()
        assertEquals("performed", status(tapping).getString("status"))
        assertTapped(1)
    }
    @Test fun blipAsThePanelClosesDoesNotRefuseOrThinTheRead() {
        onceClosed("blip")
        read() // Asserts ok, and that the target is still among the captured nodes.
    }
    @Test fun blipBeforeTheNextCommandDoesNotStaleItsReference() {
        val ref = read()
        fixture("blip")
        val tapping = tap(ref); allow()
        assertEquals("performed", status(tapping).getString("status"))
        assertTapped(1)
    }
    @Test fun lastingMoveUnderThePanelIsRefused() {
        val tapping = tap(read())
        panelUp(); fixture("move")
        touch("Allow once") // May already be gone: the event fence can get there first. Both must refuse.
        assertEquals("denied", status(tapping).getString("status"))
        assertNotTapped()
    }
    @Test fun unansweredPanelIsCancelledByALastingMove() {
        val tapping = tap(read())
        panelUp(); fixture("move")
        assertEquals("denied", status(tapping).getString("status"))
        assertNotTapped()
    }
    @Test fun contentChangeUnderThePanelIsRefused() {
        val tapping = tap(read())
        panelUp(); fixture("text")
        assertEquals("denied", status(tapping).getString("status"))
        assertTrue(shows("Different content"))
    }
    @Test fun lastingMoveAfterThePanelClosesIsRefused() {
        val ref = read()
        onceClosed("move")
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping).getString("status"))
        assertNotTapped()
    }
    @Test fun contentChangeAfterThePanelClosesIsRefused() {
        val ref = read()
        onceClosed("text")
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping).getString("status"))
        assertTrue(shows("Different content"))
    }
    @Test fun lastingMoveBeforeTheNextCommandStalesItsReference() {
        val ref = read()
        fixture("move")
        assertEquals("stale_ref", status(tap(ref)).getString("status"))
        assertNotTapped()
    }
    @Test fun readIsRefusedIfTheScreenMovesAsItsPanelCloses() {
        onceClosed("move")
        val reading = request("read_screen"); allow()
        assertEquals("stale_ref", status(reading).getString("status"))
    }
    @Test fun readRefusedOnScreenTooLargeToProve() {
        fixture("dense:320")
        assertEquals("protected_screen", status(request("read_screen")).getString("status"))
    }
    @Test fun rowsSwappedAsThePanelClosesAreRefused() {
        val ref = read("Track One")
        onceClosed("swap")
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping).getString("status"))
        assertNotTapped()
    }
    @Test fun rowsSwappedUnderThePanelAreRefused() {
        val tapping = tap(read("Track One"))
        panelUp(); fixture("swap")
        touch("Allow once")
        assertEquals("denied", status(tapping).getString("status"))
        assertNotTapped()
    }
    @Test fun rowsSwappedBeforeTheNextCommandStaleTheReference() {
        val ref = read("Track One")
        fixture("swap")
        assertEquals("stale_ref", status(tap(ref)).getString("status"))
        assertNotTapped()
    }
    @Test fun viewReplacedAsThePanelClosesIsRefused() {
        val ref = read("Track One")
        onceClosed("replace")
        val tapping = tap(ref); allow()
        assertEquals("stale_ref", status(tapping).getString("status"))
        assertNotTapped()
    }
    @Test fun contentBlipUnderThePanelIsRefused() {
        val tapping = tap(read())
        panelUp(); fixture("textblip")
        assertEquals("denied", status(tapping).getString("status"))
        await("The target was tapped, or the blip never reverted") { shows(CoveredTargetActivity.RESTING) } // A tap would have kept the blip from reverting.
    }
    @Test fun denyWorksDuringASlowRead() {
        fixture("dense:100")
        val tapping = tap(read())
        panelUp(); fixture("delay:25") // No read can finish, or reach its 2 s limit, before the assertion below.
        assertTrue(touch("Allow once")); assertTrue(touch("Deny"))
        val started = System.currentTimeMillis()
        assertEquals("denied", status(tapping).getString("status"))
        assertTrue("Took ${System.currentTimeMillis() - started} ms", System.currentTimeMillis() - started < 1_000)
        fixture("delay:0")
        assertNotTapped()
    }
    @Test fun denseSlowScreenIsApprovedOnACleanRead() {
        fixture("dense:100"); fixture("delay:5") // Each read outlasts the retry window; a clean one must still count.
        val tapping = tap(read(waitSeconds = 15)); allow()
        assertEquals("performed", status(tapping, 15).getString("status"))
        assertTapped(1)
    }
    @Test fun denseScreenAtTheLimitIsApproved() {
        fixture("dense:250")
        val tapping = tap(read(waitSeconds = 15)); allow()
        assertEquals("performed", status(tapping, 15).getString("status"))
        assertTapped(1)
    }
    @Test fun screenTooSlowToReadIsRefusedWithinTheReadLimit() {
        fixture("dense:150")
        val tapping = tap(read(waitSeconds = 15))
        panelUp(); fixture("delay:60") // A full read would take 9 s.
        touch("Allow once")
        val started = System.currentTimeMillis()
        assertEquals("denied", status(tapping, 15).getString("status"))
        assertTrue("Took ${System.currentTimeMillis() - started} ms", System.currentTimeMillis() - started < 5_000)
        fixture("delay:0")
        assertNotTapped()
    }
    @Test fun hungTargetIsRefusedWithoutWaitingForIt() {
        fixture("dense:20")
        val tapping = tap(read())
        panelUp(); fixture("hang:4000") // One call blocks past the read limit, and no check runs inside a call.
        touch("Allow once")
        val started = System.currentTimeMillis()
        assertEquals("denied", status(tapping, 15).getString("status"))
        assertTrue("Took ${System.currentTimeMillis() - started} ms", System.currentTimeMillis() - started < 3_800)
        await("The target was tapped, or never recovered") { shows(CoveredTargetActivity.RESTING) }
    }
    @Test fun actionCannotOutliveItsCommandDeadline() {
        val ref = read()
        ShuffleAccessibilityService.afterConsentClosed = { delay(4_500) }
        val tapping = request("tap_node", JSONObject().put("ref", ref), deadlineMs = 4_000); allow()
        assertEquals("stale_ref", status(tapping, 10).getString("status"))
        assertNotTapped()
    }
    @Test fun actionCannotOutliveItsConsentExpiry() {
        val ref = read()
        ShuffleAccessibilityService.afterConsentClosed = { delay(21_000) } // Past the panel's 20 s, inside the command's 40 s.
        val tapping = request("tap_node", JSONObject().put("ref", ref), deadlineMs = 40_000); allow()
        assertEquals("stale_ref", status(tapping, 30).getString("status"))
        assertNotTapped()
    }
    @Test fun readCannotOutliveItsConsentExpiry() {
        ShuffleAccessibilityService.afterConsentClosed = { delay(21_000) }
        val reading = request("read_screen", deadlineMs = 40_000); allow()
        assertEquals("stale_ref", status(reading, 30).getString("status"))
    }
}
